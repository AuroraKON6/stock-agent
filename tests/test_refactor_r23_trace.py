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
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.observability import (
    DEFAULT_ALERT_THRESHOLDS,
    aggregate_attempts,
    event_type_counts,
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
    correlation,
    project_value,
    safe_label,
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
        # "synthetic" is a bare word no registry verifies -> correlation value
        self.assertEqual(invocation["by_provider"], {correlation("synthetic"): 1})
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


class BoundaryTests(unittest.TestCase):
    """sol round 3: labels, one consistent read, corrupt ledgers, non-finite
    thresholds, intake sidecar files. Each test gets its own temporary store."""
    BODY_AS_ID = "Synthetic restricted prose fixture for R23"
    KEY_LIKE_LABEL = "sk-R23SyntheticProviderFixture0001"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.enterContext(project_path_override(lambda value: self.base / value))
        self.store = TaskStore(self.base / "tasks.sqlite3")
        self.addCleanup(self.store.close)
        task, _ = self.store.create_task(
            title="合成边界任务",
            input_payload={"research_question": "合成", "data_classification": "public"},
            execution_mode="replay")
        self.task_id = task["task_id"]

    def _prepare_attempt(self, store=None, provider="synthetic"):
        store = store or self.store
        claim = store.claim_task(self.task_id, "w-b", ttl_seconds=120)
        store.mark_running(self.task_id, worker_id="w-b", fencing_token=claim["fencing_token"],
                           expected_version=claim["version"])
        return store.prepare_invocation_attempt(
            self.task_id, worker_id="w-b", fencing_token=claim["fencing_token"], round_index=1,
            provider=provider, request_model="synthetic-model", prompt_sha256="b" * 64)

    @staticmethod
    def _query_alert(report):
        return next(c for c in report["alert_candidates"]["candidates"]
                    if c["rule"] == "query_outcome_unknown")

    # ---- 1. external correlation values ----
    def test_body_in_a_run_id_is_published_only_as_a_digest(self):
        self.store.attach_platform_run(self.task_id, run={
            "platform": "dify", "run_id": self.BODY_AS_ID, "workflow_version": "wf", "actor": "a"})
        trace = build_trace(self.store, self.task_id)
        self.assertNotIn(self.BODY_AS_ID, json.dumps(trace, ensure_ascii=False))
        run_id = trace["platform_runs"][0]["run_id"]
        self.assertEqual(run_id, safe_label(self.BODY_AS_ID))
        self.assertTrue(run_id.startswith("sha256:") and len(run_id) == 23)
        self.assertEqual(safe_label(self.BODY_AS_ID), run_id, "the digest is stable")
        # Valid identifiers are kept as they are, so traces stay joinable.
        self.assertEqual(trace["task"]["task_id"], self.task_id)
        self.assertEqual(trace["platform_runs"][0]["platform"], "dify")

    def test_key_like_provider_label_is_shared_by_detail_and_aggregate(self):
        self._prepare_attempt(provider=self.KEY_LIKE_LABEL)
        report = build_observability_report(self.store, task_id=self.task_id)
        self.assertNotIn(self.KEY_LIKE_LABEL, json.dumps(report, ensure_ascii=False))
        providers = report["reconciliation"]["invocations"]["by_provider"]
        self.assertEqual(list(providers), [report["trace"]["attempts"][0]["provider"]])
        self.assertEqual(list(providers), [safe_label(self.KEY_LIKE_LABEL)])
        self.assertEqual(sum(providers.values()), 1, "the count survives the label")
        self.assertEqual(sum(report["reconciliation"]["invocations"]["states"].values()), 1)

    def test_sentence_as_run_id_through_the_dify_http_entry(self):
        from test_dify_api_v1 import ApiServerTests

        http = ApiServerTests("test_create_and_status_round_trip")
        self.addCleanup(http.doCleanups)
        http.setUp()
        status, response = http._request("POST", "/api/dify/v1/create-research", {
            "title": "Synthetic R23 boundary", "case_id": "case-alpha", "input_version": 1,
            "research_question": "Synthetic research?", "dify_run_id": self.BODY_AS_ID,
            "workflow_version": "wf-demo-1", "business_intent_id": "r23-boundary-intent",
            "execution_mode": "replay"})
        self.assertEqual(status, 200, response)
        trace = build_trace(http.store, response["data"]["task_id"])
        self.assertNotIn(self.BODY_AS_ID, json.dumps(trace, ensure_ascii=False))
        self.assertEqual(trace["platform_runs"][0]["run_id"], safe_label(self.BODY_AS_ID))

    # ---- 2. one consistent ledger reading per report ----
    def test_commit_during_a_report_does_not_mix_aggregate_reads(self):
        from unittest.mock import patch

        import stock_agent.observability as obs

        real_aggregate = obs.aggregate_attempts
        reads = []

        def aggregate_then_commit(reader):
            data = real_aggregate(reader)
            reads.append(reader)
            if len(reads) == 1:
                self._prepare_attempt(self.store)
            return data

        reader = TaskStore(self.store.db_path, read_only=True)
        self.addCleanup(reader.close)
        with patch.object(obs, "aggregate_attempts", side_effect=aggregate_then_commit):
            report = build_observability_report(reader, task_id=self.task_id)
        invocation = report["reconciliation"]["invocations"]
        self.assertEqual(len(reads), 1, "the attempt ledger is aggregated once per report")
        self.assertEqual(sum(invocation["states"].values()),
                         sum(invocation["by_provider"].values()))
        self.assertEqual(report["trace"]["attempts"], [],
                         "the trace reads the same snapshot as the totals")
        self.assertFalse(reader._conn.in_transaction, "the read snapshot is released")

    def test_snapshot_sees_one_committed_state_for_every_section(self):
        from unittest.mock import patch

        import stock_agent.observability as obs

        real = obs.event_type_counts
        calls = []

        def then_commit(store):
            data = real(store)
            if not calls:
                calls.append(1)
                self._prepare_attempt(self.store)  # adds events + one attempt
            return data

        reader = TaskStore(self.store.db_path, read_only=True)
        self.addCleanup(reader.close)
        with patch.object(obs, "event_type_counts", side_effect=then_commit):
            report = build_observability_report(reader)
        recon = report["reconciliation"]
        self.assertEqual(sum(recon["invocations"]["states"].values()), 0)
        self.assertNotIn("invocation_dispatched", recon["authoritative"]["event_type_counts"],
                         "the event counts come from the same pre-commit snapshot")
        after = event_type_counts(self.store)["counts"]
        self.assertLess(recon["authoritative"]["event_scan"]["total"],
                        sum(after.values()), "the writer really did commit more afterwards")

    # ---- 3. corrupt audit ledger ----
    def _report_with_ledger(self, text):
        (self.base / "audit.jsonl").write_text(text, encoding="utf-8")
        config = {"kimi_sources": {"ledger_path": "audit.jsonl",
                                   "query_store_path": "intake.sqlite3"}}
        return build_observability_report(self.store, config=config, task_id=self.task_id)

    def test_ledger_with_no_parseable_record_is_unreadable_not_empty(self):
        report = self._report_with_ledger("{\n[]\n")
        queries, coverage, recon = (report["reconciliation"]["queries"],
                                    report["trace"]["coverage"], report["reconciliation"])
        alert = self._query_alert(report)
        self.assertEqual(queries["ledger_state"], "unreadable")
        self.assertEqual((queries["ledger_available"], coverage["query_ledger_available"]),
                         (False, False))
        self.assertEqual((alert["observed"], alert["evidence"], alert["candidate"]),
                         ("unknown", "unknown", False))
        self.assertIn("data_query_ledger", recon["unknown_items"])
        self.assertEqual(queries["outcome_counts"], {}, "0 is never offered as ledger evidence")

    def test_partially_unparseable_ledger_is_unknown(self):
        good = json.dumps({"record_type": "query_result", "request_id": "r1",
                           "outcome": "outcome_unknown"})
        report = self._report_with_ledger(good + "\n{\n")
        queries = report["reconciliation"]["queries"]
        self.assertEqual(queries["ledger_state"], "partially_unreadable")
        self.assertEqual(queries["ledger_unreadable_lines"], 1)
        self.assertFalse(queries["ledger_available"], "a partial count is an unknown count")
        self.assertEqual(self._query_alert(report)["evidence"], "unknown")
        self.assertIn("data_query_ledger", report["reconciliation"]["unknown_items"])

    def test_truly_empty_and_fully_valid_ledgers_stay_available(self):
        report = self._report_with_ledger("")
        self.assertEqual(report["reconciliation"]["queries"]["ledger_state"], "readable_empty")
        self.assertTrue(report["reconciliation"]["queries"]["ledger_available"])
        self.assertEqual(self._query_alert(report)["observed"], 0)
        good = json.dumps({"record_type": "query_result", "request_id": "r1",
                           "outcome": "outcome_unknown"})
        report = self._report_with_ledger(good + "\n")
        self.assertEqual(report["reconciliation"]["queries"]["ledger_state"], "readable")
        self.assertEqual(report["reconciliation"]["queries"]["outcome_counts"],
                         {"outcome_unknown": 1})
        alert = self._query_alert(report)
        self.assertEqual((alert["observed"], alert["evidence"]), (1, "ledger"))

    # ---- 4. non-finite thresholds ----
    def test_non_finite_thresholds_fail_closed_and_keep_strict_json(self):
        for literal in ("NaN", "Infinity", "-Infinity"):
            config = json.loads('{"observability":{"alerts":{"enabled":true,"thresholds":'
                                '{"stale_leases":' + literal + '}}}}')
            report = build_observability_report(self.store, config=config)
            settings = report["settings"]
            self.assertEqual((settings["config_valid"], settings["alerts_enabled"]), (False, False),
                             literal)
            self.assertIn("alerts", settings["invalid"])
            self.assertEqual(settings["thresholds"]["stale_leases"],
                             DEFAULT_ALERT_THRESHOLDS["stale_leases"])
            json.dumps(report, allow_nan=False)  # must not raise
            self.assertFalse(report["alert_candidates"]["enabled"])

    # ---- 5/6/7. intake files ----
    def test_reading_a_cleanly_closed_intake_creates_no_file(self):
        from stock_agent.connector_runtime.queries import ConnectorQueryStore

        path = self.base / "connector.sqlite3"
        with ConnectorQueryStore(path):
            pass
        before = {p.name for p in self.base.iterdir()}
        read = read_query_ledgers({"kimi_sources": {"query_store_path": str(path)}})
        self.assertEqual(read["intake_state"], "readable_empty")
        self.assertEqual({p.name for p in self.base.iterdir()} - before, set())

    def _intake_with_one_consumption(self, path):
        from stock_agent.connector_runtime.queries import ConnectorQueryStore

        store = ConnectorQueryStore(path)
        store.reserve_query("k-r23", source_id="file_price", upstream="ifind", purpose="r23",
                            per_source_rpm=60, per_source_rpd=10, group_daily_limit=10,
                            request_id="req-r23")
        intent_id = store._conn.execute("SELECT intent_id FROM query_intents").fetchone()[0]
        store.record_query_consumption(intent_id, "task-r23")
        return store

    def test_live_wal_keeps_committed_records_visible(self):
        path = self.base / "connector.sqlite3"
        store = self._intake_with_one_consumption(path)  # connection stays open: WAL pending
        try:
            self.assertGreater(Path(str(path) + "-wal").stat().st_size, 0)
            read = read_query_ledgers({"kimi_sources": {"query_store_path": str(path)}})
        finally:
            store.close()
        self.assertEqual(read["intake_state"], "readable")
        self.assertEqual((len(read["intents"]), len(read["consumptions"])), (1, 1))
        self.assertFalse(read["intake_wal_pending"])
        self.assertEqual(read["consumptions"][0]["task_id"], "task-r23")

    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory mode bits")
    def test_read_only_directory_intake_is_readable_and_untouched(self):
        directory = self.base / "readonly"
        directory.mkdir()
        path = directory / "connector.sqlite3"
        store = self._intake_with_one_consumption(path)
        store.close()
        directory.chmod(0o555)
        try:
            before = {p.name for p in directory.iterdir()}
            read = read_query_ledgers({"kimi_sources": {"query_store_path": str(path)}})
            after = {p.name for p in directory.iterdir()}
        finally:
            directory.chmod(0o755)
        self.assertEqual(read["intake_state"], "readable")
        self.assertEqual(len(read["consumptions"]), 1)
        self.assertEqual(after - before, set())

    def test_plain_identifiers_and_timestamps_are_not_hashed(self):
        for value in ("task_abc", "att_1", "dify-run-001", "2026-11-24T00:00:00+00:00",
                      "invocation_failed_known", "sha256:0123456789abcdef"):
            self.assertEqual(safe_label(value), value, value)
        self.assertEqual(safe_label(""), "")
        self.assertNotEqual(safe_label("has space"), "has space")


class ProjectionRuleTests(unittest.TestCase):
    """Where a value comes from decides how it may be published."""

    def test_unprefixed_opaque_token_in_a_run_id_is_not_echoed(self):
        token = "SolFixtureOpaqueValue20261125"
        self.assertEqual(project_value("run_id", token), correlation(token))
        for lower in (token.lower(), token.replace("Sol", "sol", 1)):
            self.assertEqual(project_value("run_id", lower), correlation(lower), lower)

    def test_structured_platform_ids_stay_readable_and_joinable(self):
        for value in ("sol-r23-first-run", "dify-run-synthetic-001",
                      "3f2a1c9e-5b7d-4e6f-8a9b-0c1d2e3f4a5b", "20261125001", "run_42"):
            self.assertEqual(project_value("run_id", value), value, value)
        self.assertEqual(project_value("run_id", correlation("x")), correlation("x"),
                         "projection is idempotent")

    def test_bare_word_external_values_need_a_known_set(self):
        self.assertEqual(project_value("platform", "dify"), "dify")
        self.assertNotEqual(project_value("platform", "difyy"), "difyy")
        self.assertEqual(project_value("provider", "openai", frozenset({"openai"})), "openai")
        self.assertEqual(project_value("provider", "openai"), correlation("openai"))
        self.assertEqual(project_value("worker_id", "worker-4242"), "worker-4242")
        self.assertNotEqual(project_value("worker_id", "SolFixtureOpaqueValue20261125"),
                            "SolFixtureOpaqueValue20261125")

    def test_business_identity_kept_under_the_entry_constraint(self):
        self.assertEqual(project_value("case_id", "case-alpha"), "case-alpha")
        self.assertEqual(project_value("business_intent_id", "sol-r23-business-intent"),
                         "sol-r23-business-intent")
        sentence = "Synthetic restricted prose fixture for R23"
        self.assertEqual(project_value("case_id", sentence), correlation(sentence))
        self.assertEqual(project_value("business_intent_id", "sk-R23SyntheticProviderFixture0001"),
                         correlation("sk-R23SyntheticProviderFixture0001"))

    def test_system_references_and_row_ids(self):
        self.assertEqual(project_value("request_id", "kq_" + "a" * 32), "kq_" + "a" * 32)
        self.assertEqual(project_value("intent_id", "qi_" + "b" * 16), "qi_" + "b" * 16)
        self.assertEqual(project_value("request_id", "req-legacy-1"), "req-legacy-1")
        self.assertEqual(project_value("request_id", "SolFixtureOpaqueValue20261125"),
                         correlation("SolFixtureOpaqueValue20261125"))
        self.assertEqual(project_value("attempt_id", "att_" + "c" * 16), "att_" + "c" * 16)
        self.assertNotEqual(project_value("task_id", "task-not-a-row-id"), "task-not-a-row-id")
        self.assertEqual(project_value("started_at", "2026-11-24T00:00:00+00:00"),
                         "2026-11-24T00:00:00+00:00")

    def test_registered_provider_is_published_by_name(self):
        import stock_agent.observability as obs

        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.sqlite3")
            try:
                task, _ = store.create_task(
                    title="合成", input_payload={"research_question": "合成",
                                                "data_classification": "public"},
                    execution_mode="replay")
                task_id = task["task_id"]
                claim = store.claim_task(task_id, "w-p", ttl_seconds=120)
                store.mark_running(task_id, worker_id="w-p", fencing_token=claim["fencing_token"],
                                   expected_version=claim["version"])
                store.prepare_invocation_attempt(
                    task_id, worker_id="w-p", fencing_token=claim["fencing_token"], round_index=1,
                    provider="openai", request_model="m", prompt_sha256="b" * 64)
                unverified = build_observability_report(store, task_id=task_id)
                with unittest.mock.patch.object(obs, "registered_providers",
                                                return_value=frozenset({"openai"})):
                    verified = build_observability_report(store, task_id=task_id)
            finally:
                store.close()
        self.assertEqual(unverified["reconciliation"]["invocations"]["by_provider"],
                         {correlation("openai"): 1})
        self.assertEqual(verified["reconciliation"]["invocations"]["by_provider"], {"openai": 1})
        self.assertEqual(verified["trace"]["attempts"][0]["provider"], "openai",
                         "detail and aggregate use the same verified label")
        self.assertEqual(sum(verified["reconciliation"]["invocations"]["states"].values()), 1)


class RuntimeEdgeTests(unittest.TestCase):
    """sol round 4: symlinked live WAL, one intake snapshot, non-record JSON,
    the original error through a nested snapshot, opaque token via Dify."""
    OPAQUE = "SolFixtureOpaqueValue20261125"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.store = TaskStore(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)
        task, _ = self.store.create_task(
            title="合成", input_payload={"research_question": "合成", "data_classification": "public"},
            execution_mode="replay")
        self.task_id = task["task_id"]
        self.intake = self.root / "connector.sqlite3"
        self.audit = self.root / "ledger.jsonl"

    def config(self, path=None):
        return {"kimi_sources": {"ledger_path": str(self.audit),
                                 "query_store_path": str(path or self.intake)}}

    def _bind(self, writer):
        from stock_agent.connector_runtime.queries import ConnectorQueryStore  # noqa: F401

        reserved = writer.reserve_query(
            "r23-fixture-query", source_id="file_price", upstream="file", purpose="synthetic",
            per_source_rpm=0, per_source_rpd=0, group_daily_limit=0, request_id="req-r23-fixture")
        intent_id = reserved["intent"]["intent_id"]
        writer.mark_dispatched(intent_id)
        writer.complete_query(intent_id, outcome="ok")
        writer.record_query_consumption(intent_id, self.task_id, supply_status="data_delivered")
        return intent_id

    def test_symlink_to_a_live_intake_sees_the_committed_binding(self):
        from stock_agent.connector_runtime.queries import ConnectorQueryStore

        with ConnectorQueryStore(self.intake):
            pass
        alias = self.root / "connector-alias.sqlite3"
        alias.symlink_to(self.intake)
        with ConnectorQueryStore(self.intake) as writer:
            self._bind(writer)
            self.assertGreater(Path(str(self.intake) + "-wal").stat().st_size, 0)
            direct = read_query_ledgers(self.config())
            via_alias = read_query_ledgers(self.config(alias))
        self.assertEqual((direct["intake_state"], len(direct["consumptions"])), ("readable", 1))
        self.assertEqual((via_alias["intake_state"], len(via_alias["consumptions"])),
                         ("readable", 1))
        self.assertFalse(via_alias["intake_wal_pending"])
        # Reading through the alias judges sidecars on the REAL file, so it
        # never creates a sidecar named after the alias.
        self.assertEqual([p.name for p in self.root.iterdir()
                          if p.name.startswith("connector-alias.sqlite3-")], [])

    def test_intents_and_consumptions_come_from_one_snapshot(self):
        import sqlite3
        from unittest.mock import patch

        from stock_agent.connector_runtime.queries import ConnectorQueryStore

        real_connect = sqlite3.connect
        test = self

        class Cursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def fetchall(self):
                rows = self.cursor.fetchall()
                test._bind(writer)  # a REAL connection commits after the intent read
                return rows

        class Reader:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            @property
            def row_factory(self):
                return self.connection.row_factory

            @row_factory.setter
            def row_factory(self, value):
                self.connection.row_factory = value

            def execute(self, sql, *args):
                cursor = self.connection.execute(sql, *args)
                return Cursor(cursor) if sql == "SELECT * FROM query_intents" else cursor

        def connect(database, *args, **kwargs):
            return Reader(real_connect(database, *args, **kwargs))

        with ConnectorQueryStore(self.intake) as writer:
            writer._conn.execute("CREATE TABLE r23_wal_anchor (id INTEGER)")  # frames land in the WAL
            with patch.object(sqlite3, "connect", side_effect=connect):
                read = read_query_ledgers(self.config())
        self.assertEqual((len(read["intents"]), len(read["consumptions"])), (0, 0),
                         "the binding committed after the intent read belongs to a later snapshot")
        for binding in read["consumptions"]:
            self.assertIn(binding["intent_id"], read["intents"])
        trace = build_trace(self.store, self.task_id, query_read=read)
        self.assertEqual(trace["queries"], [])

    def test_object_that_is_not_a_ledger_record_is_not_evidence(self):
        for text, state in (("{}", "unreadable"), ("[]", "unreadable"),
                            ('{"record_type":"future_type","x":1}', "unreadable"),
                            ('{"record_type":"query_result"}', "unreadable"),
                            ('{"record_type":"query_result","request_id":""}', "unreadable")):
            self.audit.write_text(text + "\n", encoding="utf-8")
            report = build_observability_report(self.store, config=self.config(),
                                                task_id=self.task_id)
            queries = report["reconciliation"]["queries"]
            alert = next(c for c in report["alert_candidates"]["candidates"]
                         if c["rule"] == "query_outcome_unknown")
            self.assertEqual((queries["ledger_state"], queries["ledger_available"],
                              alert["observed"], alert["evidence"]),
                             (state, False, "unknown", "unknown"), text)
            self.assertIn("data_query_ledger", report["reconciliation"]["unknown_items"], text)

    def test_supported_records_keep_their_meaning(self):
        self.audit.write_text("", encoding="utf-8")
        report = build_observability_report(self.store, config=self.config())
        self.assertEqual(report["reconciliation"]["queries"]["ledger_state"], "readable_empty")
        self.audit.write_text("\n".join(json.dumps(r) for r in (
            {"record_type": "query_intent", "request_id": "req-1"},
            {"record_type": "query_result", "request_id": "req-1", "outcome": "outcome_unknown"},
            {"record_type": "consumed", "artifact_id": "art-1"})) + "\n", encoding="utf-8")
        report = build_observability_report(self.store, config=self.config())
        queries = report["reconciliation"]["queries"]
        self.assertEqual((queries["ledger_state"], queries["outcome_counts"]),
                         ("readable", {"outcome_unknown": 1}))

    def test_missing_task_keeps_the_original_error_through_the_nested_snapshot(self):
        from stock_agent.task_runtime.store import TaskRuntimeError

        with self.assertRaises(TaskRuntimeError):
            build_observability_report(self.store, task_id="missing-task")
        self.assertFalse(self.store._conn.in_transaction)
        reader = TaskStore(self.store.db_path, read_only=True)
        self.addCleanup(reader.close)
        with self.assertRaises(TaskRuntimeError):
            build_observability_report(reader, task_id="missing-task")
        self.assertFalse(reader._conn.in_transaction)
        # and a later report on the same connections still works
        self.assertEqual(build_observability_report(self.store)["status"]["technical"]["queue_depth"], 1)

    def test_snapshot_open_failure_is_not_confused_with_a_body_error(self):
        class _Conn:
            in_transaction = False

            def execute(self, sql):
                raise RuntimeError("cannot begin a read transaction")

        class _Store:
            _conn = _Conn()

            db_path = Path("none.sqlite3")
            read_only = True

            def list_tasks(self):
                raise KeyError("body failed")

        from stock_agent.observability import _consistent_read

        with self.assertRaises(KeyError):
            with _consistent_read(_Store):
                raise KeyError("body failed")

    def test_opaque_token_as_run_id_through_the_dify_http_entry(self):
        from test_dify_api_v1 import ApiServerTests

        http = ApiServerTests("test_create_and_status_round_trip")
        self.addCleanup(http.doCleanups)
        http.setUp()
        status, response = http._request("POST", "/api/dify/v1/create-research", {
            "title": "Synthetic R23 edge", "case_id": "case-alpha", "input_version": 1,
            "research_question": "Synthetic research?", "dify_run_id": self.OPAQUE,
            "workflow_version": "wf-demo-1", "business_intent_id": "r23-runtime-edges",
            "execution_mode": "replay"})
        self.assertEqual(status, 200, response)
        report = build_observability_report(http.store, task_id=response["data"]["task_id"])
        self.assertNotIn(self.OPAQUE, json.dumps(report, ensure_ascii=False))
        run = report["trace"]["platform_runs"][0]
        self.assertEqual(run["run_id"], correlation(self.OPAQUE))
        self.assertEqual(report["trace"]["business_identity"]["business_intent_id"],
                         "r23-runtime-edges")


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
