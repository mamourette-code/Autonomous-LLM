"""Pre-migration snapshots: a safe recovery point before the schema changes.

Task 1's acceptance audit recorded one gap that becomes real the moment a
migration transforms existing rows: migrations are forward-only, so a bad one
could only be undone by restoring the database file by hand - and no such file
was being kept. This module keeps one.

It is deliberately NOT a rollback system. It creates a verified copy and tells
you where it is; putting that copy back is a documented manual step (see
docs/SNAPSHOTS.md). Automating restore would mean deciding, in code, that the
live database should be discarded - a decision that belongs to the user.

Two design choices worth stating:

*The copy is made with SQLite's online backup API*, never a file copy. A live
SQLite database has state in the WAL that a `cp` will not necessarily capture
in a consistent order, so a copied file can be torn or stale. `backup()` takes
a consistent point-in-time image even while the database is being written.

*Metadata lives in a JSON sidecar next to the snapshot*, not in a table. A
snapshot must be usable independently of the database it came from, so its
description has to travel with it. It also means recording a snapshot does not
itself require a schema migration, which would be an awkward thing to need
here. Diagnostics therefore reports what is actually on disk rather than what
the database claims exists.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jarvis import events
from jarvis.store import Store, new_id, utcnow

SNAPSHOT_DIRNAME = "snapshots"

# Every table the schema defines. A snapshot that is missing one of these is
# not a recovery point, whatever its integrity check says.
EXPECTED_TABLES = (
    "objectives",
    "tasks",
    "task_deps",
    "verifications",
    "memories",
    "memory_conflicts",
    "events",
    "sessions",
)


class SnapshotError(RuntimeError):
    """Raised when a snapshot cannot be created or does not verify."""


@dataclass
class Snapshot:
    """The record of one snapshot, mirrored in its JSON sidecar."""

    id: str
    created_at: str
    source: str
    schema_version: int
    path: str
    size_bytes: int
    integrity: str
    foreign_key_check: str
    result: str
    actor: str
    reason: str = ""
    tables: dict[str, int] = field(default_factory=dict)
    git_sha: str | None = None
    git_dirty: bool | None = None

    @property
    def is_valid(self) -> bool:
        """Recorded as good and still on disk.

        A cheap pre-filter, NOT proof: it reads the metadata written at
        creation time, so a file corrupted afterwards still looks valid here.
        Anything that *reports* a recovery point must call verify() as well -
        claiming a corrupt snapshot is a recovery point is exactly the kind of
        unverified claim this system is supposed to refuse to make.
        """
        return (
            self.result == "ok"
            and self.integrity == "ok"
            and self.foreign_key_check == "ok"
            and os.path.exists(self.path)
        )

    @property
    def sidecar(self) -> str:
        return f"{self.path}.json"


def _stamp() -> str:
    """Filename timestamp. A seam: tests patch this to force a name collision."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _run_git(args: list[str], cwd: str) -> str | None:
    """Runs one git command, returning stripped stdout or None on any failure.

    Best-effort only, by design: git being missing, the directory not being a
    checkout, or a permission error must never stop a snapshot from being
    taken - the code version is a nice-to-have annotation, not a precondition.
    """
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_state(repo_dir: str | os.PathLike | None = None) -> tuple[str | None, bool | None]:
    """(git_sha, git_dirty) for the given checkout - the jarvis package's own
    by default, since that is the code a snapshot was actually taken by.

    Both are None when git or a repo isn't available ("when available" is the
    contract). `git_dirty` exists because a SHA alone understates the risk: a
    snapshot taken with uncommitted changes was not produced by exactly that
    commit, and reporting the SHA without that flag would silently overstate
    how exactly the code version is known.
    """
    directory = str(repo_dir) if repo_dir is not None else str(Path(__file__).resolve().parent.parent)
    sha = _run_git(["rev-parse", "HEAD"], directory)
    if not sha:
        return None, None
    status = _run_git(["status", "--porcelain"], directory)
    dirty = None if status is None else bool(status)
    return sha, dirty


