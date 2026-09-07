import importlib
import unittest

from jarvis import diagnostics, memory, retrieval, tasks
from jarvis.store import open_store


class CapabilityHonestyTest(unittest.TestCase):
    """Protocol s57: never claim a capability that has not been verified."""

    def test_every_claimed_capability_resolves_to_real_code(self):
        for capability, path in diagnostics.IMPLEMENTED.items():
            module_name, dotted = path.split(":")
            target = importlib.import_module(module_name)
            for attr in dotted.split("."):
                self.assertTrue(
                    hasattr(target, attr),
                    f"{capability} claims {path}, which does not exist",
                )
                target = getattr(target, attr)
            # A claim may point at a callable or at a definition it enforces
            # (the failure taxonomy, say) - but never at something empty.
            self.assertTrue(
                callable(target) or bool(target),
                f"{capability} claims {path}, which is empty",
            )

    def test_claimed_and_disclaimed_capabilities_do_not_overlap(self):
        self.assertEqual(
            set(diagnostics.IMPLEMENTED) & set(diagnostics.NOT_IMPLEMENTED), set()
        )

    def test_every_claimed_capability_actually_functions(self):
        """Existence is not function. Each claim is exercised end to end here,
        so `doctor` cannot advertise a capability that merely has a name."""
        from jarvis import events, memory, objectives, retrieval, session, store as store_mod

        exercised = {}
        s = open_store(":memory:")

        exercised["persistent_state"] = s.schema_version() == store_mod.SCHEMA_VERSION

        events.record(s, "probe", "auditor", permission="read")
        exercised["audit_log"] = events.recent(s)[0]["kind"] == "probe"

        try:
            with events.action(s, "probe.fail", "auditor"):
                raise RuntimeError("x")
        except RuntimeError:
            pass
        exercised["traced_actions"] = events.recent(s)[0]["outcome"] == "failed"

        obj = objectives.create(
            s, "probe", "an outcome", actor="auditor", open_questions=["q?"]
        )
        exercised["objective_representation"] = obj.is_ready is False and bool(obj.readiness_gaps())

        first = tasks.create(s, "first", actor="auditor")
        second = tasks.create(s, "second", actor="auditor", depends_on=[first.id])
        exercised["dependency_gating"] = tasks.unmet_dependencies(s, second.id) == [first.id]

        tasks.transition(s, first.id, tasks.IN_PROGRESS, actor="auditor")
        tasks.transition(s, first.id, tasks.COMPLETED, actor="auditor")
        exercised["task_state_machine"] = tasks.get(s, first.id).status == tasks.COMPLETED

        tasks.verify(s, first.id, method="probe", evidence="checked", verifier="critic")
        record = tasks.verifications(s, first.id)[0]
        exercised["verification_records"] = (
            tasks.get(s, first.id).status == tasks.VERIFIED and record["independent"] == 1
        )

        try:
            tasks.transition(s, second.id, tasks.FAILED, actor="auditor")
            exercised["failure_classification"] = False
        except ValueError:
            tasks.transition(
                s, second.id, tasks.FAILED, actor="auditor", failure_class="tool_failure"
            )
            exercised["failure_classification"] = (
                tasks.get(s, second.id).failure_class == "tool_failure"
            )

        mem = memory.remember(
            s, kind="semantic", title="Probe port", body="listens on 8080",
            source="probe", source_class="sourced_knowledge", confidence=0.9, decay=0.9,
        )
        exercised["memory_write"] = memory.get(s, mem.id) is not None

        low = memory.assess(
            s, kind="episodic", title="trivial", body="nothing worth keeping",
            confidence=0.1, durability=0.1, utility=0.1,
        )
        exercised["memory_governance"] = low.decision == memory.SKIP

        exercised["knowledge_decay"] = mem.review_after is not None
        s.execute(
            "UPDATE memories SET review_after = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (mem.id,),
        )
        s.commit()
        exercised["knowledge_decay"] = bool(memory.stale(s))

        memory.remember(s, kind="semantic", title="Probe port", body="listens on 9090")
        result = retrieval.retrieve(s, "which probe port is used", actor="auditor")
        exercised["ranked_retrieval"] = len(result.hits) >= 1 and all(
            h.components for h in result.hits
        )
        exercised["contradiction_detection"] = len(result.conflicts) == 1

        m = diagnostics.metrics(s)
        exercised["metrics"] = m["tasks"]["total"] == 2 and m["verification"]["records"] == 1
        exercised["health_checks"] = diagnostics.health(s)["status"] in ("ok", "warn", "fail")

        try:
            objectives.set_status(s, obj.id, "achieved", actor="auditor")
            exercised["authority_boundary"] = False
        except objectives.AuthorityError:
            exercised["authority_boundary"] = objectives.get(s, obj.id).status == "open"

        try:
            memory.remember(s, kind="user", title="creds", body="password: hunter2")
            exercised["sensitivity_gate"] = False
        except memory.SensitiveContentError:
            exercised["sensitivity_gate"] = True

        try:
            with s.transaction():
                s.execute("INSERT INTO sessions (id, started_at) VALUES ('probe','t')")
                raise RuntimeError("rollback")
        except RuntimeError:
            pass
        exercised["atomic_writes"] = (
            s.one("SELECT COUNT(*) FROM sessions WHERE id = 'probe'")[0] == 0
        )

        sid = session.boot(s, actor="auditor")["session_id"]
        report = session.close(s, sid, summary="probe", actor="auditor")
        exercised["session_boot_close"] = report["session_id"] == sid

        unproven = sorted(k for k, v in exercised.items() if not v)
        self.assertEqual(unproven, [], f"claimed but not demonstrated: {unproven}")

        missing = sorted(set(diagnostics.IMPLEMENTED) - set(exercised))
        self.assertEqual(missing, [], f"claimed capabilities never exercised: {missing}")


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
