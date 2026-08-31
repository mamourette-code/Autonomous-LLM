"""Runs every enabled watcher on its own interval, for as long as the app lives.

Each watcher gets an independent task, so a slow or failing source never holds
up the others. Failures are logged and retried on the next tick with a short
backoff; a watcher is never dropped for erroring.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from autonomous.config import Settings
from autonomous.errors import describe
from autonomous.storage import Database
from autonomous.watchers.base import Watcher
from autonomous.watchers.email import EmailWatcher
from autonomous.watchers.feeds import FeedWatcher
from autonomous.watchers.markets import MarketsWatcher

log = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 900


@dataclass
class WatcherStatus:
    name: str
    interval_seconds: int
    enabled: bool
    last_poll: str | None = None
    last_error: str | None = None
    new_observations: int = 0
    total_polls: int = 0
    consecutive_failures: int = 0


@dataclass
class Scheduler:
    settings: Settings
    db: Database
    watchers: list[Watcher] = field(default_factory=list)
    status: dict[str, WatcherStatus] = field(default_factory=dict)
    _tasks: list[asyncio.Task] = field(default_factory=list)

    def __post_init__(self) -> None:
        for watcher in self.watchers:
            self.status[watcher.name] = WatcherStatus(
                name=watcher.name,
                interval_seconds=watcher.interval_seconds,
                enabled=watcher.enabled,
            )

    def start(self) -> None:
        if self._tasks:
            return
        for watcher in self.watchers:
            if not watcher.enabled:
                log.info("watcher %s is not configured; skipping", watcher.name)
                continue
            self._tasks.append(
                asyncio.create_task(self._loop(watcher), name=f"watch:{watcher.name}")
            )
        log.info("started %d watcher(s)", len(self._tasks))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def poll_once(self, watcher: Watcher) -> int:
        """Poll a single watcher and store what it found. Returns the new-item count."""
        status = self.status[watcher.name]
        try:
            observations = await watcher.poll()
        except Exception as exc:
            status.last_error = describe(exc)
            status.consecutive_failures += 1
            status.total_polls += 1
            status.last_poll = datetime.now(UTC).isoformat(timespec="seconds")
            log.warning("watcher %s failed: %s", watcher.name, status.last_error)
            raise

        new = self.db.add_observations(
            {
                "source": watcher.name,
                "key": obs.key,
                "title": obs.title,
                "body": obs.body,
                "url": obs.url,
                "data": obs.data,
            }
            for obs in observations
        )
        status.last_poll = datetime.now(UTC).isoformat(timespec="seconds")
        status.last_error = None
        status.consecutive_failures = 0
        status.total_polls += 1
        status.new_observations += new
        log.info("watcher %s: %d observed, %d new", watcher.name, len(observations), new)
        return new

    async def _loop(self, watcher: Watcher) -> None:
        status = self.status[watcher.name]
        while True:
            try:
                await self.poll_once(watcher)
                delay = watcher.interval_seconds
            except asyncio.CancelledError:
                raise
            except Exception:
                # Exponential backoff, capped, so a dead source stays quiet.
                delay = min(
                    watcher.interval_seconds * (2 ** min(status.consecutive_failures, 6)),
                    MAX_BACKOFF_SECONDS,
                )
            await asyncio.sleep(delay)


def build_scheduler(settings: Settings, db: Database) -> Scheduler:
    watchers: list[Watcher] = [
        EmailWatcher(settings),
        MarketsWatcher(settings),
        FeedWatcher(settings),
    ]
    return Scheduler(settings=settings, db=db, watchers=watchers)
