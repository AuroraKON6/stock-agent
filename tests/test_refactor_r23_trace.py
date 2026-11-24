"""R23 continuation (KONM-96): trace, reconciliation, metrics, alert
candidates, structured log and fail-closed configuration.

The frozen synthetic case is the one the container drill seeds
(``scripts/r23_observability_drill.py``), built through the REAL entries:
the Dify HTTP entry (first run + replayed run), one governed query through
the R11 offline file provider, and the real worker with an in-memory model
stub. Offline suite and container drill therefore assert the same facts.
"""
import importlib.util
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.observability import (
    aggregate_attempts,
    build_alert_candidates,
    build_observability_report,
    build_observability_status,
    build_reconciliation,
    build_trace,
    emit_observability_log,
    error_code,
    observability_settings,
    project_log_fields,
    read_query_ledgers,
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
    """seed_case() writes its own inputs, so it runs before the store opens."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = drill.drill_config()
        drill._write_inputs(self.root)
        self.store = TaskStore(self.root / drill.DB_REL)
        self.addCleanup(self.store.close)
        self.seeded = drill.seed_case(self.store, self.root, self.config)
        self.task_id = self.seeded["task_id"]


class TraceTests(FrozenCaseTestCase):
    def test_one_chain_case_intent_platform_query_attempt(self):
        trace = build_trace(self.store, self.task_id, config=self.config)
        self.assertEqual(trace["business_identity"]["case_id"], drill.CASE_ID)
        self.assertEqual(trace["business_identity"]["business_intent_id"], drill.INTENT_ID)
        self.assertEqual([r["run_id"] for r in trace["platform_runs"]], self.seeded["platform_runs"])
        # The business intent is never reported as, or replaced by, a platform run id.
        self.assertNotIn(drill.INTENT_ID, [r["run_id"] for r in trace["platform_runs"]])

    def test_query_link_comes_from_the_intake_consumption_binding(self):
        read = read_query_ledgers(self.config)
        # The JSONL audit intent does NOT carry the task id...
        self.assertTrue(all(not r.get("task_id") for r in read["records"]
                            if r.get("record_type") == "query_intent"))
        # ...the intake consumption binding is the authority that ties them.
        trace = build_trace(self.store, self.task_id, config=self.config)
        self.assertEqual(len(trace["queries"]), 1)
        query = trace["queries"][0]
        self.assertEqual(query["link"], "consumption_binding")
        self.assertEqual((query["outcome"], query["supply_status"]), ("ok", "data_delivered"))
        self.assertTrue(query["intent_id"])
        self.assertEqual(trace["coverage"]["unknown_items"], [])

    def test_legacy_ledger_records_still_link_by_task_id(self):
        """Pre-intake records named the task directly; they stay traceable."""
        with open(self.root / drill.LEDGER_REL, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"record_type": "query_intent", "request_id": "req-legacy",
                                     "task_id": self.task_id, "source_id": "s", "api_id": "a"}) + "\n")
            handle.write(json.dumps({"record_type": "query_result", "request_id": "req-legacy",
                                     "outcome": "ok", "cost_status": "free"}) + "\n")
        trace = build_trace(self.store, self.task_id, config=self.config)
        links = sorted(q["link"] for q in trace["queries"])
        self.assertEqual(links, ["consumption_binding", "ledger_task_id"])

    def test_attempts_keep_execution_order_within_one_second(self):
        """Both worker runs start in the same second with round 1; the trace
        still lists the known failure before the unknown it preceded."""
        rows = self.store.invocation_attempts(self.task_id)
        self.assertEqual({r["round_index"] for r in rows}, {1})
        self.assertEqual(len({r["started_at"][:19] for r in rows}), 1,
                         "fixture intends both attempts in one second")
        trace = build_trace(self.store, self.task_id, config=self.config)
        self.assertEqual([a["state"] for a in trace["attempts"]], ["failed_known", "reconciled"])

    def test_retry_replay_and_unknown_are_distinguishable(self):
        trace = build_trace(self.store, self.task_id, config=self.config)
        self.assertEqual([a["state"] for a in trace["attempts"]], ["failed_known", "reconciled"])
        self.assertEqual([a["reconciled"] for a in trace["attempts"]], [False, True])
        self.assertEqual(trace["platform_run_count"], 2, "one replayed run on the same task")
        self.assertEqual(trace["coverage"]["outcomes"], ["reconciled", "retry"])


class NoTextTests(FrozenCaseTestCase):
    def test_views_carry_codes_not_messages(self):
        report = build_observability_report(self.store, config=self.config, task_id=self.task_id)
        blob = json.dumps(report, ensure_ascii=False, default=str)
        for name, value in SECRET.items():
            self.assertNotIn(value, blob, name)
        # The marker really sits in the ledger: the worker stub's error text
        # (first attempt) and the blocked tasks' prompts.
        ledger_text = json.dumps([
            self.store.invocation_attempts(self.task_id)[0]["error"],
            [t.get("input_payload") for t in self.store.list_tasks() if t.get("status") == "blocked"]],
            ensure_ascii=False, default=str)
        self.assertIn(SECRET["body"], ledger_text)
        attempt = report["trace"]["attempts"][0]
        self.assertEqual(attempt["error_code"], "other")
        self.assertNotIn("error", attempt)
        self.assertNotIn("error", report["trace"]["task"])
        self.assertTrue(all("error" not in row or row.keys() >= {"error_code"}
                            for row in report["status"]["business"]["blocked_sample"]))

    def test_ops_view_reports_error_codes_only(self):
        view = build_ops_view(self.config, store=self.store)
        codes = {row["error"] for row in view["recent_failures"]}
        self.assertIn("governance", codes)
        blob = json.dumps(view, ensure_ascii=False)
        for name, value in SECRET.items():
            self.assertNotIn(value, blob, name)

    def test_error_code_is_stable_and_text_free(self):
        self.assertEqual(error_code("daily model-call budget exhausted: 2/2"), "budget_exhausted")
        self.assertEqual(error_code("model invocation outcome unknown (attempt att1)"),
                         "invocation_outcome_unknown")
        self.assertEqual(error_code("task input classification is not public"), "governance")
        self.assertEqual(error_code("synthetic before-send failure " + SECRET["body"]), "other")
        self.assertEqual(error_code(""), "")
        self.assertEqual(error_code("anything " + SECRET["token"]), "other")


class StructuredLogTests(FrozenCaseTestCase):
    def test_only_allow_listed_identifier_shaped_fields(self):
        sink = io.StringIO()
        record = emit_observability_log(
            "worker", {"task_id": "task-ok", "prompt": SECRET["body"],
                       "access_token": SECRET["token"]},
            config=self.config, sink=sink)
        payload = sink.getvalue()
        self.assertNotIn(SECRET["body"], payload)
        self.assertNotIn(SECRET["token"], payload)
        self.assertEqual(record["fields"], {"task_id": "task-ok"})
        self.assertEqual(record["dropped_fields"], 2)
        self.assertEqual(json.loads(payload)["event"], "worker")

    def test_body_under_an_allowed_name_is_still_dropped(self):
        kept, dropped = project_log_fields({"task_id": "x " + SECRET["body"] + " y"})
        self.assertEqual((kept, dropped), ({}, 1))

    def test_real_worker_emits_when_sampling_on(self):
        from unittest.mock import patch

        from stock_agent.research_worker import run_research_task

        payload = dict(self.store.get_task(self.task_id)["input_payload"])
        task, _ = self.store.create_task(title="采样开启", input_payload=payload,
                                         case_id=drill.CASE_ID, execution_mode="live")
        task_id = task["task_id"]
        self.store.claim_task(task_id, "w-on", ttl_seconds=120)
        stub = drill._ModelStub("synthetic before-send failure " + SECRET["body"],
                                stage="preflight", sent_hint="no")
        sink = io.StringIO()
        with patch("stock_agent.pi_runtime.call_pi_agent", side_effect=stub.fail), \
                contextlib.redirect_stderr(sink):
            run_research_task(self.store, task_id, worker_id="w-on", config=self.config)
        lines = sink.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["event"], "research_task_finished")
        self.assertEqual(record["fields"]["task_id"], task_id)
        for value in SECRET.values():
            self.assertNotIn(value, sink.getvalue())

    def test_real_worker_silent_when_sampling_off_and_business_continues(self):
        from unittest.mock import patch

        from stock_agent.research_worker import run_research_task

        payload = dict(self.store.get_task(self.task_id)["input_payload"])
        task, _ = self.store.create_task(title="采样关闭", input_payload=payload,
                                         case_id=drill.CASE_ID, execution_mode="live")
        task_id = task["task_id"]
        self.store.claim_task(task_id, "w-off", ttl_seconds=120)
        history = {e["event_id"] for e in self.store.events(limit=100000)}
        stub = drill._ModelStub("synthetic before-send failure " + SECRET["body"],
                                stage="preflight", sent_hint="no")
        sink = io.StringIO()
        with patch("stock_agent.pi_runtime.call_pi_agent", side_effect=stub.fail), \
                contextlib.redirect_stderr(sink):
            run_research_task(self.store, task_id, worker_id="w-off",
                              config=drill.drill_config(sampling=False))
        self.assertEqual(stub.calls, 1)
        self.assertEqual(sink.getvalue(), "")
        now = {e["event_id"] for e in self.store.events(limit=100000)}
        self.assertTrue(history <= now, "audit history preserved")
        self.assertEqual([e["event_type"] for e in self.store.events(task_id=task_id)],
                         ["task_created", "task_claimed", "task_running", "lease_renewed",
                          "research_attempt_started", "invocation_dispatched",
                          "invocation_failed_known", "task_retry_scheduled"])


class LedgerAvailabilityTests(unittest.TestCase):
    def _report(self, make):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            make(base)
            config = {"kimi_sources": {"ledger_path": "led.jsonl", "query_store_path": "intake.sqlite3"}}
            with project_path_override(lambda value: base / value):
                read = read_query_ledgers(config)
                store = TaskStore(base / "t.sqlite3")
                try:
                    task, _ = store.create_task(title="合成", input_payload={
                        "research_question": "合成", "data_classification": "public"},
                        execution_mode="replay")
                    report = build_observability_report(store, config=config, task_id=task["task_id"])
                finally:
                    store.close()
            created = sorted(p.name for p in base.iterdir())
            return read, report, created

    @staticmethod
    def _missing(base):
        pass

    @staticmethod
    def _unreadable(base):
        (base / "led.jsonl").write_bytes(b"\xff\xfe\x00garbage\n")

    @staticmethod
    def _empty(base):
        (base / "led.jsonl").write_text("", encoding="utf-8")

    @staticmethod
    def _readable(base):
        (base / "led.jsonl").write_text(json.dumps({
            "record_type": "query_result", "request_id": "r1", "outcome": "ok"}) + "\n",
            encoding="utf-8")

    def test_missing_ledger_is_unknown_in_report_and_trace(self):
        read, report, _ = self._report(self._missing)
        queries = report["reconciliation"]["queries"]
        self.assertEqual((read["state"], queries["ledger_state"]), ("missing", "missing"))
        self.assertFalse(queries["ledger_available"])
        self.assertFalse(report["trace"]["coverage"]["query_ledger_available"])
        self.assertIn("data_query_ledger", report["reconciliation"]["unknown_items"])
        self.assertEqual(queries["outcome_counts"], {}, "no number is invented for a missing ledger")

    def test_unreadable_ledger_is_distinguished_from_missing(self):
        read, report, _ = self._report(self._unreadable)
        self.assertEqual(read["state"], "unreadable")
        self.assertEqual(report["reconciliation"]["queries"]["ledger_state"], "unreadable")
        self.assertFalse(report["reconciliation"]["queries"]["ledger_available"])
        self.assertIn("data_query_ledger", report["reconciliation"]["unknown_items"])

    def test_readable_empty_ledger_is_available(self):
        read, report, _ = self._report(self._empty)
        self.assertEqual(read["state"], "readable_empty")
        self.assertTrue(report["reconciliation"]["queries"]["ledger_available"])
        self.assertTrue(report["trace"]["coverage"]["query_ledger_available"])
        self.assertNotIn("data_query_ledger", report["reconciliation"]["unknown_items"])

    def test_readable_ledger_counts_and_never_creates_an_intake(self):
        read, report, created = self._report(self._readable)
        self.assertEqual(read["state"], "readable")
        self.assertEqual(report["reconciliation"]["queries"]["outcome_counts"], {"ok": 1})
        self.assertEqual([name for name in created if "intake" in name], [])
        self.assertIn("query_consumption_binding", report["reconciliation"]["unknown_items"],
                      "a missing intake binding stays visible on its own")

    def test_report_and_trace_share_one_read(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with project_path_override(lambda value: base / value):
                store = TaskStore(base / "t.sqlite3")
                try:
                    task, _ = store.create_task(title="合成", input_payload={
                        "research_question": "合成", "data_classification": "public"},
                        execution_mode="replay")
                    report = build_observability_report(store, config={}, task_id=task["task_id"])
                finally:
                    store.close()
        self.assertEqual(report["reconciliation"]["queries"]["ledger_state"],
                         report["trace"]["coverage"]["query_ledger_state"])


class FullLedgerTests(unittest.TestCase):
    def test_attempt_beyond_the_old_sample_cap_is_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.sqlite3")
            try:
                task = None
                for number in range(5001):
                    task, _ = store.create_task(
                        title="R23 scale {0}".format(number),
                        input_payload={"research_question": "Synthetic case {0}?".format(number),
                                       "data_classification": "public"}, execution_mode="replay")
                task_id = task["task_id"]
                claimed = store.claim_task(task_id, "w-scale", ttl_seconds=120)
                store.mark_running(task_id, worker_id="w-scale", fencing_token=claimed["fencing_token"],
                                   expected_version=claimed["version"])
                store.prepare_invocation_attempt(
                    task_id, worker_id="w-scale", fencing_token=claimed["fencing_token"],
                    round_index=1, provider="synthetic", request_model="synthetic",
                    prompt_sha256="b" * 64)
                store._conn.execute(
                    "UPDATE research_tasks SET created_at = CASE WHEN task_id = ? THEN ? ELSE ? END",
                    (task_id, "2026-11-24T02:00:00+00:00", "2026-11-24T01:00:00+00:00"))
                store._conn.commit()
                physical = int(store._conn.execute(
                    "SELECT COUNT(*) FROM task_invocation_attempts").fetchone()[0])
                report = build_observability_report(store, task_id=task_id)
                status = build_observability_status(store)
                ledger = aggregate_attempts(store)
            finally:
                store.close()
        invocation = report["reconciliation"]["invocations"]
        self.assertEqual(physical, 1)
        self.assertEqual(sum(invocation["states"].values()), 1)
        self.assertEqual(invocation["by_provider"], {"synthetic": 1})
        self.assertEqual(len(report["trace"]["attempts"]), 1)
        self.assertEqual(sum(status["technical"]["invocation_attempts"].values()), 1)
        self.assertEqual(ledger["scope"], {"complete": True, "source": "sqlite_full_ledger"})
        self.assertEqual(status["technical"]["scope"]["tasks"], 5001)
        self.assertEqual(status["technical"]["queue_depth"], 5000,
                         "queue depth and age are not sampled either")

    def test_fallback_without_a_sqlite_connection_says_so(self):
        class _Store:
            def list_tasks(self):
                return [{"task_id": "t"}]

            def summary(self):
                return {"task_counts": {"queued": 1}, "outbox_events": 0}

            def consumption_summary(self):
                return {}

            def events(self, task_id="", limit=100):
                return []

            def invocation_attempts(self, task_id):
                return [{"state": "outcome_unknown", "provider": "p", "reconciled_at": "", "error": ""},
                        {"state": "succeeded", "provider": "p", "reconciled_at": "x", "error": "e"}]

        ledger = aggregate_attempts(_Store())
        self.assertEqual(ledger["states"]["outcome_unknown"], 1)
        self.assertEqual(ledger["reconciliations_waiting"], 1)
        self.assertEqual(ledger["scope"], {"complete": False, "source": "per_task_fallback"})
        recon = build_reconciliation(_Store())
        self.assertIn("invocation_attempts_total", recon["unknown_items"])


class ConfigTests(unittest.TestCase):
    def test_defaults_off(self):
        settings = observability_settings({})
        self.assertEqual((settings["sampling_enabled"], settings["alerts_enabled"],
                          settings["config_valid"]), (False, False, True))

    def test_malformed_blocks_fail_closed_per_capability(self):
        settings = observability_settings({"observability": {
            "sampling": [], "alerts": {"enabled": True, "thresholds": []}}})
        self.assertEqual((settings["config_valid"], settings["sampling_enabled"],
                          settings["alerts_enabled"]), (False, False, False))
        settings = observability_settings({"observability": "garbage"})
        self.assertEqual((settings["config_valid"], settings["sampling_enabled"]), (False, False))
        self.assertFalse(observability_settings({"observability": None})["alerts_enabled"])

    def test_malformed_thresholds_disable_alerts_but_keep_sampling(self):
        for thresholds in ([], {"stale_leases": "2"}, {"stale_leases": -1}, {"bogus": 1},
                          {"stale_leases": True}):
            settings = observability_settings({"observability": {
                "sampling": {"enabled": True},
                "alerts": {"enabled": True, "thresholds": thresholds}}})
            self.assertEqual((settings["config_valid"], settings["sampling_enabled"],
                              settings["alerts_enabled"]), (False, True, False), thresholds)
            self.assertEqual(settings["thresholds"]["stale_leases"], 0,
                             "a rejected threshold never leaks into the defaults")

    def test_well_formed_config_turns_capabilities_on(self):
        settings = observability_settings({"observability": {
            "sampling": {"enabled": True},
            "alerts": {"enabled": True, "thresholds": {"stale_leases": 2}}}})
        self.assertEqual((settings["config_valid"], settings["sampling_enabled"],
                          settings["alerts_enabled"], settings["thresholds"]["stale_leases"]),
                         (True, True, True, 2))
        report = build_observability_report(_Storeless(), config={"observability": {
            "alerts": {"enabled": True, "thresholds": {"stale_leases": 2}}}})
        self.assertTrue(report["alert_candidates"]["enabled"])
        self.assertFalse(report["alert_candidates"]["dispatched"])
        self.assertEqual(report["alert_candidates"]["dispatch"], "NOT_RUN")


class _Storeless:
    """A report over a real empty store is cheap; keep one shared helper."""

    db_path = Path("unused.sqlite3")
    read_only = True

    def list_tasks(self):
        return []

    def summary(self):
        return {"task_counts": {}, "outbox_events": 0}

    def consumption_summary(self):
        return {}

    def events(self, task_id="", limit=100):
        return []

    _conn = None


class LayeringAndAlertTests(FrozenCaseTestCase):
    def test_technical_and_business_reported_together(self):
        report = build_observability_report(self.store, config=self.config, task_id=self.task_id)
        technical = report["status"]["technical"]
        self.assertEqual((technical["inflight_leases"], technical["stale_leases"]), (1, 1))
        self.assertEqual(report["status"]["business"]["blocked_tasks"], 5)
        self.assertEqual(report["status"]["business"]["gate_counts"],
                         {"budget": 1, "evidence_quality": 1, "invocation_outcome_unknown": 2,
                          "governance": 1, "other": 0})

    def test_machine_anomalies_technical_gates_business(self):
        report = build_observability_report(self.store, config=self.config)
        alerts = report["alert_candidates"]
        layers = {c["rule"]: c["layer"] for c in alerts["candidates"] if c["candidate"]}
        self.assertEqual(layers["blocked_tasks"], "business")
        self.assertTrue(all(layer == "technical" for rule, layer in layers.items()
                            if rule != "blocked_tasks"))
        self.assertFalse(alerts["dispatched"])
        self.assertEqual(alerts["dispatch"], "NOT_RUN")

    def test_business_gate_is_never_a_page(self):
        alerts = build_alert_candidates(_status(business={"blocked_tasks": 9}))
        gate = next(c for c in alerts["candidates"] if c["rule"] == "blocked_tasks")
        self.assertEqual((gate["layer"], gate["severity"]), ("business", "info"))
        self.assertFalse(alerts["enabled"], "candidates are off unless configured on")

    def test_unknown_query_ledger_does_not_fire_a_number_rule(self):
        alerts = build_alert_candidates(_status(), {"queries": {"ledger_available": False}})
        rule = next(c for c in alerts["candidates"] if c["rule"] == "query_outcome_unknown")
        self.assertEqual((rule["observed"], rule["evidence"], rule["candidate"]),
                         ("unknown", "unknown", False))


class ReconciliationTests(FrozenCaseTestCase):
    def test_view_matches_authoritative_ledgers(self):
        recon = build_reconciliation(self.store, config=self.config)
        counts = drill.ledger_counts(self.store)
        self.assertEqual(sum(recon["invocations"]["states"].values()), counts["attempts"])
        self.assertEqual(recon["authoritative"]["outbox_events"], counts["outbox_events"])
        self.assertEqual(recon["invocations"]["known_failures"], 1)
        self.assertEqual(recon["invocations"]["unknown_reconciled"], 1)
        self.assertEqual(recon["queries"]["outcome_counts"], {"ok": 1})
        self.assertEqual(recon["queries"]["consumptions_bound"], 1)
        self.assertEqual(recon["rejections"]["budget_blocked_tasks"], 1)
        self.assertEqual(recon["evidence"]["missing_evidence_blocked_tasks"], 1)
        self.assertTrue(recon["authoritative"]["event_scan"]["complete"])

    def test_usage_split_replay_estimated_real(self):
        usage = build_reconciliation(self.store, config=self.config)["usage"]
        # The frozen case ran the live-mode worker path against a stub; the
        # six policy tasks are replay. Neither is real supplier usage.
        self.assertEqual(usage["replay"]["tasks_in_replay_mode"], 6)
        self.assertEqual(usage["estimated"]["status"], "unknown")
        self.assertEqual(usage["real_supplier"]["status"], "unknown")


class MetricsTests(unittest.TestCase):
    def test_every_metric_is_tagged_with_evidence(self):
        report = build_observability_report(_Storeless(), config={})
        rows = report["metrics"]
        self.assertTrue(all(row["evidence"] in ("ledger", "replay", "unknown") for row in rows))
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(by_name["stock_agent_usage_real_supplier"]["evidence"], "unknown")
        self.assertEqual(by_name["stock_agent_tasks_replay_mode"]["evidence"], "replay")


class ZeroSideEffectsTests(FrozenCaseTestCase):
    def test_repeated_reads_change_no_ledger(self):
        before = drill.ledger_counts(self.store)
        files = drill.file_fingerprints(self.root)
        for _ in range(4):
            build_observability_status(self.store)
            build_reconciliation(self.store, config=self.config)
            build_observability_report(self.store, config=self.config, task_id=self.task_id)
        self.assertEqual(drill.ledger_counts(self.store), before)
        self.assertEqual(drill.file_fingerprints(self.root), files)


if __name__ == "__main__":
    unittest.main()
