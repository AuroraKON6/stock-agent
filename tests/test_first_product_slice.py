"""First product slice acceptance tests (task runtime, protocol, worker, board).

All offline: the live model path is exercised with a stubbed 401 and the
replay path uses recorded responses — no real model calls, no business
events outside temp directories. HTTP checks talk to the board on the
explicit 127.0.0.1 loopback via http.client with fixed paths only.
"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.agent_protocol import (
    IntentValidationError,
    ProtocolBroker,
    validate_intent,
    validate_model_output,
)
from stock_agent.research_worker import (
    ResearchWorkerError,
    assert_payload_external_safe,
    run_research_task,
)
from stock_agent.task_board import create_task_board_server
from stock_agent.task_runtime import (
    STATUS_AWAITING_HUMAN,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    TaskFencingError,
    TaskStore,
    TaskTransitionError,
    TaskVersionConflictError,
)


def _spec(question: str = "分析该公开事件对标的的影响", with_questions: bool = True, replay: bool = True,
          doc_ref: str = "ev-0001", doc_sha: str | None = None) -> dict:
    payload = {
        "research_question": question,
        "data_classification": "public",
        "bound_signal_ids": ["sig-test-0001"],
        "public_context": {
            "quote_snapshots": {"600519": {"price": 1500.0, "pct_change": 1.2, "source_time": "2026-09-18T10:00:00+08:00"}},
            "news_clusters": {"cluster-001": {"title": "公开新闻", "sources": 2}},
            "evidence_summaries": {doc_ref: {"summary": "官方公告摘要", "sha256": doc_sha or ("ab" * 32)}},
        },
    }
    if replay:
        payload["replay_response"] = {
            "schema_version": "1.0",
            "task_id": "placeholder",
            "summary": "基于公开上下文的研究草稿：需要人工确认口径。",
            "key_findings": ["价格上涨 1.2%"],
            "unknowns": ["无官方原件确认"],
            "evidence_refs": [doc_ref],
            "human_questions": (
                [{"question": "是否需要补官方原件？", "why_needed": "P1 需要一手来源", "options": ["补证", "跳过"]}]
                if with_questions else []
            ),
            "model_self_assessed_confidence": 0.4,
        }
    return payload


_COUNTER = [0]


class TaskRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.tmp.name) / "tasks.sqlite3")

    def _unique_case(self):
        _COUNTER[0] += 1
        return "case-{0}".format(_COUNTER[0])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _create(self, **kwargs):
        spec = kwargs.pop("spec", _spec())
        kwargs.setdefault("case_id", self._unique_case())
        task, status = self.store.create_task(title=kwargs.pop("title", "切片任务"), input_payload=spec, **kwargs)
        return task, status

    def test_full_lifecycle_with_human_loop(self):
        task, status = self._create()
        self.assertEqual(status, "RECORDED")
        self.assertEqual(task["status"], STATUS_QUEUED)

        claimed = self.store.claim_next("worker-a")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["task_id"], task["task_id"])
        self.assertEqual(claimed["fencing_token"], 1)

        running = self.store.mark_running(task["task_id"], worker_id="worker-a", fencing_token=1, expected_version=claimed["version"])
        self.assertEqual(running["status"], STATUS_RUNNING)

        awaiting = self.store.request_awaiting_human(
            task["task_id"], worker_id="worker-a", fencing_token=1,
            human_request={"question": "是否接受该结论？"},
        )
        self.assertEqual(awaiting["status"], STATUS_AWAITING_HUMAN)

        responded = self.store.record_human_response(
            task["task_id"], actor="user-1", decision="provide_input", expected_version=awaiting["version"],
            command_id="cmd-001", input_patch={"extra_note": "补充信息"},
        )
        self.assertEqual(responded["status"], STATUS_QUEUED)
        self.assertEqual(responded["input_version"], 2)
        self.assertIn("extra_note", responded["input_payload"])

        again = self.store.record_human_response(
            task["task_id"], actor="user-1", decision="provide_input", expected_version=awaiting["version"],
            command_id="cmd-001", input_patch={"extra_note": "补充信息"},
        )
        self.assertEqual(again["status"], STATUS_QUEUED)

        claimed2 = self.store.claim_next("worker-b")
        self.assertEqual(claimed2["fencing_token"], 2)
        self.store.mark_running(task["task_id"], worker_id="worker-b", fencing_token=2, expected_version=claimed2["version"])
        done = self.store.complete(
            task["task_id"], worker_id="worker-b", fencing_token=2,
            output={"summary": "ok"}, model_call={"mode": "replay"},
        )
        self.assertEqual(done["status"], STATUS_SUCCEEDED)
        events = [event["event_type"] for event in self.store.events(task["task_id"])]
        self.assertIn("task_created", events)
        self.assertIn("task_succeeded", events)
        self.assertIn("human_response_recorded", events)

    def test_stale_fencing_token_rejected(self):
        task, _ = self._create()
        claimed = self.store.claim_next("worker-a")
        self.store.mark_running(task["task_id"], worker_id="worker-a", fencing_token=1, expected_version=claimed["version"])
        with self.assertRaises(TaskFencingError):
            self.store.complete(task["task_id"], worker_id="worker-b", fencing_token=1, output={})

    def test_cancel_queued_and_running(self):
        task, _ = self._create()
        cancelled = self.store.request_cancel(task["task_id"], actor="user", reason="不需要了", expected_version=task["version"])
        self.assertEqual(cancelled["status"], STATUS_CANCELLED)

        task2, _ = self._create(title="第二任务")
        claimed = self.store.claim_next("worker-a")
        self.store.mark_running(task2["task_id"], worker_id="worker-a", fencing_token=claimed["fencing_token"], expected_version=claimed["version"])
        current_version = self.store.get_task(task2["task_id"])["version"]
        flagged = self.store.request_cancel(task2["task_id"], actor="user", reason="停止", expected_version=current_version)
        self.assertEqual(flagged["cancel_requested"], 1)
        done = self.store.complete(task2["task_id"], worker_id="worker-a", fencing_token=claimed["fencing_token"], output={})
        self.assertEqual(done["status"], STATUS_CANCELLED)

    def test_version_conflict_and_bad_transition(self):
        task, _ = self._create()
        with self.assertRaises(TaskVersionConflictError):
            self.store.request_cancel(task["task_id"], actor="user", reason="r", expected_version=task["version"] + 5)
        with self.assertRaises(TaskTransitionError):
            self.store.record_human_response(task["task_id"], actor="u", decision="acknowledge",
                                             expected_version=task["version"], command_id="c1")

    def test_recovery_requeues_expired_and_expires_exhausted(self):
        from datetime import datetime, timedelta

        task, _ = self._create()
        self.store.claim_next("worker-a", ttl_seconds=120)
        past = (datetime.now().astimezone() - timedelta(seconds=600)).isoformat(timespec="seconds")
        self.store._conn.execute("UPDATE research_tasks SET lease_expires_at = ? WHERE task_id = ?", (past, task["task_id"]))
        recovered = self.store.recover_abandoned(ttl_grace_seconds=0)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["status"], STATUS_QUEUED)
        # Remove the requeued task from the queue so the next claim is the
        # exhausted one; recovery of THIS task has already been asserted.
        self.store.request_cancel(recovered[0]["task_id"], actor="test", reason="done asserting", expected_version=recovered[0]["version"])

        exhausted, _ = self._create(title="重试耗尽", spec=_spec(), max_attempts=1)
        self.store.claim_next("worker-b", ttl_seconds=120)
        self.store._conn.execute("UPDATE research_tasks SET lease_expires_at = ? WHERE task_id = ?", (past, exhausted["task_id"]))
        recovered2 = self.store.recover_abandoned(ttl_grace_seconds=0)
        statuses = {item["task_id"]: item["status"] for item in recovered2}
        self.assertEqual(statuses.get(exhausted["task_id"]), STATUS_EXPIRED)

    def test_idempotent_create_and_public_only(self):
        task, first = self._create(case_id="c1")
        twin, second = self._create(case_id="c1")
        self.assertEqual(second, "DUPLICATE")
        self.assertEqual(twin["task_id"], task["task_id"])
        with self.assertRaises(Exception):
            self.store.create_task(title="非公开", input_payload={"data_classification": "internal"})


class AgentProtocolTests(unittest.TestCase):
    def _context(self):
        return {
            "quote_snapshots": {"600519": {"price": 1500.0}},
            "news_clusters": {"cluster-001": {"title": "n"}},
            "evidence_summaries": {"ev-0001": {"summary": "s"}},
        }

    def test_valid_intent_reads_context(self):
        broker = ProtocolBroker(self._context())
        result = broker.execute_intent({
            "schema_version": "1.0", "task_id": "task_x", "action": "read_public_quote_snapshot",
            "parameters": {"symbol": "600519"}, "reason_summary": "核对最新快照",
        })
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["result"]["snapshot"]["price"], 1500.0)

    def test_url_shell_path_inputs_rejected(self):
        broker = ProtocolBroker(self._context())
        for bad in (
            {"action": "read_public_quote_snapshot", "parameters": {"symbol": "https://evil.example"}},
            {"action": "read_public_quote_snapshot", "parameters": {"symbol": "600519; rm -rf /"}},
            {"action": "calculate_approved_indicator", "parameters": {"indicator": "__import__", "series": [1, 2]}},
            {"action": "read_verified_evidence_summary", "parameters": {"evidence_ref": "../../etc/passwd"}},
            {"action": "run_shell", "parameters": {"cmd": "dir"}, "reason_summary": "x"},
        ):
            with self.subTest(bad=bad):
                intent = {"schema_version": "1.0", "task_id": "task_x", "reason_summary": "r", **bad}
                if "evil.example" in str(bad) or "etc/passwd" in str(bad) or "run_shell" in str(bad):
                    with self.assertRaises(IntentValidationError):
                        validate_intent(intent)
                with self.assertRaises(IntentValidationError):
                    broker.execute_intent(intent)

    def test_out_of_context_symbol_rejected(self):
        broker = ProtocolBroker(self._context())
        with self.assertRaises(IntentValidationError):
            broker.execute_intent({
                "schema_version": "1.0", "task_id": "t", "action": "read_public_quote_snapshot",
                "parameters": {"symbol": "000001"}, "reason_summary": "越界读取",
            })

    def test_indicator_math(self):
        broker = ProtocolBroker({})
        result = broker.execute_intent({
            "schema_version": "1.0", "task_id": "t", "action": "calculate_approved_indicator",
            "parameters": {"indicator": "pct_change", "series": [100, 110]}, "reason_summary": "计算涨跌",
        })
        self.assertAlmostEqual(result["result"]["value"], 10.0)

    def test_model_output_validation(self):
        good = _spec()["replay_response"]
        self.assertTrue(validate_model_output(good)["summary"])
        bad = dict(good, schema_version="9.9")
        with self.assertRaises(IntentValidationError):
            validate_model_output(bad)
        bad2 = dict(good, model_self_assessed_confidence=7)
        with self.assertRaises(IntentValidationError):
            validate_model_output(bad2)


class ResearchWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = TaskStore(self.root / "t.sqlite3")
        import sys as _sys

        if str(Path(__file__).resolve().parent) not in _sys.path:
            _sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _vault_fixture import import_document, vault_config

        self.vault_cfg = vault_config(self.root, {})
        _, self.doc_sha = import_document(self.vault_cfg, self.root, content="官方公告正文", title="官方公告摘要")
        self.doc_id = "document:sha256:{0}".format(self.doc_sha)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _run(self, spec, config=None, execution_mode="replay"):
        from stock_agent.config import project_path_override

        _COUNTER[0] += 1
        cfg = json.loads(json.dumps(self.vault_cfg))
        if config:
            cfg.update(config)
        mdg = cfg.setdefault("market_data_governance", {})
        mdg["approved_internal_research_providers"] = ["eastmoney"]
        mdg["use_production_pilot_provider_registry"] = False
        mdg["require_consensus_for_llm"] = False
        mdg["allow_degraded_offhours_llm"] = True
        task, _ = self.store.create_task(title="worker任务", input_payload=spec, execution_mode=execution_mode, case_id="case-{0}".format(_COUNTER[0]))
        claimed = self.store.claim_next("worker-a")
        with project_path_override(lambda value, r=self.root: r / value):
            return run_research_task(self.store, claimed["task_id"], worker_id="worker-a", config=cfg), claimed

    def test_replay_with_human_question_awaits_human(self):
        result, claimed = self._run(_spec(with_questions=True, doc_ref=self.doc_id, doc_sha=self.doc_sha))
        self.assertEqual(result["status"], STATUS_AWAITING_HUMAN)
        self.assertEqual(result["model_call"]["mode"], "replay")
        self.assertFalse(result["model_call"]["called"])
        task = self.store.get_task(claimed["task_id"])
        self.assertTrue(task["human_request"]["question"])

    def test_replay_without_question_succeeds(self):
        result, claimed = self._run(_spec(with_questions=False, doc_ref=self.doc_id, doc_sha=self.doc_sha))
        self.assertEqual(result["status"], STATUS_SUCCEEDED)
        self.assertEqual(self.store.get_task(claimed["task_id"])["status"], STATUS_SUCCEEDED)

    def test_replay_missing_response_fails(self):
        result, _ = self._run({"research_question": "q", "data_classification": "public", "public_context": {}})
        self.assertEqual(result["status"], STATUS_FAILED)
        self.assertIn("replay", str(result["model_call"].get("error") or ""))

    def test_live_auth_error_fails_without_retry(self):
        spec = _spec(with_questions=False)
        spec.pop("replay_response")
        with patch("stock_agent.pi_runtime.call_pi_agent", side_effect=OSError("HTTP Error 401: Unauthorized")):
            result, claimed = self._run(spec, execution_mode="live")
        self.assertEqual(result["status"], STATUS_FAILED)
        self.assertEqual(self.store.get_task(claimed["task_id"])["status"], STATUS_FAILED)
        self.assertIn("401", str(result["model_call"].get("error") or ""))

    def test_external_payload_gate_blocks_local_paths(self):
        with self.assertRaises(ResearchWorkerError):
            assert_payload_external_safe("请分析 E:\\stock agent\\data 下的文件")
        with self.assertRaises(ResearchWorkerError):
            assert_payload_external_safe("DEEPSEEK_API_KEY 泄漏内容")


class TaskBoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = TaskStore(self.root / "t.sqlite3")
        import sys as _sys

        if str(Path(__file__).resolve().parent) not in _sys.path:
            _sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _vault_fixture import import_document, vault_config

        self.vault_cfg = vault_config(self.root, {})
        _, self.doc_sha = import_document(self.vault_cfg, self.root, content="官方公告正文", title="官方公告摘要")
        self.doc_id = "document:sha256:{0}".format(self.doc_sha)
        self.server = create_task_board_server(self.store, host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path, headers={"Host": "127.0.0.1"})
            response = conn.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            conn.close()

    def _post(self, path, body, headers=None):
        data = json.dumps(body).encode("utf-8")
        request_headers = {"Host": "127.0.0.1", "Content-Type": "application/json"}
        request_headers.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", path, body=data, headers=request_headers)
            response = conn.getresponse()
            payload = response.read().decode("utf-8")
            status = response.status
        finally:
            conn.close()
        try:
            parsed = json.loads(payload or "{}")
        except json.JSONDecodeError:
            parsed = {}
        return status, parsed

    def test_readonly_routes_and_replay_marker(self):
        self.store.create_task(title="看板任务", input_payload=_spec(with_questions=False), execution_mode="replay", case_id="board-view")
        status, payload = self._get("/api/v1/tasks")
        self.assertEqual(status, 200)
        body = json.loads(payload)
        self.assertTrue(body["tasks"][0]["is_replay"])
        status_html, html = self._get("/")
        self.assertEqual(status_html, 200)
        self.assertIn("REPLAY", html)
        status_health, health = self._get("/health")
        self.assertEqual(json.loads(health)["status"], "ok")

    def test_feedback_requires_csrf_and_records_with_idempotency(self):
        from stock_agent.config import project_path_override

        task, _ = self.store.create_task(
            title="反馈任务",
            input_payload=_spec(with_questions=True, doc_ref=self.doc_id, doc_sha=self.doc_sha),
            execution_mode="replay", case_id="board-feedback",
        )
        claimed = self.store.claim_next("worker-a")
        cfg = json.loads(json.dumps(self.vault_cfg))
        mdg = cfg.setdefault("market_data_governance", {})
        mdg["approved_internal_research_providers"] = ["eastmoney"]
        mdg["use_production_pilot_provider_registry"] = False
        mdg["require_consensus_for_llm"] = False
        mdg["allow_degraded_offhours_llm"] = True
        with project_path_override(lambda value, r=self.root: self.root / value):
            run_research_task(self.store, claimed["task_id"], worker_id="worker-a", config=cfg)
        awaiting = self.store.get_task(task["task_id"])
        self.assertEqual(awaiting["status"], STATUS_AWAITING_HUMAN)

        body = {"task_id": task["task_id"], "actor": "user-1", "decision": "acknowledge",
                "expected_version": awaiting["version"], "command_id": "cmd-board-1"}
        status, _ = self._post("/api/v1/human-responses", body)
        self.assertEqual(status, 403)  # no CSRF token

        token = str(self.server.csrf_token)
        status, response = self._post("/api/v1/human-responses", body, headers={"X-Task-Board-CSRF": token})
        self.assertEqual(status, 200)
        self.assertEqual(response["task"]["status"], STATUS_SUCCEEDED)
        ack = response["task"]["output"]["personal_acknowledgement"]
        self.assertEqual(ack["scope"], "personal_research_acknowledgement_not_institutional_approval")

        status, replay = self._post("/api/v1/human-responses", body, headers={"X-Task-Board-CSRF": token})
        self.assertEqual(status, 200)  # idempotent command_id replay


if __name__ == "__main__":
    unittest.main()
