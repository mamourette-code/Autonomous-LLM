"""A tiny in-process pub/sub bus, so the panel can be pushed to rather than poll.

Subscribers are bounded queues: a browser tab that stops reading gets its
oldest events dropped instead of growing without limit. Publishing never blocks
and never raises, because a failing subscriber must not break an agent run or a
watcher poll.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

QUEUE_SIZE = 64


@dataclass(slots=True)
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, type: str, **data: Any) -> None:
        event = Event(type=type, data=data)
        for queue in list(self._subscribers):
            if queue.full():
                # Drop the oldest so a stalled reader loses history, not liveness.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Event]]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)
