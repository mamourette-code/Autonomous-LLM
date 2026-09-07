import unittest

from jarvis import memory
from jarvis.store import open_store


class GovernanceTest(unittest.TestCase):
    def setUp(self):
        self.store = open_store(":memory:")

    def test_valuable_candidate_is_stored(self):
        assessment = memory.assess(
            self.store, kind="semantic", title="Deploy target",
            body="Production runs on the eu-west cluster",
            confidence=0.9, durability=0.8, utility=0.8,
        )
        self.assertEqual(assessment.decision, memory.STORE)
        self.assertTrue(assessment.should_store)

    def test_low_value_candidate_is_skipped(self):
        assessment = memory.assess(
            self.store, kind="episodic", title="Ran ls", body="Listed a directory once",
            confidence=0.3, durability=0.1, utility=0.1, context_specificity=0.9,
        )
        self.assertEqual(assessment.decision, memory.SKIP)
        self.assertIn("below threshold", assessment.reasons[0])

    def test_duplicate_is_detected_and_skipped(self):
        memory.remember(
            self.store, kind="semantic", title="Deploy target",
            body="Production runs on the eu-west cluster", confidence=0.9,
        )
        assessment = memory.assess(
            self.store, kind="semantic", title="Deploy target",
            body="Production runs on the eu-west cluster",
            confidence=0.9, durability=0.8, utility=0.8,
        )
        self.assertLess(assessment.scores["uniqueness"], 0.35)
        self.assertEqual(assessment.decision, memory.SKIP)
        self.assertIsNotNone(assessment.nearest_id)

    def test_sensitive_content_goes_to_the_user(self):
        assessment = memory.assess(
            self.store, kind="user", title="Login", body="password: hunter2",
            confidence=1.0, durability=1.0, utility=1.0,
        )
        self.assertEqual(assessment.decision, memory.REFER_TO_USER)
        self.assertFalse(assessment.should_store)

    def test_score_bounds_are_validated(self):
        with self.assertRaises(ValueError):
            memory.assess(self.store, kind="semantic", title="t", body="b", confidence=1.5)

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            memory.assess(self.store, kind="vibes", title="t", body="b")


class WriteTest(unittest.TestCase):
    def setUp(self):
        self.store = open_store(":memory:")

    def test_sourced_knowledge_requires_a_source(self):
        with self.assertRaises(ValueError):
            memory.remember(
                self.store, kind="semantic", title="t", body="b",
                source_class="sourced_knowledge",
            )

    def test_epistemic_class_is_validated(self):
        with self.assertRaises(ValueError):
            memory.remember(self.store, kind="semantic", title="t", body="b", source_class="truth")

    def test_decay_sets_a_review_date_and_zero_decay_does_not(self):
        volatile = memory.remember(
            self.store, kind="semantic", title="Latest release", body="version 4.2", decay=0.9
        )
        durable = memory.remember(
            self.store, kind="user", title="Preferred name", body="Dirk", decay=0.0
        )
        self.assertIsNotNone(volatile.review_after)
        self.assertIsNone(durable.review_after)

    def test_stale_memories_are_reported(self):
        mem = memory.remember(self.store, kind="semantic", title="t", body="b", decay=0.5)
        self.store.execute(
            "UPDATE memories SET review_after = '2000-01-01T00:00:00+00:00' WHERE id = ?", (mem.id,)
        )
        self.store.commit()
        stale = memory.stale(self.store)
        self.assertEqual([m.id for m in stale], [mem.id])
        self.assertTrue(stale[0].is_stale)

    def test_supersede_preserves_the_old_record(self):
        old = memory.remember(self.store, kind="semantic", title="Port", body="Service runs on 8080")
        new = memory.remember(self.store, kind="semantic", title="Port", body="Service runs on 9090")
        memory.supersede(self.store, old.id, new.id, note="port changed")
        refreshed = memory.get(self.store, old.id)
        self.assertEqual(refreshed.archived, 1)
        self.assertEqual(refreshed.superseded_by, new.id)
        self.assertEqual([m.id for m in memory.all_live(self.store)], [new.id])

    def test_touch_counts_use(self):
        mem = memory.remember(self.store, kind="semantic", title="t", body="b")
        memory.touch(self.store, mem.id)
        memory.touch(self.store, mem.id)
        self.assertEqual(memory.get(self.store, mem.id).access_count, 2)


class TokenizerTest(unittest.TestCase):
    def test_stemming_folds_plurals_and_verb_forms(self):
        self.assertEqual(memory._stem("tests"), memory._stem("test"))
        self.assertEqual(memory._stem("runner"), memory._stem("running"))
        self.assertEqual(memory._stem("verified"), memory._stem("verifies"))

    def test_stemming_leaves_native_double_letters_alone(self):
        self.assertEqual(memory._stem("class"), "class")
        self.assertEqual(memory._stem("classes"), "class")

    def test_stopwords_removed(self):
        self.assertEqual(memory.tokenize("the and of a"), set())


