"""Reactive rules: turn a new observation into an agent run.

A rule is a condition over a newly observed item plus a goal template. When a
watcher finds something matching, the agent is asked to look into it - without
you being there.

This is the one part of the system that spends money unprompted, so it is
bounded on three sides:

* **Only new observations.** De-duplication means an unchanged headline cannot
  re-fire a rule.
* **Per-rule cooldown.** A rule that just fired stays quiet for its cooldown,
  so a burst of similar headlines produces one run, not thirty.
* **A daily budget.** ``MAX_AUTO_RUNS_PER_DAY`` caps automatic runs across all
  rules; when it is spent, rules stop firing until tomorrow.

Rules live in ``rules.json`` - see ``rules.example.json``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from autonomous.config import Settings
from autonomous.errors import describe
from autonomous.storage import Database

log = logging.getLogger(__name__)

StartRun = Callable[[str, str], Awaitable[int]]


class _SafeDict(dict):
    """Leaves unknown placeholders intact instead of raising."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass(slots=True)
class Rule:
    name: str
    goal: str
    kind: str | None = None
    source: str | None = None
    symbol: str | None = None
    title_matches: list[str] = field(default_factory=list)
    change_percent_above: float | None = None
    change_percent_below: float | None = None
    change_percent_abs_above: float | None = None
    cooldown_minutes: int = 60

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Rule:
        when = data.get("when") or {}
        return cls(
            name=data["name"],
            goal=data["goal"],
            kind=when.get("kind"),
            source=when.get("source"),
            symbol=when.get("symbol"),
            title_matches=[m.lower() for m in when.get("title_matches", [])],
            change_percent_above=when.get("change_percent_above"),
            change_percent_below=when.get("change_percent_below"),
            change_percent_abs_above=when.get("change_percent_abs_above"),
            cooldown_minutes=int(data.get("cooldown_minutes", 60)),
        )

    def matches(self, observation: dict[str, Any]) -> bool:
        data = observation.get("data") or {}

        if self.kind and data.get("kind") != self.kind:
            return False
        if self.source and observation.get("source") != self.source:
            return False
        if self.symbol and data.get("symbol") != self.symbol:
            return False
        if self.title_matches:
            title = (observation.get("title") or "").lower()
            if not any(needle in title for needle in self.title_matches):
                return False

        change = data.get("change_percent")
        for threshold, test in (
            (self.change_percent_above, lambda c, t: c > t),
            (self.change_percent_below, lambda c, t: c < t),
            (self.change_percent_abs_above, lambda c, t: abs(c) > t),
        ):
            if threshold is not None:
                if change is None:
                    return False
                if not test(change, threshold):
                    return False
        return True

    def render(self, observation: dict[str, Any]) -> str:
        fields = {
            "title": observation.get("title", ""),
            "url": observation.get("url") or "",
            "source": observation.get("source", ""),
            **(observation.get("data") or {}),
        }
        return self.goal.format_map(_SafeDict(fields))


def load_rules(path: Path) -> list[Rule]:
    if not path.exists():
        return []
    import json

    data = json.loads(path.read_text())
    return [Rule.from_dict(item) for item in data.get("rules", [])]


class RuleEngine:
    def __init__(
        self,
        rules: list[Rule],
        settings: Settings,
        db: Database,
        start_run: StartRun,
    ) -> None:
        self.rules = rules
        self.settings = settings
        self.db = db
        self.start_run = start_run
        self._last_fired: dict[str, datetime] = {}

    @property
    def budget_used(self) -> int:
        midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.db.count_runs_since(midnight.isoformat(timespec="seconds"), automatic=True)

    @property
    def budget_remaining(self) -> int:
        return max(0, self.settings.max_auto_runs_per_day - self.budget_used)

    def _in_cooldown(self, rule: Rule, now: datetime) -> bool:
        last = self._last_fired.get(rule.name)
        return last is not None and now - last < timedelta(minutes=rule.cooldown_minutes)

    async def react(self, source: str, observations: list[dict[str, Any]]) -> list[int]:
        """Fire any matching rules. Returns the run ids started."""
        if not self.rules or not self.settings.rules_enabled:
            return []

        now = datetime.now(UTC)
        started: list[int] = []

        for rule in self.rules:
            if self._in_cooldown(rule, now):
                continue
            match = next((o for o in observations if rule.matches(o)), None)
            if match is None:
                continue
            if self.budget_remaining <= 0:
                log.warning(
                    "rule %r matched but the daily budget of %d automatic runs is spent",
                    rule.name,
                    self.settings.max_auto_runs_per_day,
                )
                break

            # Mark the cooldown before awaiting, so a slow run cannot let the
            # same rule fire twice from an overlapping poll.
            self._last_fired[rule.name] = now
            try:
                run_id = await self.start_run(rule.render(match), rule.name)
            except Exception as exc:
                log.exception("rule %r failed to start a run: %s", rule.name, describe(exc))
                continue
            log.info("rule %r fired on %s: run %s", rule.name, source, run_id)
            started.append(run_id)

        return started
