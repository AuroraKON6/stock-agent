"""R23 continuation (KONM-96): trace, reconciliation, metrics, alert
candidates, structured log and redaction over the authoritative ledgers.

The frozen synthetic case is the same one the container drill seeds
(``scripts/r23_observability_drill.py``), so the offline suite and the
container run assert the identical expectations.
"""
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.observability import (
    build_alert_candidates,
    build_observability_report,
    build_observability_status,
    build_reconciliation,
    build_trace,
    emit_observability_log,
    observability_settings,
    redact,
)
from stock_agent.ops_view import build_ops_view
from stock_agent.task_runtime import TaskStore

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "r23_observability_drill", _ROOT / "scripts" / "r23_observability_drill.py")
drill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drill)

SECRET = drill.SECRETS


def _status(technical=None, business=None):
    technical = technical or {}
    return {"technical": {"queue_depth": 0, "oldest_queued_age_seconds": 0, "inflight_leases": 0,
                          "stale_leases": 0, "reconciliations_waiting": 0,
                          "invocation_attempts": {"succeeded": 0, "failed_known": 0,
                                                  "outcome_unknown": 0, "reconciled": 0,
                                                  "outstanding": 0}, **technical},
            "business": {"blocked_tasks": 0, "gate_counts": {"budget": 0}, **(business or {})}}


class FrozenCaseTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = drill.drill_config()
        self.store = TaskStore(self.root / drill.DB_REL)
        self.addCleanup(self.store.close)
        self.seeded = drill.seed_case(self.store, self.root, self.config)


class TraceTests(FrozenCaseTestCase):
    def test_one_chain_case_intent_platform_query_attempt(self):
        trace = build_trace(self.store, self.seeded["task_id"], config=self.config)
        self.assertEqual(trace["business_identity"]["case_id"], drill.CASE_ID)
        self.assertEqual(trace["business_identity"]["business_intent_id"], drill.INTENT_ID)
        self.assertEqual([r["run_id"] for r in trace["platform_runs"]],
                         self.seeded["platform_runs"])
        # The business intent is never reported as, or replaced by, a platform run id.
        self.assertNotIn(drill.INTENT_ID, [r["run_id"] for r in trace["platform_runs"]])
        self.assertEqual([a["round_index"] for a in trace["attempts"]], [1, 2, 3, 4])

    def test_retry_replay_and_unknown_are_distinguishable(self):
        trace = build_trace(self.store, self.seeded["task_id"], config=self.config)
        self.assertEqual([a["state"] for a in trace["attempts"]],
                         ["failed_known", "reconciled", "outcome_unknown", "succeeded"])
        self.assertEqual([a["reconciled"] for a in trace["attempts"]], [False, True, False, False])
        self.assertEqual(trace["platform_run_count"], 2, "one replayed run on the same task")
        self.assertEqual(trace["coverage"]["outcomes"],
                         ["reconciled", "retry", "success", "unknown"])
        self.assertEqual(sorted(q["request_id"] for q in trace["queries"]),
                         ["req-r23-ok", "req-r23-unknown"])

    def test_trace_without_ledger_reports_it(self):
        trace = build_trace(self.store, self.seeded["task_id"])
        self.assertEqual(trace["queries"], [])
        self.assertFalse(trace["coverage"]["query_ledger_available"])


class ReconciliationTests(FrozenCaseTestCase):
    def test_view_matches_authoritative_ledgers(self):
        recon = build_reconciliation(self.store, config=self.config)
        counts = drill.ledger_counts(self.store)
        self.assertEqual(sum(recon["invocations"]["states"].values()), counts["attempts"])
        self.assertEqual(recon["authoritative"]["outbox_events"], counts["outbox_events"])
        self.assertEqual(recon["invocations"]["known_failures"], 1)
        self.assertEqual(recon["invocations"]["unknown_open"], 1)
        self.assertEqual(recon["invocations"]["unknown_reconciled"], 1)
        self.assertEqual(recon["invocations"]["reconciliations_waiting"], 1)
        self.assertEqual(recon["queries"]["outcome_counts"], {"ok": 1, "outcome_unknown": 1})
        self.assertEqual(recon["queries"]["intents_without_result"], 1)
        self.assertEqual(recon["rejections"]["query_artifact_rejections"], 1)
        self.assertEqual(recon["rejections"]["budget_blocked_tasks"], 1)
        self.assertEqual(recon["evidence"]["missing_evidence_blocked_tasks"], 1)
        self.assertTrue(recon["authoritative"]["event_scan"]["complete"])

    def test_usage_split_replay_estimated_real(self):
        usage = build_reconciliation(self.store, config=self.config)["usage"]
        self.assertEqual(usage["replay"]["tasks_in_replay_mode"], len(self.store.list_tasks()))
        self.assertEqual(usage["estimated"]["status"], "unknown")
        self.assertEqual(usage["real_supplier"]["status"], "unknown")
        self.assertTrue({"estimated_usage", "real_supplier_usage"} <= set(
            build_reconciliation(self.store, config=self.config)["unknown_items"]))

    def test_without_ledger_unknown_stays_visible(self):
        recon = build_reconciliation(self.store)
        self.assertFalse(recon["queries"]["ledger_available"])
        self.assertIn("data_query_ledger", recon["unknown_items"])


