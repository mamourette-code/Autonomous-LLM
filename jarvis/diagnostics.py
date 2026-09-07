"""Metrics, health checks and self-diagnosis (protocol s44, s45, s57).

Every number here is derived from the event log and the state tables at call
time. Nothing is a maintained counter, so a metric cannot quietly disagree with
what actually happened.

The reporting rule is s57: never claim a capability that has not been verified.
`self_report()` therefore lists what this installation actually implements and
names what it does not.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from jarvis import memory as mem_mod, tasks as task_mod
from jarvis.store import Store, SCHEMA_VERSION

# Capabilities this package implements, and capabilities it deliberately does
# not. Kept honest by tests/test_diagnostics.py, which checks that every
# claimed capability resolves to a real callable.
IMPLEMENTED: dict[str, str] = {
    "persistent_state": "jarvis.store:open_store",
    "audit_log": "jarvis.events:record",
    "traced_actions": "jarvis.events:action",
    "objective_representation": "jarvis.objectives:create",
    "task_state_machine": "jarvis.tasks:transition",
    "dependency_gating": "jarvis.tasks:unmet_dependencies",
    "verification_records": "jarvis.tasks:verify",
    "failure_classification": "jarvis.tasks:FAILURE_CLASSES",
    "memory_write": "jarvis.memory:remember",
    "memory_governance": "jarvis.memory:assess",
    "knowledge_decay": "jarvis.memory:stale",
    "ranked_retrieval": "jarvis.retrieval:retrieve",
    "contradiction_detection": "jarvis.retrieval:detect_conflicts",
    "metrics": "jarvis.diagnostics:metrics",
    "health_checks": "jarvis.diagnostics:health",
    "session_boot_close": "jarvis.session:boot",
}

NOT_IMPLEMENTED: dict[str, str] = {
    "planning_engine": "no decomposition or replanning exists; plans are external to this package",
    "agent_orchestration": "no agent registry, delegation or inter-agent protocol",
    "skill_registry": "no executable reusable procedures are stored or versioned",
    "tool_registry": "tool reliability and selection are not tracked",
    "experimentation_harness": "no baseline/experiment/adoption machinery",
    "automation_discovery": "recurring workflows are not detected",
    "proactive_triggers": "nothing runs without being invoked",
}


def metrics(store: Store, window_days: int | None = None) -> dict[str, Any]:
    """System-level performance metrics (protocol s44).

    `window_days` limits event-derived metrics to recent activity; state counts
    always reflect the whole store.
    """
    since = None
    if window_days is not None:
        since = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()

    def ev_sql(extra: str) -> str:
        """Build an event query, adding the time window when one is requested."""
        clauses = (["ts >= ?"] if since else []) + ([extra] if extra else [])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return f"SELECT {{cols}} FROM events{where}"

    ev_params: tuple = (since,) if since else ()
    total_events = _scalar(store, ev_sql("").format(cols="COUNT(*)"), ev_params)
    failed_events = _scalar(
        store, ev_sql("outcome = 'failed'").format(cols="COUNT(*)"), ev_params
    )
    denied_events = _scalar(
        store, ev_sql("outcome = 'denied'").format(cols="COUNT(*)"), ev_params
    )

    by_status = {
        row["status"]: row["n"]
        for row in store.query("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
    }
    total_tasks = sum(by_status.values())
    completed = by_status.get(task_mod.COMPLETED, 0)
    verified = by_status.get(task_mod.VERIFIED, 0)
    failed = by_status.get(task_mod.FAILED, 0)
    resolved = completed + verified + failed

    ver_total = _scalar(store, "SELECT COUNT(*) FROM verifications")
    ver_independent = _scalar(store, "SELECT COUNT(*) FROM verifications WHERE independent = 1")
    ver_failed = _scalar(store, "SELECT COUNT(*) FROM verifications WHERE passed = 0")

    recovered = _recovered_failures(store)
    failure_mix = {
        row["failure_class"]: row["n"]
        for row in store.query(
            "SELECT failure_class, COUNT(*) AS n FROM tasks WHERE failure_class IS NOT NULL"
            " GROUP BY failure_class ORDER BY n DESC"
        )
    }

    mem_total = _scalar(store, "SELECT COUNT(*) FROM memories WHERE archived = 0")
    mem_used = _scalar(store, "SELECT COUNT(*) FROM memories WHERE archived = 0 AND access_count > 0")
    mem_stale = len(mem_mod.stale(store, limit=10_000))
    retrievals = store.query(
        ev_sql("kind = 'memory.retrieve'").format(cols="detail"), ev_params
    )
    empty_retrievals = sum(
        1 for r in retrievals if json.loads(r["detail"]).get("returned", 0) == 0
    )

    durations = [
        r["duration_ms"]
        for r in store.query(
            ev_sql("duration_ms IS NOT NULL").format(cols="duration_ms"), ev_params
        )
    ]

    return {
        "window_days": window_days,
        "tasks": {
            "total": total_tasks,
            "by_status": by_status,
            "resolved": resolved,
            "success_rate": _ratio(completed + verified, resolved),
            "verified_success_rate": _ratio(verified, resolved),
            "failure_rate": _ratio(failed, resolved),
            "recovery_rate": _ratio(recovered["recovered"], recovered["failures"]),
            "failure_classes": failure_mix,
        },
        "verification": {
            "records": ver_total,
            "independent": ver_independent,
            "independence_rate": _ratio(ver_independent, ver_total),
            "rejections": ver_failed,
        },
        "memory": {
            "live": mem_total,
            "ever_retrieved": mem_used,
            "utilization": _ratio(mem_used, mem_total),
            "stale": mem_stale,
            "retrievals": len(retrievals),
            "empty_retrievals": empty_retrievals,
            "empty_retrieval_rate": _ratio(empty_retrievals, len(retrievals)),
            "open_conflicts": _scalar(
                store, "SELECT COUNT(*) FROM memory_conflicts WHERE resolution = 'unresolved'"
            ),
        },
        "events": {
            "total": total_events,
            "failed": failed_events,
            "denied": denied_events,
            "error_rate": _ratio(failed_events, total_events),
            "median_duration_ms": _median(durations),
        },
    }


def health(store: Store) -> dict[str, Any]:
    """Subsystem health checks (protocol s45).

    Each check reports ok / warn / fail with a reason. A system that cannot
    describe its own operational state cannot improve itself.
    """
    checks: list[dict[str, str]] = []

    # Storage reachable and writable.
    try:
        store.execute("CREATE TABLE IF NOT EXISTS _health_probe (x INTEGER)")
        store.execute("DROP TABLE _health_probe")
        store.commit()
        checks.append(_check("storage", "ok", f"read/write at {store.path}"))
    except sqlite3.Error as exc:
        checks.append(_check("storage", "fail", f"database not writable: {exc}"))

    # Schema at the version this code expects.
    version = store.schema_version()
    if version == SCHEMA_VERSION:
        checks.append(_check("schema", "ok", f"version {version}"))
    elif version < SCHEMA_VERSION:
        checks.append(_check("schema", "fail", f"version {version} < expected {SCHEMA_VERSION}; run migrations"))
    else:
        checks.append(_check("schema", "warn", f"version {version} newer than this code expects ({SCHEMA_VERSION})"))

    # Integrity of the stored state.
    integrity = _scalar_str(store, "PRAGMA integrity_check")
    checks.append(
        _check("integrity", "ok" if integrity == "ok" else "fail", f"sqlite integrity_check: {integrity}")
    )
    orphans = _scalar(
        store,
        "SELECT COUNT(*) FROM task_deps d LEFT JOIN tasks t ON t.id = d.depends_on WHERE t.id IS NULL",
    )
    checks.append(
        _check("referential_integrity", "ok" if orphans == 0 else "fail",
               f"{orphans} dependency row(s) point at missing tasks")
    )

    # Observability: state changes should leave a trail.
    task_count = _scalar(store, "SELECT COUNT(*) FROM tasks")
    task_events = _scalar(store, "SELECT COUNT(*) FROM events WHERE kind LIKE 'task.%'")
    if task_count == 0:
        checks.append(_check("observability", "ok", "no tasks recorded yet"))
    elif task_events >= task_count:
        checks.append(_check("observability", "ok", f"{task_events} task events for {task_count} tasks"))
    else:
        checks.append(
            _check("observability", "fail", f"only {task_events} events for {task_count} tasks; writes bypassed the log")
        )

    # Memory hygiene.
    stale_count = len(mem_mod.stale(store, limit=10_000))
    checks.append(
        _check("memory_freshness", "ok" if stale_count == 0 else "warn",
               f"{stale_count} memory item(s) past their review date")
    )
    conflicts = _scalar(store, "SELECT COUNT(*) FROM memory_conflicts WHERE resolution = 'unresolved'")
    checks.append(
        _check("memory_consistency", "ok" if conflicts == 0 else "warn",
               f"{conflicts} unresolved contradiction(s) awaiting a decision")
    )

    # Stuck work.
    stuck = _scalar(
        store, "SELECT COUNT(*) FROM tasks WHERE status IN ('blocked','waiting','failed')"
    )
    checks.append(
        _check("task_flow", "ok" if stuck == 0 else "warn", f"{stuck} task(s) blocked, waiting or failed")
    )

    status = "ok"
    if any(c["status"] == "fail" for c in checks):
        status = "fail"
    elif any(c["status"] == "warn" for c in checks):
        status = "warn"
    return {"status": status, "checks": checks}


def self_report(store: Store) -> dict[str, Any]:
    """What this system can actually do right now (protocol s57)."""
    from jarvis import objectives as obj_mod

    return {
        "schema_version": store.schema_version(),
        "store_path": store.path,
        "implemented": sorted(IMPLEMENTED),
        "not_implemented": NOT_IMPLEMENTED,
        "open_objectives": [
            {"id": o.id, "title": o.title, "ready": o.is_ready, "gaps": o.readiness_gaps()}
            for o in obj_mod.list_open(store)
        ],
        "open_tasks": [
            {"id": t.id, "title": t.title, "status": t.status,
             "unmet_dependencies": task_mod.unmet_dependencies(store, t.id)}
            for t in task_mod.list_tasks(store, open_only=True)
        ],
        "recent_failures": [
            {"id": t.id, "title": t.title, "class": t.failure_class, "note": t.failure_note}
            for t in task_mod.list_tasks(store, status=task_mod.FAILED)
        ],
        "health": health(store),
        "metrics": metrics(store),
    }


def _recovered_failures(store: Store) -> dict[str, int]:
    """A failure counts as recovered when the same task later reaches VERIFIED."""
    failures = 0
    recovered = 0
    seen: set[str] = set()
    for row in store.query(
        "SELECT target, detail FROM events WHERE kind = 'task.transition' ORDER BY ts ASC"
    ):
        detail = json.loads(row["detail"])
        if detail.get("to") == task_mod.FAILED and row["target"] not in seen:
            seen.add(row["target"])
            failures += 1
    for target in seen:
        row = store.one("SELECT status FROM tasks WHERE id = ?", (target,))
        if row and row["status"] in (task_mod.COMPLETED, task_mod.VERIFIED):
            recovered += 1
    return {"failures": failures, "recovered": recovered}


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def _ratio(numerator: int, denominator: int) -> float | None:
    """None, not zero, when there is no evidence - an unmeasured rate is not 0%."""
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def _scalar(store: Store, sql: str, params: tuple = ()) -> int:
    row = store.one(sql, params)
    return int(row[0]) if row else 0


def _scalar_str(store: Store, sql: str, params: tuple = ()) -> str:
    row = store.one(sql, params)
    return str(row[0]) if row else ""
