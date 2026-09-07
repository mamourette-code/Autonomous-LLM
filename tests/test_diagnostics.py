import importlib
import unittest

from jarvis import diagnostics, memory, retrieval, tasks
from jarvis.store import open_store


class CapabilityHonestyTest(unittest.TestCase):
    """Protocol s57: never claim a capability that has not been verified."""

    def test_every_claimed_capability_resolves_to_real_code(self):
        for capability, path in diagnostics.IMPLEMENTED.items():
            module_name, attr = path.split(":")
            module = importlib.import_module(module_name)
            self.assertTrue(
                hasattr(module, attr), f"{capability} claims {path}, which does not exist"
            )

    def test_claimed_and_disclaimed_capabilities_do_not_overlap(self):
        self.assertEqual(
            set(diagnostics.IMPLEMENTED) & set(diagnostics.NOT_IMPLEMENTED), set()
        )


class MetricsTest(unittest.TestCase):
    def setUp(self):
        self.store = open_store(":memory:")

    def test_unmeasured_rates_are_none_not_zero(self):
        m = diagnostics.metrics(self.store)
        self.assertIsNone(m["tasks"]["success_rate"])
        self.assertIsNone(m["verification"]["independence_rate"])
        self.assertIsNone(m["events"]["error_rate"])

    def test_verified_and_completed_are_counted_separately(self):
        done = tasks.create(self.store, "done")
        tasks.transition(self.store, done.id, tasks.IN_PROGRESS)
        tasks.transition(self.store, done.id, tasks.COMPLETED)

        checked = tasks.create(self.store, "checked")
        tasks.transition(self.store, checked.id, tasks.IN_PROGRESS)
        tasks.transition(self.store, checked.id, tasks.COMPLETED)
        tasks.verify(self.store, checked.id, method="rerun", evidence="matches", verifier="critic")

        m = diagnostics.metrics(self.store)["tasks"]
        self.assertEqual(m["success_rate"], 1.0)
        self.assertEqual(m["verified_success_rate"], 0.5)

    def test_recovery_rate_tracks_failures_that_later_succeeded(self):
        task = tasks.create(self.store, "flaky")
        tasks.transition(self.store, task.id, tasks.IN_PROGRESS)
        tasks.transition(self.store, task.id, tasks.FAILED, failure_class="tool_failure")
        tasks.transition(self.store, task.id, tasks.IN_PROGRESS)
        tasks.transition(self.store, task.id, tasks.COMPLETED)
        m = diagnostics.metrics(self.store)["tasks"]
        self.assertEqual(m["recovery_rate"], 1.0)
        self.assertEqual(m["failure_classes"], {"tool_failure": 1})

    def test_memory_metrics_reflect_actual_use(self):
        memory.remember(self.store, kind="semantic", title="Deploy target",
                        body="Production runs in eu-west")
        memory.remember(self.store, kind="semantic", title="Coffee", body="Flat white")
        retrieval.retrieve(self.store, "where does production run")
        m = diagnostics.metrics(self.store)["memory"]
        self.assertEqual(m["live"], 2)
        self.assertEqual(m["ever_retrieved"], 1)
        self.assertEqual(m["utilization"], 0.5)
        self.assertEqual(m["retrievals"], 1)
        self.assertEqual(m["empty_retrieval_rate"], 0.0)

    def test_empty_retrievals_are_measured(self):
        memory.remember(self.store, kind="semantic", title="Coffee", body="Flat white")
        retrieval.retrieve(self.store, "kubernetes ingress")
        self.assertEqual(diagnostics.metrics(self.store)["memory"]["empty_retrieval_rate"], 1.0)

    def test_window_limits_event_metrics(self):
        tasks.create(self.store, "t")
        self.assertGreater(diagnostics.metrics(self.store, window_days=1)["events"]["total"], 0)
        self.assertEqual(diagnostics.metrics(self.store, window_days=0)["events"]["total"], 0)


class HealthTest(unittest.TestCase):
    def setUp(self):
        self.store = open_store(":memory:")

    def test_fresh_store_is_healthy(self):
        report = diagnostics.health(self.store)
        self.assertEqual(report["status"], "ok")
        self.assertTrue(all(c["status"] == "ok" for c in report["checks"]))

    def test_blocked_work_warns(self):
        task = tasks.create(self.store, "t")
        tasks.transition(self.store, task.id, tasks.BLOCKED)
        report = diagnostics.health(self.store)
        self.assertEqual(report["status"], "warn")
        self.assertEqual(self._check(report, "task_flow")["status"], "warn")

    def test_writes_that_bypass_the_audit_log_fail_the_check(self):
        self.store.execute(
            "INSERT INTO tasks (id,title,status,priority,created_at,updated_at)"
            " VALUES ('tsk_smuggled','snuck in','pending',3,'2020-01-01','2020-01-01')"
        )
        self.store.commit()
        report = diagnostics.health(self.store)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(self._check(report, "observability")["status"], "fail")

    def test_unresolved_conflicts_warn(self):
        memory.remember(self.store, kind="semantic", title="Port", body="listens on 8080")
        memory.remember(self.store, kind="semantic", title="Port", body="listens on 9090")
        retrieval.retrieve(self.store, "port")
        self.assertEqual(
            self._check(diagnostics.health(self.store), "memory_consistency")["status"], "warn"
        )

    def _check(self, report, name):
        return next(c for c in report["checks"] if c["name"] == name)


class SelfReportTest(unittest.TestCase):
    def test_report_names_gaps_as_well_as_capabilities(self):
        store = open_store(":memory:")
        report = diagnostics.self_report(store)
        self.assertIn("ranked_retrieval", report["implemented"])
        self.assertIn("agent_orchestration", report["not_implemented"])
        self.assertIn("health", report)
        self.assertIn("metrics", report)


if __name__ == "__main__":
    unittest.main()
