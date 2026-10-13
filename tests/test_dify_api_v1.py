"""DF-02: the controlled Dify-facing API — gateway contract + loopback HTTP.

Covers the plan's DF-02 acceptance surface: authorization scope (disabled /
unknown credential / missing scope / case scope / task enumeration),
idempotency (duplicate creates, result replay vs conflict), cancellation
(verified principal required; version conflicts), fact bundles (minimization,
revocation blocking, citation validation), budget receipts, result receipts,
Dify run correlation, and the HTTP binding's status-code mapping.
All data lives in a TemporaryDirectory via project_path_override; HTTP tests
bind 127.0.0.1 only.
"""
import hashlib
import http.client
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from stock_agent.budget_guard import reserve_model_attempt
from stock_agent.config import project_path_override
from stock_agent.dify_access import DifyAccessError
from stock_agent.dify_api import (
    LEDGER_ACCESS_LOG,
    LEDGER_CANDIDATE_RESULTS,
    LEDGER_PENDING_FEEDBACK,
    DifyGateway,
    DifySubmissionError,
    build_dify_access_status,
)
from stock_agent.dify_api_server import create_dify_api_server
from stock_agent.task_runtime import TaskStore
from stock_agent.task_runtime.store import (
    TaskIdempotencyConflictError,
    TaskVersionConflictError,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

TOKEN = "unit-test-dify-token"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class GatewayTestCase(unittest.TestCase):
    """Shared fixture: temp root, one store, one enabled gateway."""

    def setUp(self):
        _tmp = tempfile.TemporaryDirectory()
        self.addCleanup(_tmp.cleanup)
        self.root = Path(_tmp.name)
        self._override = project_path_override(lambda value: self.root / value)
        self._override.__enter__()
        self.addCleanup(self._override.__exit__, None, None, None)
        self.store = TaskStore(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)
        self._write_credentials(
            scopes=["create_research", "read_research", "read_fact_bundle",
                    "submit_result", "submit_feedback"],
            case_scope=["case-alpha"])
        self.config = {
            "dify": {
                "enabled": True,
                "service_credentials_path": "config/dify/service_credentials.json",
                "approved_workflow_versions": ["wf-demo-1"],
            },
            "task_runtime": {"budget": {"daily_model_call_limit": 2}},
        }
        self.gateway = DifyGateway(self.store, self.config)

    # ---------------- fixture helpers ---------------- #

    def _write_credentials(self, *, scopes, case_scope, credential_id="cred-dify-1",
                           token=TOKEN, active=True, expires_at=""):
        path = self.root / "config" / "dify" / "service_credentials.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema_version": "1.0",
            "credentials": [{
                "credential_id": credential_id,
                "token_sha256": _digest(token),
                "scopes": list(scopes),
                "case_scope": list(case_scope),
                "active": active,
                "created_at": "2026-10-13T00:00:00+08:00",
                "expires_at": expires_at,
            }],
        }, ensure_ascii=False), encoding="utf-8")

    def _config_with(self, **dify_overrides):
        raw = {"enabled": True,
               "service_credentials_path": "config/dify/service_credentials.json",
               "approved_workflow_versions": ["wf-demo-1"]}
        raw.update(dify_overrides)
        config = json.loads(json.dumps(self.config))
        config["dify"] = raw
        return config

    def _create(self, gateway=None, *, case_id="case-alpha", run_id="run-1",
                workflow_version="wf-demo-1", question="该公告对盈利预测的影响？",
                input_version=1, idempotency_key=""):
        return (gateway or self.gateway).create_research(
            TOKEN, title="单标的公开事件研究", case_id=case_id,
            input_version=input_version, research_question=question,
            public_context="", dify_run_id=run_id, workflow_version=workflow_version,
            idempotency_key=idempotency_key)

    def _issue_bundle(self, gateway=None, *, case_id="case-alpha", input_version=1):
        return (gateway or self.gateway).issue_fact_bundle(
            case_id=case_id, input_version=input_version, facts=[
                {"fact_id": "fact-price", "kind": "market_snapshot",
                 "source": "internal-governed-cache", "classification": "public",
                 "external_send_allowed": True, "content": "收盘价 100（来源快照）"},
                {"fact_id": "fact-news", "kind": "news_meta",
                 "source": "registry-approved", "classification": "public",
                 "external_send_allowed": False, "content": "未获外发权的新闻正文"},
                {"fact_id": "fact-roster", "kind": "roster_note",
                 "source": "internal", "classification": "internal",
                 "external_send_allowed": False, "content": "内部名册信息"},
            ])

    def _submit_result(self, gateway=None, *, task_id, run_id="run-1",
                       workflow_version="wf-demo-1", bundle_id, citations=("fact-price",),
                       summary="研究草稿：基于价格快照的初步归纳。",
                       attempt_receipt=None, result_extra=None):
        result = {"summary": summary}
        if result_extra:
            result.update(result_extra)
        return (gateway or self.gateway).submit_candidate_result(
            TOKEN, task_id=task_id, dify_run_id=run_id,
            workflow_version=workflow_version, bundle_id=bundle_id,
            citations=list(citations), result=result,
            attempt_receipt=attempt_receipt or {"reservation_event_id": "evt-1", "calls_today": 1, "limit": 2})

    def _ledger(self, name):
        path = self.root / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _drive_to_awaiting_human(self, task_id):
        task = self.store.claim_next("worker-1")
        self.assertEqual(task["task_id"], task_id)
        self.store.mark_running(task_id, worker_id="worker-1",
                                fencing_token=task["fencing_token"],
                                expected_version=task["version"])
        return self.store.request_awaiting_human(
            task_id, worker_id="worker-1", fencing_token=task["fencing_token"],
            human_request={"question": "需要补充公告原件吗？"})


