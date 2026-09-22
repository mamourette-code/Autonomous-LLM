"""Pre-migration snapshot tests.

The snapshot exists to be a recovery point before a migration transforms data,
so these tests care about one thing above all: that the file left on disk is a
complete, consistent, independently usable database - not merely that a
function returned without raising.
"""

import io
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

from jarvis import diagnostics, events, memory, objectives, snapshot, tasks
from jarvis.cli import main
from jarvis.store import SCHEMA_VERSION, open_store


def populate(store, tasks_wanted=6):
    """A store with rows in every table a snapshot must carry."""
    obj = objectives.create(
        store, "Snapshot subject", "a populated database", actor="claude",
        verification=["rows survive"],
    )
    first = tasks.create(store, "first", actor="claude", objective_id=obj.id)
    tasks.transition(store, first.id, tasks.IN_PROGRESS, actor="claude")
    tasks.transition(store, first.id, tasks.COMPLETED, actor="claude")
    tasks.verify(store, first.id, method="rerun", evidence="matches", verifier="critic")
    for i in range(tasks_wanted - 1):
        tasks.create(store, f"task {i}", actor="claude", depends_on=[first.id])
    memory.remember(
        store, kind="semantic", title="Snapshot policy",
        body="Take a verified snapshot before any migration",
        source="docs/SNAPSHOTS.md", source_class="sourced_knowledge", confidence=0.9,
    )
    return obj, first


class SnapshotCreationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = open_store(os.path.join(self.tmp.name, "jarvis.db"))
        self.addCleanup(self.store.close)

    def test_snapshot_is_created_and_reported_valid(self):
        snap = snapshot.create(self.store, actor="claude", reason="before migration")
        self.assertEqual(snap.result, "ok")
        self.assertTrue(snap.is_valid)
        self.assertTrue(os.path.exists(snap.path))

    def test_snapshot_passes_sqlite_integrity_checks(self):
        snap = snapshot.create(self.store)
        self.assertEqual(snap.integrity, "ok")
        self.assertEqual(snap.foreign_key_check, "ok")
        # Re-verified independently, not trusting the value recorded at creation.
        self.assertTrue(snapshot.verify(snap)["ok"])

    def test_snapshot_is_a_single_self_contained_file(self):
        """A recovery point split across .db/-wal/-shm is one someone will
        eventually copy incompletely."""
        snap = snapshot.create(self.store)
        directory = os.path.dirname(snap.path)
        strays = [f for f in os.listdir(directory) if f.endswith(("-wal", "-shm"))]
        self.assertEqual(strays, [])

    def test_default_location_is_beside_the_database(self):
        snap = snapshot.create(self.store)
        self.assertEqual(
            os.path.dirname(snap.path),
            str(snapshot.default_directory(self.store)),
        )

    def test_creation_is_audited(self):
        snap = snapshot.create(self.store, actor="claude", reason="pre-task2")
        event = events.recent(self.store)[0]
        self.assertEqual(event["kind"], "snapshot.create")
        self.assertEqual(event["outcome"], "ok")
        self.assertEqual(event["target"], snap.id)
        self.assertEqual(event["detail"]["schema_version"], SCHEMA_VERSION)
        self.assertEqual(event["detail"]["reason"], "pre-task2")


