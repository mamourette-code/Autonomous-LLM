"""Relevance-driven memory retrieval (protocol s9, s50).

Retrieval never loads everything. It ranks candidates against the current
objective and returns, with each hit, the reason it ranked where it did - so
retrieval quality itself is auditable rather than a black box.

Conflict handling is deliberately conservative. Contradictions are surfaced and
recorded as unresolved, and confidence is reduced; nothing is silently merged
and no winner is silently picked.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from jarvis import events, memory as mem_mod
from jarvis.memory import Memory, tokenize
from jarvis.store import Store, new_id, utcnow

# How much an epistemic class is trusted when ranking (protocol s11).
SOURCE_WEIGHT = {
    "system_knowledge": 1.0,
    "user_provided": 1.0,
    "sourced_knowledge": 0.9,
    "lesson": 0.85,
    "raw_information": 0.6,
    "hypothesis": 0.4,
    "unknown": 0.5,
}


@dataclass
class Hit:
    memory: Memory
    score: float
    components: dict[str, float]
    notes: list[str] = field(default_factory=list)

    @property
    def effective_confidence(self) -> float:
        """Stored confidence after conflict and staleness penalties."""
        c = self.memory.confidence
        if any(n.startswith("conflicts with") for n in self.notes):
            c *= 0.6
        if self.memory.is_stale:
            c *= 0.8
        return round(c, 4)


@dataclass
class Retrieval:
    hits: list[Hit]
    conflicts: list[tuple[str, str, str]] = field(default_factory=list)
    considered: int = 0

    def ids(self) -> list[str]:
        return [h.memory.id for h in self.hits]


def retrieve(
    store: Store,
    query: str,
    *,
    kinds: list[str] | None = None,
    project: str | None = None,
    entities: list[str] | None = None,
    limit: int = 8,
    min_score: float = 0.05,
    include_stale: bool = True,
    actor: str = "claude",
    session_id: str | None = None,
    record_use: bool = True,
) -> Retrieval:
    """Rank live memories against a query and surface any contradictions."""
    candidates = _candidates(store, kinds, project)
    if not candidates:
        return Retrieval(hits=[], considered=0)

    idf = _idf([tokenize(f"{m.title}\n{m.body}") for m in candidates])
    q_tokens = tokenize(query)
    want_entities = {e.lower() for e in (entities or [])}
    now = datetime.now(timezone.utc)

    hits: list[Hit] = []
    for m in candidates:
        if m.is_stale and not include_stale:
            continue
        m_tokens = tokenize(f"{m.title}\n{m.body}")
        lexical = _weighted_overlap(q_tokens, m_tokens, idf)
        entity = (
            len(want_entities & {e.lower() for e in m.entities}) / len(want_entities)
            if want_entities
            else 0.0
        )
        proj = 1.0 if (project and m.project == project) else 0.0
        recency = _recency(m.updated_at or m.created_at, now)
        trust = SOURCE_WEIGHT.get(m.source_class, 0.5) * m.confidence
        usefulness = min(1.0, m.access_count / 10.0)

        score = (
            0.45 * lexical
            + 0.15 * entity
            + 0.08 * proj
            + 0.10 * recency
            + 0.17 * trust
            + 0.05 * usefulness
        )
        notes: list[str] = []
        if m.is_stale:
            score *= 0.75
            notes.append(f"past its review date ({m.review_after[:10]}); re-validate before relying on it")
        if lexical == 0.0 and entity == 0.0:
            continue  # no topical connection at all: not relevant, only recent
        hits.append(
            Hit(
                memory=m,
                score=round(score, 4),
                components={
                    "lexical": round(lexical, 4),
                    "entity": round(entity, 4),
                    "project": proj,
                    "recency": round(recency, 4),
                    "trust": round(trust, 4),
                    "usefulness": round(usefulness, 4),
                },
                notes=notes,
            )
        )

    hits = [h for h in hits if h.score >= min_score]
    hits.sort(key=lambda h: h.score, reverse=True)
    hits = hits[:limit]

    conflicts = detect_conflicts(store, [h.memory for h in hits], actor=actor)
    conflicted = {i for pair in conflicts for i in pair[:2]}
    for h in hits:
        if h.memory.id in conflicted:
            other = next(
                (b if a == h.memory.id else a) for a, b, _ in conflicts
                if h.memory.id in (a, b)
            )
            h.notes.append(f"conflicts with {other}; unresolved, treat with reduced confidence")

    if record_use:
        for h in hits:
            mem_mod.touch(store, h.memory.id)
    events.record(
        store,
        "memory.retrieve",
        actor,
        permission="read",
        session_id=session_id,
        query=query[:200],
        considered=len(candidates),
        returned=len(hits),
        conflicts=len(conflicts),
        top=[h.memory.id for h in hits[:3]],
    )
    return Retrieval(hits=hits, conflicts=conflicts, considered=len(candidates))


def detect_conflicts(
    store: Store, memories: list[Memory], *, actor: str = "claude"
) -> list[tuple[str, str, str]]:
    """Flag pairs that look contradictory and persist them as unresolved.

    This is a lexical heuristic, not a truth engine: it catches same-subject
    pairs that differ in polarity or in a stated number. It will miss subtler
    contradictions and will occasionally flag a pair that merely differs in
    context - which is why the output is a flag for resolution, never an
    automatic deletion or merge.
    """
    found: list[tuple[str, str, str]] = []
    for i, a in enumerate(memories):
        for b in memories[i + 1 :]:
            if a.kind != b.kind or a.kind not in ("semantic", "user", "project"):
                continue
            reason = mem_mod.contradiction_reason(a.title, a.body, b.title, b.body)
            if reason:
                found.append((a.id, b.id, reason))
                _persist_conflict(store, a.id, b.id, reason, actor)
    return found


def open_conflicts(store: Store) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in store.query(
            "SELECT * FROM memory_conflicts WHERE resolution = 'unresolved' ORDER BY created_at DESC"
        )
    ]


def resolve_conflict(
    store: Store, conflict_id: str, resolution: str, *, actor: str = "user", note: str = ""
) -> None:
    """Record how a contradiction was settled. Only a human resolves by fiat."""
    if resolution not in ("left", "right", "both_contextual", "neither", "unresolved"):
        raise ValueError(f"unknown resolution {resolution!r}")
    row = store.one("SELECT id FROM memory_conflicts WHERE id = ?", (conflict_id,))
    if row is None:
        raise KeyError(f"no such conflict: {conflict_id}")
    store.execute(
        "UPDATE memory_conflicts SET resolution = ?, note = ? WHERE id = ?",
        (resolution, note, conflict_id),
    )
    store.commit()
    events.record(
        store,
        "memory.conflict_resolved",
        actor,
        target=conflict_id,
        permission="modify",
        resolution=resolution,
        note=note,
    )


def _persist_conflict(store: Store, left: str, right: str, reason: str, actor: str) -> None:
    existing = store.one(
        """SELECT id FROM memory_conflicts
           WHERE (left_id = ? AND right_id = ?) OR (left_id = ? AND right_id = ?)""",
        (left, right, right, left),
    )
    if existing:
        return
    store.execute(
        """INSERT INTO memory_conflicts (id, left_id, right_id, detected_by, note, resolution, created_at)
           VALUES (?,?,?,?,?,'unresolved',?)""",
        (new_id("cfl"), left, right, actor, reason, utcnow()),
    )
    store.commit()
    events.record(
        store, "memory.conflict_detected", actor, target=left, permission="create",
        other=right, reason=reason,
    )


def _candidates(store: Store, kinds: list[str] | None, project: str | None) -> list[Memory]:
    sql = "SELECT * FROM memories WHERE archived = 0"
    params: list[Any] = []
    if kinds:
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        params.extend(kinds)
    if project:
        sql += " AND (project = ? OR project IS NULL)"
        params.append(project)
    return [mem_mod._from_row(r) for r in store.query(sql, tuple(params))]


def _idf(docs: list[set[str]]) -> dict[str, float]:
    n = len(docs)
    df: dict[str, int] = {}
    for d in docs:
        for t in d:
            df[t] = df.get(t, 0) + 1
    return {t: math.log((n + 1) / (c + 0.5)) + 1.0 for t, c in df.items()}


def _weighted_overlap(q: set[str], d: set[str], idf: dict[str, float]) -> float:
    if not q:
        return 0.0
    hit = sum(idf.get(t, 1.0) for t in q & d)
    total = sum(idf.get(t, 1.0) for t in q)
    return hit / total if total else 0.0


def _recency(ts: str, now: datetime) -> float:
    try:
        age_days = (now - datetime.fromisoformat(ts)).total_seconds() / 86400
    except (TypeError, ValueError):
        return 0.0
    return math.exp(-age_days / 30.0)  # half-life of roughly three weeks
