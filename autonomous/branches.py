"""Branches of interest - the things you want updated daily.

Each branch is one cylinder in the panel's engine: a name, the feeds and
symbols that supply it, and a line of guidance for the daily brief. Add a
branch and a cylinder appears; nothing else needs changing.

Defaults ship for the three chosen branches. Override them by creating
``branches.json`` with the same shape (see ``branches.example.json``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_BRANCHES: list[dict[str, Any]] = [
    {
        "slug": "markets",
        "name": "Financial Markets",
        "tagline": "indices, FX, commodities",
        "symbols": [
            "^GSPC",
            "^IXIC",
            "^DJI",
            "^FTSE",
            "^STOXX50E",
            "EURUSD=X",
            "GC=F",
            "CL=F",
            "BTC-USD",
        ],
        "feeds": [
            "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
            "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
            "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
        ],
        "focus": (
            "Where the major indices stand and what moved them. Lead with the levels, "
            "then the one or two stories that explain the day."
        ),
    },
    {
        "slug": "tech",
        "name": "Technology & AI",
        "tagline": "launches, models, funding",
        "feeds": [
            "https://techcrunch.com/feed/",
            "https://www.theverge.com/rss/index.xml",
            "https://arstechnica.com/feed/",
        ],
        "focus": (
            "What actually shipped or changed - releases, launches, funding, notable "
            "research. Skip opinion pieces, reviews and rumour."
        ),
    },
    {
        "slug": "world",
        "name": "World & Geopolitics",
        "tagline": "conflict, policy, energy",
        "feeds": [
            "https://feeds.bbci.co.uk/news/world/rss.xml",
            "https://www.aljazeera.com/xml/rss/all.xml",
            "https://rss.dw.com/rdf/rss-en-world",
        ],
        "focus": (
            "The developments that change something - conflict, elections, trade, "
            "energy. Say why each matters, not just that it happened."
        ),
    },
]


@dataclass(slots=True)
class Branch:
    slug: str
    name: str
    tagline: str = ""
    focus: str = ""
    feeds: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    poll_seconds: int = 1800
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Branch:
        return cls(
            slug=data["slug"],
            name=data["name"],
            tagline=data.get("tagline", ""),
            focus=data.get("focus", ""),
            feeds=list(data.get("feeds", [])),
            symbols=list(data.get("symbols", [])),
            poll_seconds=int(data.get("poll_seconds", 1800)),
            enabled=bool(data.get("enabled", True)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "tagline": self.tagline,
            "feeds": len(self.feeds),
            "symbols": len(self.symbols),
            "enabled": self.enabled,
        }


def load_branches(path: Path | None = None) -> list[Branch]:
    """Branches from ``branches.json`` if present, otherwise the built-in defaults."""
    raw = DEFAULT_BRANCHES
    if path is not None and path.exists():
        data = json.loads(path.read_text())
        if data.get("branches"):
            raw = data["branches"]
    return [b for b in (Branch.from_dict(item) for item in raw) if b.enabled]