class PopulatedSnapshotTest(unittest.TestCase):
    """The snapshot must carry the whole database, not an empty shell."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = open_store(os.path.join(self.tmp.name, "jarvis.db"))
        populate(self.store)

    def test_every_table_matches_the_source_row_for_row(self):
        """The copy carries every row. The one documented difference is the
        events table: the audit record of the snapshot is written after the
        copy is taken, so the snapshot cannot contain the event announcing
        itself - it holds the state as it was immediately before."""
        before = {
            table: self.store.one(f"SELECT COUNT(*) FROM {table}")[0]
            for table in snapshot.EXPECTED_TABLES
        }
        snap = snapshot.create(self.store)
        copy = sqlite3.connect(snap.path)
        try:
            for table in snapshot.EXPECTED_TABLES:
                copied = copy.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                self.assertEqual(copied, before[table], f"table {table} did not match")
                self.assertEqual(snap.tables[table], copied)
        finally:
            copy.close()
        self.assertEqual(
            self.store.one("SELECT COUNT(*) FROM events")[0], before["events"] + 1
        )

    def test_snapshot_preserves_verification_and_audit_history(self):
        """Restoring must bring back the evidence, not just the state."""
        snap = snapshot.create(self.store)
        copy = sqlite3.connect(snap.path)
        try:
            verified = copy.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'verified'"
            ).fetchone()[0]
            independent = copy.execute(
                "SELECT independent FROM verifications LIMIT 1"
            ).fetchone()[0]
            self.assertEqual(verified, 1)
            self.assertEqual(independent, 1)
            self.assertGreater(
                copy.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0
            )
        finally:
            copy.close()

    def test_snapshot_is_a_point_in_time_and_does_not_follow_the_source(self):
        snap = snapshot.create(self.store)
        before = snap.tables["tasks"]
        tasks.create(self.store, "added after the snapshot", actor="claude")
        copy = sqlite3.connect(snap.path)
        try:
            self.assertEqual(
                copy.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], before
            )
        finally:
            copy.close()
        self.assertEqual(
            self.store.one("SELECT COUNT(*) FROM tasks")[0], before + 1
        )

    def test_snapshot_is_usable_independently_of_the_source(self):
        """Requirement: usable to reconstruct the pre-migration state. The file
        is moved away from its origin and opened with no reference to it."""
        snap = snapshot.create(self.store)
        moved = os.path.join(self.tmp.name, "moved-elsewhere.db")
        os.rename(snap.path, moved)
        restored = open_store(moved)
        self.assertEqual(restored.schema_version(), SCHEMA_VERSION)
        self.assertEqual(
            restored.one("SELECT COUNT(*) FROM tasks")[0], snap.tables["tasks"]
        )
        # And it is a working store, not just readable bytes.
        self.assertEqual(len(tasks.list_tasks(restored, open_only=True)), 5)
        self.assertEqual(diagnostics.health(restored)["status"] in ("ok", "warn"), True)


class RecoveryTest(unittest.TestCase):
    """The documented recovery procedure, executed. Requirement: the snapshot
    must be independently usable to reconstruct the pre-migration state."""

    def test_restoring_after_a_destructive_migration_recovers_the_state(self):
        import shutil

        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "jarvis.db")
            store = open_store(db)
            obj, _ = populate(store)
            before = {
                table: store.one(f"SELECT COUNT(*) FROM {table}")[0]
                for table in snapshot.EXPECTED_TABLES
            }
            snap = snapshot.create(store, reason="before destructive migration")
            store.close()

            # A migration goes wrong: it destroys rows and bumps the version.
            broken = sqlite3.connect(db)
            broken.execute("DELETE FROM tasks")
            broken.execute("DELETE FROM memories")
            broken.execute("PRAGMA user_version = 2")
            broken.commit()
            broken.close()
            damaged = open_store(db)
            self.assertEqual(damaged.one("SELECT COUNT(*) FROM tasks")[0], 0)
            self.assertEqual(damaged.schema_version(), 2)
            damaged.close()

            # The documented procedure: verify, preserve, replace.
            self.assertTrue(snapshot.inspect(snap.path)["ok"])
            os.rename(db, db + ".failed-migration")
            for suffix in ("-wal", "-shm"):
                if os.path.exists(db + suffix):
                    os.unlink(db + suffix)
            shutil.copy(snap.path, db)

            restored = open_store(db)
            self.assertEqual(restored.schema_version(), SCHEMA_VERSION)
            for table in snapshot.EXPECTED_TABLES:
                self.assertEqual(
                    restored.one(f"SELECT COUNT(*) FROM {table}")[0],
                    snap.tables[table],
                    f"table {table} was not recovered",
                )
            self.assertEqual(restored.one("SELECT COUNT(*) FROM tasks")[0], before["tasks"])
            self.assertEqual(objectives.get(restored, obj.id).title, "Snapshot subject")
            # A working store, not just recovered bytes.
            self.assertEqual(
                tasks.create(restored, "post-recovery work", actor="claude").status,
                tasks.PENDING,
            )
            # Evidence and the snapshot both survive the procedure.
            self.assertTrue(os.path.exists(db + ".failed-migration"))
            self.assertTrue(os.path.exists(snap.path))
            self.assertTrue(snapshot.verify(snap)["ok"])


class ActivityDuringSnapshotTest(unittest.TestCase):
    """A snapshot taken while the database is being written must be a
    consistent point in time, never a torn half-transaction."""

    def test_snapshot_during_concurrent_writes_is_internally_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "jarvis.db")
            store = open_store(db)
            populate(store, tasks_wanted=3)

            stop = threading.Event()
            errors: list[str] = []

            def writer():
                # A separate connection, as a second process would have.
                writer_store = open_store(db)
                try:
                    i = 0
                    while not stop.is_set() and i < 200:
                        tasks.create(writer_store, f"concurrent {i}", actor="writer")
                        i += 1
                except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                    errors.append(f"{type(exc).__name__}: {exc}")
                finally:
                    writer_store.close()

            thread = threading.Thread(target=writer)
            thread.start()
            try:
                snapshots = [snapshot.create(store, reason=f"during activity {n}")
                             for n in range(3)]
            finally:
                stop.set()
                thread.join(timeout=30)

            self.assertEqual(errors, [], f"writer failed: {errors}")
            for snap in snapshots:
                self.assertEqual(snap.integrity, "ok")
                self.assertEqual(snap.foreign_key_check, "ok")
                copy = sqlite3.connect(snap.path)
                try:
                    # Every task is written together with its create event, so
                    # a torn copy would leave a task without one.
                    orphans = copy.execute(
                        "SELECT COUNT(*) FROM tasks t LEFT JOIN events e"
                        " ON e.target = t.id WHERE e.id IS NULL"
                    ).fetchone()[0]
                    self.assertEqual(orphans, 0, "snapshot caught a partial transaction")
                    dangling = copy.execute(
                        "SELECT COUNT(*) FROM task_deps d LEFT JOIN tasks t"
                        " ON t.id = d.depends_on WHERE t.id IS NULL"
                    ).fetchone()[0]
                    self.assertEqual(dangling, 0)
                finally:
                    copy.close()


class MetadataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = open_store(os.path.join(self.tmp.name, "jarvis.db"))
        populate(self.store)

    def test_every_required_field_is_recorded_and_correct(self):
        snap = snapshot.create(self.store, actor="auditor", reason="acceptance")
        self.assertTrue(snap.id.startswith("snp_"))
        datetime.fromisoformat(snap.created_at)  # parses, so it is a real timestamp
        self.assertEqual(snap.source, os.path.abspath(self.store.path))
        self.assertEqual(snap.schema_version, SCHEMA_VERSION)
        self.assertEqual(snap.integrity, "ok")
        self.assertEqual(snap.path, os.path.abspath(snap.path))
        self.assertEqual(snap.size_bytes, os.path.getsize(snap.path))
        self.assertGreater(snap.size_bytes, 0)
        self.assertEqual(snap.result, "ok")
        self.assertEqual(snap.actor, "auditor")
        self.assertEqual(snap.reason, "acceptance")
        self.assertEqual(set(snap.tables), set(snapshot.EXPECTED_TABLES))

    def test_sidecar_travels_with_the_snapshot_and_matches(self):
        snap = snapshot.create(self.store)
        self.assertTrue(os.path.exists(snap.sidecar))
        with open(snap.sidecar) as handle:
            data = json.load(handle)
        self.assertEqual(data["id"], snap.id)
        self.assertEqual(data["schema_version"], SCHEMA_VERSION)
        self.assertEqual(data["tables"], snap.tables)

    def test_filename_carries_the_schema_version(self):
        snap = snapshot.create(self.store, label="pre-task2")
        name = os.path.basename(snap.path)
        self.assertTrue(name.startswith(f"jarvis-v{SCHEMA_VERSION}-"))
        self.assertIn("pre-task2", name)

    def test_listing_reads_what_is_on_disk_newest_first(self):
        first = snapshot.create(self.store, reason="one")
        second = snapshot.create(self.store, reason="two")
        listed = snapshot.list_snapshots(self.store)
        self.assertEqual([s.id for s in listed], [second.id, first.id])

    def test_latest_valid_filters_by_schema_version(self):
        snap = snapshot.create(self.store)
        self.assertEqual(
            snapshot.latest_valid(self.store, schema_version=SCHEMA_VERSION).id, snap.id
        )
        self.assertIsNone(snapshot.latest_valid(self.store, schema_version=99))


def _init_repo(directory):
    """A throwaway git repo, independent of whatever state this checkout is
    in, so dirty/clean behaviour is tested deterministically rather than by
    hoping the real working tree happens to be in the right state."""
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=directory, check=True, capture_output=True, text=True,
    )
    run("init", "-q")
    run("-c", "user.email=test@example.com", "-c", "user.name=test", "commit",
        "-q", "--allow-empty", "-m", "initial")


class GitVersionTest(unittest.TestCase):
    """A2: a snapshot's sidecar must say which code version produced it, when
    that is knowable - and must never claim to know it when it is not."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = open_store(os.path.join(self.tmp.name, "jarvis.db"))

    def test_git_state_is_none_outside_a_repo(self):
        sha, dirty = snapshot._git_state(self.tmp.name)
        self.assertIsNone(sha)
        self.assertIsNone(dirty)

    def test_git_state_is_none_when_git_itself_is_unavailable(self):
        real_run = snapshot.subprocess.run

        def missing_git(*args, **kwargs):
            raise FileNotFoundError("no such file or directory: 'git'")

        snapshot.subprocess.run = missing_git
        try:
            sha, dirty = snapshot._git_state(self.tmp.name)
        finally:
            snapshot.subprocess.run = real_run
        self.assertIsNone(sha)
        self.assertIsNone(dirty)

    def test_git_state_reports_the_head_sha_and_a_clean_tree(self):
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            expected = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
            ).stdout.strip()
            sha, dirty = snapshot._git_state(repo)
            self.assertEqual(sha, expected)
            self.assertFalse(dirty)

    def test_git_state_reports_dirty_when_the_tree_has_uncommitted_changes(self):
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            Path(repo, "untracked.txt").write_text("uncommitted")
            sha, dirty = snapshot._git_state(repo)
            self.assertIsNotNone(sha)
            self.assertTrue(dirty)

    def test_create_never_fails_when_git_is_unavailable(self):
        """The SHA is a nice-to-have annotation, never a precondition."""
        real_git_state = snapshot._git_state
        snapshot._git_state = lambda *a, **k: (None, None)
        try:
            snap = snapshot.create(self.store)
        finally:
            snapshot._git_state = real_git_state
        self.assertEqual(snap.result, "ok")
        self.assertIsNone(snap.git_sha)
        self.assertIsNone(snap.git_dirty)

    def test_create_records_the_git_state_of_this_checkout(self):
        """This test runs inside the jarvis repo's own checkout, so it is a
        real integration check against `create()`'s default repo_dir."""
        expected_sha, expected_dirty = snapshot._git_state()
        snap = snapshot.create(self.store)
        self.assertEqual(snap.git_sha, expected_sha)
        self.assertEqual(snap.git_dirty, expected_dirty)
        self.assertIsInstance(snap.git_dirty, bool)

    def test_sidecar_and_audit_event_carry_git_sha_and_dirty(self):
        snap = snapshot.create(self.store)
        with open(snap.sidecar) as handle:
            data = json.load(handle)
        self.assertEqual(data["git_sha"], snap.git_sha)
        self.assertEqual(data["git_dirty"], snap.git_dirty)

        event = events.recent(self.store)[0]
        self.assertEqual(event["kind"], "snapshot.create")
        self.assertEqual(event["detail"]["git_sha"], snap.git_sha)
        self.assertEqual(event["detail"]["git_dirty"], snap.git_dirty)

    def test_old_sidecar_without_git_fields_still_loads(self):
        """Backward compatibility: a sidecar written before this change has no
        git_sha/git_dirty keys at all, not null values for them."""
        snap = snapshot.create(self.store)
        with open(snap.sidecar) as handle:
            data = json.load(handle)
        del data["git_sha"]
        del data["git_dirty"]
        with open(snap.sidecar, "w") as handle:
            json.dump(data, handle)

        loaded = snapshot.load(snap.sidecar)
        self.assertIsNotNone(loaded)
        self.assertIsNone(loaded.git_sha)
        self.assertIsNone(loaded.git_dirty)


class FailureHandlingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = open_store(os.path.join(self.tmp.name, "jarvis.db"))

    def test_unusable_directory_is_refused_and_audited(self):
        blocker = os.path.join(self.tmp.name, "a-file-not-a-directory")
        with open(blocker, "w") as handle:
            handle.write("x")
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.create(self.store, directory=blocker)
        event = events.recent(self.store)[0]
        self.assertEqual(event["kind"], "snapshot.create")
        self.assertEqual(event["outcome"], "failed")

    def test_existing_snapshot_is_never_silently_overwritten(self):
        fixed_id, fixed_stamp = "snp_fixed000000", "20260101T000000Z"
        real_new_id, real_stamp = snapshot.new_id, snapshot._stamp
        snapshot.new_id = lambda prefix: fixed_id
        snapshot._stamp = lambda: fixed_stamp
        try:
            first = snapshot.create(self.store, reason="original")
            size_before = os.path.getsize(first.path)
            with self.assertRaises(snapshot.SnapshotError) as caught:
                snapshot.create(self.store, reason="would clobber")
            self.assertIn("refusing to overwrite", str(caught.exception))
        finally:
            snapshot.new_id, snapshot._stamp = real_new_id, real_stamp
        # The original is untouched and still valid.
        self.assertEqual(os.path.getsize(first.path), size_before)
        self.assertTrue(snapshot.verify(first)["ok"])
        with open(first.sidecar) as handle:
            self.assertEqual(json.load(handle)["reason"], "original")

    def test_in_memory_store_has_no_snapshot_directory(self):
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.create(open_store(":memory:"))

    def test_corrupt_snapshot_does_not_verify(self):
        snap = snapshot.create(self.store)
        with open(snap.path, "r+b") as handle:
            handle.seek(0)
            handle.write(b"garbage bytes over the header")
        result = snapshot.verify(snap)
        self.assertFalse(result["ok"])
        self.assertIsNone(snapshot.latest_valid(self.store))

    def test_missing_snapshot_file_is_reported_not_assumed(self):
        snap = snapshot.create(self.store)
        os.unlink(snap.path)
        self.assertFalse(snap.is_valid)
        self.assertEqual(snapshot.verify(snap)["integrity"], "missing")

    def test_damaged_sidecar_is_skipped_rather_than_crashing_the_listing(self):
        snap = snapshot.create(self.store)
        with open(snap.sidecar, "w") as handle:
            handle.write("{ not json")
        self.assertEqual(snapshot.list_snapshots(self.store), [])


class DiagnosticsIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = open_store(os.path.join(self.tmp.name, "jarvis.db"))

    def _check(self, store=None):
        report = diagnostics.health(store or self.store)
        return next(c for c in report["checks"] if c["name"] == "recovery_point")

    def test_empty_store_does_not_nag(self):
        self.assertEqual(self._check()["status"], "ok")
        self.assertIn("nothing to snapshot", self._check()["detail"])

    def test_populated_store_without_a_snapshot_warns(self):
        populate(self.store)
        self.assertEqual(self._check()["status"], "warn")
        self.assertIn("jarvis snapshot create", self._check()["detail"])

    def test_valid_snapshot_clears_the_warning(self):
        populate(self.store)
        snapshot.create(self.store)
        check = self._check()
        self.assertEqual(check["status"], "ok")
        self.assertIn(f"schema v{SCHEMA_VERSION}", check["detail"])

    def test_corrupt_snapshot_does_not_count_as_a_recovery_point(self):
        populate(self.store)
        snap = snapshot.create(self.store)
        with open(snap.path, "r+b") as handle:
            handle.seek(0)
            handle.write(b"garbage bytes over the header")
        check = self._check()
        self.assertEqual(check["status"], "warn")
        self.assertIn("none verified", check["detail"])

    def test_self_report_lists_snapshots(self):
        snap = snapshot.create(self.store)
        report = diagnostics.self_report(self.store)
        self.assertEqual(report["snapshots"][0]["id"], snap.id)
        self.assertIn("pre_migration_snapshot", report["implemented"])


class SnapshotCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "jarvis.db")
        populate(open_store(self.db))

    def _json(self, *argv, expect=0):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--db", self.db, *argv, "--json"])
        self.assertEqual(code, expect, out.getvalue())
        return json.loads(out.getvalue())

    def test_create_list_and_verify_through_the_cli(self):
        created = self._json("snapshot", "create", "--reason", "pre-task2")
        self.assertTrue(created["valid"])
        self.assertEqual(created["schema_version"], SCHEMA_VERSION)

        listed = self._json("snapshot", "list")
        self.assertEqual([s["id"] for s in listed], [created["id"]])

        verified = self._json("snapshot", "verify")
        self.assertEqual(verified["checked"], 1)
        self.assertTrue(verified["snapshots"][0]["ok"])

    def test_verify_a_single_file_by_path(self):
        created = self._json("snapshot", "create")
        result = self._json("snapshot", "verify", "--path", created["path"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)

    def test_failure_is_a_clean_cli_error(self):
        blocker = os.path.join(self.tmp.name, "blocker")
        with open(blocker, "w") as handle:
            handle.write("x")
        out, err = io.StringIO(), io.StringIO()
        from contextlib import redirect_stderr
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--db", self.db, "snapshot", "create", "--dir", blocker])
        self.assertEqual(code, 2)
        self.assertTrue(err.getvalue().startswith("error:"))


if __name__ == "__main__":
    unittest.main()
