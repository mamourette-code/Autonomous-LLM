import unittest

from jarvis import events, tasks
from jarvis.store import open_store


class TaskStateMachineTest(unittest.TestCase):
    def setUp(self):
        self.store = open_store(":memory:")

    def _completed(self, actor="claude", title="t"):
        task = tasks.create(self.store, title, actor=actor)
        tasks.transition(self.store, task.id, tasks.IN_PROGRESS, actor=actor)
        return tasks.transition(self.store, task.id, tasks.COMPLETED, actor=actor)

    def test_new_task_is_pending(self):
        self.assertEqual(tasks.create(self.store, "t").status, tasks.PENDING)

    def test_illegal_transition_rejected_and_audited(self):
        task = tasks.create(self.store, "t")
        with self.assertRaises(tasks.TransitionError):
            tasks.transition(self.store, task.id, tasks.COMPLETED)
        denied = [e for e in events.for_target(self.store, task.id) if e["outcome"] == "denied"]
        self.assertEqual(len(denied), 1)
        self.assertEqual(tasks.get(self.store, task.id).status, tasks.PENDING)

    def test_verified_is_unreachable_without_evidence(self):
        task = self._completed()
        with self.assertRaises(tasks.TransitionError):
            tasks.transition(self.store, task.id, tasks.VERIFIED)

    def test_completed_is_not_verified(self):
        task = self._completed()
        self.assertEqual(task.status, tasks.COMPLETED)
        self.assertIsNone(task.verified_at)

    def test_verify_requires_method_and_evidence(self):
        task = self._completed()
        with self.assertRaises(ValueError):
            tasks.verify(self.store, task.id, method="", evidence="x", verifier="critic")
        with self.assertRaises(ValueError):
            tasks.verify(self.store, task.id, method="x", evidence="  ", verifier="critic")

    def test_verify_only_from_completed(self):
        task = tasks.create(self.store, "t")
        with self.assertRaises(tasks.TransitionError):
            tasks.verify(self.store, task.id, method="m", evidence="e", verifier="critic")

    def test_independence_is_derived_not_claimed(self):
        same = self._completed(actor="claude", title="a")
        tasks.verify(self.store, same.id, method="m", evidence="e", verifier="claude")
        self.assertEqual(tasks.verifications(self.store, same.id)[0]["independent"], 0)

        other = self._completed(actor="claude", title="b")
        tasks.verify(self.store, other.id, method="m", evidence="e", verifier="critic")
        self.assertEqual(tasks.verifications(self.store, other.id)[0]["independent"], 1)

    def test_failed_verification_sends_task_back_to_failed(self):
        task = self._completed()
        task = tasks.verify(
            self.store, task.id, method="rerun", evidence="output differs",
            verifier="critic", passed=False,
        )
        self.assertEqual(task.status, tasks.FAILED)
        self.assertEqual(task.failure_class, "verification_failure")

    def test_failure_must_be_classified(self):
        task = tasks.create(self.store, "t")
        with self.assertRaises(ValueError):
            tasks.transition(self.store, task.id, tasks.FAILED)
        with self.assertRaises(ValueError):
            tasks.transition(self.store, task.id, tasks.FAILED, failure_class="just_broke")
        task = tasks.transition(
            self.store, task.id, tasks.FAILED, failure_class="tool_failure", note="timeout"
        )
        self.assertEqual(task.failure_class, "tool_failure")
        self.assertEqual(task.failure_note, "timeout")

    def test_archived_is_terminal(self):
        task = tasks.create(self.store, "t")
        tasks.transition(self.store, task.id, tasks.ARCHIVED)
        with self.assertRaises(tasks.TransitionError):
            tasks.transition(self.store, task.id, tasks.IN_PROGRESS)


class TaskDependencyTest(unittest.TestCase):
    def setUp(self):
        self.store = open_store(":memory:")
        self.first = tasks.create(self.store, "first")
        self.second = tasks.create(self.store, "second", depends_on=[self.first.id])

    def test_cannot_start_with_unmet_dependency(self):
        self.assertEqual(tasks.unmet_dependencies(self.store, self.second.id), [self.first.id])
        with self.assertRaises(tasks.TransitionError):
            tasks.transition(self.store, self.second.id, tasks.IN_PROGRESS)

    def test_dependency_clears_on_completion(self):
        tasks.transition(self.store, self.first.id, tasks.IN_PROGRESS)
        tasks.transition(self.store, self.first.id, tasks.COMPLETED)
        self.assertEqual(tasks.unmet_dependencies(self.store, self.second.id), [])
        started = tasks.transition(self.store, self.second.id, tasks.IN_PROGRESS)
        self.assertEqual(started.status, tasks.IN_PROGRESS)

    def test_ready_tasks_excludes_blocked_work(self):
        ready = [t.id for t in tasks.ready_tasks(self.store)]
        self.assertEqual(ready, [self.first.id])

    def test_listing_filters(self):
        self.assertEqual(len(tasks.list_tasks(self.store, open_only=True)), 2)
        self.assertEqual(len(tasks.list_tasks(self.store, status=tasks.PENDING)), 2)
        self.assertEqual(len(tasks.list_tasks(self.store, status=tasks.VERIFIED)), 0)


if __name__ == "__main__":
    unittest.main()
