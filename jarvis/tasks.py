"""Task model, state machine and verification (protocol s6, s29, s30).

Two rules are enforced here rather than left to good intentions:

1. COMPLETED and VERIFIED are different states. A task reaches VERIFIED only
   through a recorded verification carrying a method and evidence.
2. A failure must be classified before it can be recorded, so recovery can be
   causal rather than a retry loop (s31).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jarvis import events
from jarvis.store import Store, new_id, utcnow

PENDING = "pending"
IN_PROGRESS = "in_progress"
BLOCKED = "blocked"
WAITING = "waiting"
FAILED = "failed"
COMPLETED = "completed"
VERIFIED = "verified"
ARCHIVED = "archived"

STATUSES = (PENDING, IN_PROGRESS, BLOCKED, WAITING, FAILED, COMPLETED, VERIFIED, ARCHIVED)
OPEN_STATUSES = (PENDING, IN_PROGRESS, BLOCKED, WAITING, FAILED)

# Allowed transitions. Anything absent is rejected; the state machine is the
# reason the log can be trusted.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    PENDING: (IN_PROGRESS, BLOCKED, WAITING, FAILED, ARCHIVED),
    IN_PROGRESS: (COMPLETED, BLOCKED, WAITING, FAILED, PENDING),
    BLOCKED: (PENDING, IN_PROGRESS, WAITING, FAILED, ARCHIVED),
    WAITING: (PENDING, IN_PROGRESS, BLOCKED, FAILED, ARCHIVED),
    FAILED: (PENDING, IN_PROGRESS, ARCHIVED),
    COMPLETED: (VERIFIED, FAILED, IN_PROGRESS, ARCHIVED),
    VERIFIED: (ARCHIVED,),
    ARCHIVED: (),
}

# Failure taxonomy (protocol s30). Classification precedes recovery.
FAILURE_CLASSES = (
    "incorrect_assumption",
    "missing_information",
    "invalid_tool_call",
    "tool_failure",
    "implementation_error",
    "planning_error",
    "retrieval_failure",
    "memory_failure",
    "verification_failure",
    "permission_failure",
    "environmental_failure",
    "external_dependency_failure",
)


class TransitionError(RuntimeError):
    """Raised when a status change is not permitted from the current state."""


@dataclass
class Task:
    id: str
    title: str
    status: str
    objective_id: str | None = None
    priority: int = 3
    parent_id: str | None = None
    assignee: str | None = None
    expected: str | None = None
    actual: str | None = None
    failure_class: str | None = None
    failure_note: str | None = None
    project: str | None = None
    created_at: str = ""
    updated_at: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    verified_at: str | None = None
    depends_on: list[str] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


def create(
    store: Store,
    title: str,
    *,
    actor: str = "claude",
    objective_id: str | None = None,
    priority: int = 3,
    parent_id: str | None = None,
    assignee: str | None = None,
    expected: str | None = None,
    project: str | None = None,
    depends_on: list[str] | None = None,
    session_id: str | None = None,
) -> Task:
    if not title.strip():
        raise ValueError("task title must not be empty")
    now = utcnow()
    task = Task(
        id=new_id("tsk"),
        title=title,
        status=PENDING,
        objective_id=objective_id,
        priority=priority,
        parent_id=parent_id,
        assignee=assignee,
        expected=expected,
        project=project,
        created_at=now,
        updated_at=now,
        depends_on=list(depends_on or []),
    )
    # The row, its dependencies and its audit event commit together or not at
    # all. Previously a bad dependency left an orphan task with no event.
    with store.transaction():
        store.execute(
            """INSERT INTO tasks (id, objective_id, title, status, priority, parent_id,
               assignee, expected, actual, failure_class, failure_note, project,
               created_at, updated_at, started_at, completed_at, verified_at)
               VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,NULL,NULL,NULL)""",
            (
                task.id,
                task.objective_id,
                task.title,
                task.status,
                task.priority,
                task.parent_id,
                task.assignee,
                task.expected,
                task.project,
                task.created_at,
                task.updated_at,
            ),
        )
        for dep in task.depends_on:
            store.execute(
                "INSERT INTO task_deps (task_id, depends_on) VALUES (?,?)", (task.id, dep)
            )
        events.record(
            store,
            "task.create",
            actor,
            target=task.id,
            permission="create",
            session_id=session_id,
            title=title,
            objective_id=objective_id,
        )
    return task


def get(store: Store, task_id: str) -> Task | None:
    row = store.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if row is None:
        return None
    deps = [
        r["depends_on"]
        for r in store.query("SELECT depends_on FROM task_deps WHERE task_id = ?", (task_id,))
    ]
    return _from_row(row, deps)


def unmet_dependencies(store: Store, task_id: str) -> list[str]:
    """Dependencies that are not yet at least COMPLETED."""
    rows = store.query(
        """SELECT d.depends_on AS dep, t.status AS status
           FROM task_deps d LEFT JOIN tasks t ON t.id = d.depends_on
           WHERE d.task_id = ?""",
        (task_id,),
    )
    return [r["dep"] for r in rows if r["status"] not in (COMPLETED, VERIFIED)]


def transition(
    store: Store,
    task_id: str,
    status: str,
    *,
    actor: str = "claude",
    note: str = "",
    actual: str | None = None,
    failure_class: str | None = None,
    session_id: str | None = None,
) -> Task:
    """Move a task to a new status, enforcing the state machine.

    VERIFIED is never reachable here - use verify(), which requires evidence.
    """
    task = _require(store, task_id)
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; expected one of {STATUSES}")
    if status == VERIFIED:
        raise TransitionError("VERIFIED is only reachable through verify() with evidence")
    if status not in TRANSITIONS[task.status]:
        events.record(
            store,
            "task.transition",
            actor,
            target=task_id,
            outcome="denied",
            permission="modify",
            session_id=session_id,
            **{"from": task.status, "to": status, "reason": "illegal transition"},
        )
        raise TransitionError(f"cannot move task {task_id} from {task.status} to {status}")
    if status == FAILED:
        if failure_class not in FAILURE_CLASSES:
            raise ValueError(
                "a failure must be classified before it is recorded; "
                f"failure_class must be one of {FAILURE_CLASSES}"
            )
    if status == IN_PROGRESS:
        unmet = unmet_dependencies(store, task_id)
        if unmet:
            events.record(
                store,
                "task.transition",
                actor,
                target=task_id,
                outcome="blocked",
                permission="modify",
                session_id=session_id,
                unmet_dependencies=unmet,
            )
            raise TransitionError(
                f"task {task_id} has unmet dependencies: {', '.join(unmet)}"
            )

    previous = task.status
    task.status = status
    task.updated_at = utcnow()
    if status == IN_PROGRESS and task.started_at is None:
        task.started_at = task.updated_at
    if status == COMPLETED:
        task.completed_at = task.updated_at
    if actual is not None:
        task.actual = actual
    if status == FAILED:
        task.failure_class = failure_class
        task.failure_note = note
    with store.transaction():
        _save(store, task)
        events.record(
            store,
            "task.transition",
            actor,
            target=task_id,
            permission="modify",
            session_id=session_id,
            note=note,
            failure_class=failure_class,
            **{"from": previous, "to": status},
        )
    return task


def verify(
    store: Store,
    task_id: str,
    *,
    method: str,
    evidence: str,
    verifier: str,
    passed: bool = True,
    session_id: str | None = None,
) -> Task:
    """Record a verification and, if it passed, move the task to VERIFIED.

    `independent` is derived, not claimed: it is true only when the verifier
    differs from whoever moved the task to COMPLETED (protocol s29). Re-running
    the same reasoning under the same actor is recorded as non-independent.
    """
    task = _require(store, task_id)
    if task.status != COMPLETED:
        raise TransitionError(
            f"only a COMPLETED task can be verified; {task_id} is {task.status}"
        )
    if not method.strip() or not evidence.strip():
        raise ValueError("verification requires both a method and concrete evidence")

    completer = _completer(store, task_id)
    independent = bool(completer and verifier != completer)
    with store.transaction():
        store.execute(
            """INSERT INTO verifications
               (id, task_id, method, evidence, verifier, completer, independent, passed, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                new_id("ver"),
                task_id,
                method,
                evidence,
                verifier,
                completer,
                int(independent),
                int(passed),
                utcnow(),
            ),
        )
        events.record(
            store,
            "task.verify",
            verifier,
            target=task_id,
            outcome="ok" if passed else "failed",
            permission="modify",
            session_id=session_id,
            method=method,
            independent=independent,
            passed=passed,
        )

    if not passed:
        return transition(
            store,
            task_id,
            FAILED,
            actor=verifier,
            note=f"verification failed: {evidence}",
            failure_class="verification_failure",
            session_id=session_id,
        )
    task.status = VERIFIED
    task.verified_at = utcnow()
    task.updated_at = task.verified_at
    with store.transaction():
        _save(store, task)
        events.record(
            store,
            "task.transition",
            verifier,
            target=task_id,
            permission="modify",
            session_id=session_id,
            **{"from": COMPLETED, "to": VERIFIED},
        )
    return task


