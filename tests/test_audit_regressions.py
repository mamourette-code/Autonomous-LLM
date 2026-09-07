"""Regressions from the Task 1 acceptance audit.

Every test here reproduces a defect that the original suite passed over.
Each names the defect it locks down.
"""

import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

from jarvis import diagnostics, events, memory, objectives, retrieval, tasks
from jarvis.cli import _GLOBAL_DEFAULTS, _build_parser, main
from jarvis.store import open_store


class TransactionBoundaryTest(unittest.TestCase):
    """Defect: a partially-written row survived and was committed by an
    unrelated later write, producing state with no audit event - exactly what
    the observability health check exists to catch."""

    def setUp(self):
        self.store = open_store(":memory:")

    def test_failed_create_leaves_nothing_behind(self):
        with self.assertRaises(sqlite3.IntegrityError):
            tasks.create(self.store, "depends on a ghost", depends_on=["tsk_missing"])
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM tasks")[0], 0)

    def test_orphan_is_not_committed_by_a_later_unrelated_write(self):
        with self.assertRaises(sqlite3.IntegrityError):
            tasks.create(self.store, "doomed", depends_on=["tsk_missing"])
        memory.remember(self.store, kind="semantic", title="later", body="unrelated write")
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM tasks")[0], 0)

    def test_no_state_row_ever_lacks_an_audit_event(self):
        with self.assertRaises(sqlite3.IntegrityError):
            tasks.create(self.store, "doomed", depends_on=["tsk_missing"])
        tasks.create(self.store, "legitimate")
        orphans = self.store.query(
            "SELECT t.id FROM tasks t LEFT JOIN events e ON e.target = t.id WHERE e.id IS NULL"
        )
        self.assertEqual(orphans, [])
        self.assertEqual(diagnostics.health(self.store)["status"], "ok")

    def test_rollback_restores_the_prior_state(self):
        task = tasks.create(self.store, "t")
        try:
            with self.store.transaction():
                self.store.execute(
                    "UPDATE tasks SET status = 'in_progress' WHERE id = ?", (task.id,)
                )
                raise RuntimeError("something failed after the write")
        except RuntimeError:
            pass
        self.assertEqual(tasks.get(self.store, task.id).status, tasks.PENDING)

    def test_nested_transaction_does_not_commit_early(self):
        try:
            with self.store.transaction():
                self.store.execute(
                    "INSERT INTO sessions (id, started_at) VALUES ('outer','t')"
                )
                with self.store.transaction():
                    self.store.execute(
                        "INSERT INTO sessions (id, started_at) VALUES ('inner','t')"
                    )
                raise RuntimeError("outer fails after the inner block succeeded")
        except RuntimeError:
            pass
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM sessions")[0], 0)


class AttributionTest(unittest.TestCase):
    """Defect: mutating functions defaulted actor to "user", so the audit log
    named the user for work the model had done."""

    def setUp(self):
        self.store = open_store(":memory:")

    def test_resolve_question_requires_an_explicit_actor(self):
        obj = objectives.create(self.store, "t", "o", actor="claude", open_questions=["q?"])
        with self.assertRaises(TypeError):
            objectives.resolve_question(self.store, obj.id, "q?", "a")

    def test_set_status_requires_an_explicit_actor(self):
        obj = objectives.create(self.store, "t", "o", actor="claude")
        with self.assertRaises(TypeError):
            objectives.set_status(self.store, obj.id, "achieved")

    def test_resolve_conflict_requires_an_explicit_actor(self):
        with self.assertRaises(TypeError):
            retrieval.resolve_conflict(self.store, "cfl_x", "left")

    def test_the_log_never_names_an_actor_who_did_not_act(self):
        obj = objectives.create(self.store, "t", "o", actor="claude", open_questions=["q?"])
        objectives.resolve_question(self.store, obj.id, "q?", "a", actor="claude")
        actors = {e["actor"] for e in events.for_target(self.store, obj.id)}
        self.assertEqual(actors, {"claude"})


class AuthorityBoundaryTest(unittest.TestCase):
    """Defect: authorized_by was copied from the objective's stored authority,
    so the log asserted the user had approved actions they never saw."""

    def setUp(self):
        self.store = open_store(":memory:")
        self.obj = objectives.create(
            self.store, "Ship", "accepted", actor="claude", authority="user"
        )

    def test_non_authority_cannot_close_an_objective(self):
        with self.assertRaises(objectives.AuthorityError):
            objectives.set_status(self.store, self.obj.id, "achieved", actor="claude")
        self.assertEqual(objectives.get(self.store, self.obj.id).status, "open")

    def test_a_refused_attempt_is_recorded_as_denied(self):
        with self.assertRaises(objectives.AuthorityError):
            objectives.set_status(self.store, self.obj.id, "achieved", actor="claude")
        denied = [e for e in events.for_target(self.store, self.obj.id) if e["outcome"] == "denied"]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["actor"], "claude")
        self.assertIsNone(denied[0]["authorized_by"])

    def test_the_authority_may_close_it_and_that_is_recorded_truthfully(self):
        objectives.set_status(self.store, self.obj.id, "achieved", actor="user")
        ok = [
            e for e in events.for_target(self.store, self.obj.id)
            if e["kind"] == "objective.status" and e["outcome"] == "ok"
        ]
        self.assertEqual(ok[0]["authorized_by"], "user")

    def test_creation_does_not_claim_an_authorization_that_did_not_happen(self):
        created = [
            e for e in events.for_target(self.store, self.obj.id)
            if e["kind"] == "objective.create"
        ][0]
        self.assertEqual(created["actor"], "claude")
        self.assertIsNone(created["authorized_by"])
        self.assertEqual(created["detail"]["authority"], "user")


