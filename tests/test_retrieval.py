import unittest

from jarvis import memory, retrieval
from jarvis.store import open_store


class RankingTest(unittest.TestCase):
    def setUp(self):
        self.store = open_store(":memory:")

    def test_regression_query_matches_morphological_variants(self):
        """Regression: 'run the tests' must find 'Test runner'.

        Exact token matching missed this during the first CLI walkthrough,
        because 'tests' != 'test' and 'runner' != 'run'.
        """
        memory.remember(
            self.store, kind="semantic", title="Test runner",
            body="This repo uses python unittest; pytest is unavailable",
            source="pip list", source_class="sourced_knowledge", confidence=0.9,
        )
        result = retrieval.retrieve(self.store, "how do I run the tests here")
        self.assertEqual(len(result.hits), 1)
        self.assertGreater(result.hits[0].score, 0.1)

    def test_irrelevant_memory_is_not_returned(self):
        memory.remember(
            self.store, kind="semantic", title="Coffee order", body="Flat white, no sugar"
        )
        result = retrieval.retrieve(self.store, "kubernetes ingress configuration")
        self.assertEqual(result.hits, [])
        self.assertEqual(result.considered, 1)

    def test_sourced_knowledge_outranks_an_equally_relevant_hypothesis(self):
        memory.remember(
            self.store, kind="semantic", title="Database engine",
            body="The service stores data in postgres", source="schema.sql",
            source_class="sourced_knowledge", confidence=0.95,
        )
        memory.remember(
            self.store, kind="semantic", title="Database engine",
            body="The service probably stores data in postgres",
            source_class="hypothesis", confidence=0.4,
        )
        hits = retrieval.retrieve(self.store, "which database engine stores the data").hits
        self.assertEqual(hits[0].memory.source_class, "sourced_knowledge")

    def test_hits_explain_their_score(self):
        memory.remember(self.store, kind="project", title="Deploy pipeline",
                        body="Deploys run from CI", project="alpha", entities=["ci"])
        hit = retrieval.retrieve(
            self.store, "deploy pipeline", project="alpha", entities=["ci"]
        ).hits[0]
        self.assertEqual(hit.components["project"], 1.0)
        self.assertEqual(hit.components["entity"], 1.0)
        self.assertGreater(hit.components["lexical"], 0)

    def test_retrieval_records_use_and_is_audited(self):
        mem = memory.remember(self.store, kind="semantic", title="Deploy target",
                              body="Production runs in eu-west")
        retrieval.retrieve(self.store, "where does production run")
        self.assertEqual(memory.get(self.store, mem.id).access_count, 1)
        from jarvis import events
        self.assertEqual(events.recent(self.store)[0]["kind"], "memory.retrieve")

    def test_stale_memory_is_penalised_and_flagged(self):
        mem = memory.remember(self.store, kind="semantic", title="Runtime version",
                              body="The runtime is python 3.9", decay=0.5)
        self.store.execute(
            "UPDATE memories SET review_after='2000-01-01T00:00:00+00:00' WHERE id=?", (mem.id,)
        )
        self.store.commit()
        hit = retrieval.retrieve(self.store, "which python runtime version").hits[0]
        self.assertTrue(any("review date" in n for n in hit.notes))
        self.assertLess(hit.effective_confidence, mem.confidence)

    def test_archived_memory_is_not_retrieved(self):
        old = memory.remember(self.store, kind="semantic", title="Port", body="Service on 8080")
        new = memory.remember(self.store, kind="semantic", title="Port", body="Service on 9090")
        memory.supersede(self.store, old.id, new.id)
        self.assertEqual(retrieval.retrieve(self.store, "which port").ids(), [new.id])


class ConflictTest(unittest.TestCase):
    def setUp(self):
        self.store = open_store(":memory:")

    def test_opposite_polarity_is_flagged_not_merged(self):
        memory.remember(self.store, kind="semantic", title="Test runner",
                        body="This repo uses pytest")
        memory.remember(self.store, kind="semantic", title="Test runner",
                        body="This repo does not use pytest")
        result = retrieval.retrieve(self.store, "test runner for the repo")
        self.assertEqual(len(result.conflicts), 1)
        self.assertEqual(len(result.hits), 2, "neither side may be silently discarded")
        for hit in result.hits:
            self.assertTrue(any("conflicts with" in n for n in hit.notes))
            self.assertLess(hit.effective_confidence, hit.memory.confidence)

    def test_differing_values_are_flagged(self):
        memory.remember(self.store, kind="semantic", title="Service port",
                        body="The service listens on 8080")
        memory.remember(self.store, kind="semantic", title="Service port",
                        body="The service listens on 9090")
        conflicts = retrieval.retrieve(self.store, "service port").conflicts
        self.assertEqual(len(conflicts), 1)
        self.assertIn("different stated values", conflicts[0][2])

    def test_agreeing_memories_are_not_flagged(self):
        memory.remember(self.store, kind="semantic", title="Service port",
                        body="The service listens on 8080")
        memory.remember(self.store, kind="semantic", title="Service port",
                        body="Port 8080 is the listening port")
        self.assertEqual(retrieval.retrieve(self.store, "service port").conflicts, [])

    def test_conflicts_persist_once_and_can_be_resolved(self):
        memory.remember(self.store, kind="semantic", title="Service port",
                        body="The service listens on 8080")
        memory.remember(self.store, kind="semantic", title="Service port",
                        body="The service listens on 9090")
        retrieval.retrieve(self.store, "service port")
        retrieval.retrieve(self.store, "service port")
        conflicts = retrieval.open_conflicts(self.store)
        self.assertEqual(len(conflicts), 1, "the same conflict must not be recorded twice")

        retrieval.resolve_conflict(
            self.store, conflicts[0]["id"], "both_contextual",
            actor="user", note="different environments",
        )
        self.assertEqual(retrieval.open_conflicts(self.store), [])

    def test_unknown_resolution_rejected(self):
        with self.assertRaises(ValueError):
            retrieval.resolve_conflict(self.store, "cfl_x", "whatever")


if __name__ == "__main__":
    unittest.main()