class LayeringAndAlertTests(FrozenCaseTestCase):
    def test_technical_and_business_reported_together(self):
        report = build_observability_report(self.store, config=self.config,
                                            task_id=self.seeded["task_id"])
        technical = report["status"]["technical"]
        self.assertEqual(technical["inflight_leases"], 1)
        self.assertEqual(technical["stale_leases"], 1)
        self.assertEqual(report["status"]["business"]["blocked_tasks"], 4)
        self.assertEqual(report["status"]["business"]["gate_counts"],
                         {"budget": 1, "evidence_quality": 1, "invocation_outcome_unknown": 1,
                          "governance": 1, "other": 0})

    def test_machine_anomalies_technical_gates_business(self):
        report = build_observability_report(self.store, config=self.config)
        alerts = report["alert_candidates"]
        layers = {c["rule"]: c["layer"] for c in alerts["candidates"] if c["candidate"]}
        self.assertEqual(sorted(r for r, layer in layers.items() if layer == "technical"),
                         ["query_outcome_unknown", "queue_oldest_age",
                          "reconciliations_waiting", "stale_leases"])
        self.assertEqual(layers["blocked_tasks"], "business")
        self.assertFalse(alerts["dispatched"])
        self.assertEqual(alerts["dispatch"], "NOT_RUN")

    def test_business_gate_is_never_a_page(self):
        alerts = build_alert_candidates(_status(business={"blocked_tasks": 9}))
        gate = next(c for c in alerts["candidates"] if c["rule"] == "blocked_tasks")
        self.assertEqual((gate["layer"], gate["severity"]), ("business", "info"))
        self.assertFalse(alerts["enabled"], "candidates are off unless configured on")

    def test_disabled_by_default_and_thresholds(self):
        self.assertFalse(observability_settings({})["alerts_enabled"])
        self.assertFalse(observability_settings({})["sampling_enabled"])
        settings = observability_settings({"observability": {"alerts": {
            "enabled": True, "thresholds": {"stale_leases": 2, "bogus": 1}}}})
        self.assertTrue(settings["alerts_enabled"])
        self.assertEqual(settings["thresholds"]["stale_leases"], 2)
        self.assertFalse(settings["config_valid"], "an unknown threshold is reported, not applied")
        self.assertEqual(observability_settings({"observability": "garbage"})["config_valid"], False)
        firing = build_alert_candidates(_status({"stale_leases": 1}),
                                        thresholds={"stale_leases": 2})
        self.assertEqual(firing["triggered"], [])


class MetricsTests(unittest.TestCase):
    def test_every_metric_is_tagged_with_evidence(self):
        from stock_agent.observability import build_metrics

        rows = build_metrics(_status({"stale_leases": 1}), {"usage": {
            "ledger": {"model_calls_today": 0},
            "replay": {"tasks_in_replay_mode": 7}}})
        self.assertTrue(all(row["evidence"] in ("ledger", "replay", "unknown") for row in rows))
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(by_name["stock_agent_tasks_replay_mode"]["evidence"], "replay")
        self.assertEqual(by_name["stock_agent_usage_real_supplier"]["value"], "unknown")


class LogAndRedactionTests(FrozenCaseTestCase):
    def test_redaction_patterns(self):
        # Credential-shaped values are masked; the body marker is not a
        # credential and is kept out of views structurally (see the report test).
        for name, value in SECRET.items():
            if name != "body":
                self.assertNotIn(value, redact("x {0} y".format(value), 400), name)
        self.assertEqual(redact("task_abc attempt att_1 run 1234"), "task_abc attempt att_1 run 1234")

    def test_report_carries_no_secret_or_body(self):
        report = build_observability_report(self.store, config=self.config,
                                            task_id=self.seeded["task_id"])
        blob = json.dumps(report, ensure_ascii=False, default=str)
        for name, value in SECRET.items():
            self.assertNotIn(value, blob, name)

    def test_ops_view_error_is_masked(self):
        view = build_ops_view(self.config, store=self.store)
        errors = " ".join(f["error"] for f in view["recent_failures"])
        self.assertNotIn(SECRET["bearer"], errors)
        self.assertIn("[REDACTED]", errors)

    def test_structured_log_redacted_and_off_by_default(self):
        sink = io.StringIO()
        self.assertIsNone(emit_observability_log("x", {"a": 1}, config={}, sink=sink))
        self.assertEqual(sink.getvalue(), "")
        on = {"observability": {"sampling": {"enabled": True}}}
        record = emit_observability_log("r23", {"token": SECRET["api_key"],
                                                "header": SECRET["bearer"]}, config=on, sink=sink)
        self.assertIsNotNone(record)
        self.assertNotIn(SECRET["api_key"], sink.getvalue())
        self.assertEqual(json.loads(sink.getvalue())["event"], "r23")

    def test_business_chain_with_sampling_off_keeps_audit(self):
        result = drill.business_with_sampling_off(self.store, drill.drill_config(sampling=False))
        self.assertTrue(result["ok"], result)


class ZeroSideEffectsTests(FrozenCaseTestCase):
    def test_repeated_reads_change_no_ledger(self):
        before = drill.ledger_counts(self.store)
        for _ in range(4):
            build_observability_status(self.store)
            build_reconciliation(self.store, config=self.config)
            build_observability_report(self.store, config=self.config,
                                       task_id=self.seeded["task_id"])
        self.assertEqual(drill.ledger_counts(self.store), before)


if __name__ == "__main__":
    unittest.main()
