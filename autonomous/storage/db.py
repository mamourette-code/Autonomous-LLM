"""SQLite persistence for runs, steps and watcher observations.

Single-user, local-first: one connection guarded by a lock is plenty, and it
keeps the storage layer dependency-free. Calls are wrapped in ``asyncio.to_thread``
by the callers that need to stay off the event loop.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    goal        TEXT    NOT NULL,
    status      TEXT    NOT NULL,           -- running | succeeded | failed
    provider    TEXT    NOT NULL,
    model       TEXT    NOT NULL,
    result      TEXT,
    error       TEXT,
    created_at  TEXT    NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS steps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,
    kind       TEXT    NOT NULL,            -- thought | tool_call | tool_result | answer
    name       TEXT,
    content    TEXT,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS steps_run_idx ON steps(run_id, idx);

CREATE TABLE IF NOT EXISTS observations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,               -- watcher name
    key        TEXT NOT NULL,               -- stable id, used to de-duplicate
    title      TEXT NOT NULL,
    body       TEXT,
    url        TEXT,
    data       TEXT,                        -- JSON blob of structured extras
    created_at TEXT NOT NULL,
    UNIQUE(source, key)
);
CREATE INDEX IF NOT EXISTS observations_created ON observations(created_at DESC);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- runs --------------------------------------------------------------

    def create_run(self, goal: str, provider: str, model: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO runs (goal, status, provider, model, created_at)"
                " VALUES (?, 'running', ?, ?, ?)",
                (goal, provider, model, _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def finish_run(
        self, run_id: int, *, status: str, result: str | None = None, error: str | None = None
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status = ?, result = ?, error = ?, finished_at = ? WHERE id = ?",
                (status, result, error, _now(), run_id),
            )
            self._conn.commit()

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # --- steps -------------------------------------------------------------

    def add_step(
        self, run_id: int, idx: int, kind: str, *, name: str | None = None, content: str = ""
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO steps (run_id, idx, kind, name, content, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, idx, kind, name, content, _now()),
            )
            self._conn.commit()

    def list_steps(self, run_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM steps WHERE run_id = ? ORDER BY idx, id", (run_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # --- observations ------------------------------------------------------

    def add_observations(self, items: Iterable[dict[str, Any]]) -> int:
        """Insert observations, ignoring ones already seen. Returns the new count."""
        rows = [
            (
                item["source"],
                item["key"],
                item["title"],
                item.get("body"),
                item.get("url"),
                json.dumps(item.get("data")) if item.get("data") is not None else None,
                item.get("created_at") or _now(),
            )
            for item in items
        ]
        if not rows:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO observations"
                " (source, key, title, body, url, data, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()
            return self._conn.total_changes - before

    def list_observations(
        self, limit: int = 100, source: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM observations"
        params: list[Any] = []
        if source:
            sql += " WHERE source = ?"
            params.append(source)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["data"] = json.loads(item["data"]) if item["data"] else None
            out.append(item)
        return out
