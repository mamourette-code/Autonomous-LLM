import unittest

from jarvis import events
from jarvis.store import open_store


class EventsTest(unittest.TestCase):
    def setUp(self):
        self.store = open_store(":memory:")

    def test_records_event_with_detail(self):
        events.record(self.store, "x.y", "claude", target="t1", permission="modify", note="hi")
        (event,) = events.recent(self.store)
        self.assertEqual(event["kind"], "x.y")
        self.assertEqual(event["outcome"], "ok")
        self.assertEqual(event["detail"]["note"], "hi")

    def test_rejects_unknown_outcome_and_permission(self):
        with self.assertRaises(ValueError):
            events.record(self.store, "x", "claude", outcome="great")
        with self.assertRaises(ValueError):
            events.record(self.store, "x", "claude", permission="sudo")

    def test_action_records_failure_and_reraises(self):
        with self.assertRaises(ZeroDivisionError):
            with events.action(self.store, "compute", "claude", target="t") as detail:
                detail["stage"] = "divide"
                1 / 0
        (event,) = events.recent(self.store)
        self.assertEqual(event["outcome"], "failed")
        self.assertEqual(event["detail"]["stage"], "divide")
        self.assertIn("ZeroDivisionError", event["detail"]["error"])
        self.assertIsNotNone(event["duration_ms"])

    def test_action_records_success(self):
        with events.action(self.store, "compute", "claude"):
            pass
        self.assertEqual(events.recent(self.store)[0]["outcome"], "ok")

    def test_history_for_target_is_chronological(self):
        events.record(self.store, "a", "claude", target="t")
        events.record(self.store, "b", "claude", target="t")
        events.record(self.store, "c", "claude", target="other")
        history = events.for_target(self.store, "t")
        self.assertEqual([e["kind"] for e in history], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
