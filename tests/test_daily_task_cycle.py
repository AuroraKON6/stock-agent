"""GLM-04/09: one scheduled task-engine cycle (rescan -> drain queue), Python-level."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_agent.cli import main
from stock_agent.config import load_config, project_path_override
from stock_agent.models import AnalyzedSignal
from stock_agent.storage import SignalStore
from stock_agent.task_runtime import TaskStore

sys.path.insert(0, str(Path(__file__).resolve().parent))

NOW = datetime.now(timezone.utc)


class DailyTaskCycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        base = load_config(str(Path(__file__).resolve().parents[1] / "config/watchlist.example.json"))
        self.config = dict(base)
        self.config["storage"] = {"sqlite_path": str(self.root / "signals.sqlite3")}
        self.config["task_runtime"] = {
            "db_path": str(self.root / "tasks.sqlite3"),
            "event_intake_mode": "task",
        }
        self.store = TaskStore(self.config["task_runtime"]["db_path"])
        self.addCleanup(self.store.close)
        self.config_file = self.root / "config.json"
        self.config_file.write_text(json.dumps(self.config), encoding="utf-8")

    def seed_signal(self, sid, url, title="某公告"):
        signal_store = SignalStore(Path(self.config["storage"]["sqlite_path"]))
        try:
            signal_store.upsert_many([AnalyzedSignal(
                signal_id=sid, source="manual", source_type="filing", title=title,
                summary="s", url=url,
                published_at=(NOW - timedelta(hours=1)).isoformat(),
                collected_at=NOW.isoformat(), symbols=["600519"],
                payload={"data_classification": "public"})])
        finally:
            signal_store.close()

    def run_cycle(self):
        import contextlib
        import io
        from unittest.mock import patch

        argv = ["stock_agent", "daily-task-cycle", "--config", str(self.config_file)]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as captured:
            main()
        return json.loads(captured.getvalue())

    def test_refused_in_batch_mode(self):
        self.config["task_runtime"]["event_intake_mode"] = "batch"
        self.config_file.write_text(json.dumps(self.config), encoding="utf-8")
        with self.assertRaises(SystemExit) as raised:
            self.run_cycle()
        self.assertIn("batch", str(raised.exception))

    def test_cycle_rescans_and_drains_queue_with_replay_worker(self):
        import stock_agent.task_intake as intake
        from _task_fixture_helpers import final_replay_response

        self.seed_signal("sig-1", "https://www.cninfo.com.cn/x/1")
        self.seed_signal("sig-2", "https://www.cninfo.com.cn/x/2", title="另一公告")
        # Record final replay answers (no human questions) for the tasks the
        # cycle will create — mirroring recorded real final notes.
        original_builder = intake.build_task_spec

        def with_final_replay(*args, **kwargs):
            spec = original_builder(*args, **kwargs)
            spec["replay_response"] = final_replay_response()
            return spec

        from unittest.mock import patch as mock_patch

        with mock_patch.object(intake, "build_task_spec", side_effect=with_final_replay):
            report = self.run_cycle()
        self.assertEqual(report["rescan"]["created"], 2)
        self.assertEqual(report["processed"], 2)
        self.assertEqual(report["succeeded"], 2)
        self.assertEqual(report["remaining_queued"], 0)
        statuses = {t["status"] for t in self.store.list_tasks()}
        self.assertEqual(statuses, {"succeeded"})

    def test_default_replay_asks_human_and_is_counted(self):
        # The default recorded replay response asks whether to switch to live;
        # such tasks land in awaiting_human and are counted, not hidden.
        self.seed_signal("sig-1", "https://www.cninfo.com.cn/x/1")
        report = self.run_cycle()
        self.assertEqual(report["awaiting_human"], 1)
        self.assertEqual(report["remaining_queued"], 0)

    def test_second_cycle_is_a_no_op(self):
        self.seed_signal("sig-1", "https://www.cninfo.com.cn/x/1")
        first = self.run_cycle()
        second = self.run_cycle()
        self.assertEqual(first["processed"], 1)
        self.assertEqual(second["rescan"]["created"], 0)
        self.assertEqual(second["processed"], 0)

    def test_failed_task_is_counted_not_hidden(self):
        # A terminal failed task must appear in the cycle report's full store
        # status counts, never silently dropped or re-run.
        self.run_cycle()
        from stock_agent.task_intake import ensure_question_task

        result = ensure_question_task(self.store, research_question="坏任务",
                                      symbols=["600519"], config=self.config)
        task_id = result["task"]["task_id"]
        claimed = self.store.claim_next("w-cycle")
        self.store.mark_running(task_id, worker_id="w-cycle",
                                fencing_token=claimed["fencing_token"],
                                expected_version=claimed["version"])
        self.store.fail(task_id, worker_id="w-cycle",
                        fencing_token=claimed["fencing_token"],
                        error="injected", retryable=False)
        report = self.run_cycle()
        self.assertGreaterEqual((report.get("status_counts") or {}).get("failed", 0), 1)


if __name__ == "__main__":
    unittest.main()
