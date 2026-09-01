"""Assembles the tool registry available to an agent run."""

from __future__ import annotations

from autonomous.config import REPO_ROOT, Settings
from autonomous.storage import Database
from autonomous.tools.base import Tool, ToolRegistry
from autonomous.tools.browser import build_browser_tools
from autonomous.tools.services import Service, build_service_tools, load_services
from autonomous.tools.web import build_web_tools

SERVICES_FILE = REPO_ROOT / "services.json"


def _build_memory_tools(db: Database) -> list[Tool]:
    async def recent_observations(source: str | None = None, limit: int = 20) -> str:
        items = db.list_observations(limit=min(int(limit), 100), source=source)
        if not items:
            # Be explicit about what to do next: a vague empty result makes the
            # model retry with different arguments, and on a rate-limited free
            # tier every wasted call costs a minute.
            return (
                "No observations recorded yet - the watchers have collected nothing "
                "for this source. Do not call this tool again for this task; use "
                "fetch_url or read_feed to get the information from the web instead."
            )
        return "\n".join(
            f"[{item['created_at']}] ({item['source']}) {item['title']}"
            f"{' - ' + item['url'] if item['url'] else ''}"
            for item in items
        )

    return [
        Tool(
            name="recent_observations",
            description=(
                "Read what the background watchers have collected recently - new email, "
                "market levels and headlines, feed items. Use this before searching the web for "
                "something the watchers may already have seen."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Optional watcher name, e.g. 'email', 'markets' or 'feeds'.",
                    },
                    "limit": {"type": "integer", "description": "Max items (default 20)."},
                },
            },
            fn=recent_observations,
        )
    ]


def build_registry(settings: Settings, db: Database) -> ToolRegistry:
    services: list[Service] = load_services(SERVICES_FILE)
    tools = [
        *build_web_tools(settings),
        *build_browser_tools(settings),
        *build_service_tools(settings, services),
        *_build_memory_tools(db),
    ]
    return ToolRegistry(tools)


__all__ = ["Tool", "ToolRegistry", "build_registry"]
