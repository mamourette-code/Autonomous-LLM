"""The daily brief: one update per branch, on a strict call budget.

This deliberately does **not** use the tool-calling agent loop. That loop costs
three to five model calls to answer one question, because the model has to go
and fetch things. The watchers have already fetched everything, for free, so a
brief is a single completion per branch with the material handed to it.

Three things keep the cost down, in order of effect:

1. **No tools.** One call per branch, not a loop.
2. **Nothing new, nothing spent.** A branch whose watcher found nothing since
   the last brief is skipped entirely - it costs zero calls.
3. **Batching.** If you add more branches than ``DAILY_BRIEF_MAX_CALLS``, they
   are grouped so the total number of calls never exceeds the budget.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from autonomous.branches import Branch
from autonomous.config import Settings
from autonomous.errors import describe
from autonomous.events import EventBus
from autonomous.providers.base import LLMProvider, Message
from autonomous.storage import Database

log = logging.getLogger(__name__)

MAX_QUOTES = 12
MAX_HEADLINES = 18
SECTION = re.compile(r"^===\s*([a-z0-9_-]+)\s*===\s*$", re.IGNORECASE | re.MULTILINE)

SYSTEM = """\
You write a short daily briefing from material that has already been collected.

Rules:
- Use only the material given. Do not invent numbers, quotes or events.
- Lead with what changed. No preamble, no "here is your briefing".
- Prefer specifics: levels, figures, names. Cut adjectives.
- If the material is thin, say so in one line rather than padding.
- Markdown: short bold lead-ins and bullets. No headings.
- Today is {today}.
"""


@dataclass(slots=True)
class BriefSection:
    branch: str
    summary: str
    sources: int


def _format_material(observations: list[dict[str, Any]]) -> tuple[str, int]:
    """Turn a branch's observations into compact prompt material."""
    quotes: list[str] = []
    headlines: list[str] = []
    seen_symbols: set[str] = set()

    for item in observations:
        data = item.get("data") or {}
        if data.get("kind") == "quote":
            symbol = data.get("symbol")
            # Newest-first, so the first sighting of a symbol is its latest level.
            if symbol in seen_symbols or len(quotes) >= MAX_QUOTES:
                continue
            seen_symbols.add(symbol)
            change = data.get("change_percent")
            line = f"- {data.get('name', symbol)}: {data.get('price')}"
            if data.get("currency"):
                line += f" {data['currency']}"
            if change is not None:
                line += f" ({change:+.2f}%)"
            quotes.append(line)
        elif len(headlines) < MAX_HEADLINES:
            source = data.get("source")
            headlines.append(f"- {item['title']}{f' [{source}]' if source else ''}")

    parts = []
    if quotes:
        parts.append("Latest levels:\n" + "\n".join(quotes))
    if headlines:
        parts.append("Headlines:\n" + "\n".join(headlines))
    return "\n\n".join(parts), len(quotes) + len(headlines)


def _batch(branches: list[Branch], budget: int) -> list[list[Branch]]:
    """Split branches into at most ``budget`` groups, as evenly as possible."""
    if budget < 1:
        return []
    if len(branches) <= budget:
        return [[b] for b in branches]
    groups: list[list[Branch]] = [[] for _ in range(budget)]
    for i, branch in enumerate(branches):
        groups[i % budget].append(branch)
    return [g for g in groups if g]


class BriefGenerator:
    def __init__(
        self,
        provider: LLMProvider,
        db: Database,
        settings: Settings,
        branches: list[Branch],
        bus: EventBus | None = None,
    ) -> None:
        self.provider = provider
        self.db = db
        self.settings = settings
        self.branches = branches
        self.bus = bus

    def _today(self) -> str:
        return datetime.now(UTC).date().isoformat()

    def pending(self, force: bool = False) -> list[tuple[Branch, str, int]]:
        """Branches with material worth briefing, and their prompt text."""
        today = self._today()
        out: list[tuple[Branch, str, int]] = []
        for branch in self.branches:
            if not force and self.db.get_brief(branch.slug, today):
                continue
            observations = self.db.list_observations(limit=120, source=branch.slug)
            material, count = _format_material(observations)
            if not count:
                log.info("branch %s has nothing to brief; costs no call", branch.slug)
                continue
            out.append((branch, material, count))
        return out

    async def run(self, force: bool = False) -> list[BriefSection]:
        pending = self.pending(force=force)
        if not pending:
            return []

        by_slug = {branch.slug: (material, count) for branch, material, count in pending}
        groups = _batch([branch for branch, _, _ in pending], self.settings.daily_brief_max_calls)

        system = SYSTEM.format(today=self._today())
        sections: list[BriefSection] = []

        for group in groups:
            prompt = self._prompt(group, by_slug)
            try:
                response = await self.provider.complete(
                    [Message(role="user", text=prompt)], system=system
                )
            except Exception as exc:
                log.warning(
                    "brief call failed for %s: %s", ", ".join(b.slug for b in group), describe(exc)
                )
                continue

            for slug, summary in self._split(response.text, group).items():
                sources = by_slug[slug][1]
                self.db.save_brief(slug, self._today(), summary, sources)
                sections.append(BriefSection(branch=slug, summary=summary, sources=sources))
                if self.bus:
                    self.bus.publish("brief.updated", branch=slug)

        log.info("daily brief: %d section(s) in %d model call(s)", len(sections), len(groups))
        return sections

    def _prompt(self, group: list[Branch], by_slug: dict[str, tuple[str, int]]) -> str:
        if len(group) == 1:
            branch = group[0]
            return (
                f"Write today's update on {branch.name}.\n"
                f"Focus: {branch.focus}\n"
                f"Keep it under 140 words.\n\n{by_slug[branch.slug][0]}"
            )
        # Batched: ask for delimited sections so one call yields several updates.
        blocks = "\n\n".join(
            f"=== {b.slug} ===\n{b.name}. Focus: {b.focus}\n\n{by_slug[b.slug][0]}" for b in group
        )
        slugs = ", ".join(b.slug for b in group)
        return (
            f"Write today's update for each of these topics: {slugs}.\n"
            "Return one section per topic, each introduced by a line of exactly "
            "'=== slug ===' using the slug given below, and nothing before the first "
            "such line. Keep each section under 120 words.\n\n" + blocks
        )

    def _split(self, text: str, group: list[Branch]) -> dict[str, str]:
        text = (text or "").strip()
        if not text:
            return {}
        if len(group) == 1:
            return {group[0].slug: text}

        valid = {b.slug.lower() for b in group}
        found: dict[str, str] = {}
        matches = list(SECTION.finditer(text))
        for i, match in enumerate(matches):
            slug = match.group(1).lower()
            if slug not in valid:
                continue
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[match.end() : end].strip()
            if body:
                found[slug] = body
        if not found:
            # The model ignored the format. Rather than lose the call, give the
            # whole answer to the first branch and say so.
            log.warning("batched brief came back without section markers")
            found[group[0].slug] = text
        return found