def verifications(store: Store, task_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in store.query(
        "SELECT * FROM verifications WHERE task_id = ? ORDER BY created_at ASC", (task_id,)
    )]


def list_tasks(
    store: Store,
    *,
    status: str | None = None,
    open_only: bool = False,
    project: str | None = None,
    objective_id: str | None = None,
    limit: int = 200,
) -> list[Task]:
    sql = "SELECT * FROM tasks WHERE 1=1"
    params: list[Any] = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if open_only:
        sql += f" AND status IN ({','.join('?' * len(OPEN_STATUSES))})"
        params.extend(OPEN_STATUSES)
    if project:
        sql += " AND project = ?"
        params.append(project)
    if objective_id:
        sql += " AND objective_id = ?"
        params.append(objective_id)
    sql += " ORDER BY priority ASC, created_at ASC LIMIT ?"
    params.append(limit)
    tasks = []
    for row in store.query(sql, tuple(params)):
        deps = [
            r["depends_on"]
            for r in store.query(
                "SELECT depends_on FROM task_deps WHERE task_id = ?", (row["id"],)
            )
        ]
        tasks.append(_from_row(row, deps))
    return tasks


def ready_tasks(store: Store, project: str | None = None) -> list[Task]:
    """Open tasks whose dependencies are all satisfied - the actionable set."""
    return [
        t
        for t in list_tasks(store, open_only=True, project=project)
        if t.status in (PENDING, IN_PROGRESS) and not unmet_dependencies(store, t.id)
    ]