if __name__ == "__main__":
    unittest.main()


class ContradictionGovernanceTest(unittest.TestCase):
    """Regression: governance must not suppress disagreeing information.

    Found during a CLI walkthrough - a second memory saying production runs in
    a different region was skipped as a near-duplicate (67% token overlap), so
    the system silently kept its older belief. Protocol s50 forbids exactly
    that, and the conflict can only be surfaced if both sides are stored.
    """

    def setUp(self):
        self.store = open_store(":memory:")
        self.first = memory.remember(
            self.store, kind="semantic", title="Deploy region",
            body="Production runs in eu-west-1", source="terraform/main.tf",
            source_class="sourced_knowledge", confidence=0.9,
        )

    def test_contradicting_memory_is_stored_despite_overlap(self):
        assessment = memory.assess(
            self.store, kind="semantic", title="Deploy region",
            body="Production runs in us-east-2",
            confidence=0.7, durability=0.7, utility=0.8,
        )
        self.assertEqual(assessment.decision, memory.STORE)
        self.assertGreaterEqual(assessment.scores["uniqueness"], 0.9)
        self.assertTrue(any("contradicts" in r for r in assessment.reasons))

    def test_polarity_flip_is_stored(self):
        memory.remember(self.store, kind="semantic", title="Test runner",
                        body="This repo uses pytest")
        assessment = memory.assess(
            self.store, kind="semantic", title="Test runner",
            body="This repo does not use pytest",
            confidence=0.8, durability=0.7, utility=0.8,
        )
        self.assertEqual(assessment.decision, memory.STORE)

    def test_true_duplicate_is_still_skipped(self):
        assessment = memory.assess(
            self.store, kind="semantic", title="Deploy region",
            body="Production runs in eu-west-1",
            confidence=0.9, durability=0.8, utility=0.8,
        )
        self.assertEqual(assessment.decision, memory.SKIP)
        self.assertTrue(any("already known" in r for r in assessment.reasons))
        self.assertIsNone(assessment.supersede_candidate)

    def test_overlapping_revision_suggests_supersede_instead_of_vanishing(self):
        """Heavy but not total overlap, no detectable disagreement: likely a revision."""
        assessment = memory.assess(
            self.store, kind="semantic", title="Deploy region",
            body="Production runs in eu-west-1, primary",
            confidence=0.9, durability=0.8, utility=0.8,
        )
        self.assertEqual(assessment.decision, memory.SKIP)
        self.assertEqual(assessment.supersede_candidate, self.first.id)
        self.assertTrue(any("supersede" in r for r in assessment.reasons))

    def test_materially_different_body_is_simply_stored(self):
        """Enough new content that it is neither duplicate nor revision."""
        assessment = memory.assess(
            self.store, kind="semantic", title="Deploy region",
            body="Production runs behind the shared load balancer in the primary cluster",
            confidence=0.9, durability=0.8, utility=0.8,
        )
        self.assertEqual(assessment.decision, memory.STORE)
        self.assertIsNone(assessment.supersede_candidate)

    def test_unrelated_memory_is_not_treated_as_a_conflict(self):
        assessment = memory.assess(
            self.store, kind="semantic", title="Coffee order", body="Flat white, no sugar",
            confidence=0.9, durability=0.8, utility=0.8,
        )
        self.assertEqual(assessment.decision, memory.STORE)
        self.assertFalse(any("contradicts" in r for r in assessment.reasons))

    def test_low_confidence_contradiction_still_beats_the_value_threshold(self):
        """A weak claim that disagrees must not be dropped for being weak.

        Second regression from the same walkthrough: after uniqueness was
        waived, a hypothesis-grade contradiction was still skipped because its
        own low confidence put it under the value threshold - silently settling
        the disagreement in favour of the older belief.
        """
        assessment = memory.assess(
            self.store, kind="semantic", title="Deploy region",
            body="Production runs in us-east-2",
            confidence=0.4, durability=0.7, utility=0.8,
        )
        self.assertEqual(assessment.decision, memory.STORE)
        self.assertLess(assessment.expected_value, 0.18)
        self.assertTrue(any("waived" in r for r in assessment.reasons))

    def test_sensitive_content_still_wins_over_a_conflict_waiver(self):
        assessment = memory.assess(
            self.store, kind="semantic", title="Deploy region",
            body="Production runs in us-east-2 with password: hunter2",
            confidence=0.9, durability=0.8, utility=0.8,
        )
        self.assertEqual(assessment.decision, memory.REFER_TO_USER)
