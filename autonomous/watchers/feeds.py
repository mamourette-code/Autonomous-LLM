"""RSS/Atom watcher - notices to mariners, forecasts, news, release feeds.

Set FEED_URLS to a JSON list of URLs to enable it.
"""

from __future__ import annotations

import asyncio

import httpx

from autonomous.config import Settings
from autonomous.watchers.base import Observation, Watcher


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
        import feedparser

        observations: list[Observation] = []
        headers = {"User-Agent": self.settings.user_agent}
        async with httpx.AsyncClient(
            timeout=self.settings.http_timeout_seconds, follow_redirects=True, headers=headers
        ) as client:
            for url in self.settings.feed_urls:
                try:
                    response = await client.get(url)
                    parsed = await asyncio.to_thread(feedparser.parse, response.content)
                except Exception as exc:
                    observations.append(
                        Observation(
                            key=f"error:{url}:{type(exc).__name__}",
                            title=f"Feed unavailable: {url} ({exc})",
                            url=url,
                        )
                    )
                    continue
                feed_title = parsed.feed.get("title", url)
                for entry in parsed.entries[: self.per_feed_limit]:
                    link = entry.get("link", "")
                    observations.append(
                        Observation(
                            key=entry.get("id") or link or f"{url}:{entry.get('title', '')}",
                            title=f"{feed_title}: {entry.get('title', '(untitled)')}",
                            body=entry.get("summary"),
                            url=link,
                            data={"feed": feed_title, "feed_url": url},
                        )
                    )
        return observations