class AuthorizationScopeTests(GatewayTestCase):
    def test_disabled_gateway_denies_everything_with_audit(self):
        gateway = DifyGateway(self.store, self._config_with(enabled=False))
        with self.assertRaises(DifyAccessError) as caught:
            self._create(gateway)
        self.assertEqual(caught.exception.reason, "dify_disabled")
        denials = [row for row in self._ledger(LEDGER_ACCESS_LOG) if not row["allowed"]]
        self.assertEqual(denials[-1]["reason"], "dify_disabled")

    def test_unknown_token_denied(self):
        with self.assertRaises(DifyAccessError) as caught:
            self.gateway.create_research("wrong-token", title="x", case_id="case-alpha",
                                         input_version=1, research_question="q",
                                         dify_run_id="r", workflow_version="wf-demo-1")
        self.assertEqual(caught.exception.reason, "credential_unknown")

    def test_missing_scope_denied(self):
        self._write_credentials(scopes=["read_research"], case_scope=["case-alpha"])
        gateway = DifyGateway(self.store, self.config)
        with self.assertRaises(DifyAccessError) as caught:
            self._create(gateway)
        self.assertEqual(caught.exception.reason, "scope_not_granted")

    def test_case_out_of_scope_denied_on_create(self):
        self._write_credentials(scopes=["create_research"], case_scope=["case-alpha"])
        gateway = DifyGateway(self.store, self.config)
        with self.assertRaises(DifyAccessError) as caught:
            self._create(gateway, case_id="case-beta")
        self.assertEqual(caught.exception.reason, "case_out_of_scope")

    def test_task_enumeration_is_uniform_for_missing_and_foreign_tasks(self):
        created = self._create()
        self._write_credentials(scopes=["read_research"], case_scope=["case-beta"])
        gateway = DifyGateway(self.store, self.config)
        reasons = set()
        for task_id in (created["task_id"], "task-does-not-exist"):
            with self.assertRaises(DifyAccessError) as caught:
                gateway.research_status(TOKEN, task_id=task_id)
            reasons.add(caught.exception.reason)
        self.assertEqual(reasons, {"task_not_found_or_out_of_scope"})


