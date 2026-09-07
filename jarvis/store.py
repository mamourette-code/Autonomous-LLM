"""Persistent state: SQLite storage, schema and migrations.

Everything durable in JARVIS lives here. A single connection object is passed
explicitly to the subsystems; no module-level global state, so tests and
experiments can run against isolated databases (protocol s25: experiments must
not corrupt the stable system).
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1


def utcnow() -> str:
    """Current time as an ISO-8601 UTC string, second resolution preserved."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def new_id(prefix: str) -> str:
    """Short, sortable-enough, human-quotable identifier."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def default_home() -> Path:
    """Root directory for JARVIS state.

    Overridable with JARVIS_HOME so a session can run against a scratch
    database without touching the stable one.
    """
    env = os.environ.get("JARVIS_HOME")
    if env:
        return Path(env).expanduser()
    return Path.cwd() / ".jarvis"


# Migrations are applied in order. Each entry is (version, sql). Never edit a
# migration that has shipped; add a new one (protocol s26: rollback needs a
# truthful history of what the schema was).
_MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE objectives (
            id              TEXT PRIMARY KEY,
            title           TEXT NOT NULL,
            desired_outcome TEXT NOT NULL,
            constraints     TEXT NOT NULL DEFAULT '[]',
            priority        INTEGER NOT NULL DEFAULT 3,
            deadline        TEXT,
            assumptions     TEXT NOT NULL DEFAULT '[]',
            open_questions  TEXT NOT NULL DEFAULT '[]',
            verification    TEXT NOT NULL DEFAULT '[]',
            risks           TEXT NOT NULL DEFAULT '[]',
            authority       TEXT NOT NULL DEFAULT 'user',
            project         TEXT,
            status          TEXT NOT NULL DEFAULT 'open',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );

        CREATE TABLE tasks (
            id            TEXT PRIMARY KEY,
            objective_id  TEXT REFERENCES objectives(id),
            title         TEXT NOT NULL,
            status        TEXT NOT NULL,
            priority      INTEGER NOT NULL DEFAULT 3,
            parent_id     TEXT REFERENCES tasks(id),
            assignee      TEXT,
            expected      TEXT,
            actual        TEXT,
            failure_class TEXT,
            failure_note  TEXT,
            project       TEXT,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            started_at    TEXT,
            completed_at  TEXT,
            verified_at   TEXT
        );
        CREATE INDEX idx_tasks_status ON tasks(status);
        CREATE INDEX idx_tasks_objective ON tasks(objective_id);

        CREATE TABLE task_deps (
            task_id    TEXT NOT NULL REFERENCES tasks(id),
            depends_on TEXT NOT NULL REFERENCES tasks(id),
            PRIMARY KEY (task_id, depends_on)
        );

        CREATE TABLE verifications (
            id           TEXT PRIMARY KEY,
            task_id      TEXT NOT NULL REFERENCES tasks(id),
            method       TEXT NOT NULL,
            evidence     TEXT NOT NULL,
            verifier     TEXT NOT NULL,
            completer    TEXT,
            independent  INTEGER NOT NULL,
            passed       INTEGER NOT NULL,
            created_at   TEXT NOT NULL
        );
        CREATE INDEX idx_verifications_task ON verifications(task_id);

        CREATE TABLE memories (
            id           TEXT PRIMARY KEY,
            kind         TEXT NOT NULL,
            title        TEXT NOT NULL,
            body         TEXT NOT NULL,
            payload      TEXT NOT NULL DEFAULT '{}',
            source       TEXT,
            source_class TEXT NOT NULL DEFAULT 'unknown',
            confidence   REAL NOT NULL DEFAULT 0.5,
            project      TEXT,
            entities     TEXT NOT NULL DEFAULT '[]',
            tags         TEXT NOT NULL DEFAULT '[]',
            review_after TEXT,
            superseded_by TEXT REFERENCES memories(id),
            archived     INTEGER NOT NULL DEFAULT 0,
            access_count INTEGER NOT NULL DEFAULT 0,
            last_used_at TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );
        CREATE INDEX idx_memories_kind ON memories(kind);
        CREATE INDEX idx_memories_project ON memories(project);

        CREATE TABLE memory_conflicts (
            id          TEXT PRIMARY KEY,
            left_id     TEXT NOT NULL REFERENCES memories(id),
            right_id    TEXT NOT NULL REFERENCES memories(id),
            detected_by TEXT NOT NULL,
            note        TEXT NOT NULL DEFAULT '',
            resolution  TEXT NOT NULL DEFAULT 'unresolved',
            created_at  TEXT NOT NULL
        );

        CREATE TABLE events (
            id           TEXT PRIMARY KEY,
            ts           TEXT NOT NULL,
            kind         TEXT NOT NULL,
            actor        TEXT NOT NULL,
            target       TEXT,
            outcome      TEXT NOT NULL,
            permission   TEXT,
            authorized_by TEXT,
            duration_ms  INTEGER,
            session_id   TEXT,
            detail       TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX idx_events_ts ON events(ts);
        CREATE INDEX idx_events_kind ON events(kind);
        CREATE INDEX idx_events_session ON events(session_id);

        CREATE TABLE sessions (
            id         TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            ended_at   TEXT,
            summary    TEXT,
            note       TEXT
        );
        """,
    ),
]


