import unittest

from jarvis import events, objectives
from jarvis.store import open_store


class ObjectivesTest(unittest.TestCase):
    def setUp(self):
        self.store = open_store(":memory:")

    def test_requires_title_and_outcome(self):
        with self.assertRaises(ValueError):
            objectives.create(self.store, "  ", "outcome")
        with self.assertRaises(ValueError):
            objectives.create(self.store, "title", "")

    def test_open_question_blocks_readiness(self):
        obj = objectives.create(
            self.store, "t", "o", open_questions=["which database?"], verification=["it runs"]
        )
        self.assertFalse(obj.is_ready)
        self.assertIn("unresolved question", obj.readiness_gaps()[0])

    def test_missing_verification_blocks_readiness(self):
        obj = objectives.create(self.store, "t", "o")
        self.assertFalse(obj.is_ready)
        self.assertIn("unfalsifiable", obj.readiness_gaps()[0])

    def test_resolving_question_keeps_answer_as_assumption(self):
        obj = objectives.create(
            self.store, "t", "o", open_questions=["which db?"], verification=["it runs"]
        )
        obj = objectives.resolve_question(self.store, obj.id, "which db?", "sqlite", actor="user")
        self.assertTrue(obj.is_ready)
        self.assertEqual(obj.assumptions, ["which db? -> sqlite"])
        self.assertEqual(objectives.get(self.store, obj.id).assumptions, ["which db? -> sqlite"])

    def test_unknown_question_rejected(self):
        obj = objectives.create(self.store, "t", "o")
        with self.assertRaises(ValueError):
            objectives.resolve_question(self.store, obj.id, "nope", "x", actor="claude")

    def test_creation_is_audited(self):
        obj = objectives.create(self.store, "t", "o")
        history = events.for_target(self.store, obj.id)
        self.assertEqual(history[0]["kind"], "objective.create")


if __name__ == "__main__":
    unittest.main()
