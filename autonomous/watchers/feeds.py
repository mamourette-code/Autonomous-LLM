"""RSS/Atom watcher, plus the feed parsing shared with the markets watcher."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from autonomous.config import Settings
from autonomous.errors import describe
from autonomous.watchers.base import Observation, Watcher


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


class FeedWatcher(Watcher):
    name = "feeds"

    def __init__(self, settings: Settings, per_feed_limit: int = 15) -> None:
        self.settings = settings
        self.per_feed_limit = per_feed_limit

    @property
    def interval_seconds(self) -> int:
        return self.settings.feed_poll_seconds

    @property
    def enabled(self) -> bool:
        return bool(self.settings.feed_urls)

    async def poll(self) -> list[Observation]:
        observations: list[Observation] = []
        headers = {"User-Agent": self.settings.user_agent}
        async with httpx.AsyncClient(
            timeout=self.settings.http_timeout_seconds, follow_redirects=True, headers=headers
        ) as client:
            for url in self.settings.feed_urls:
                entries, error = await fetch_feed(client, url, self.per_feed_limit)
                if error:
                    observations.append(
                        Observation(
                            key=f"error:{url}:{error}",
                            title=f"Feed unavailable: {url} ({error})",
                            url=url,
                        )
                    )
                    continue
                observations.extend(
                    Observation(
                        key=entry.key,
                        title=f"{entry.feed_title}: {entry.title}",
                        body=entry.summary,
                        url=entry.link,
                        data={"feed": entry.feed_title, "feed_url": url, "kind": "headline"},
                    )
                    for entry in entries
                )
        return observations