class SensitivityGateTest(unittest.TestCase):
    """Defect: remember() skipped the sensitivity scan that assess() applies,
    so an unassessed caller could persist a credential to disk."""

    def setUp(self):
        self.store = open_store(":memory:")

    def test_write_path_refuses_flagged_content(self):
        with self.assertRaises(memory.SensitiveContentError):
            memory.remember(self.store, kind="user", title="login", body="password: hunter2")
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM memories")[0], 0)

    def test_refusal_is_audited_without_echoing_the_secret(self):
        with self.assertRaises(memory.SensitiveContentError):
            memory.remember(self.store, kind="user", title="login", body="password: hunter2")
        event = events.recent(self.store)[0]
        self.assertEqual(event["outcome"], "denied")
        self.assertNotIn("hunter2", str(event))

    def test_override_is_explicit_and_recorded_as_an_override(self):
        mem = memory.remember(
            self.store, kind="user", title="login", body="password: hunter2",
            allow_sensitive=True,
        )
        self.assertIsNotNone(memory.get(self.store, mem.id))
        self.assertTrue(events.recent(self.store)[0]["detail"]["sensitive_override"])

    def test_ordinary_content_is_unaffected(self):
        mem = memory.remember(self.store, kind="semantic", title="Port", body="listens on 8080")
        self.assertIsNotNone(memory.get(self.store, mem.id))


class CliGlobalFlagTest(unittest.TestCase):
    """Defect: --db before the subcommand was silently discarded, so the CLI
    read and wrote a different database than the one the user named."""

    def _parse(self, argv):
        args = _build_parser().parse_args(argv)
        for name, fallback in _GLOBAL_DEFAULTS.items():
            if not hasattr(args, name):
                setattr(args, name, fallback)
        return args

    def test_db_is_honoured_before_the_subcommand(self):
        self.assertEqual(self._parse(["--db", "/x/a.db", "task", "list"]).db, "/x/a.db")

    def test_db_is_honoured_after_the_subcommand(self):
        self.assertEqual(self._parse(["task", "list", "--db", "/x/a.db"]).db, "/x/a.db")

    def test_json_and_actor_work_in_both_positions(self):
        self.assertTrue(self._parse(["--json", "doctor"]).json)
        self.assertTrue(self._parse(["doctor", "--json"]).json)
        self.assertEqual(self._parse(["--actor", "critic", "task", "list"]).actor, "critic")
        self.assertEqual(self._parse(["task", "list", "--actor", "critic"]).actor, "critic")

    def test_defaults_still_apply_when_nothing_is_given(self):
        args = self._parse(["task", "list"])
        self.assertIsNone(args.db)
        self.assertFalse(args.json)
        self.assertEqual(args.actor, "claude")

    def test_writes_actually_land_in_the_named_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            named = os.path.join(tmp, "named.db")
            other = os.path.join(tmp, "other.db")
            with redirect_stdout(io.StringIO()):
                main(["--db", named, "task", "add", "--title", "goes to the named db"])
            open_store(other)
            self.assertEqual(open_store(named).one("SELECT COUNT(*) FROM tasks")[0], 1)
            self.assertEqual(open_store(other).one("SELECT COUNT(*) FROM tasks")[0], 0)


class ConcurrentOpenTest(unittest.TestCase):
    """Defect: two processes opening a fresh database both ran the schema
    script; the loser died with "table already exists", losing all its work."""

    def test_simultaneous_first_open_does_not_race(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "race.db")
            script = (
                "import sys;"
                "sys.path.insert(0, %r);" % os.getcwd()
                + "from jarvis.store import open_store;"
                "from jarvis import tasks;"
                "s = open_store(%r);" % db
                + "[tasks.create(s, 'row-%d' % i) for i in range(15)];"
                "print('ok')"
            )
            procs = [
                subprocess.Popen([sys.executable, "-c", script],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for _ in range(4)
            ]
            results = [p.communicate() for p in procs]
            for out, err in results:
                self.assertIn("ok", out, f"a process failed to open the store: {err[-400:]}")
            self.assertEqual(open_store(db).one("SELECT COUNT(*) FROM tasks")[0], 60)


if __name__ == "__main__":
    unittest.main()


class CliErrorHandlingTest(unittest.TestCase):
    """The exception types introduced by the audit fixes must reach the user as
    errors, not tracebacks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "cli.db")

    def _run(self, argv):
        import io
        from contextlib import redirect_stderr
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = main(["--db", self.db, *argv])
        return code, err.getvalue()

    def test_authority_refusal_is_a_clean_error(self):
        store = open_store(self.db)
        obj = objectives.create(store, "t", "o", actor="claude", authority="user")
        store.close()
        code, err = self._run(["--actor", "claude", "objective", "resolve",
                               "--id", obj.id, "--question-text", "nope", "--answer", "x"])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error:"), err)

    def test_sensitive_write_is_a_clean_error(self):
        code, err = self._run(["memory", "add", "--title", "creds",
                               "--body", "password: hunter2", "--force",
                               "--confidence", "0.9", "--durability", "0.9",
                               "--utility", "0.9"])
        self.assertEqual(code, 2)
        self.assertIn("sensitive", err)
        self.assertEqual(open_store(self.db).one("SELECT COUNT(*) FROM memories")[0], 0)
