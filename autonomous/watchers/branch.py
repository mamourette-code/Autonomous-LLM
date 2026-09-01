"""One watcher per branch of interest.

Collects that branch's raw material - feed headlines and, where the branch
lists them, market quotes - and writes it to the observation feed. No model
calls happen here: gathering is free, which is what lets the daily brief spend
so little.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from autonomous.branches import Branch
from autonomous.config import Settings
from autonomous.errors import describe
from autonomous.watchers.base import Observation, Watcher
from autonomous.watchers.feeds import fetch_feed
from autonomous.watchers.quotes import fetch_quote

log = logging.getLogger(__name__)


class BranchWatcher(Watcher):
    def __init__(self, branch: Branch, settings: Settings, headlines_per_feed: int = 12) -> None:
        self.branch = branch
        self.settings = settings
        self.headlines_per_feed = headlines_per_feed

    @property
    def name(self) -> str:
        return self.branch.slug

    @property
    def interval_seconds(self) -> int:
        return self.branch.poll_seconds

    @property
    def enabled(self) -> bool:
        return bool(self.branch.feeds or self.branch.symbols)

    async def poll(self) -> list[Observation]:
        headers = {"User-Agent": self.settings.user_agent}
        async with httpx.AsyncClient(
            timeout=self.settings.http_timeout_seconds, follow_redirects=True, headers=headers
        ) as client:
            quotes, headlines = await asyncio.gather(self._quotes(client), self._headlines(client))
        for observation in quotes + headlines:
            observation.data["branch"] = self.branch.slug
        return quotes + headlines

    async def _quotes(self, client: httpx.AsyncClient) -> list[Observation]:
        if not self.branch.symbols:
            return []
        results = await asyncio.gather(
            *(fetch_quote(client, symbol) for symbol in self.branch.symbols),
            return_exceptions=True,
        )
        observations: list[Observation] = []
        for symbol, result in zip(self.branch.symbols, results, strict=True):
            if isinstance(result, BaseException):
                # One unavailable symbol must not lose the rest of the poll.
                log.warning("quote for %s unavailable: %s", symbol, describe(result))
            elif result is not None:
                observations.append(result)
        return observations

    async def _headlines(self, client: httpx.AsyncClient) -> list[Observation]:
        observations: list[Observation] = []
        for url in self.branch.feeds:
            entries, error = await fetch_feed(client, url, self.headlines_per_feed)
            if error:
                log.warning("%s feed %s unavailable: %s", self.branch.slug, url, error)
                continue
            observations.extend(
                Observation(
                    key=entry.key,
                    title=entry.title,
                    body=entry.summary,
                    url=entry.link,
                    data={
                        "kind": "headline",
                        "source": entry.feed_title,
                        "published": entry.published,
                    },
                )
                for entry in entries
            )
        return observations
