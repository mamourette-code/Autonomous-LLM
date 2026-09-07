"""Objective representation (protocol s5).

A request becomes an explicit objective before it becomes a plan. The point of
this module is to force the ambiguity question to be answered rather than
silently guessed: an objective carries its assumptions and its unresolved
questions as first-class fields, and is not 'ready' while a blocking question
is open.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from jarvis import events
from jarvis.store import Store, new_id, utcnow

STATUSES = ("open", "achieved", "abandoned")

_JSON_FIELDS = ("constraints", "assumptions", "open_questions", "verification", "risks")


@dataclass
class Objective:
    id: str
    title: str
    desired_outcome: str
    constraints: list[str] = field(default_factory=list)
    priority: int = 3
    deadline: str | None = None
    assumptions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    authority: str = "user"
    project: str | None = None
    status: str = "open"
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_ready(self) -> bool:
        """An objective is ready to plan against when nothing material is open.

        Ambiguity that does not change execution belongs in `assumptions`;
        ambiguity that does belongs in `open_questions` and blocks readiness.
        """
        return self.status == "open" and not self.open_questions and bool(self.verification)

    def readiness_gaps(self) -> list[str]:
        gaps = []
        if self.open_questions:
            gaps.append(
                f"{len(self.open_questions)} unresolved question(s) that materially affect execution"
            )
        if not self.verification:
            gaps.append("no verification criteria: success would be unfalsifiable")
        return gaps


def create(
    store: Store,
    title: str,
    desired_outcome: str,
    *,
    actor: str = "claude",
    constraints: list[str] | None = None,
    priority: int = 3,
    deadline: str | None = None,
    assumptions: list[str] | None = None,
    open_questions: list[str] | None = None,
    verification: list[str] | None = None,
    risks: list[str] | None = None,
    authority: str = "user",
    project: str | None = None,
    session_id: str | None = None,
) -> Objective:
    if not title.strip():
        raise ValueError("objective title must not be empty")
    if not desired_outcome.strip():
        raise ValueError("objective must state a desired outcome")
    if not 1 <= priority <= 5:
        raise ValueError("priority must be 1 (highest) to 5 (lowest)")
    now = utcnow()
    obj = Objective(
        id=new_id("obj"),
        title=title,
        desired_outcome=desired_outcome,
        constraints=constraints or [],
        priority=priority,
        deadline=deadline,
        assumptions=assumptions or [],
        open_questions=open_questions or [],
        verification=verification or [],
        risks=risks or [],
        authority=authority,
        project=project,
        status="open",
        created_at=now,
        updated_at=now,
    )
    store.execute(
        """INSERT INTO objectives
           (id, title, desired_outcome, constraints, priority, deadline, assumptions,
            open_questions, verification, risks, authority, project, status,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            obj.id,
            obj.title,
            obj.desired_outcome,
            json.dumps(obj.constraints),
            obj.priority,
            obj.deadline,
            json.dumps(obj.assumptions),
            json.dumps(obj.open_questions),
            json.dumps(obj.verification),
            json.dumps(obj.risks),
            obj.authority,
            obj.project,
            obj.status,
            obj.created_at,
            obj.updated_at,
        ),
    )
    store.commit()
    events.record(
        store,
        "objective.create",
        actor,
        target=obj.id,
        permission="create",
        authorized_by=authority,
        session_id=session_id,
        title=title,
        ready=obj.is_ready,
    )
    return obj


def get(store: Store, objective_id: str) -> Objective | None:
    row = store.one("SELECT * FROM objectives WHERE id = ?", (objective_id,))
    return _from_row(row) if row else None


def resolve_question(
    store: Store, objective_id: str, question: str, answer: str, *, actor: str = "user"
) -> Objective:
    """Answer an open question; the answer is retained as a stated assumption."""
    obj = _require(store, objective_id)
    if question not in obj.open_questions:
        raise ValueError(f"objective {objective_id} has no open question {question!r}")
    obj.open_questions = [q for q in obj.open_questions if q != question]
    obj.assumptions = obj.assumptions + [f"{question} -> {answer}"]
    _save(store, obj)
    events.record(
        store,
        "objective.resolve_question",
        actor,
        target=obj.id,
        permission="modify",
        question=question,
        answer=answer,
    )
    return obj


def set_status(
    store: Store, objective_id: str, status: str, *, actor: str = "user", note: str = ""
) -> Objective:
    """Close an objective. Only the authority may declare it achieved (s64)."""
    if status not in STATUSES:
        raise ValueError(f"unknown objective status {status!r}; expected {STATUSES}")
    obj = _require(store, objective_id)
    obj.status = status
    _save(store, obj)
    events.record(
        store,
        "objective.status",
        actor,
        target=obj.id,
        permission="modify",
        authorized_by=obj.authority,
        status=status,
        note=note,
    )
    return obj


def list_open(store: Store, project: str | None = None) -> list[Objective]:
    if project:
        rows = store.query(
            "SELECT * FROM objectives WHERE status = 'open' AND project = ?"
            " ORDER BY priority ASC, created_at ASC",
            (project,),
        )
    else:
        rows = store.query(
            "SELECT * FROM objectives WHERE status = 'open' ORDER BY priority ASC, created_at ASC"
        )
    return [_from_row(r) for r in rows]


def _require(store: Store, objective_id: str) -> Objective:
    obj = get(store, objective_id)
    if obj is None:
        raise KeyError(f"no such objective: {objective_id}")
    return obj


def _save(store: Store, obj: Objective) -> None:
    obj.updated_at = utcnow()
    store.execute(
        """UPDATE objectives SET title=?, desired_outcome=?, constraints=?, priority=?,
           deadline=?, assumptions=?, open_questions=?, verification=?, risks=?,
           authority=?, project=?, status=?, updated_at=? WHERE id=?""",
        (
            obj.title,
            obj.desired_outcome,
            json.dumps(obj.constraints),
            obj.priority,
            obj.deadline,
            json.dumps(obj.assumptions),
            json.dumps(obj.open_questions),
            json.dumps(obj.verification),
            json.dumps(obj.risks),
            obj.authority,
            obj.project,
            obj.status,
            obj.updated_at,
            obj.id,
        ),
    )
    store.commit()


def _from_row(row) -> Objective:
    d: dict[str, Any] = dict(row)
    for key in _JSON_FIELDS:
        d[key] = json.loads(d[key])
    return Objective(**d)
