"""R28 (KONM-137): field provenance of the research request at the model client.

The drive is the NORMAL lease entry (``TaskStore.create_task`` ->
``claim_next`` -> ``run_research_task``); the only substitute is an offline
client at the real call boundary (``stock_agent.pi_runtime.call_pi_agent``),
behind a network guard and a real-client guard. Every case records the mock
call count, the user prompt the client received, the source of each field and
the SHA-256 of the final user text, then checks that digest against the
invocation ledger and the context delivery record of the SAME round.

Two governance configurations are real (both from ``watchlist.example.json``):
enabled+enforced, and disabled. Cases that replace the governance result are
labelled synthetic fault injection and never counted as a real reproduction.
"""
import atexit
import contextlib
import copy
import hashlib
import json
import os
import socket
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from stock_agent.config import project_path, project_path_override
from stock_agent.evidence_context import verified_evidence_documents
from stock_agent.evidence_excerpts import create_excerpt, excerpt_selector
from stock_agent.model_operations import LLMResponse
from stock_agent.research_worker import govern_egress
from stock_agent.task_runtime import TaskRuntimeError
from stock_agent.task_runtime import TaskStore
from stock_agent.task_supply import build_task_spec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import import_document, vault_config

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.now(timezone.utc)
SEVEN_QUESTIONS = " ".join(
    "合成七问{0}：{1}".format(index, item) for index, item in enumerate(
        ["披露日期是否落在观察窗口内", "公告类型属于哪一类", "是否涉及经营数据", "数据与上一期相比如何",
         "是否提及风险因素", "是否提及后续安排", "是否有需要人工核对的事项"], start=1))
# Fields that were never licensed for egress; each carries a sentinel.
UNLICENSED_FIELDS = {
    "answer_fixture": {"answer": "ANSWER_SENTINEL_DO_NOT_SEND"},
    "score_rubric": {"score": "SCORE_SENTINEL_DO_NOT_SEND"},
    "raw_document_text": "RAW_ORIGINAL_SENTINEL_DO_NOT_SEND",
    "private_working_note": "NOTE_SENTINEL_DO_NOT_SEND",
}
SENTINELS = [
    "ANSWER_SENTINEL_DO_NOT_SEND", "SCORE_SENTINEL_DO_NOT_SEND",
    "RAW_ORIGINAL_SENTINEL_DO_NOT_SEND", "NOTE_SENTINEL_DO_NOT_SEND"]
RECORDS = []


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _guard(*args, **kwargs):
    raise AssertionError("offline probe: real network or real model client reached")


