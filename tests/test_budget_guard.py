"""GLM-05 remainder: model-call budget guard (daily attempt cap, honest block)."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from stock_agent.budget_guard import budget_config, may_start_model_call, model_calls_today
from stock_agent.config import project_path_override
from stock_agent.model_operations import LLMResponse
from stock_agent.task_runtime import TaskStore
from stock_agent.task_supply import build_task_spec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import vault_config

NOW = datetime.now(timezone.utc)


def _spec(question="预算测试"):
    spec = build_task_spec(research_question=question, symbols=["600519"],
                           execution_mode="replay", config=None)
    spec["bound_signal_ids"] = []
    spec["public_context"]["evidence_summaries"] = {}
    spec["public_context"]["quote_snapshots"] = {}
    spec["public_context"]["news_clusters"] = {}
    return spec


def _final_output(task_id="placeholder"):
    return {"schema_version": "1.0", "task_id": task_id, "summary": "最终",
            "key_findings": [], "unknowns": [], "evidence_refs": [],
            "human_questions": [], "model_self_assessed_confidence": 0.5}


class BudgetConfigTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        defaults = budget_config({})
        self.assertEqual(defaults["daily_model_call_limit"], 0)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = TaskStore(Path(temporary.name) / "budget.sqlite3")
        try:
            self.assertTrue(may_start_model_call(store, {})["allowed"])
        finally:
            store.close()
        with self.assertRaises(ValueError):
            budget_config({"task_runtime": {"budget": {"daily_model_call_limit": -1}}})
        with self.assertRaises(ValueError):
            budget_config({"task_runtime": {"budget": {"daily_model_call_limit": "5"}}})


class CallCountingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.store = TaskStore(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)

    def seed_attempt(self, *, task_suffix, days_ago=0):
        task, _ = self.store.create_task(title=task_suffix,
                                         input_payload=_spec("预算种子 " + task_suffix),
                                         execution_mode="live")
        claimed = self.store.claim_next("w-seed")
        self.store.mark_running(task["task_id"], worker_id="w-seed",
                                fencing_token=claimed["fencing_token"],
                                expected_version=claimed["version"])
        self.store.record_research_attempt_start(
            task["task_id"], worker_id="w-seed",
            fencing_token=claimed["fencing_token"],
            attempt={"round": 1, "note": "seed"})
        if days_ago:
            # Move the attempt event into the past by rewriting its row date.
            old = (datetime.now().astimezone() - timedelta(days=days_ago)).isoformat(timespec="seconds")
            self.store._conn.execute(
                "UPDATE task_outbox SET created_at = ? WHERE task_id = ? AND event_type = 'research_attempt_started'",
                (old, task["task_id"]))
            self.store._conn.commit()
        return task

    def test_counts_only_todays_attempts(self):
        self.seed_attempt(task_suffix="today-1")
        self.seed_attempt(task_suffix="today-2")
        self.seed_attempt(task_suffix="yesterday", days_ago=2)
        self.assertEqual(model_calls_today(self.store), 2)

    def test_guard_blocks_at_limit(self):
        self.seed_attempt(task_suffix="used-1")
        config = {"task_runtime": {"budget": {"daily_model_call_limit": 1}}}
        decision = may_start_model_call(self.store, config)
        self.assertFalse(decision["allowed"])
        self.assertIn("budget", decision["reason"])
        self.assertEqual(decision["calls_today"], 1)

    def test_guard_allows_under_limit(self):
        self.seed_attempt(task_suffix="used-1")
        config = {"task_runtime": {"budget": {"daily_model_call_limit": 4}}}
        decision = may_start_model_call(self.store, config)
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["calls_today"], 1)


class WorkerBudgetIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root, {})
        self.config["task_runtime"] = {"db_path": str(self.root / "tasks.sqlite3"),
                                       "budget": {"daily_model_call_limit": 1}}
        self.store = TaskStore(self.config["task_runtime"]["db_path"])
        self.addCleanup(self.store.close)

    def make_live_task(self, name="live-budget-task"):
        task, _ = self.store.create_task(title=name,
                                         input_payload=_spec("预算集成 " + name),
                                         execution_mode="live")
        claimed = self.store.claim_next("w1")
        self.assertEqual(claimed["task_id"], task["task_id"])
        return task

    def run_worker(self, task):
        from stock_agent.research_worker import run_research_task

        return run_research_task(self.store, task["task_id"], worker_id="w1",
                                 config=self.config)

    def test_blocked_before_model_call_when_budget_spent(self):
        # Spend today's single budgeted attempt on the first task.
        first = self.make_live_task(name="first-budget-task")
        with patch("stock_agent.pi_runtime.call_pi_agent",
                   return_value=LLMResponse(json.dumps(_final_output(first["task_id"]),
                                                       ensure_ascii=False),
                                            {"model": "m", "usage": {}})):
            result = self.run_worker(first)
        self.assertEqual(result["status"], "succeeded")

        second = self.make_live_task(name="second-budget-task")
        with patch("stock_agent.pi_runtime.call_pi_agent") as stub:
            result = self.run_worker(second)
        self.assertEqual(stub.call_count, 0, "no model entry once the budget is spent")
        self.assertEqual(result["status"], "blocked")
        self.assertIn("budget", str(result.get("error") or ""))
        stored = self.store.get_task(second["task_id"])
        self.assertEqual(stored["status"], "blocked")
        self.assertEqual(stored["model_call"]["governance"]["gate"], "budget")

    def test_replay_never_consumes_or_consults_budget(self):
        spec = _spec("预算回放任务")
        spec["replay_response"] = _final_output()
        task, _ = self.store.create_task(title="replay", input_payload=spec,
                                         execution_mode="replay")
        claimed = self.store.claim_next("w1")
        with patch("stock_agent.pi_runtime.call_pi_agent") as stub:
            from stock_agent.research_worker import run_research_task

            result = run_research_task(self.store, task["task_id"], worker_id="w1",
                                       config=self.config)
        self.assertEqual(stub.call_count, 0)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(model_calls_today(self.store), 0)


if __name__ == "__main__":
    unittest.main()