def _completer(store: Store, task_id: str) -> str | None:
    """Who last moved this task into COMPLETED, according to the audit log."""
    for ev in reversed(events.for_target(store, task_id)):
        if ev["kind"] == "task.transition" and ev["detail"].get("to") == COMPLETED:
            return ev["actor"]
    return None


def _require(store: Store, task_id: str) -> Task:
    task = get(store, task_id)
    if task is None:
        raise KeyError(f"no such task: {task_id}")
    return task


def _save(store: Store, task: Task) -> None:
    store.execute(
        """UPDATE tasks SET objective_id=?, title=?, status=?, priority=?, parent_id=?,
           assignee=?, expected=?, actual=?, failure_class=?, failure_note=?, project=?,
           updated_at=?, started_at=?, completed_at=?, verified_at=? WHERE id=?""",
        (
            task.objective_id,
            task.title,
            task.status,
            task.priority,
            task.parent_id,
            task.assignee,
            task.expected,
            task.actual,
            task.failure_class,
            task.failure_note,
            task.project,
            task.updated_at,
            task.started_at,
            task.completed_at,
            task.verified_at,
            task.id,
        ),
    )
    store.commit()


def _from_row(row, deps: list[str]) -> Task:
    d: dict[str, Any] = dict(row)
    return Task(**d, depends_on=deps)