def _statements(script: str) -> Iterator[str]:
    """Split a SQL script into complete statements.

    Uses sqlite3.complete_statement rather than a naive split on ";" so a
    semicolon inside a string literal cannot cut a statement in half.
    """
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                yield statement
            buffer = ""
    tail = buffer.strip()
    if tail:
        yield tail


class Store:
    """A thin wrapper over a SQLite connection with schema management."""

    def __init__(self, conn: sqlite3.Connection, path: str) -> None:
        self.conn = conn
        self.path = path
        self._depth = 0

    # -- transactions ------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator["Store"]:
        """All-or-nothing boundary for a multi-statement operation.

        Without this, a write that fails partway leaves rows behind that the
        next unrelated commit silently makes permanent - which is how a task
        can end up in the database with no audit event, the exact condition
        the observability health check exists to detect.

        Reentrant: an inner block joins the outer transaction rather than
        committing early, so `record an event` nested inside `change state`
        cannot commit half of the pair.
        """
        self._depth += 1
        try:
            yield self
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.conn.rollback()
            raise
        self._depth -= 1
        if self._depth == 0:
            self.conn.commit()

    @property
    def in_transaction(self) -> bool:
        return self._depth > 0

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- schema ------------------------------------------------------------
    def schema_version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> int:
        """Apply outstanding migrations. Returns the resulting version.

        Serialised with BEGIN IMMEDIATE: two processes opening a fresh database
        at the same time would otherwise both read version 0 and both run the
        schema script, and the loser died with "table already exists" - taking
        every write it had queued with it.

        Statements are executed one at a time rather than with executescript(),
        because executescript() issues an implicit COMMIT that would drop the
        very lock this relies on.
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            current = self.schema_version()
            for version, sql in _MIGRATIONS:
                if version > current:
                    for statement in _statements(sql):
                        self.conn.execute(statement)
                    self.conn.execute(f"PRAGMA user_version = {version}")
                    current = version
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return current

    # -- convenience -------------------------------------------------------
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def commit(self) -> None:
        """Commit, unless a transaction block is in charge of that decision."""
        if self._depth == 0:
            self.conn.commit()


def open_store(path: str | os.PathLike | None = None) -> Store:
    """Open (creating if needed) the JARVIS database and apply migrations.

    Pass ':memory:' for an ephemeral store.
    """
    if path is None:
        home = default_home()
        home.mkdir(parents=True, exist_ok=True)
        path = home / "jarvis.db"
    path = str(path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL" if path != ":memory:" else "PRAGMA journal_mode = MEMORY")
    conn.execute("PRAGMA busy_timeout = 5000")
    store = Store(conn, path)
    store.migrate()
    return store