class IdempotencyTests(GatewayTestCase):
    def test_duplicate_create_is_one_logical_task(self):
        first = self._create()
        second = self._create()
        self.assertEqual(first["outcome"], "RECORDED")
        self.assertEqual(second["outcome"], "DUPLICATE")
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(len(self.store.list_tasks()), 1,
                         "re-clicks / platform retries must not create extra tasks")

    def test_same_explicit_key_different_content_conflicts(self):
        self._create(idempotency_key="idem-K")
        with self.assertRaises(TaskIdempotencyConflictError):
            self._create(idempotency_key="idem-K", question="不同的问题内容")

    def test_result_replay_is_idempotent_and_divergence_conflicts(self):
        created = self._create()
        bundle = self._issue_bundle()
        receipt = self._submit_result(task_id=created["task_id"], bundle_id=bundle["bundle_id"])
        self.assertEqual(receipt["outcome"], "RECORDED")
        replay = self._submit_result(task_id=created["task_id"], bundle_id=bundle["bundle_id"])
        self.assertEqual(replay["outcome"], "DUPLICATE")
        self.assertEqual(replay["result_id"], receipt["result_id"])
        self.assertEqual(len(self._ledger(LEDGER_CANDIDATE_RESULTS)), 1,
                         "exact replay must not append a second result record")
        with self.assertRaises(DifySubmissionError) as caught:
            self._submit_result(task_id=created["task_id"], bundle_id=bundle["bundle_id"],
                                summary="被替换的另一份结论")
        self.assertEqual(caught.exception.reason, "result_conflict_same_run")


class FactBundleTests(GatewayTestCase):
    def test_bundle_minimization_delivers_only_approved_public_content(self):
        bundle = self._issue_bundle()
        self.assertEqual(bundle["delivered_count"], 1)
        self.assertEqual(bundle["total_count"], 3)
        view = self.gateway.read_fact_bundle(TOKEN, case_id="case-alpha", input_version=1)
        self.assertEqual([fact["fact_id"] for fact in view["facts"]], ["fact-price"])
        self.assertIn("收盘价", view["facts"][0]["content"])
        self.assertTrue(view["bundle_hash"])

    def test_revoked_bundle_blocks_reads_and_submissions(self):
        created = self._create()
        bundle = self._issue_bundle()
        self.gateway.revoke_fact_bundle(bundle_id=bundle["bundle_id"], reason="来源权利被撤回")
        with self.assertRaises(DifySubmissionError) as caught:
            self.gateway.read_fact_bundle(TOKEN, case_id="case-alpha", input_version=1)
        self.assertEqual(caught.exception.reason, "fact_bundle_revoked")
        with self.assertRaises(DifySubmissionError) as caught:
            self._submit_result(task_id=created["task_id"], bundle_id=bundle["bundle_id"])
        self.assertEqual(caught.exception.reason, "fact_bundle_revoked")

    def test_missing_bundle_is_not_found(self):
        with self.assertRaises(DifyAccessError) as caught:
            self.gateway.read_fact_bundle(TOKEN, case_id="case-alpha", input_version=7)
        self.assertEqual(caught.exception.reason, "fact_bundle_not_found")

    def test_citations_must_stay_inside_the_delivered_set(self):
        created = self._create()
        bundle = self._issue_bundle()
        with self.assertRaises(DifySubmissionError) as caught:
            self._submit_result(task_id=created["task_id"], bundle_id=bundle["bundle_id"],
                                citations=["fact-unknown"])
        self.assertEqual(caught.exception.reason, "citation_not_in_bundle")
        with self.assertRaises(DifySubmissionError) as caught:
            self._submit_result(task_id=created["task_id"], bundle_id=bundle["bundle_id"],
                                citations=["fact-news"])
        self.assertEqual(caught.exception.reason, "citation_not_deliverable")


