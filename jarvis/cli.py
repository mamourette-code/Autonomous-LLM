"""Command line interface.

The CLI exists so the persistent state is inspectable by a human without going
through the model (protocol s64: autonomy increases execution capability, it
does not remove human control). Every command accepts --json for machine use.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from jarvis import (
    diagnostics,
    events as event_mod,
    memory as mem_mod,
    objectives as obj_mod,
    retrieval,
    session as session_mod,
    snapshot as snapshot_mod,
    tasks as task_mod,
)
from jarvis.store import open_store

_STATUS_MARK = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}

# Applied after parsing, because these flags must keep argparse's SUPPRESS
# default to survive being given before the subcommand (see _build_parser).
_GLOBAL_DEFAULTS = {"db": None, "json": False, "actor": "claude", "session": None}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    for name, fallback in _GLOBAL_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, fallback)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    with open_store(args.db) as store:
        try:
            result = args.func(store, args)
        except (
            ValueError,
            KeyError,
            RuntimeError,
            task_mod.TransitionError,
            obj_mod.AuthorityError,
            snapshot_mod.SnapshotError,
        ) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _render(args.command, result)
    return 0


# -- command implementations ----------------------------------------------

def _cmd_boot(store, args) -> dict[str, Any]:
    return session_mod.boot(store, project=args.project, focus=args.focus, actor=args.actor)


def _cmd_close(store, args) -> dict[str, Any]:
    return session_mod.close(store, args.session_id, summary=args.summary, actor=args.actor)


def _cmd_doctor(store, args) -> dict[str, Any]:
    return diagnostics.self_report(store)


def _cmd_metrics(store, args) -> dict[str, Any]:
    return diagnostics.metrics(store, window_days=args.days)


def _cmd_objective(store, args) -> Any:
    if args.action == "add":
        obj = obj_mod.create(
            store,
            args.title,
            args.outcome,
            actor=args.actor,
            priority=args.priority,
            project=args.project,
            verification=args.verify or [],
            open_questions=args.question or [],
            assumptions=args.assume or [],
        )
        return _objective_dict(obj)
    if args.action == "list":
        return [_objective_dict(o) for o in obj_mod.list_open(store, project=args.project)]
    if args.action == "resolve":
        obj = obj_mod.resolve_question(store, args.id, args.question_text, args.answer, actor=args.actor)
        return _objective_dict(obj)
    raise ValueError(f"unknown objective action {args.action!r}")


def _cmd_task(store, args) -> Any:
    if args.action == "add":
        t = task_mod.create(
            store,
            args.title,
            actor=args.actor,
            objective_id=args.objective,
            priority=args.priority,
            project=args.project,
            expected=args.expected,
            depends_on=args.depends_on or [],
            session_id=args.session,
        )
        return _task_dict(store, t)
    if args.action == "list":
        tasks = (
            task_mod.ready_tasks(store, project=args.project)
            if args.ready
            else task_mod.list_tasks(
                store, status=args.status, open_only=args.open, project=args.project
            )
        )
        return [_task_dict(store, t) for t in tasks]
    if args.action == "show":
        t = task_mod.get(store, args.id)
        if t is None:
            raise KeyError(f"no such task: {args.id}")
        d = _task_dict(store, t)
        d["verifications"] = task_mod.verifications(store, t.id)
        d["history"] = event_mod.for_target(store, t.id)
        return d
    if args.action in ("start", "block", "wait", "complete", "fail", "archive"):
        status = {
            "start": task_mod.IN_PROGRESS,
            "block": task_mod.BLOCKED,
            "wait": task_mod.WAITING,
            "complete": task_mod.COMPLETED,
            "fail": task_mod.FAILED,
            "archive": task_mod.ARCHIVED,
        }[args.action]
        t = task_mod.transition(
            store,
            args.id,
            status,
            actor=args.actor,
            note=args.note or "",
            actual=args.actual,
            failure_class=args.failure_class,
            session_id=args.session,
        )
        return _task_dict(store, t)
    if args.action == "verify":
        t = task_mod.verify(
            store,
            args.id,
            method=args.method,
            evidence=args.evidence,
            verifier=args.actor,
            passed=not args.reject,
            session_id=args.session,
        )
        return _task_dict(store, t)
    raise ValueError(f"unknown task action {args.action!r}")


def _cmd_memory(store, args) -> Any:
    if args.action == "assess":
        a = mem_mod.assess(
            store,
            kind=args.kind,
            title=args.title,
            body=args.body,
            confidence=args.confidence,
            durability=args.durability,
            utility=args.utility,
            decay=args.decay,
            project=args.project,
        )
        return {
            "decision": a.decision,
            "expected_value": a.expected_value,
            "scores": a.scores,
            "reasons": a.reasons,
            "nearest_id": a.nearest_id,
        }
    if args.action == "add":
        a = mem_mod.assess(
            store,
            kind=args.kind,
            title=args.title,
            body=args.body,
            confidence=args.confidence,
            durability=args.durability,
            utility=args.utility,
            decay=args.decay,
            project=args.project,
        )
        if not a.should_store and not args.force:
            hint = "re-run with --force to store anyway (the override is recorded)"
            if a.supersede_candidate:
                hint = (
                    f"this looks like a revision of {a.supersede_candidate}; supersede that "
                    "memory rather than storing a second overlapping copy, or --force"
                )
            return {
                "stored": False,
                "decision": a.decision,
                "reasons": a.reasons,
                "supersede_candidate": a.supersede_candidate,
                "hint": hint,
            }
        m = mem_mod.remember(
            store,
            kind=args.kind,
            title=args.title,
            body=args.body,
            actor=args.actor,
            source=args.source,
            source_class=args.source_class,
            confidence=args.confidence,
            project=args.project,
            entities=args.entity or [],
            tags=args.tag or [],
            decay=args.decay,
            assessment=a,
            session_id=args.session,
        )
        return {"stored": True, "id": m.id, "decision": a.decision, "forced": bool(args.force),
                "review_after": m.review_after}
    if args.action == "search":
        r = retrieval.retrieve(
            store,
            args.query,
            project=args.project,
            kinds=args.kind_filter or None,
            limit=args.limit,
            actor=args.actor,
        )
        return {
            "considered": r.considered,
            "hits": [
                {
                    "id": h.memory.id,
                    "kind": h.memory.kind,
                    "title": h.memory.title,
                    "body": h.memory.body,
                    "source_class": h.memory.source_class,
                    "score": h.score,
                    "effective_confidence": h.effective_confidence,
                    "components": h.components,
                    "notes": h.notes,
                }
                for h in r.hits
            ],
            "conflicts": r.conflicts,
        }
    if args.action == "stale":
        return [
            {"id": m.id, "title": m.title, "review_after": m.review_after, "kind": m.kind}
            for m in mem_mod.stale(store)
        ]
    if args.action == "conflicts":
        return retrieval.open_conflicts(store)
    raise ValueError(f"unknown memory action {args.action!r}")


def _cmd_snapshot(store, args) -> Any:
    if args.action == "create":
        snap = snapshot_mod.create(
            store,
            actor=args.actor,
            reason=args.reason or "",
            directory=args.dir,
            label=args.label,
            session_id=args.session,
        )
        return _snapshot_dict(snap)
    if args.action == "list":
        return [_snapshot_dict(s) for s in snapshot_mod.list_snapshots(store, directory=args.dir)]
    if args.action == "verify":
        if args.path:
            return {"path": args.path, **snapshot_mod.inspect(args.path)}
        snapshots = snapshot_mod.list_snapshots(store, directory=args.dir)
        if not snapshots:
            return {"checked": 0, "snapshots": []}
        return {
            "checked": len(snapshots),
            "snapshots": [
                {"id": s.id, "path": s.path, **snapshot_mod.verify(s)} for s in snapshots
            ],
        }
    raise ValueError(f"unknown snapshot action {args.action!r}")


def _snapshot_dict(snap) -> dict[str, Any]:
    return {
        "id": snap.id,
        "created_at": snap.created_at,
        "source": snap.source,
        "schema_version": snap.schema_version,
        "path": snap.path,
        "size_bytes": snap.size_bytes,
        "integrity": snap.integrity,
        "foreign_key_check": snap.foreign_key_check,
        "result": snap.result,
        "valid": snap.is_valid,
        "reason": snap.reason,
        "rows": snap.tables,
    }


def _cmd_events(store, args) -> Any:
    if args.target:
        return event_mod.for_target(store, args.target)
    return event_mod.recent(store, limit=args.limit, kind=args.kind)


# -- rendering -------------------------------------------------------------

def _render(command: str, result: Any) -> None:
    if command == "doctor":
        _render_doctor(result)
    elif command == "boot":
        _render_boot(result)
    elif isinstance(result, list):
        if not result:
            print("(nothing)")
        for item in result:
            print(_line(item))
    elif isinstance(result, dict):
        for key, value in result.items():
            print(f"{key}: {_short(value)}")
    else:
        print(result)


def _render_doctor(report: dict[str, Any]) -> None:
    health = report["health"]
    print(f"JARVIS self-diagnostic   store={report['store_path']}  schema=v{report['schema_version']}")
    print(f"health: {health['status'].upper()}")
    for check in health["checks"]:
        print(f"  [{_STATUS_MARK.get(check['status'], check['status'])}] {check['name']}: {check['detail']}")
    print(f"\nimplemented ({len(report['implemented'])}): {', '.join(report['implemented'])}")
    print("not implemented:")
    for name, why in report["not_implemented"].items():
        print(f"  - {name}: {why}")
    tasks = report["metrics"]["tasks"]
    print(
        f"\ntasks: {tasks['total']} total, verified success rate "
        f"{_pct(tasks['verified_success_rate'])}, failure rate {_pct(tasks['failure_rate'])}"
    )
    mem = report["metrics"]["memory"]
    print(
        f"memory: {mem['live']} live, {_pct(mem['utilization'])} ever retrieved, "
        f"{mem['stale']} stale, {mem['open_conflicts']} unresolved conflicts"
    )
    recovery = next(
        (c for c in health["checks"] if c["name"] == "recovery_point"), None
    )
    if recovery:
        print(f"\nrecovery point: [{_STATUS_MARK.get(recovery['status'], recovery['status'])}] "
              f"{recovery['detail']}")
    if report["open_tasks"]:
        print(f"\nopen tasks ({len(report['open_tasks'])}):")
        for t in report["open_tasks"][:10]:
            unmet = f"  blocked on {len(t['unmet_dependencies'])}" if t["unmet_dependencies"] else ""
            print(f"  {t['id']}  {t['status']:<12} {t['title']}{unmet}")


def _render_boot(b: dict[str, Any]) -> None:
    print(f"session {b['session_id']} started {b['started_at']}")
    if b["previous_session"]:
        print(f"previous: {b['previous_session']['summary'] or '(no summary)'}")
    print(f"health: {b['health']['status'].upper()}")
    for label, key in (
        ("open objectives", "open_objectives"),
        ("ready tasks", "ready_tasks"),
        ("blocked tasks", "blocked_tasks"),
        ("recent failures", "recent_failures"),
        ("completed but unverified", "unverified_completions"),
        ("stale memories", "stale_memories"),
        ("relevant memories", "relevant_memories"),
    ):
        items = b.get(key) or []
        if items:
            print(f"\n{label} ({len(items)}):")
            for item in items:
                print(f"  {_line(item)}")
    if b["changed_since_last_session"]:
        print(f"\nchanged since last session: {len(b['changed_since_last_session'])} event(s)")


def _line(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    ident = item.get("id", "")
    label = item.get("title") or item.get("note") or item.get("kind") or ""
    status = item.get("status") or item.get("decision") or ""
    extra = f" [{status}]" if status else ""
    return f"{ident}  {label}{extra}".strip()


def _short(value: Any) -> str:
    text = json.dumps(value, default=str) if isinstance(value, (dict, list)) else str(value)
    return text if len(text) <= 300 else text[:297] + "..."


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _objective_dict(obj) -> dict[str, Any]:
    return {
        "id": obj.id,
        "title": obj.title,
        "desired_outcome": obj.desired_outcome,
        "priority": obj.priority,
        "status": obj.status,
        "ready": obj.is_ready,
        "gaps": obj.readiness_gaps(),
        "open_questions": obj.open_questions,
        "assumptions": obj.assumptions,
        "verification": obj.verification,
        "project": obj.project,
    }


def _task_dict(store, task) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "priority": task.priority,
        "objective_id": task.objective_id,
        "project": task.project,
        "expected": task.expected,
        "actual": task.actual,
        "failure_class": task.failure_class,
        "depends_on": task.depends_on,
        "unmet_dependencies": task_mod.unmet_dependencies(store, task.id),
    }


# -- parser ----------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    # Global flags live on a parent parser so they are accepted both before and
    # after the subcommand. SUPPRESS keeps a subparser from overwriting a value
    # that was already given at the top level.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS,
                        help="database path (default: $JARVIS_HOME/jarvis.db)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="emit JSON")
    common.add_argument("--actor", default=argparse.SUPPRESS,
                        help="who is performing this action")
    common.add_argument("--session", default=argparse.SUPPRESS,
                        help="session id to attribute events to")

    p = argparse.ArgumentParser(
        prog="jarvis", description="JARVIS foundation layer", parents=[common]
    )
    # Only `func` may use set_defaults here. argparse's set_defaults rewrites
    # the default of any *matching action*, and parents=[] shares the very same
    # action objects with every subparser - so set_defaults(db=None) silently
    # turned the SUPPRESS default into None, and the subparser then wrote that
    # None back over a --db given before the subcommand. Defaults for the
    # global flags are applied after parsing instead, in main().
    p.set_defaults(func=None)
    sub = p.add_subparsers(dest="command")

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_, parents=[common])

    boot = add("boot", "start a session and print the briefing")
    boot.add_argument("--project")
    boot.add_argument("--focus", help="what this session is about; drives memory retrieval")
    boot.set_defaults(func=_cmd_boot)

    close = add("close", "close a session and report what happened")
    close.add_argument("session_id", help="session id returned by boot")
    close.add_argument("--summary", required=True)
    close.set_defaults(func=_cmd_close)

    doctor = add("doctor", "self-diagnostic report")
    doctor.set_defaults(func=_cmd_doctor)

    metrics = add("metrics", "performance metrics")
    metrics.add_argument("--days", type=int, default=None)
    metrics.set_defaults(func=_cmd_metrics)

    obj = add("objective", "objectives")
    obj.add_argument("action", choices=["add", "list", "resolve"])
    obj.add_argument("--id")
    obj.add_argument("--title", default="")
    obj.add_argument("--outcome", default="")
    obj.add_argument("--priority", type=int, default=3)
    obj.add_argument("--project")
    obj.add_argument("--verify", action="append", help="verification criterion (repeatable)")
    obj.add_argument("--question", action="append", help="open question (repeatable)")
    obj.add_argument("--assume", action="append", help="stated assumption (repeatable)")
    obj.add_argument("--question-text", dest="question_text", default="")
    obj.add_argument("--answer", default="")
    obj.set_defaults(func=_cmd_objective)

    task = add("task", "tasks")
    task.add_argument(
        "action",
        choices=["add", "list", "show", "start", "block", "wait", "complete", "fail", "verify", "archive"],
    )
    task.add_argument("--id")
    task.add_argument("--title", default="")
    task.add_argument("--objective")
    task.add_argument("--priority", type=int, default=3)
    task.add_argument("--project")
    task.add_argument("--expected")
    task.add_argument("--actual")
    task.add_argument("--note")
    task.add_argument("--depends-on", dest="depends_on", action="append")
    task.add_argument("--status")
    task.add_argument("--open", action="store_true", help="only open tasks")
    task.add_argument("--ready", action="store_true", help="only actionable tasks")
    task.add_argument("--failure-class", dest="failure_class", choices=list(task_mod.FAILURE_CLASSES))
    task.add_argument("--method", default="")
    task.add_argument("--evidence", default="")
    task.add_argument("--reject", action="store_true", help="record a failed verification")
    task.set_defaults(func=_cmd_task)

    mem = add("memory", "memory")
    mem.add_argument("action", choices=["assess", "add", "search", "stale", "conflicts"])
    mem.add_argument("--kind", choices=list(mem_mod.KINDS), default="semantic")
    mem.add_argument("--kind-filter", dest="kind_filter", action="append", choices=list(mem_mod.KINDS))
    mem.add_argument("--title", default="")
    mem.add_argument("--body", default="")
    mem.add_argument("--query", default="")
    mem.add_argument("--source")
    mem.add_argument("--source-class", dest="source_class", choices=list(mem_mod.SOURCE_CLASSES), default="unknown")
    mem.add_argument("--confidence", type=float, default=0.5)
    mem.add_argument("--durability", type=float, default=0.5)
    mem.add_argument("--utility", type=float, default=0.5)
    mem.add_argument("--decay", type=float, default=0.3)
    mem.add_argument("--project")
    mem.add_argument("--entity", action="append")
    mem.add_argument("--tag", action="append")
    mem.add_argument("--limit", type=int, default=8)
    mem.add_argument("--force", action="store_true", help="store despite an adverse assessment")
    mem.set_defaults(func=_cmd_memory)

    snap = add("snapshot", "pre-migration snapshots")
    snap.add_argument("action", choices=["create", "list", "verify"])
    snap.add_argument("--reason", help="why this recovery point is being taken")
    snap.add_argument("--label", help="short tag added to the filename")
    snap.add_argument("--dir", help="snapshot directory (default: snapshots/ beside the db)")
    snap.add_argument("--path", help="verify one snapshot file by path")
    snap.set_defaults(func=_cmd_snapshot)

    ev = add("events", "audit log")
    ev.add_argument("--limit", type=int, default=25)
    ev.add_argument("--kind")
    ev.add_argument("--target", help="full history for one entity")
    ev.set_defaults(func=_cmd_events)

    return p


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
