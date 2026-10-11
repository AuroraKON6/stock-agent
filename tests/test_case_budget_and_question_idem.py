"""GLM-07 prerequisite: case total wall-clock budget is enforced honestly."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from stock_agent.config import project_path_override
from stock_agent.model_operations import LLMResponse
from stock_agent.task_runtime import TaskStore
from stock_agent.task_supply import build_task_spec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import import_document, vault_config

NOW = datetime.now(timezone.utc)


def _spec():
    spec = build_task_spec(research_question="评估", symbols=["600519"],
                           execution_mode="replay", config=None)
    spec["bound_signal_ids"] = []
    spec["public_context"]["evidence_summaries"] = {}
    spec["public_context"]["quote_snapshots"] = {}
    spec["public_context"]["news_clusters"] = {}
    return spec


def _round_output(task_id, intents=None):
    output = {
        "schema_version": "1.0", "task_id": task_id, "summary": "阶段",
        "key_findings": [], "unknowns": [], "evidence_refs": [],
        "human_questions": [], "model_self_assessed_confidence": 0.4,
    }
    if intents is not None:
        output["requested_intents"] = intents
    return output


class CaseTimeLimitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root, {})
        self.config["task_runtime"] = {"db_path": str(self.root / "tasks.sqlite3")}
        self.store = TaskStore(self.config["task_runtime"]["db_path"])
        self.addCleanup(self.store.close)

    def create_live_task(self, **runtime_extra):
        self.config["task_runtime"].update(runtime_extra)
        task, _ = self.store.create_task(title="t", input_payload=_spec(), execution_mode="live")
        claimed = self.store.claim_next("w1")
        self.assertEqual(claimed["task_id"], task["task_id"])
        return task

    def test_time_limit_fails_honestly_without_next_round(self):
        import stock_agent.research_worker as worker

        task = self.create_live_task(case_time_limit_seconds=60)
        round1 = _round_output(task["task_id"], intents=[{
            "schema_version": "1.0", "task_id": task["task_id"],
            "action": "read_public_quote_snapshot", "parameters": {"symbol": "600519"},
            "reason_summary": "需要行情",
        }])
        responses = [LLMResponse(json.dumps(round1, ensure_ascii=False), {"model": "m", "usage": {}})]
        # perf_counter call order: started, round-1 budget check, round-1
        # start, round-1 duration — then the round-2 budget check must see
        # the spent budget (700s > 60s limit) and stop before a second call.
        clock = [0.0, 0.0, 0.0, 0.0] + [700.0] * 20
        with patch("stock_agent.pi_runtime.call_pi_agent", side_effect=responses) as stub, \
                patch.object(worker.time, "perf_counter", side_effect=clock):
            result = worker.run_research_task(self.store, task["task_id"], worker_id="w1",
                                              config=self.config)
        self.assertEqual(stub.call_count, 1, "no second model call once the case budget is spent")
        self.assertEqual(result["status"], "failed")
        self.assertIn("case time limit", str(result.get("error") or ""))
        self.assertEqual(self.store.get_task(task["task_id"])["status"], "failed")

    def test_default_limit_is_ten_minutes_and_allows_normal_run(self):
        task = self.create_live_task()
        final = _round_output(task["task_id"])
        with patch("stock_agent.pi_runtime.call_pi_agent",
                   return_value=LLMResponse(json.dumps(final, ensure_ascii=False),
                                            {"model": "m", "usage": {}})):
            result = self.store  # placeholder to keep flake calm
            from stock_agent.research_worker import run_research_task

            result = run_research_task(self.store, task["task_id"], worker_id="w1",
                                       config=self.config)
        self.assertEqual(result["status"], "succeeded")


class QuestionIdempotencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root, {})
        self.config["task_runtime"] = {"db_path": str(self.root / "tasks.sqlite3")}
        self.store = TaskStore(self.config["task_runtime"]["db_path"])
        self.addCleanup(self.store.close)

    def test_rebuilt_context_returns_same_question_task(self):
        import stock_agent.task_intake as intake
        from stock_agent.task_intake import ensure_question_task

        first = ensure_question_task(self.store, research_question="白酒龙头风险",
                                     symbols=["600519"], config=self.config)
        # Simulate real context drift: a rebuilt public context that differs
        # byte-wise on every call (fresh cache/timestamps in production).
        original_builder = intake.build_task_spec
        drift = {"n": 0}

        def drifting_builder(*args, **kwargs):
            spec = original_builder(*args, **kwargs)
            drift["n"] += 1
            spec["public_context"]["drift_marker"] = drift["n"]
            return spec

        with patch.object(intake, "build_task_spec", side_effect=drifting_builder):
            second = ensure_question_task(self.store, research_question="白酒龙头风险",
                                          symbols=["600519"], config=self.config)
        self.assertEqual(first["status"], "RECORDED")
        self.assertEqual(second["status"], "DUPLICATE")
        self.assertEqual(first["task"]["task_id"], second["task"]["task_id"])
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_changed_question_creates_new_task(self):
        from stock_agent.task_intake import ensure_question_task

        first = ensure_question_task(self.store, research_question="问题甲",
                                     symbols=["600519"], config=self.config)
        second = ensure_question_task(self.store, research_question="问题乙",
                                      symbols=["600519"], config=self.config)
        self.assertNotEqual(first["task"]["task_id"], second["task"]["task_id"])

    def test_cli_from_symbols_routes_through_intake_service(self):
        import contextlib
        import io
        from unittest.mock import patch as mock_patch

        from stock_agent.cli import main

        config_file = self.root / "config.json"
        config_file.write_text(json.dumps(self.config), encoding="utf-8")
        argv = ["stock_agent", "task-create", "--config", str(config_file),
                "--title", "CLI 问题", "--from-symbols", "600519",
                "--research-question", "同一问题", "--execution-mode", "replay"]
        with mock_patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            main()
        with mock_patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            main()
        tasks = self.store.list_tasks()
        self.assertEqual(len(tasks), 1, "same question must not create a second task")
        self.assertEqual(tasks[0]["input_payload"]["origin"]["kind"], "question")
        self.assertEqual(tasks[0]["input_payload"]["research_question"], "同一问题")


if __name__ == "__main__":
    unittest.main()
