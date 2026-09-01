"""Feed fetching and parsing, shared by every branch watcher."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from autonomous.errors import describe


@dataclass(slots=True)
class FeedEntry:
    feed_title: str
    title: str
    link: str
    summary: str | None
    published: str | None

    @property
    def key(self) -> str:
        return self.link or f"{self.feed_title}:{self.title}"


async def fetch_feed(
    client: httpx.AsyncClient, url: str, limit: int = 15
) -> tuple[list[FeedEntry], str | None]:
    """Fetch and parse one feed. Returns (entries, error) - never raises."""
    import feedparser

    try:
        response = await client.get(url)
        response.raise_for_status()
        # feedparser is synchronous and CPU-bound; keep it off the event loop.
        parsed = await asyncio.to_thread(feedparser.parse, response.content)
    except Exception as exc:
        return [], describe(exc)

    feed_title = parsed.feed.get("title", url)
    entries = [
        FeedEntry(
            feed_title=feed_title,
            title=entry.get("title", "(untitled)"),
            link=entry.get("link", ""),
            summary=entry.get("summary"),
            published=entry.get("published") or entry.get("updated"),
        )
        for entry in parsed.entries[:limit]
    ]
    return entries, None