if os.environ.get("R28_EVIDENCE_PATH"):
    def _dump():
        Path(os.environ["R28_EVIDENCE_PATH"]).write_text(
            json.dumps(RECORDS, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    atexit.register(_dump)


def _governance(enabled: bool) -> dict:
    """The example config's shared-governance block; only the two switches move."""
    example = json.loads((ROOT / "config" / "watchlist.example.json").read_text(encoding="utf-8"))
    block = copy.deepcopy(example["llm_governance"])
    block["enabled"] = enabled
    block["enforce"] = enabled
    return block


def _output(task_id, *, summary="合成最终结论", intents=None):
    output = {"schema_version": "1.0", "task_id": task_id, "summary": summary,
              "key_findings": [], "unknowns": [], "evidence_refs": [],
              "human_questions": [], "model_self_assessed_confidence": 0.5}
    if intents is not None:
        output["requested_intents"] = intents
    return output


class EgressProvenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root, {})
        market = self.config.setdefault("market_data_governance", {})
        market.update({"approved_internal_research_providers": ["eastmoney"],
                       "use_production_pilot_provider_registry": False,
                       "require_consensus_for_llm": False,
                       "allow_degraded_offhours_llm": True})
        self.body = "\ufeff标题\n合成事实：营业收入为 150.0 亿元。\n研发费用为（300.00）万元。"
        self.doc_id, self.sha = import_document(
            self.config, self.root, content=self.body, title="合成公司 2024 年年度报告")
        text = project_path(verified_evidence_documents(self.config)[self.sha]["object_path"]).read_bytes().decode("utf-8-sig")
        start = text.index("营业收入")
        self.excerpt = create_excerpt(self.config, self.doc_id, start, text.index("。", start) + 1)
        self.selector = excerpt_selector(self.excerpt)
        self.store = TaskStore(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)
        self.enterContext(patch("stock_agent.pi_runtime._run_process", side_effect=_guard))
        self.enterContext(patch.object(socket.socket, "connect", side_effect=_guard))
        self.enterContext(patch.object(socket.socket, "connect_ex", side_effect=_guard))

    def spec(self, question=SEVEN_QUESTIONS, **extra):
        spec = build_task_spec(research_question=question, symbols=["600519"], execution_mode="replay",
                               config=self.config)
        spec["bound_signal_ids"] = ["sig-test-0001"]
        spec["public_context"]["evidence_summaries"] = {
            self.doc_id: {"sha256": self.sha, "summary": "合成公司 2024 年年度报告"}}
        spec["public_context"]["quote_snapshots"] = {}
        spec["public_context"]["news_clusters"] = {}
        spec["approved_facts"] = [self.selector]
        spec.update(copy.deepcopy(UNLICENSED_FIELDS))
        spec.update(extra)
        return spec

    def drive(self, name, spec, outputs, *, enabled, injected=None):
        """Run one leased task to the client boundary and record what arrived."""
        config = dict(self.config, llm_governance=_governance(enabled))
        task, _ = self.store.create_task(title="R28 合成研究", input_payload=spec, execution_mode="live")
        claimed = self.store.claim_next("w-r28")
        self.assertEqual(claimed["task_id"], task["task_id"])
        calls = []

        def client(*args, **kwargs):
            user_content = str(kwargs.get("user_content") or (args[1] if len(args) > 1 else ""))
            calls.append({"system_prompt": str(kwargs.get("system_prompt") or ""),
                          "user_content": user_content})
            prompt = json.loads(user_content)
            reply = outputs[len(calls) - 1] if len(calls) - 1 < len(outputs) else outputs[-1]
            reply = dict(reply, task_id=prompt["task_id"])
            return LLMResponse(json.dumps(reply, ensure_ascii=False),
                               {"model": "offline-simulated", "request_id": "simulated", "usage": {}})

        patches = [patch("stock_agent.pi_runtime.call_pi_agent", side_effect=client)]
        if injected is not None:
            patches.append(patch("stock_agent.llm_governance.prepare_llm_batch", side_effect=injected))
        import stock_agent.research_worker as worker

        with contextlib.ExitStack() as stack:
            stub = stack.enter_context(patches[0])
            for extra in patches[1:]:
                stack.enter_context(extra)
            result = worker.run_research_task(self.store, task["task_id"], worker_id="w-r28", config=config)
        stored = self.store.get_task(task["task_id"])
        model_call = stored.get("model_call") or {}
        # Rounds that share a second are not returned in round order; pair by round.
        attempts = {row["round_index"]: row for row in self.store.invocation_attempts(task["task_id"])}
        attempt_rows = sorted(attempts.values(), key=lambda row: row["round_index"])
        rounds = model_call.get("rounds") or []
        contexts = model_call.get("context_rounds") or []
        received = []
        for index, call in enumerate(calls):
            prompt = json.loads(call["user_content"])
            question = str(spec.get("research_question") or "")
            delivered = prompt.get("research_question")
            if delivered == question:
                source = ("llm_governance projection: summary (= task research_question, bounded)"
                          if enabled else "task payload research_question (governance disabled: no projection screened)")
            else:
                source = "UNEXPECTED: not the task research_question"
            received.append({
                "round": index + 1,
                "user_prompt_json": call["user_content"],
                "field_sources": {
                    "schema_version": "worker constant",
                    "task_id": "store column research_tasks.task_id",
                    "input_version": "store column research_tasks.input_version",
                    "research_question": source,
                    "citation_contract": "derived from this round's delivered evidence_summaries / verified_facts",
                    "public_context.quote_snapshots": "market-data governance llm_eligible snapshots",
                    "public_context.evidence_summaries": "vault title projection (human_note only when the task summary differs)",
                    "public_context.verified_facts": "R13 fact bundle: vault excerpt selector or registry external-send right",
                    "public_context.context_notes": "delivered / dropped refs for this round",
                    "prior_rounds": "earlier rounds' provisional summaries and intent results (round 2+)",
                },
                "user_prompt_sha256": _sha256(call["user_content"]),
                "system_prompt_sha256": _sha256(call["system_prompt"]),
                "prompt_keys": sorted(prompt.keys()),
                "research_question": prompt.get("research_question"),
                "seven_questions_reached_client": SEVEN_QUESTIONS == prompt.get("research_question"),
                "delivered_evidence": sorted(((prompt.get("public_context") or {}).get("evidence_summaries") or {})),
                "delivered_facts": [fact.get("content") for fact in
                                    (prompt.get("public_context") or {}).get("verified_facts") or []],
                "sentinels_in_prompt": [item for item in SENTINELS if item in call["user_content"]],
                "ledger": {
                    "invocation_prompt_sha256": attempts[index + 1]["prompt_sha256"] if index + 1 in attempts else None,
                    "round_record_prompt_sha256": rounds[index]["prompt_sha256"] if index < len(rounds) else None,
                    "context_round_user_prompt_sha256": contexts[index]["user_prompt_sha256"] if index < len(contexts) else None,
                },
            })
        delivery = model_call.get("context_delivery") or {}
        RECORDS.append({
            "case": name,
            "run": os.environ.get("R28_RUN_LABEL", ""),
            "kind": "synthetic_fault_injection" if injected is not None else "real_config",
            "governance_enabled": enabled,
            "status": result.get("status"),
            "error": result.get("error"),
            "governance": model_call.get("governance"),
            "mock_calls": stub.call_count,
            "dispatched_attempts": len(attempt_rows),
            "received": received,
            "context_delivery_outbound_sha256": delivery.get("outbound_sha256"),
            "fact_bundle_outbound_sha256": (model_call.get("fact_bundle") or {}).get("outbound_payload_sha256"),
            "fact_bundle_excluded": (model_call.get("fact_bundle") or {}).get("excluded"),
            "dropped_evidence": delivery.get("dropped_evidence"),
        })
        return result, calls

    # ---- real governance, enabled and enforced ----

    def test_enabled_delivers_question_projection_and_licensed_facts_only(self):
        result, calls = self.drive("enabled_positive", self.spec(), [_output(None)], enabled=True)
        self.assertEqual(result["status"], "succeeded", result.get("error"))
        self.assertEqual(len(calls), 1)
        prompt = json.loads(calls[0]["user_content"])
        self.assertEqual(prompt["research_question"], SEVEN_QUESTIONS)
        self.assertEqual(list(prompt["public_context"]["evidence_summaries"]), [self.doc_id])
        self.assertEqual(len(prompt["public_context"]["verified_facts"]), 1)
        self.assertIn("150.0", prompt["public_context"]["verified_facts"][0]["content"])
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, calls[0]["user_content"])
        for key in UNLICENSED_FIELDS:
            self.assertNotIn(key, calls[0]["user_content"])
        self.assertNotIn(self.body, calls[0]["user_content"])
        self.assertEqual(RECORDS[-1]["received"][0]["sentinels_in_prompt"], [])

    def test_enabled_rebuild_keeps_projection_on_later_rounds(self):
        intent = {"schema_version": "1.0", "action": "read_public_quote_snapshot",
                  "parameters": {"symbol": "600519"}, "reason_summary": "需要已批准行情快照"}
        spec = self.spec()
        spec["public_context"]["quote_snapshots"] = {"600519": {
            "price": 1500.0, "pct_change": 2.5, "provider": "eastmoney",
            "source_time": (NOW - timedelta(minutes=2)).isoformat(),
            "observed_at": (NOW - timedelta(minutes=1)).isoformat()}}
        result, calls = self.drive("enabled_round_rebuild", spec,
                                   [_output(None, summary="先取行情", intents=[intent]), _output(None)],
                                   enabled=True)
        self.assertEqual(result["status"], "succeeded", result.get("error"))
        self.assertEqual(len(calls), 2)
        for call in calls:
            prompt = json.loads(call["user_content"])
            self.assertEqual(prompt["research_question"], SEVEN_QUESTIONS)
            self.assertEqual(len(prompt["public_context"]["verified_facts"]), 1)
            for sentinel in SENTINELS:
                self.assertNotIn(sentinel, call["user_content"])
        self.assertNotIn(SEVEN_QUESTIONS, calls[0]["system_prompt"])

    def test_enabled_hash_of_received_text_matches_ledger_each_round(self):
        intent = {"schema_version": "1.0", "action": "read_public_quote_snapshot",
                  "parameters": {"symbol": "600519"}, "reason_summary": "需要已批准行情快照"}
        spec = self.spec()
        spec["public_context"]["quote_snapshots"] = {"600519": {
            "price": 1500.0, "pct_change": 2.5, "provider": "eastmoney",
            "source_time": (NOW - timedelta(minutes=2)).isoformat(),
            "observed_at": (NOW - timedelta(minutes=1)).isoformat()}}
        self.drive("enabled_hash_ledger", spec,
                   [_output(None, summary="先取行情", intents=[intent]), _output(None)], enabled=True)
        record = RECORDS[-1]
        self.assertEqual(record["mock_calls"], 2)
        self.assertEqual(record["dispatched_attempts"], 2)
        for item in record["received"]:
            self.assertEqual(item["ledger"], {
                "invocation_prompt_sha256": item["user_prompt_sha256"],
                "round_record_prompt_sha256": item["user_prompt_sha256"],
                "context_round_user_prompt_sha256": item["user_prompt_sha256"]})
            # The system prompt and the fact-package digest are separate objects.
            self.assertNotEqual(item["system_prompt_sha256"], item["user_prompt_sha256"])
        self.assertEqual(record["context_delivery_outbound_sha256"], record["fact_bundle_outbound_sha256"])
        self.assertNotIn(record["context_delivery_outbound_sha256"],
                         [item["user_prompt_sha256"] for item in record["received"]])

    def test_enabled_normal_intake_refuses_non_public_input(self):
        # The public entry rejects the input before any lease exists: nothing to dispatch.
        before = len(RECORDS)
        with self.assertRaises(TaskRuntimeError):
            self.store.create_task(title="R28 合成研究", input_payload=self.spec(data_classification="internal"),
                                   execution_mode="live")
        self.assertEqual(len(RECORDS), before)
        allowed, reason, governance, safe = govern_egress(
            {"task_id": "task-unleased", "title": "R28", "input_payload": self.spec(data_classification="internal")},
            dict(self.config, llm_governance=_governance(True)))
        self.assertEqual((allowed, governance["gate"], safe), (False, "classification", {}))

    def test_enabled_multiline_question_is_still_refused_by_the_minimization_gate(self):
        # Existing rejection, unchanged: an escaped newline trips the local-path needle.
        result, calls = self.drive("enabled_multiline_question", self.spec(question="第一行\n第二行"), [_output(None)],
                                   enabled=True)
        self.assertEqual((result["status"], len(calls)), ("blocked", 0))
        self.assertEqual(RECORDS[-1]["dispatched_attempts"], 0)

    def test_enabled_registry_denied_fact_is_not_sent(self):
        path = self.root / "config" / "source_registry.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": "1.0", "entries": [{
            "source_id": "kimi_denied", "kind": "price", "upstream": "kimi_denied",
            "purpose": "test", "purposes": ["research"],
            "approval": {"state": "approved", "approved_by": "合成负责人", "approved_at": "2026-10-01T00:00:00"},
            "body_retention_allowed": True, "llm_external_send_allowed": False,
            "timeliness": {"expected_max_delay_minutes": 1440, "staleness_policy": "flag"},
            "rate_limit": {"max_requests_per_minute": 60, "max_requests_per_day": 10},
            "expires_at": "2099-12-31"}]}), encoding="utf-8")
        self.config = dict(self.config, source_registry={"path": str(path)})
        result, calls = self.drive("enabled_registry_denied_fact", self.spec(
            approved_facts=[self.selector, {"fact_id": "f-denied", "source_id": "kimi_denied",
                                            "statement": "DENIED_FACT_SENTINEL_DO_NOT_SEND"}]),
            [_output(None)], enabled=True)
        self.assertEqual(result["status"], "succeeded", result.get("error"))
        prompt = json.loads(calls[0]["user_content"])
        self.assertEqual(len(prompt["public_context"]["verified_facts"]), 1)
        self.assertNotIn("DENIED_FACT_SENTINEL_DO_NOT_SEND", calls[0]["user_content"])
        self.assertEqual(RECORDS[-1]["fact_bundle_excluded"][0]["reason"], "external_send_not_allowed")

    def test_enabled_unverified_evidence_is_dropped_not_sent(self):
        spec = self.spec()
        spec["public_context"]["evidence_summaries"]["doc-unverified"] = {
            "sha256": "0" * 64, "summary": "UNVERIFIED_SENTINEL_DO_NOT_SEND"}
        result, calls = self.drive("enabled_unverified_evidence", spec, [_output(None)], enabled=True)
        self.assertEqual(result["status"], "succeeded", result.get("error"))
        self.assertNotIn("doc-unverified", json.loads(calls[0]["user_content"])["public_context"]["evidence_summaries"])
        self.assertNotIn("UNVERIFIED_SENTINEL_DO_NOT_SEND", calls[0]["user_content"])
        self.assertIn("doc-unverified:not_verified_in_vault", RECORDS[-1]["dropped_evidence"])

    def test_enabled_blank_question_blocks_before_dispatch(self):
        # The public spec builder refuses a blank question; only a stored payload can carry one.
        result, calls = self.drive("enabled_blank_question", self.spec(research_question="  "), [_output(None)], enabled=True)
        self.assertEqual((result["status"], len(calls)), ("blocked", 0))
        self.assertEqual(RECORDS[-1]["dispatched_attempts"], 0)
        self.assertEqual(RECORDS[-1]["governance"]["gate"], "question_projection")

    # ---- real governance, disabled (the example config's shipped switches) ----

    def test_disabled_still_delivers_the_task_question(self):
        result, calls = self.drive("disabled_question", self.spec(), [_output(None)], enabled=False)
        self.assertEqual(result["status"], "succeeded", result.get("error"))
        self.assertEqual(json.loads(calls[0]["user_content"])["research_question"], SEVEN_QUESTIONS)

    def test_disabled_payload_summary_key_cannot_replace_the_question(self):
        result, calls = self.drive("disabled_payload_summary_key", self.spec(summary="PAYLOAD_SUMMARY_SENTINEL_DO_NOT_SEND"),
                                   [_output(None)], enabled=False)
        self.assertEqual(result["status"], "succeeded", result.get("error"))
        self.assertEqual(json.loads(calls[0]["user_content"])["research_question"], SEVEN_QUESTIONS)
        self.assertNotIn("PAYLOAD_SUMMARY_SENTINEL_DO_NOT_SEND", calls[0]["user_content"])

    # ---- synthetic fault injection: never a real-configuration reproduction ----

    def test_injected_approved_without_projection_blocks(self):
        result, calls = self.drive("injected_projection_absent", self.spec(), [_output(None)], enabled=True,
                                   injected=lambda signals, config, component: (list(signals), {}, {}))
        self.assertEqual((result["status"], len(calls)), ("blocked", 0))
        self.assertEqual(RECORDS[-1]["dispatched_attempts"], 0)
        self.assertEqual(RECORDS[-1]["governance"]["gate"], "question_projection")

    def test_injected_projection_without_question_blocks(self):
        for label, projection in (("blank", {"summary": ""}), ("missing", {"title": "合成标题"})):
            result, calls = self.drive("injected_projection_question_" + label,
                                       self.spec(probe_label=label), [_output(None)],
                                       enabled=True,
                                       injected=lambda signals, config, component, projection=projection: (
                                           list(signals), {str(signal.signal_id): dict(projection) for signal in signals}, {}))
            with self.subTest(label):
                self.assertEqual((result["status"], len(calls)), ("blocked", 0))
                self.assertEqual(RECORDS[-1]["dispatched_attempts"], 0)
                self.assertEqual(RECORDS[-1]["governance"]["reason"], "research_question_missing")


if __name__ == "__main__":
    unittest.main()