class ResultSubmissionTests(GatewayTestCase):
    def test_happy_path_receipt_binds_everything(self):
        created = self._create()
        bundle = self._issue_bundle()
        receipt = self._submit_result(task_id=created["task_id"], bundle_id=bundle["bundle_id"],
                                      result_extra={"draft_markdown": "# 草稿\n结论…",
                                                    "unknown_questions": ["费用口径未确认"]})
        self.assertEqual(receipt["task_id"], created["task_id"])
        self.assertEqual(receipt["case_id"], "case-alpha")
        self.assertEqual(receipt["input_version"], 1)
        self.assertEqual(receipt["dify_run_id"], "run-1")
        self.assertEqual(receipt["workflow_version"], "wf-demo-1")
        self.assertEqual(receipt["bundle_id"], bundle["bundle_id"])
        self.assertEqual(receipt["bundle_hash"], bundle["bundle_hash"])
        self.assertEqual(receipt["citations"], ["fact-price"])
        self.assertEqual(receipt["task_version"], 1)
        self.assertEqual(receipt["attempt_receipt"]["calls_today"], 1,
                         "budget/attempt receipt must ride along for accounting")
        self.assertEqual(receipt["status"], "recorded")

    def test_unapproved_workflow_version_rejected_on_create_and_submit(self):
        with self.assertRaises(DifySubmissionError) as caught:
            self._create(workflow_version="wf-hotfix-unreviewed")
        self.assertEqual(caught.exception.reason, "workflow_version_not_approved")
        created = self._create()
        bundle = self._issue_bundle()
        with self.assertRaises(DifySubmissionError) as caught:
            self._submit_result(task_id=created["task_id"], bundle_id=bundle["bundle_id"],
                                workflow_version="wf-hotfix-unreviewed")
        self.assertEqual(caught.exception.reason, "workflow_version_not_approved")

    def test_cancelled_task_rejects_late_results(self):
        created = self._create()
        bundle = self._issue_bundle()
        self.store.request_cancel(created["task_id"], actor="lead-analyst",
                                  reason="案件换版", expected_version=1)
        with self.assertRaises(DifySubmissionError) as caught:
            self._submit_result(task_id=created["task_id"], bundle_id=bundle["bundle_id"])
        self.assertEqual(caught.exception.reason, "task_cancelled_result_rejected")

    def test_terminal_task_rejects_results(self):
        created = self._create()
        bundle = self._issue_bundle()
        task = self.store.claim_next("worker-1")
        self.store.mark_running(created["task_id"], worker_id="worker-1",
                                fencing_token=task["fencing_token"],
                                expected_version=task["version"])
        self.store.complete(created["task_id"], worker_id="worker-1",
                            fencing_token=task["fencing_token"],
                            output={"summary": "完成"}, model_call={"usage": {}})
        with self.assertRaises(DifySubmissionError) as caught:
            self._submit_result(task_id=created["task_id"], bundle_id=bundle["bundle_id"])
        self.assertEqual(caught.exception.reason, "task_terminal")

    def test_bundle_mismatch_rejected(self):
        created = self._create()
        with self.assertRaises(DifySubmissionError) as caught:
            self._submit_result(task_id=created["task_id"], bundle_id="fb-not-the-current-one")
        self.assertEqual(caught.exception.reason, "bundle_mismatch")


class FeedbackAndCancelTests(GatewayTestCase):
    def test_cancel_without_verified_principal_only_parks_pending(self):
        created = self._create()
        result = self.gateway.request_cancel(
            TOKEN, task_id=created["task_id"], expected_version=1,
            reason="伪造 actor 尝试", actor="attacker-via-dify", verification_id="")
        self.assertFalse(result["executed"])
        self.assertEqual(result["status"], "pending_verification")
        self.assertEqual(self.store.get_task(created["task_id"])["status"], "queued")
        pending = self._ledger(LEDGER_PENDING_FEEDBACK)
        self.assertEqual(pending[-1]["reason"], "principal_verification_required")

    def test_unknown_verification_cannot_cancel(self):
        created = self._create()
        result = self.gateway.request_cancel(
            TOKEN, task_id=created["task_id"], expected_version=1, reason="r",
            actor="lead-analyst", verification_id="pv-does-not-exist")
        self.assertFalse(result["executed"])
        self.assertEqual(result["status"], "pending_verification")
        self.assertEqual(self.store.get_task(created["task_id"])["status"], "queued")

    def test_cancel_with_verification_executes_then_version_conflict(self):
        created = self._create()
        verification_id = self.gateway.issue_principal_verification(actor="lead-analyst")
        result = self.gateway.request_cancel(
            TOKEN, task_id=created["task_id"], expected_version=1, reason="案件取消",
            actor="lead-analyst", verification_id=verification_id)
        self.assertTrue(result["executed"])
        self.assertEqual(result["status"], "cancelled")
        # Replay with a stale expected_version hits the authoritative conflict.
        second_verification = self.gateway.issue_principal_verification(actor="lead-analyst")
        with self.assertRaises(TaskVersionConflictError):
            self.gateway.request_cancel(
                TOKEN, task_id=created["task_id"], expected_version=1, reason="再次取消",
                actor="lead-analyst", verification_id=second_verification)

    def test_verification_is_single_use_across_commands(self):
        created = self._create()
        other = self._create(run_id="run-2", question="第二个案件任务？")
        verification_id = self.gateway.issue_principal_verification(actor="lead-analyst")
        result = self.gateway.request_cancel(
            TOKEN, task_id=created["task_id"], expected_version=1, reason="r",
            actor="lead-analyst", verification_id=verification_id)
        self.assertTrue(result["executed"])
        replay = self.gateway.request_cancel(
            TOKEN, task_id=other["task_id"], expected_version=1, reason="r",
            actor="lead-analyst", verification_id=verification_id)
        self.assertFalse(replay["executed"])
        self.assertEqual(replay["reason"], "principal_verification_already_used")
        self.assertEqual(self.store.get_task(other["task_id"])["status"], "queued")

    def test_feedback_pending_then_executed_through_awaiting_human(self):
        created = self._create()
        task = self._drive_to_awaiting_human(created["task_id"])
        self.assertEqual(task["status"], "awaiting_human")
        parked = self.gateway.submit_feedback(
            TOKEN, task_id=created["task_id"], command_id="cmd-1",
            expected_version=task["version"], decision="acknowledge",
            actor="whoever", verification_id="")
        self.assertFalse(parked["executed"])
        self.assertEqual(self.store.get_task(created["task_id"])["status"], "awaiting_human")
        verification_id = self.gateway.issue_principal_verification(actor="lead-analyst")
        executed = self.gateway.submit_feedback(
            TOKEN, task_id=created["task_id"], command_id="cmd-2",
            expected_version=task["version"], decision="acknowledge",
            actor="lead-analyst", verification_id=verification_id)
        self.assertTrue(executed["executed"])
        self.assertEqual(executed["task"]["status"], "succeeded")

    def test_feedback_version_conflict_propagates(self):
        created = self._create()
        task = self._drive_to_awaiting_human(created["task_id"])
        verification_id = self.gateway.issue_principal_verification(actor="lead-analyst")
        with self.assertRaises(TaskVersionConflictError):
            self.gateway.submit_feedback(
                TOKEN, task_id=created["task_id"], command_id="cmd-x",
                expected_version=int(task["version"]) + 5, decision="acknowledge",
                actor="lead-analyst", verification_id=verification_id)


