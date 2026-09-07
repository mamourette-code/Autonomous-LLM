"""Session boot and close protocols (protocol s47, s48).

Continuity across sessions is the whole point: the conversation window is
temporary, so what matters must be reconstructed from state rather than
remembered. `boot()` loads what is relevant - not everything - and `close()`
records what actually happened without manufacturing memories just because a
session ended.
"""

from __future__ import annotations

import json
from typing import Any

from jarvis import diagnostics, events, memory as mem_mod, objectives as obj_mod, retrieval, tasks as task_mod
from jarvis.store import Store, new_id, utcnow


def boot(
    store: Store,
    *,
    project: str | None = None,
    actor: str = "claude",
    focus: str | None = None,
    max_items: int = 10,
) -> dict[str, Any]:
    """Start a session and return an oriented briefing.

    `focus` is an optional statement of what this session is about; when given,
    relevant memories are retrieved for it instead of loading memory wholesale.
    """
    session_id = new_id("ses")
    started = utcnow()
    previous = store.one(
        "SELECT id, started_at, ended_at, summary FROM sessions ORDER BY started_at DESC LIMIT 1"
    )
    store.execute(
        "INSERT INTO sessions (id, started_at, ended_at, summary, note) VALUES (?,?,NULL,NULL,?)",
        (session_id, started, project or ""),
    )
    store.commit()  # the session row must exist before events reference it

    since = previous["ended_at"] or previous["started_at"] if previous else None
    changed = _changes_since(store, since) if since else []

    briefing: dict[str, Any] = {
        "session_id": session_id,
        "started_at": started,
        "project": project,
        "previous_session": (
            {
                "id": previous["id"],
                "ended_at": previous["ended_at"],
                "summary": previous["summary"],
            }
            if previous
            else None
        ),
        "changed_since_last_session": changed[:max_items],
        "open_objectives": [
            {
                "id": o.id,
                "title": o.title,
                "priority": o.priority,
                "ready": o.is_ready,
                "gaps": o.readiness_gaps(),
                "open_questions": o.open_questions,
            }
            for o in obj_mod.list_open(store, project=project)[:max_items]
        ],
        "ready_tasks": [
            {"id": t.id, "title": t.title, "status": t.status, "priority": t.priority}
            for t in task_mod.ready_tasks(store, project=project)[:max_items]
        ],
        "blocked_tasks": [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "unmet_dependencies": task_mod.unmet_dependencies(store, t.id),
            }
            for t in task_mod.list_tasks(store, open_only=True, project=project)
            if t.status in (task_mod.BLOCKED, task_mod.WAITING)
            or task_mod.unmet_dependencies(store, t.id)
        ][:max_items],
        "recent_failures": [
            {"id": t.id, "title": t.title, "class": t.failure_class, "note": t.failure_note}
            for t in task_mod.list_tasks(store, status=task_mod.FAILED, project=project)[:max_items]
        ],
        "unverified_completions": [
            {"id": t.id, "title": t.title}
            for t in task_mod.list_tasks(store, status=task_mod.COMPLETED, project=project)[:max_items]
        ],
        "stale_memories": [
            {"id": m.id, "title": m.title, "review_after": m.review_after}
            for m in mem_mod.stale(store, limit=max_items)
        ],
        "open_conflicts": retrieval.open_conflicts(store)[:max_items],
        "health": diagnostics.health(store),
    }

    if focus:
        result = retrieval.retrieve(
            store, focus, project=project, limit=max_items, actor=actor, session_id=session_id
        )
        briefing["relevant_memories"] = [
            {
                "id": h.memory.id,
                "kind": h.memory.kind,
                "title": h.memory.title,
                "body": h.memory.body,
                "source_class": h.memory.source_class,
                "score": h.score,
                "effective_confidence": h.effective_confidence,
                "notes": h.notes,
            }
            for h in result.hits
        ]

    events.record(
        store,
        "session.boot",
        actor,
        target=session_id,
        permission="read",
        session_id=session_id,
        project=project,
        focus=focus,
        ready_tasks=len(briefing["ready_tasks"]),
        health=briefing["health"]["status"],
    )
    return briefing


