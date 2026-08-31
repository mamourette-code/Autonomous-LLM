"""Local web UI and JSON API.

Starting the app starts the watchers; they run for as long as the server does.
Agent runs are started from the UI and execute as background tasks, so the page
can poll their progress while they work.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from autonomous.agent import Agent
from autonomous.config import Settings, get_settings
from autonomous.errors import describe
from autonomous.providers import ProviderError, build_provider
from autonomous.storage import Database
from autonomous.tools import build_registry
from autonomous.watchers import build_scheduler

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class RunRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=8000)
    provider: str | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings.db_path)
    registry = build_registry(settings, db)
    scheduler = build_scheduler(settings, db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.watchers_enabled:
            scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()
            db.close()

    app = FastAPI(title="Autonomous-LLM", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.registry = registry
    app.state.scheduler = scheduler
    # Runs in flight, so a page reload does not lose them.
    app.state.tasks: dict[int, asyncio.Task] = {}

    async def _execute(run_id: int, goal: str, provider_name: str | None) -> None:
        try:
            provider = build_provider(settings, provider_name)
        except ProviderError as exc:
            db.finish_run(run_id, status="failed", error=str(exc))
            return
        agent = Agent(provider, registry, db, settings)
        await agent.run(goal, run_id=run_id)

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
        order = {symbol: i for i, symbol in enumerate(settings.markets_symbols)}
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

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app  # uvicorn factory target: `uvicorn autonomous.web.app:app --factory`
