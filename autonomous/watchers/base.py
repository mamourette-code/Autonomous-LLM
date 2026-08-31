"""Watcher contract.

A watcher is the continuously running half of the system: it wakes on an
interval, looks at one source, and returns observations. It never calls the
model and never acts - it only reports. Anything that should *act* on what a
watcher saw is a task you trigger.

Observations are de-duplicated on ``(source, key)``, so a watcher can safely
return the same items every poll.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Observation:
    key: str
    title: str
    body: str | None = None
    url: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


class Watcher(ABC):
    #: Stable identifier, used as the observation source and in the UI.
    name: str = "watcher"

    @property
    @abstractmethod
    def interval_seconds(self) -> int: ...

    @property
    def enabled(self) -> bool:
        """False when the watcher has no configuration; the scheduler skips it."""
        return True

    @abstractmethod
    async def poll(self) -> list[Observation]:
        """Look at the source once. Raise on failure; the scheduler logs and retries."""
