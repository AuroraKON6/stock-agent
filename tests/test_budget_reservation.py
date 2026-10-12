"""GLM-11: atomic budget reservation across concurrent workers."""
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from stock_agent.budget_guard import reserve_model_attempt
from stock_agent.config import project_path_override
from stock_agent.model_operations import LLMResponse
from stock_agent.task_runtime import TaskStore
from stock_agent.task_supply import build_task_spec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _task_fixture_helpers import final_replay_response
from _vault_fixture import vault_config

NOW = datetime.now(timezone.utc)


def _spec(question):
    spec = build_task_spec(research_question=question, symbols=["600519"],
                           execution_mode="replay", config=None)
    spec["bound_signal_ids"] = []
    spec["public_context"]["evidence_summaries"] = {}
    spec["public_context"]["quote_snapshots"] = {}
    spec["public_context"]["news_clusters"] = {}
    spec["replay_response"] = final_replay_response()
    return spec


def _final(task_id):
    return {"schema_version": "1.0", "task_id": task_id, "summary": "最终",
            "key_findings": [], "unknowns": [], "evidence_refs": [],
            "human_questions": [], "model_self_assessed_confidence": 0.5}


class ReservationTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        # Same enriched config as the budget-guard worker tests: real vault
        # paths and market relaxations so governance passes and the budget
        # gate is what actually decides.
        self.config = vault_config(self.root, {})
        self.config["task_runtime"] = {"db_path": str(self.root / "tasks.sqlite3"),
                                       "budget": {"daily_model_call_limit": 1}}
        self.store = TaskStore(self.config["task_runtime"]["db_path"])
        self.addCleanup(self.store.close)

    def live_task(self, name):
        # Claim only: the worker itself performs mark_running (leased ->
        # running); pre-marking here would make that transition illegal.
        task, _ = self.store.create_task(title=name, input_payload=_spec(name + " 问题"),
                                         execution_mode="live")
        claimed = self.store.claim_next("w-" + name)
        self.assertEqual(claimed["task_id"], task["task_id"])
        return task, "w-" + name, claimed["fencing_token"]


class AtomicReservationTests(ReservationTestCase):
    def test_concurrent_reservations_never_exceed_limit(self):
        task_a, worker_a, token_a = self.live_task("alpha")
        task_b, worker_b, token_b = self.live_task("beta")
        # Real concurrency uses separate connections (multi-process / one
        # store per worker); a shared connection would interleave
        # transactions unsafely by design.
        store_b = TaskStore(self.config["task_runtime"]["db_path"])
        self.addCleanup(store_b.close)
        barrier = threading.Barrier(3)
        results = {}

        def contender(name, store, task, worker, token):
            barrier.wait()
            results[name] = reserve_model_attempt(
                store, self.config, task["task_id"],
                worker_id=worker, fencing_token=token,
                attempt={"round": 1, "prompt_sha256": "x"})

        threads = [threading.Thread(target=contender, args=("a", self.store, task_a, worker_a, token_a)),
                   threading.Thread(target=contender, args=("b", store_b, task_b, worker_b, token_b))]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=15)
        reserved = [results[name]["reserved"] for name in ("a", "b")]
        self.assertEqual(sorted(reserved), [False, True], str(results))
        denied = results["a"] if not results["a"]["reserved"] else results["b"]
        self.assertIn("budget", denied["reason"])
        self.assertEqual(denied["calls_today"], 1)
        self.assertEqual(denied["limit"], 1)

    def test_denial_records_no_attempt_event(self):
        task_a, worker_a, token_a = self.live_task("first")
        task_b, worker_b, token_b = self.live_task("second")
        first = reserve_model_attempt(self.store, self.config, task_a["task_id"],
                                      worker_id=worker_a, fencing_token=token_a,
                                      attempt={"round": 1})
        second = reserve_model_attempt(self.store, self.config, task_b["task_id"],
                                       worker_id=worker_b, fencing_token=token_b,
                                       attempt={"round": 1})
        self.assertTrue(first["reserved"])
        self.assertFalse(second["reserved"])
        events = [e for e in self.store.events(limit=1000)
                  if e["event_type"] == "research_attempt_started"]
        self.assertEqual(len(events), 1, "denial must not consume budget")

    def test_off_by_default_allows_all(self):
        task, worker, token = self.live_task("free")
        config = dict(self.config)
        config["task_runtime"] = {"db_path": self.config["task_runtime"]["db_path"]}
        for round_index in range(1, 4):
            decision = reserve_model_attempt(self.store, config, task["task_id"],
                                             worker_id=worker, fencing_token=token,
                                             attempt={"round": round_index})
            self.assertTrue(decision["reserved"], str(decision))
        self.assertEqual(decision["calls_today"], 3)


class ConcurrentWorkerBudgetTests(ReservationTestCase):
    def test_two_concurrent_live_workers_one_call_total(self):
        import stock_agent.research_worker as worker_module

        task_a, worker_a, _ = self.live_task("worker-a")
        task_b, worker_b, _ = self.live_task("worker-b")
        store_b = TaskStore(self.config["task_runtime"]["db_path"])
        self.addCleanup(store_b.close)
        results = {}
        barrier = threading.Barrier(3)

        def run(name, store, task, worker):
            barrier.wait()
            results[name] = worker_module.run_research_task(
                store, task["task_id"], worker_id=worker, config=self.config)

        def call_pi(*args, **kwargs):
            # Only the worker that won the atomic reservation reaches here.
            prompt = kwargs.get("user_content") or ""
            task_id = json.loads(prompt).get("task_id", "placeholder")
            return LLMResponse(json.dumps(_final(task_id), ensure_ascii=False),
                               {"model": "m", "usage": {}})

        with patch("stock_agent.pi_runtime.call_pi_agent", side_effect=call_pi) as stub:
            threads = [threading.Thread(target=run, args=("a", self.store, task_a, worker_a)),
                       threading.Thread(target=run, args=("b", store_b, task_b, worker_b))]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(stub.call_count, 1,
                         "concurrent workers must share the budget atomically")
        statuses = sorted(results[name]["status"] for name in ("a", "b"))
        self.assertEqual(statuses, ["blocked", "succeeded"],
                         str({k: v.get("error") for k, v in results.items()}))


if __name__ == "__main__":
    unittest.main()