class BudgetAndCorrelationTests(GatewayTestCase):
    def test_status_carries_budget_receipt_reflecting_reservations(self):
        created = self._create()
        view = self.gateway.research_status(TOKEN, task_id=created["task_id"])
        self.assertEqual(view["budget"]["calls_today"], 0)
        self.assertEqual(view["budget"]["limit"], 2)
        self.assertIn("suggest_interval_seconds", view["poll"])
        task = self.store.claim_next("worker-1")
        reservation = reserve_model_attempt(
            self.store, self.config, created["task_id"], worker_id="worker-1",
            fencing_token=task["fencing_token"], attempt={"attempt": 1})
        self.assertTrue(reservation["reserved"])
        view = self.gateway.research_status(TOKEN, task_id=created["task_id"])
        self.assertEqual(view["budget"]["calls_today"], 1)

    def test_create_receipt_includes_budget_snapshot(self):
        created = self._create()
        self.assertIn("budget", created)
        self.assertEqual(created["budget"]["limit"], 2)

    def test_dify_run_correlation_round_trip(self):
        created = self._create(run_id="run-xyz", workflow_version="wf-demo-1")
        self.assertEqual(created["dify"]["dify_run_id"], "run-xyz")
        view = self.gateway.research_status(TOKEN, task_id=created["task_id"])
        self.assertEqual(view["dify"]["dify_run_id"], "run-xyz")
        self.assertEqual(view["dify"]["workflow_version"], "wf-demo-1")
        self.assertEqual(view["input_version"], 1)

    def test_public_view_leaks_no_internals(self):
        created = self._create()
        view = self.gateway.research_status(TOKEN, task_id=created["task_id"])
        text = json.dumps(view, ensure_ascii=False, default=str)
        self.assertNotIn("sqlite", text.lower())
        self.assertNotIn("db_path", text)
        self.assertNotIn("worker_id", text)
        self.assertNotIn(str(self.root), text)

    def test_access_status_projection_has_layers_and_no_hashes(self):
        self._create()
        status = build_dify_access_status(self.config)
        text = json.dumps(status, ensure_ascii=False)
        self.assertFalse(status["enabled"] is None)
        self.assertEqual(status["credentials_configured"], 1)
        self.assertNotIn("token_sha256", text)
        self.assertNotIn(_digest(TOKEN), text, "hashes must not leak into status output")
        self.assertIn("access_log_entries", status["ledgers"])


