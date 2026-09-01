"""Local web UI and JSON API.

Starting the app starts the watchers; they run for as long as the server does.
Agent runs are started from the UI and execute as background tasks, so the page
can poll their progress while they work.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import compare_digest
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from autonomous.agent import Agent
from autonomous.branches import load_branches
from autonomous.brief import BriefGenerator
from autonomous.config import REPO_ROOT, Settings, get_settings
from autonomous.errors import describe
from autonomous.events import EventBus
from autonomous.providers import ProviderError, build_provider
from autonomous.rules import RuleEngine, load_rules
from autonomous.storage import Database
from autonomous.tools import build_registry
from autonomous.watchers import build_scheduler
from autonomous.web import auth

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
RULES_FILE = REPO_ROOT / "rules.json"
BRANCHES_FILE = REPO_ROOT / "branches.json"


class RunRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=8000)
    provider: str | None = None


def _install_auth(app: FastAPI, settings: Settings) -> None:
    """Gate every route behind the shared token, and add the login endpoints."""
    token = settings.auth_token or ""

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        path = request.url.path
        if path.startswith(auth.PUBLIC_PATHS) or auth.is_authorised(request, token):
            return await call_next(request)
        if auth.wants_html(request):
            return auth.redirect_to_login()
        return JSONResponse({"detail": "authentication required"}, status_code=401)

    @app.get("/login")
    async def login_form(request: Request) -> Response:
        if auth.is_authorised(request, token):
            return RedirectResponse("/", status_code=303)
        return auth.login_page()

    @app.post("/login")
    async def login(token_field: str = Form(alias="token", default="")) -> Response:
        if not compare_digest(token_field, token):
            return auth.login_page("That token was not accepted.", status_code=401)
        response = RedirectResponse("/", status_code=303)
        auth.set_session_cookie(response, token, secure=settings.cookie_secure)
        return response

    @app.post("/logout")
    async def logout() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(auth.COOKIE_NAME)
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings.db_path)
    bus = EventBus()
    registry = build_registry(settings, db)
    branches = load_branches(BRANCHES_FILE)
    scheduler = build_scheduler(settings, db, bus, branches)
    rules = load_rules(RULES_FILE)

    async def _generate_brief(force: bool = False) -> list[Any]:
        """One completion per branch. Cheap because the watchers already fetched."""
        provider = build_provider(settings)
        generator = BriefGenerator(provider, db, settings, branches, bus)
        return await generator.run(force=force)

    async def _brief_daily() -> None:
        """Brief on start - the service starts at login - then once a day."""
        await asyncio.sleep(settings.daily_brief_startup_delay_seconds)
        while True:
            try:
                await _generate_brief()
            except ProviderError as exc:
                log.warning("daily brief skipped: %s", exc)
            except Exception:
                log.exception("daily brief failed")
            await asyncio.sleep(24 * 60 * 60)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        brief_task: asyncio.Task | None = None
        if settings.watchers_enabled:
            scheduler.start()
        if settings.daily_brief_enabled:
            brief_task = asyncio.create_task(_brief_daily(), name="daily-brief")
        try:
            yield
        finally:
            if brief_task:
                brief_task.cancel()
            await scheduler.stop()
            db.close()

    app = FastAPI(title="Autonomous-LLM", version="0.1.0", lifespan=lifespan)

    if settings.auth_token:
        _install_auth(app, settings)
    elif settings.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "The panel is bound to %s with no AUTH_TOKEN set - anyone who can reach "
            "port %s can read your watchers and spend your API credits.",
            settings.host,
            settings.port,
        )

    app.state.settings = settings
    app.state.db = db
    app.state.registry = registry
    app.state.scheduler = scheduler
    app.state.bus = bus
    # Runs in flight, so a page reload does not lose them.
    app.state.tasks: dict[int, asyncio.Task] = {}

    async def _start_run(goal: str, trigger: str | None = None) -> int:
        """Create a run and execute it in the background. Returns its id."""
        model = settings.gemini_model if settings.provider == "gemini" else settings.provider
        run_id = db.create_run(goal, settings.provider, model, trigger)
        task = asyncio.create_task(_execute(run_id, goal, None))
        app.state.tasks[run_id] = task
        task.add_done_callback(lambda _t, rid=run_id: app.state.tasks.pop(rid, None))
        return run_id

    async def _execute(run_id: int, goal: str, provider_name: str | None) -> None:
        try:
            provider = build_provider(settings, provider_name)
        except ProviderError as exc:
            db.finish_run(run_id, status="failed", error=str(exc))
            bus.publish("run.finished", run_id=run_id, status="failed", error=str(exc))
            return
        agent = Agent(provider, registry, db, settings, bus)
        await agent.run(goal, run_id=run_id)

    # Rules react to what the watchers find, by starting runs through the same
    # path the panel uses.
    engine = RuleEngine(rules, settings, db, _start_run)
    app.state.rules = engine
    scheduler.on_new_observations = engine.react

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return {
            "provider": settings.provider,
            "model": settings.gemini_model if settings.provider == "gemini" else settings.provider,
            "provider_ready": settings.provider != "gemini" or bool(settings.gemini_api_key),
            "tools": registry.names,
            "max_steps": settings.max_steps,
            "browser_enabled": settings.browser_enabled,
            "browser_actions_enabled": settings.browser_actions_enabled,
            "watchers": [vars(s) for s in scheduler.status.values()],
        }

    @app.post("/api/runs", status_code=201)
    async def start_run(request: RunRequest) -> dict[str, Any]:
        provider_name = request.provider or settings.provider
        model = settings.gemini_model if provider_name == "gemini" else provider_name
        run_id = db.create_run(request.goal, provider_name, model)
        task = asyncio.create_task(_execute(run_id, request.goal, request.provider))
        app.state.tasks[run_id] = task
        task.add_done_callback(lambda _t, rid=run_id: app.state.tasks.pop(rid, None))
        return {"id": run_id, "status": "running"}

    @app.get("/api/rules")
    async def list_rules() -> dict[str, Any]:
        return {
            "enabled": settings.rules_enabled,
            "budget_per_day": settings.max_auto_runs_per_day,
            "budget_used": engine.budget_used,
            "rules": [
                {"name": r.name, "cooldown_minutes": r.cooldown_minutes, "goal": r.goal}
                for r in rules
            ],
        }

    @app.get("/api/runs")
    async def list_runs(limit: int = 50) -> list[dict[str, Any]]:
        return db.list_runs(limit=min(limit, 200))

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: int) -> dict[str, Any]:
        run = db.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="no such run")
        return {**run, "steps": db.list_steps(run_id)}

    @app.get("/api/observations")
    async def observations(limit: int = 100, source: str | None = None) -> list[dict[str, Any]]:
        return db.list_observations(limit=min(limit, 500), source=source)

    @app.get("/api/branches")
    async def list_branches() -> dict[str, Any]:
        """Every branch with its latest update - what the engine renders."""
        latest = db.latest_briefs()
        out = []
        for branch in branches:
            brief = latest.get(branch.slug)
            status = scheduler.status.get(branch.slug)
            out.append(
                {
                    **branch.as_dict(),
                    "focus": branch.focus,
                    "brief": brief["summary"] if brief else None,
                    "brief_date": brief["brief_date"] if brief else None,
                    "brief_sources": brief["sources"] if brief else 0,
                    "last_poll": status.last_poll if status else None,
                    "last_error": status.last_error if status else None,
                }
            )
        return {"branches": out, "max_calls": settings.daily_brief_max_calls}

    @app.post("/api/brief/run")
    async def run_brief(force: bool = True) -> dict[str, Any]:
        try:
            sections = await _generate_brief(force=force)
        except ProviderError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "sections": [{"branch": s.branch, "sources": s.sources} for s in sections],
            "calls_budget": settings.daily_brief_max_calls,
        }

    @app.get("/api/markets")
    async def markets() -> dict[str, Any]:
        """Latest level per symbol plus recent headlines, shaped for the panel."""
        raw = db.list_observations(limit=400, source="markets")
        latest: dict[str, dict[str, Any]] = {}
        headlines: list[dict[str, Any]] = []
        for item in raw:
            data = item.get("data") or {}
            if data.get("kind") == "quote":
                # list_observations is newest-first, so the first hit per symbol wins.
                latest.setdefault(data["symbol"], {**data, "observed_at": item["created_at"]})
            elif data.get("kind") == "headline":
                headlines.append(
                    {
                        "title": item["title"],
                        "url": item["url"],
                        "source": data.get("source"),
                        "published": data.get("published"),
                        "observed_at": item["created_at"],
                    }
                )
        # Keep the configured symbol order rather than whatever the DB returned.
        markets = next((b for b in branches if b.slug == "markets"), None)
        order = {symbol: i for i, symbol in enumerate(markets.symbols if markets else [])}
        quotes = sorted(latest.values(), key=lambda q: order.get(q["symbol"], 999))
        return {"quotes": quotes, "headlines": headlines[:40]}

    @app.post("/api/watchers/{name}/poll")
    async def poll_watcher(name: str) -> dict[str, Any]:
        watcher = next((w for w in scheduler.watchers if w.name == name), None)
        if watcher is None:
            raise HTTPException(status_code=404, detail="no such watcher")
        if not watcher.enabled:
            raise HTTPException(status_code=409, detail=f"{name} is not configured")
        try:
            new = await scheduler.poll_once(watcher)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=describe(exc)) from exc
        return {"watcher": name, "new_observations": new}

    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        """Server-sent events: run steps and watcher polls, pushed as they happen."""

        async def publisher():
            async with bus.subscribe() as queue:
                # An initial comment opens the stream immediately, so the browser
                # reports "connected" without waiting for the first real event.
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=20.0)
                    except TimeoutError:
                        # Keep-alive: proxies drop a stream that goes quiet.
                        yield ": keep-alive\n\n"
                        continue
                    yield f"event: {event.type}\ndata: {json.dumps(event.data, default=str)}\n\n"

        return StreamingResponse(
            publisher(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app  # uvicorn factory target: `uvicorn autonomous.web.app:app --factory`
