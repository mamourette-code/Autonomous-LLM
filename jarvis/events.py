"""Observability and audit (protocol s35, s43).

Every consequential mutation in JARVIS records an event. The event log is the
only source of truth for "what happened" and "why did it happen" - metrics in
jarvis.diagnostics are derived from it rather than maintained separately, so
counters cannot drift away from reality.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Any, Iterator

from jarvis.store import Store, new_id, utcnow

# Outcomes are a closed set so metrics can be computed without guessing.
OUTCOMES = ("ok", "failed", "blocked", "denied", "skipped")

# Permission categories (protocol s34). Knowing how to act is not authority to
# act; recording the category makes unauthorized action auditable.
PERMISSIONS = (
    "read",
    "create",
    "modify",
    "execute",
    "communicate",
    "transact",
    "delete",
    "administrative",
)


def record(
    store: Store,
    kind: str,
    actor: str,
    outcome: str = "ok",
    target: str | None = None,
    permission: str | None = None,
    authorized_by: str | None = None,
    duration_ms: int | None = None,
    session_id: str | None = None,
    **detail: Any,
) -> str:
    """Append one event to the audit log. Returns the event id."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}; expected one of {OUTCOMES}")
    if permission is not None and permission not in PERMISSIONS:
        raise ValueError(f"unknown permission {permission!r}; expected one of {PERMISSIONS}")
    event_id = new_id("evt")
    store.execute(
        """INSERT INTO events
           (id, ts, kind, actor, target, outcome, permission, authorized_by,
            duration_ms, session_id, detail)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            utcnow(),
            kind,
            actor,
            target,
            outcome,
            permission,
            authorized_by,
            duration_ms,
            session_id,
            json.dumps(detail, default=str),
        ),
    )
    store.commit()
    return event_id


@contextmanager
def action(
    store: Store,
    kind: str,
    actor: str,
    target: str | None = None,
    permission: str | None = None,
    authorized_by: str | None = None,
    session_id: str | None = None,
    **detail: Any,
) -> Iterator[dict[str, Any]]:
    """Time an operation and record its true outcome, including on exception.

    Yields a mutable dict; anything put in it is merged into the event detail.
    A failure inside the block is recorded as 'failed' and re-raised - the log
    must never make a failure look like a success (protocol s63).
    """
    started = time.monotonic()
    extra: dict[str, Any] = {}
    try:
        yield extra
    except Exception as exc:  # noqa: BLE001 - deliberately broad: we re-raise
        record(
            store,
            kind,
            actor,
            outcome="failed",
            target=target,
            permission=permission,
            authorized_by=authorized_by,
            duration_ms=int((time.monotonic() - started) * 1000),
            session_id=session_id,
            error=f"{type(exc).__name__}: {exc}",
            **{**detail, **extra},
        )
        raise
    record(
        store,
        kind,
        actor,
        outcome="ok",
        target=target,
        permission=permission,
        authorized_by=authorized_by,
        duration_ms=int((time.monotonic() - started) * 1000),
        session_id=session_id,
        **{**detail, **extra},
    )


def recent(store: Store, limit: int = 50, kind: str | None = None) -> list[dict[str, Any]]:
    """Most recent events, newest first."""
    if kind:
        rows = store.query(
            "SELECT * FROM events WHERE kind = ? ORDER BY ts DESC, rowid DESC LIMIT ?",
            (kind, limit),
        )
    else:
        rows = store.query(
            "SELECT * FROM events ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,)
        )
    return [_row(r) for r in rows]


def for_target(store: Store, target: str, limit: int = 100) -> list[dict[str, Any]]:
    """Full history for one entity - the 'why did it happen' query."""
    rows = store.query(
        "SELECT * FROM events WHERE target = ? ORDER BY ts ASC, rowid ASC LIMIT ?",
        (target, limit),
    )
    return [_row(r) for r in rows]


def _row(row) -> dict[str, Any]:
    d = dict(row)
    d["detail"] = json.loads(d.get("detail") or "{}")
    return d
