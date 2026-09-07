import sqlite3
import unittest

from jarvis.store import SCHEMA_VERSION, new_id, open_store, utcnow


class StoreTest(unittest.TestCase):
    def test_migrations_apply_and_are_idempotent(self):
        store = open_store(":memory:")
        self.assertEqual(store.schema_version(), SCHEMA_VERSION)
        self.assertEqual(store.migrate(), SCHEMA_VERSION)

    def test_expected_tables_exist(self):
        store = open_store(":memory:")
        names = {
            r[0] for r in store.query("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in ("objectives", "tasks", "task_deps", "verifications",
                      "memories", "memory_conflicts", "events", "sessions"):
            self.assertIn(table, names)

    def test_foreign_keys_enforced(self):
        store = open_store(":memory:")
        with self.assertRaises(sqlite3.IntegrityError):
            store.execute(
                "INSERT INTO task_deps (task_id, depends_on) VALUES ('nope','also_nope')"
            )
            store.commit()

    def test_ids_are_prefixed_and_unique(self):
        ids = {new_id("tsk") for _ in range(500)}
        self.assertEqual(len(ids), 500)
        self.assertTrue(all(i.startswith("tsk_") for i in ids))

    def test_timestamps_are_utc_iso(self):
        self.assertTrue(utcnow().endswith("+00:00"))


if __name__ == "__main__":
    unittest.main()