class ApiServerTests(GatewayTestCase):
    """Loopback HTTP binding: status-code mapping and round trips."""

    def setUp(self):
        super().setUp()
        self.server = create_dify_api_server(self.gateway, host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _request(self, method, path, body=None, token=TOKEN, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = "Bearer {0}".format(token)
            payload = json.dumps(body) if body is not None else None
            if host is None:
                conn.request(method, path, payload, headers)
            else:
                conn.putrequest(method, path, skip_host=True)
                for key, value in headers.items():
                    conn.putheader(key, value)
                conn.putheader("Host", host)
                conn.endheaders(payload.encode("utf-8") if payload else None)
            response = conn.getresponse()
            data = response.read().decode("utf-8")
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                parsed = {"raw": data}
            return response.status, parsed
        finally:
            conn.close()

    def test_health(self):
        status, payload = self._request("GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["service"], "dify-api")

    def test_create_and_status_round_trip(self):
        status, payload = self._request("POST", "/api/dify/v1/create-research", {
            "title": "单标的公开事件研究", "case_id": "case-alpha",
            "input_version": 1, "research_question": "公告影响？",
            "dify_run_id": "run-http-1", "workflow_version": "wf-demo-1",
        })
        self.assertEqual(status, 200)
        task_id = payload["data"]["task_id"]
        status, payload = self._request("POST", "/api/dify/v1/research-status",
                                        {"task_id": task_id})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["dify"]["dify_run_id"], "run-http-1")

    def test_missing_fields_is_400(self):
        status, payload = self._request("POST", "/api/dify/v1/create-research", {
            "title": "x", "case_id": "case-alpha"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "missing_fields")

    def test_unknown_token_is_401_and_foreign_case_is_403(self):
        status, payload = self._request("POST", "/api/dify/v1/research-status",
                                        {"task_id": "task-x"}, token="bad-token")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "credential_unknown")
        self._write_credentials(scopes=["create_research"], case_scope=["case-beta"])
        gateway = DifyGateway(self.store, self.config)
        server = create_dify_api_server(gateway, host="127.0.0.1", port=0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", "/api/dify/v1/create-research", json.dumps({
                "title": "x", "case_id": "case-alpha", "input_version": 1,
                "research_question": "q", "dify_run_id": "r",
                "workflow_version": "wf-demo-1"}),
                {"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN})
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            conn.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_version_conflict_maps_to_409(self):
        status, payload = self._request("POST", "/api/dify/v1/create-research", {
            "title": "t", "case_id": "case-alpha", "input_version": 1,
            "research_question": "q", "dify_run_id": "run-409",
            "workflow_version": "wf-demo-1"})
        task_id = payload["data"]["task_id"]
        verification_id = self.gateway.issue_principal_verification(actor="lead-analyst")
        status, payload = self._request("POST", "/api/dify/v1/cancel", {
            "task_id": task_id, "expected_version": 1, "reason": "r",
            "actor": "lead-analyst", "verification_id": verification_id})
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["executed"])
        second_verification = self.gateway.issue_principal_verification(actor="lead-analyst")
        status, payload = self._request("POST", "/api/dify/v1/cancel", {
            "task_id": task_id, "expected_version": 1, "reason": "again",
            "actor": "lead-analyst", "verification_id": second_verification})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "TaskVersionConflictError")

    def test_unverified_cancel_stays_pending_over_http(self):
        status, payload = self._request("POST", "/api/dify/v1/create-research", {
            "title": "t", "case_id": "case-alpha", "input_version": 1,
            "research_question": "q", "dify_run_id": "run-pending",
            "workflow_version": "wf-demo-1"})
        task_id = payload["data"]["task_id"]
        status, payload = self._request("POST", "/api/dify/v1/cancel", {
            "task_id": task_id, "expected_version": 1, "reason": "r",
            "actor": "someone", "verification_id": ""})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["status"], "pending_verification")
        self.assertEqual(self.store.get_task(task_id)["status"], "queued")

    def test_non_loopback_host_rejected(self):
        status, payload = self._request("POST", "/api/dify/v1/research-status",
                                        {"task_id": "task-x"}, host="evil.example")
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "non_loopback_request")

    def test_unknown_route_is_404(self):
        status, _ = self._request("POST", "/api/dify/v1/nope", {"x": 1})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