def default_directory(store: Store) -> Path:
    """Where snapshots for this store live: a `snapshots/` dir beside the db."""
    if store.path == ":memory:":
        raise SnapshotError(
            "an in-memory store has no directory; pass an explicit `directory`"
        )
    return Path(store.path).resolve().parent / SNAPSHOT_DIRNAME


def create(
    store: Store,
    *,
    actor: str = "claude",
    reason: str = "",
    directory: str | os.PathLike | None = None,
    label: str | None = None,
    session_id: str | None = None,
) -> Snapshot:
    """Take a verified snapshot of `store` and return its record.

    Raises SnapshotError if the target already exists, if the copy fails, or
    if the copy does not verify - never returns a snapshot it could not check.
    """
    target_dir = Path(directory) if directory is not None else default_directory(store)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        events.record(
            store,
            "snapshot.create",
            actor,
            outcome="failed",
            permission="create",
            session_id=session_id,
            target=str(target_dir),
            error=f"{type(exc).__name__}: {exc}",
        )
        raise SnapshotError(f"snapshot directory {target_dir} is not usable: {exc}") from exc

    snapshot_id = new_id("snp")
    version = store.schema_version()
    stamp = _stamp()
    name = f"jarvis-v{version}-{stamp}-{snapshot_id}"
    if label:
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:40]
        name = f"{name}-{safe}"
    path = target_dir / f"{name}.db"

    # Requirement: never silently overwrite. Ids make collisions vanishingly
    # unlikely, but an explicit refusal beats a silent one.
    if path.exists() or Path(f"{path}.json").exists():
        raise SnapshotError(f"refusing to overwrite an existing snapshot at {path}")

    source = os.path.abspath(store.path) if store.path != ":memory:" else ":memory:"
    try:
        destination = sqlite3.connect(str(path))
        try:
            # The online backup API, not a file copy: this is consistent even
            # while the source is being written to.
            store.conn.backup(destination)
            # The copy inherits the source's WAL mode, which would leave -wal
            # and -shm files beside it. Checkpoint into a single self-contained
            # file so the snapshot can be moved or archived on its own - a
            # recovery point split across three files is a recovery point
            # someone will eventually copy incompletely.
            destination.execute("PRAGMA journal_mode = DELETE")
        finally:
            destination.close()
    except (sqlite3.Error, OSError) as exc:
        _cleanup(path)
        events.record(
            store,
            "snapshot.create",
            actor,
            outcome="failed",
            permission="create",
            session_id=session_id,
            snapshot_id=snapshot_id,
            target=str(path),
            error=f"{type(exc).__name__}: {exc}",
        )
        raise SnapshotError(f"could not write snapshot to {path}: {exc}") from exc

    checks = inspect(str(path))
    git_sha, git_dirty = _git_state()
    snapshot = Snapshot(
        id=snapshot_id,
        created_at=utcnow(),
        source=source,
        schema_version=version,
        path=str(path),
        size_bytes=path.stat().st_size,
        integrity=checks["integrity"],
        foreign_key_check=checks["foreign_key_check"],
        result="ok" if checks["ok"] else "failed",
        actor=actor,
        reason=reason,
        tables=checks["tables"],
        git_sha=git_sha,
        git_dirty=git_dirty,
    )
    _write_sidecar(snapshot)

    events.record(
        store,
        "snapshot.create",
        actor,
        outcome="ok" if snapshot.result == "ok" else "failed",
        permission="create",
        target=snapshot.id,
        session_id=session_id,
        path=snapshot.path,
        schema_version=version,
        size_bytes=snapshot.size_bytes,
        integrity=snapshot.integrity,
        rows=sum(snapshot.tables.values()),
        reason=reason,
        git_sha=git_sha,
        git_dirty=git_dirty,
    )
    if snapshot.result != "ok":
        raise SnapshotError(
            f"snapshot written to {path} did not verify "
            f"(integrity={snapshot.integrity}, foreign_key_check={snapshot.foreign_key_check}); "
            "the file and its metadata were kept so the failure is inspectable"
        )
    return snapshot


