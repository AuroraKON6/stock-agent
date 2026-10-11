"""GLM-05 bounded multi-round research executor: intent -> whitelisted result -> re-govern."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from stock_agent.config import project_path_override
from stock_agent.model_operations import LLMResponse
from stock_agent.task_runtime import TaskStore, TaskFencingError, TaskTransitionError
from stock_agent.task_supply import build_task_spec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import import_document, vault_config

NOW = datetime.now(timezone.utc)
_RESEARCH_WORKER_GOVERN = None


def _spec(question="评估该公告", symbols=("600519",), **payload_extra):
    spec = build_task_spec(
        research_question=question, symbols=list(symbols),
        execution_mode="replay", config=None)
    spec["bound_signal_ids"] = []
    spec["public_context"]["evidence_summaries"] = {}
    spec["public_context"]["quote_snapshots"] = {
        "600519": {"price": 1500.0, "pct_change": 2.5, "provider": "eastmoney",
                   "source_time": (NOW - timedelta(minutes=2)).isoformat(),
                   "source_time_status": "verified",
                   "observed_at": (NOW - timedelta(minutes=1)).isoformat()},
    }
    spec["public_context"]["news_clusters"] = {
        "news-600519": {"items": [{"signal_id": "sig-n1", "title": "公告",
                                   "url": "https://www.cninfo.com.cn/x",
                                   "published_at": (NOW - timedelta(hours=1)).isoformat()}]},
    }
    spec.update(payload_extra)
    return spec


def _round_output(task_id, *, summary="阶段结论", intents=None, evidence_refs=None):
    output = {
        "schema_version": "1.0", "task_id": task_id,
        "summary": summary,
        "key_findings": [], "unknowns": [],
        "evidence_refs": evidence_refs or [],
        "human_questions": [],
        "model_self_assessed_confidence": 0.5,
    }
    if intents is not None:
        output["requested_intents"] = intents
    return output


class MultiRoundTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root, {})
        mdg = self.config.setdefault("market_data_governance", {})
        mdg["approved_internal_research_providers"] = ["eastmoney"]
        mdg["use_production_pilot_provider_registry"] = False
        mdg["require_consensus_for_llm"] = False
        mdg["allow_degraded_offhours_llm"] = True
        self.store = TaskStore(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)

    def create_live_task(self, **spec_extra):
        task, _ = self.store.create_task(
            title="多轮研究", input_payload=_spec(**spec_extra), execution_mode="live")
        claimed = self.store.claim_next("w1")
        self.assertEqual(claimed["task_id"], task["task_id"])
        return task

    def response(self, payload, model="deepseek-flash"):
        meta = {"model": model, "request_id": "req-r", "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
        return LLMResponse(json.dumps(payload, ensure_ascii=False), meta)

    def quote_intent(self, symbol="600519", task_id="placeholder"):
        return {
            "schema_version": "1.0", "task_id": task_id,
            "action": "read_public_quote_snapshot",
            "parameters": {"symbol": symbol},
            "reason_summary": "需要该标的已批准行情快照",
        }


class MultiRoundFlowTests(MultiRoundTestCase):
    def test_intermediate_intent_results_are_fed_to_next_round(self):
        task = self.create_live_task()
        round1 = _round_output(task["task_id"], summary="先取行情",
                               intents=[self.quote_intent(task_id=task["task_id"])])
        final = _round_output(task["task_id"], summary="最终结论")
        with patch("stock_agent.pi_runtime.call_pi_agent",
                   side_effect=[self.response(round1), self.response(final)]) as stub:
            result = _run(self.store, task, self.config)
        self.assertEqual(stub.call_count, 2)
        self.assertEqual(result["status"], "succeeded")
        second_prompt = stub.call_args_list[1].kwargs.get("user_content") or stub.call_args_list[1][0][1]
        self.assertIn("read_public_quote_snapshot", second_prompt)
        self.assertIn("600519", second_prompt)
        stored = self.store.get_task(task["task_id"])
        self.assertEqual(len(stored["model_call"]["rounds"]), 2)
        self.assertEqual(stored["output"]["summary"], "最终结论")
        round_events = [e for e in self.store.events(task["task_id"]) if e["event_type"] == "research_round"]
        self.assertEqual(len(round_events), 1)

    def test_round_budget_caps_model_calls(self):
        task = self.create_live_task()
        endless = _round_output(task["task_id"], summary="还要更多数据",
                                intents=[self.quote_intent(task_id=task["task_id"])])
        with patch("stock_agent.pi_runtime.call_pi_agent",
                   side_effect=[self.response(endless)] * 10) as stub:
            result = _run(self.store, task, self.config)
        self.assertEqual(stub.call_count, 4)
        self.assertEqual(result["status"], "failed")
        self.assertIn("round budget", str(result.get("error") or ""))
        stored = self.store.get_task(task["task_id"])
        self.assertEqual(stored["status"], "failed")

    def test_out_of_context_intent_rejected_and_visible_next_round(self):
        task = self.create_live_task()
        round1 = _round_output(task["task_id"], summary="越权查询",
                               intents=[self.quote_intent(symbol="000001", task_id=task["task_id"])])
        final = _round_output(task["task_id"], summary="收尾")
        with patch("stock_agent.pi_runtime.call_pi_agent",
                   side_effect=[self.response(round1), self.response(final)]) as stub:
            result = _run(self.store, task, self.config)
        self.assertEqual(stub.call_count, 2)
        second_prompt = stub.call_args_list[1].kwargs.get("user_content") or stub.call_args_list[1][0][1]
        self.assertIn("rejected", second_prompt)
        stored = self.store.get_task(task["task_id"])
        round1_record = stored["model_call"]["rounds"][0]
        self.assertEqual(round1_record["intent_results"][0]["status"], "rejected")

    def test_governance_and_delivery_reverified_each_round(self):
        import stock_agent.research_worker as worker_module

        task = self.create_live_task()
        round1 = _round_output(task["task_id"], summary="中间轮",
                               intents=[self.quote_intent(task_id=task["task_id"])])
        final = _round_output(task["task_id"], summary="最终")
        with patch("stock_agent.pi_runtime.call_pi_agent",
                   side_effect=[self.response(round1), self.response(final)]), \
             patch("stock_agent.research_worker.govern_egress",
                   side_effect=_RESEARCH_WORKER_GOVERN) as govern_stub:
            _run(self.store, task, self.config)
        self.assertEqual(govern_stub.call_count, 2)

    def test_cancel_between_rounds_stops_before_next_model_call(self):
        task = self.create_live_task()
        round1 = _round_output(task["task_id"], summary="中间轮",
                               intents=[self.quote_intent(task_id=task["task_id"])])

        def first_call_sets_cancel(*args, **kwargs):
            current = self.store.get_task(task["task_id"])
            self.store.request_cancel(task["task_id"], actor="operator", reason="用户取消",
                                      expected_version=current["version"])
            return self.response(round1)

        with patch("stock_agent.pi_runtime.call_pi_agent",
                   side_effect=first_call_sets_cancel) as stub:
            result = _run(self.store, task, self.config)
        self.assertEqual(stub.call_count, 1)
        self.assertEqual(result["status"], "cancelled", json.dumps(
            {"error": result.get("error"), "model_error": (result.get("model_call") or {}).get("error"),
             "rounds": (result.get("model_call") or {}).get("rounds")}, ensure_ascii=False, default=str)[:800])
        self.assertEqual(self.store.get_task(task["task_id"])["status"], "cancelled")

    def test_illegal_json_fails_retryable_without_more_rounds(self):
        task = self.create_live_task()
        with patch("stock_agent.pi_runtime.call_pi_agent",
                   side_effect=[LLMResponse("not-json-at-all", {"model": "m", "usage": {}})]) as stub:
            result = _run(self.store, task, self.config)
        self.assertEqual(stub.call_count, 1)
        self.assertEqual(result["status"], "failed")

    def test_forged_evidence_ref_rejected_in_intermediate_round(self):
        task = self.create_live_task()
        round1 = _round_output(task["task_id"], summary="伪造引用",
                               evidence_refs=["document:sha256:" + "f" * 64],
                               intents=[self.quote_intent(task_id=task["task_id"])])
        with patch("stock_agent.pi_runtime.call_pi_agent",
                   side_effect=[self.response(round1)]) as stub:
            result = _run(self.store, task, self.config)
        self.assertEqual(stub.call_count, 1)
        self.assertEqual(result["status"], "failed")
        self.assertIn("approved context", str(result.get("error") or ""))

    def test_actual_model_unknown_when_metadata_missing(self):
        task = self.create_live_task()
        final = _round_output(task["task_id"], summary="最终")
        with patch("stock_agent.pi_runtime.call_pi_agent",
                   return_value=LLMResponse(json.dumps(final, ensure_ascii=False),
                                            {"request_id": "r", "usage": {}})):
            result = _run(self.store, task, self.config)
        self.assertEqual(result["status"], "succeeded")
        stored = self.store.get_task(task["task_id"])
        self.assertEqual(stored["model_call"]["model"], "unknown")
        self.assertTrue(stored["model_call"].get("request_model"))


def _run(store, task, config):
    from stock_agent.research_worker import run_research_task

    return run_research_task(store, task["task_id"], worker_id="w1", config=config)


# Capture the original governance function BEFORE any test patches the module
# attribute, so the per-round counting wrapper cannot recurse into the patch.
import stock_agent.research_worker as _worker_module
_RESEARCH_WORKER_GOVERN = _worker_module.govern_egress


class RoundRecordStoreTests(MultiRoundTestCase):
    def test_record_research_round_requires_active_worker_and_fencing(self):
        task, _ = self.store.create_task(title="t", input_payload=_spec(), execution_mode="live")
        # A queued task has no active worker at all: the status gate fires first.
        with self.assertRaises(TaskTransitionError):
            self.store.record_research_round(task["task_id"], worker_id="w1", fencing_token=1,
                                             record={"round": 1})
        claimed = self.store.claim_next("w1")
        self.store.record_research_round(task["task_id"], worker_id="w1",
                                         fencing_token=claimed["fencing_token"],
                                         record={"round": 1, "intent_results": []})
        events = [e for e in self.store.events(task["task_id"]) if e["event_type"] == "research_round"]
        self.assertEqual(len(events), 1)
        # A claimed task with a stale token is rejected by fencing.
        with self.assertRaises(TaskFencingError):
            self.store.record_research_round(task["task_id"], worker_id="w1",
                                             fencing_token=claimed["fencing_token"] - 1,
                                             record={"round": 2})
        with self.assertRaises(TaskFencingError):
            self.store.record_research_round(task["task_id"], worker_id="w2",
                                             fencing_token=claimed["fencing_token"],
                                             record={"round": 2})

    def test_cancel_if_requested_transitions_running_task(self):
        task, _ = self.store.create_task(title="t", input_payload=_spec(), execution_mode="live")
        claimed = self.store.claim_next("w1")
        self.store.mark_running(task["task_id"], worker_id="w1",
                                fencing_token=claimed["fencing_token"],
                                expected_version=claimed["version"])
        untouched = self.store.cancel_if_requested(task["task_id"], worker_id="w1",
                                                   fencing_token=claimed["fencing_token"])
        self.assertEqual(untouched["status"], "running")
        current = self.store.get_task(task["task_id"])
        self.store.request_cancel(task["task_id"], actor="operator", reason="r",
                                  expected_version=current["version"])
        cancelled = self.store.cancel_if_requested(task["task_id"], worker_id="w1",
                                                   fencing_token=claimed["fencing_token"])
        self.assertEqual(cancelled["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
