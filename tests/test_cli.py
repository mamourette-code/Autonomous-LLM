import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from jarvis.cli import main


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "jarvis.db")
        self.addCleanup(self.tmp.cleanup)

    def run_cli(self, *args, expect: int = 0) -> str:
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--db", self.db, *args])
        self.assertEqual(code, expect, err.getvalue() or out.getvalue())
        return out.getvalue()

    def json_cli(self, *args, expect: int = 0):
        return json.loads(self.run_cli(*args, "--json", expect=expect))

    def test_global_flags_work_after_the_subcommand(self):
        payload = self.json_cli("task", "add", "--title", "written after the flag")
        self.assertEqual(payload["title"], "written after the flag")

    def test_full_lifecycle_through_the_cli(self):
        session_id = self.json_cli("boot", "--project", "demo")["session_id"]
        task = self.json_cli(
            "task", "add", "--title", "write docs", "--project", "demo", "--session", session_id
        )
        self.json_cli("task", "start", "--id", task["id"], "--session", session_id)
        self.json_cli(
            "task", "complete", "--id", task["id"], "--actual", "docs written",
            "--session", session_id,
        )
        verified = self.json_cli(
            "task", "verify", "--id", task["id"], "--method", "read them",
            "--evidence", "docs cover every command", "--actor", "critic",
            "--session", session_id,
        )
        self.assertEqual(verified["status"], "verified")

        report = self.json_cli("close", session_id, "--summary", "docs done")
        self.assertEqual([t["id"] for t in report["verified"]], [task["id"]])

    def test_illegal_transition_exits_nonzero(self):
        task = self.json_cli("task", "add", "--title", "t")
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--db", self.db, "task", "verify", "--id", task["id"],
                         "--method", "m", "--evidence", "e"])
        self.assertEqual(code, 2)
        self.assertIn("COMPLETED", err.getvalue())

    def test_memory_add_respects_governance_and_force_override(self):
        blocked = self.json_cli(
            "memory", "add", "--kind", "episodic", "--title", "ran ls",
            "--body", "listed a directory", "--confidence", "0.2",
            "--durability", "0.1", "--utility", "0.1",
        )
        self.assertFalse(blocked["stored"])
        self.assertIn("--force", blocked["hint"])

        forced = self.json_cli(
            "memory", "add", "--kind", "episodic", "--title", "ran ls",
            "--body", "listed a directory", "--confidence", "0.2",
            "--durability", "0.1", "--utility", "0.1", "--force",
        )
        self.assertTrue(forced["stored"])
        self.assertTrue(forced["forced"])

    def test_memory_search_returns_ranked_hits(self):
        self.json_cli(
            "memory", "add", "--title", "Test runner",
            "--body", "This repo uses python unittest", "--source", "pip list",
            "--source-class", "sourced_knowledge", "--confidence", "0.9",
            "--durability", "0.8", "--utility", "0.8",
        )
        result = self.json_cli("memory", "search", "--query", "how do I run the tests")
        self.assertEqual(len(result["hits"]), 1)
        self.assertIn("components", result["hits"][0])

    def test_doctor_reports_health_and_gaps(self):
        report = self.json_cli("doctor")
        self.assertEqual(report["health"]["status"], "ok")
        self.assertIn("agent_orchestration", report["not_implemented"])

    def test_doctor_human_output_is_readable(self):
        text = self.run_cli("doctor")
        self.assertIn("JARVIS self-diagnostic", text)
        self.assertIn("not implemented:", text)

    def test_events_expose_the_audit_trail(self):
        task = self.json_cli("task", "add", "--title", "audited")
        history = self.json_cli("events", "--target", task["id"])
        self.assertEqual(history[0]["kind"], "task.create")

    def test_no_subcommand_prints_help(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main([])
        self.assertEqual(code, 1)
        self.assertIn("usage: jarvis", out.getvalue())


if __name__ == "__main__":
    unittest.main()