def inspect(path: str) -> dict[str, Any]:
    """Verify a snapshot file on its own terms, opening it independently.

    Deliberately opens a fresh connection to the snapshot alone: a check that
    leaned on the source database would not prove the snapshot stands by itself.
    """
    result: dict[str, Any] = {
        "integrity": "unreadable",
        "foreign_key_check": "unreadable",
        "schema_version": None,
        "tables": {},
        "ok": False,
    }
    if not os.path.exists(path):
        result["integrity"] = "missing"
        return result
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        result["integrity"] = f"unreadable: {exc}"
        return result
    try:
        result["integrity"] = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        result["foreign_key_check"] = "ok" if not violations else f"{len(violations)} violation(s)"
        result["schema_version"] = int(conn.execute("PRAGMA user_version").fetchone()[0])
        present = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = [t for t in EXPECTED_TABLES if t not in present]
        if missing:
            result["integrity"] = f"incomplete: missing table(s) {', '.join(missing)}"
        else:
            for table in EXPECTED_TABLES:
                result["tables"][table] = int(
                    conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
    except sqlite3.DatabaseError as exc:
        result["integrity"] = f"unreadable: {exc}"
        return result
    finally:
        conn.close()
    result["ok"] = result["integrity"] == "ok" and result["foreign_key_check"] == "ok"
    return result


def verify(snapshot: Snapshot | str) -> dict[str, Any]:
    """Re-verify an existing snapshot, by record or by path."""
    return inspect(snapshot.path if isinstance(snapshot, Snapshot) else snapshot)


def load(sidecar_path: str | os.PathLike) -> Snapshot | None:
    """Read one snapshot record from its sidecar, tolerating a damaged file."""
    try:
        with open(sidecar_path) as handle:
            data = json.load(handle)
        return Snapshot(**{k: v for k, v in data.items() if k in Snapshot.__annotations__})
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def list_snapshots(
    store: Store | None = None, directory: str | os.PathLike | None = None
) -> list[Snapshot]:
    """Every snapshot on disk, newest first - valid or not.

    Reads the directory rather than a table, so what is reported is what
    actually exists.
    """
    if directory is None:
        if store is None:
            raise ValueError("pass either a store or a directory")
        try:
            directory = default_directory(store)
        except SnapshotError:
            return []
    target = Path(directory)
    if not target.is_dir():
        return []
    found = [s for s in (load(p) for p in sorted(target.glob("*.db.json"))) if s]
    return sorted(found, key=lambda s: s.created_at, reverse=True)


def valid_snapshots(
    store: Store | None = None, directory: str | os.PathLike | None = None
) -> list[Snapshot]:
    """Snapshots that verify right now, re-checked against the files on disk."""
    return [
        snap
        for snap in list_snapshots(store, directory)
        if snap.is_valid and verify(snap)["ok"]
    ]


def latest_valid(
    store: Store | None = None,
    directory: str | os.PathLike | None = None,
    schema_version: int | None = None,
) -> Snapshot | None:
    """Newest snapshot that still verifies, optionally for a given schema version."""
    for snapshot in list_snapshots(store, directory):
        if schema_version is not None and snapshot.schema_version != schema_version:
            continue
        if snapshot.is_valid and verify(snapshot)["ok"]:
            return snapshot
    return None


def _write_sidecar(snapshot: Snapshot) -> None:
    with open(snapshot.sidecar, "w") as handle:
        json.dump(asdict(snapshot), handle, indent=2, sort_keys=True)


def _cleanup(path: Path) -> None:
    """Remove a half-written snapshot so it cannot be mistaken for a good one."""
    for candidate in (path, Path(f"{path}.json")):
        try:
            candidate.unlink()
        except OSError:
            pass