def close(
    store: Store,
    session_id: str,
    *,
    summary: str,
    actor: str = "claude",
) -> dict[str, Any]:
    """End a session and report what actually happened during it.

    The report distinguishes verified from merely completed work and names what
    is unfinished. It does not write memories on its own - that decision goes
    through jarvis.memory.assess like any other.
    """
    row = store.one("SELECT * FROM sessions WHERE id = ?", (session_id,))
    if row is None:
        raise KeyError(f"no such session: {session_id}")
    if row["ended_at"]:
        raise RuntimeError(f"session {session_id} is already closed")

    started_at = row["started_at"]
    session_events = store.query(
        "SELECT * FROM events WHERE session_id = ? ORDER BY ts ASC", (session_id,)
    )
    transitions = [
        (e["target"], json.loads(e["detail"]))
        for e in session_events
        if e["kind"] == "task.transition"
    ]
    verified = sorted({t for t, d in transitions if d.get("to") == task_mod.VERIFIED})
    completed = sorted(
        {t for t, d in transitions if d.get("to") == task_mod.COMPLETED} - set(verified)
    )
    failed = sorted({t for t, d in transitions if d.get("to") == task_mod.FAILED})

    ended = utcnow()
    open_now = task_mod.list_tasks(store, open_only=True)
    report = {
        "session_id": session_id,
        "started_at": started_at,
        "ended_at": ended,
        "summary": summary,
        "verified": [_title(store, t) for t in verified],
        "completed_unverified": [_title(store, t) for t in completed],
        "failed": [_title(store, t) for t in failed],
        "still_open": [{"id": t.id, "title": t.title, "status": t.status} for t in open_now],
        "memories_written": [
            e["target"] for e in session_events if e["kind"] == "memory.write"
        ],
        "events_recorded": len(session_events),
        "carry_forward": _carry_forward(store, completed, open_now),
    }
    # Closing the session and recording that it closed commit together.
    with store.transaction():
        store.execute(
            "UPDATE sessions SET ended_at = ?, summary = ? WHERE id = ?",
            (ended, summary, session_id),
        )
        events.record(
            store,
            "session.close",
            actor,
            target=session_id,
            permission="modify",
            session_id=session_id,
            verified=len(verified),
            completed_unverified=len(completed),
            failed=len(failed),
        )
    return report


def _carry_forward(store: Store, completed_unverified: list[str], open_now: list) -> list[str]:
    """The honest list of what the next session inherits."""
    items: list[str] = []
    if completed_unverified:
        items.append(
            f"{len(completed_unverified)} task(s) completed but not verified - "
            "completion is not evidence of correctness"
        )
    if open_now:
        items.append(f"{len(open_now)} task(s) still open")
    conflicts = retrieval.open_conflicts(store)
    if conflicts:
        items.append(f"{len(conflicts)} unresolved memory contradiction(s) need a decision")
    stale = mem_mod.stale(store, limit=10_000)
    if stale:
        items.append(f"{len(stale)} memory item(s) past their review date")
    return items


def _changes_since(store: Store, since: str) -> list[dict[str, Any]]:
    rows = store.query(
        """SELECT kind, actor, target, outcome, ts FROM events
           WHERE ts > ? AND kind NOT IN ('memory.retrieve','session.boot')
           ORDER BY ts DESC LIMIT 100""",
        (since,),
    )
    return [dict(r) for r in rows]


def _title(store: Store, task_id: str) -> dict[str, str]:
    row = store.one("SELECT id, title, status FROM tasks WHERE id = ?", (task_id,))
    return dict(row) if row else {"id": task_id, "title": "(deleted)", "status": "unknown"}
