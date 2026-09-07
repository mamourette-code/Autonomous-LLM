import unittest

from jarvis import memory, objectives, session, tasks
from jarvis.store import open_store


class BootTest(unittest.TestCase):
    def setUp(self):
        self.store = open_store(":memory:")

    def test_first_boot_has_no_predecessor(self):
        briefing = session.boot(self.store)
        self.assertIsNone(briefing["previous_session"])
        self.assertEqual(briefing["ready_tasks"], [])

    def test_briefing_separates_ready_from_blocked_work(self):
        first = tasks.create(self.store, "first")
        tasks.create(self.store, "second", depends_on=[first.id])
        briefing = session.boot(self.store)
        self.assertEqual([t["title"] for t in briefing["ready_tasks"]], ["first"])
        self.assertEqual([t["title"] for t in briefing["blocked_tasks"]], ["second"])

    def test_briefing_surfaces_unverified_completions(self):
        task = tasks.create(self.store, "t")
        tasks.transition(self.store, task.id, tasks.IN_PROGRESS)
        tasks.transition(self.store, task.id, tasks.COMPLETED)
        briefing = session.boot(self.store)
        self.assertEqual([t["id"] for t in briefing["unverified_completions"]], [task.id])

    def test_briefing_reports_objective_readiness_gaps(self):
        objectives.create(self.store, "t", "o", open_questions=["which db?"])
        entry = session.boot(self.store)["open_objectives"][0]
        self.assertFalse(entry["ready"])
        self.assertEqual(entry["open_questions"], ["which db?"])

    def test_focus_retrieves_relevant_memory_only(self):
        memory.remember(self.store, kind="semantic", title="Test runner",
                        body="This repo uses python unittest")
        memory.remember(self.store, kind="semantic", title="Coffee", body="Flat white")
        briefing = session.boot(self.store, focus="how do I run the tests")
        titles = [m["title"] for m in briefing["relevant_memories"]]
        self.assertEqual(titles, ["Test runner"])

    def test_boot_without_focus_does_not_load_memory(self):
        memory.remember(self.store, kind="semantic", title="Test runner",
                        body="This repo uses python unittest")
        self.assertNotIn("relevant_memories", session.boot(self.store))


class CloseTest(unittest.TestCase):
    def setUp(self):
        self.store = open_store(":memory:")
        self.session_id = session.boot(self.store)["session_id"]

    def test_close_distinguishes_verified_from_completed(self):
        done = tasks.create(self.store, "done", session_id=self.session_id)
        tasks.transition(self.store, done.id, tasks.IN_PROGRESS, session_id=self.session_id)
        tasks.transition(self.store, done.id, tasks.COMPLETED, session_id=self.session_id)

        checked = tasks.create(self.store, "checked", session_id=self.session_id)
        tasks.transition(self.store, checked.id, tasks.IN_PROGRESS, session_id=self.session_id)
        tasks.transition(self.store, checked.id, tasks.COMPLETED, session_id=self.session_id)
        tasks.verify(self.store, checked.id, method="rerun", evidence="matches",
                     verifier="critic", session_id=self.session_id)

        report = session.close(self.store, self.session_id, summary="did work")
        self.assertEqual([t["title"] for t in report["verified"]], ["checked"])
        self.assertEqual([t["title"] for t in report["completed_unverified"]], ["done"])
        self.assertTrue(
            any("not verified" in item for item in report["carry_forward"])
        )

    def test_close_is_idempotent_guarded(self):
        session.close(self.store, self.session_id, summary="done")
        with self.assertRaises(RuntimeError):
            session.close(self.store, self.session_id, summary="again")

    def test_unknown_session_rejected(self):
        with self.assertRaises(KeyError):
            session.close(self.store, "ses_nope", summary="x")

    def test_close_does_not_invent_memories(self):
        report = session.close(self.store, self.session_id, summary="quiet session")
        self.assertEqual(report["memories_written"], [])
        self.assertEqual(memory.all_live(self.store), [])

    def test_next_session_inherits_the_summary_and_changes(self):
        task = tasks.create(self.store, "carried", session_id=self.session_id)
        session.close(self.store, self.session_id, summary="left work behind")
        briefing = session.boot(self.store)
        self.assertEqual(briefing["previous_session"]["summary"], "left work behind")
        self.assertEqual([t["title"] for t in briefing["ready_tasks"]], ["carried"])
        self.assertTrue(briefing["changed_since_last_session"] is not None)
        self.assertEqual(tasks.get(self.store, task.id).status, tasks.PENDING)


if __name__ == "__main__":
    unittest.main()
