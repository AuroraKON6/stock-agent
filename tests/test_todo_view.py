"""GLM-10 todo view: what needs a real human, honestly aggregated."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_agent.config import load_config, project_path_override
from stock_agent.task_runtime import TaskStore
from stock_agent.task_supply import build_task_spec
from stock_agent.todo_view import build_todo_view

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _task_fixture_helpers import final_replay_response

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


class TodoViewTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        base = load_config(str(Path(__file__).resolve().parents[1] / "config/watchlist.example.json"))
        self.config = dict(base)
        self.config["task_runtime"] = {"db_path": str(self.root / "tasks.sqlite3")}
        self.store = TaskStore(self.config["task_runtime"]["db_path"])
        self.addCleanup(self.store.close)

    def make_awaiting(self, name, question, *, minutes_ago=5):
        task, _ = self.store.create_task(title=name, input_payload=_spec(name),
                                         execution_mode="replay")
        claimed = self.store.claim_next("w1")
        self.store.mark_running(task["task_id"], worker_id="w1",
                                fencing_token=claimed["fencing_token"],
                                expected_version=claimed["version"])
        # Age the task row so ordering by updated_at is observable.
        stamp = (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
        self.store.request_awaiting_human(
            task["task_id"], worker_id="w1", fencing_token=claimed["fencing_token"],
            human_request={"question": question, "why_needed": "缺上下文",
                           "options": ["A", "B"]},
            output=final_replay_response(task["task_id"]),
            model_call={"called": False, "rounds": [], "context_delivery": {}})
        self.store._conn.execute(
            "UPDATE research_tasks SET updated_at = ? WHERE task_id = ?", (stamp, task["task_id"]))
        self.store._conn.commit()
        return task


class TodoAggregationTests(TodoViewTestCase):
    def test_awaiting_human_listed_with_question_and_oldest_first(self):
        newer = self.make_awaiting("较新待办", "新问题？", minutes_ago=5)
        older = self.make_awaiting("较旧待办", "旧问题？", minutes_ago=60)
        view = build_todo_view(self.config, store=self.store)
        self.assertEqual(view["counts"]["awaiting_human"], 2)
        first = view["awaiting_human"][0]
        self.assertEqual(first["task_id"], older["task_id"])
        self.assertEqual(first["question"], "旧问题？")
        self.assertEqual(first["why_needed"], "缺上下文")
        self.assertEqual(first["options"], ["A", "B"])
        self.assertGreaterEqual(first["age_minutes"], 59)
        self.assertEqual(view["awaiting_human"][1]["task_id"], newer["task_id"])

    def test_blocked_tasks_listed_with_error_excerpt(self):
        task, _ = self.store.create_task(title="被阻断", input_payload=_spec("被阻断问题"),
                                         execution_mode="live")
        claimed = self.store.claim_next("w1")
        self.store.mark_running(task["task_id"], worker_id="w1",
                                fencing_token=claimed["fencing_token"],
                                expected_version=claimed["version"])
        self.store.mark_blocked(task["task_id"], worker_id="w1",
                                fencing_token=claimed["fencing_token"],
                                error="governance denied: internal classification")
        view = build_todo_view(self.config, store=self.store)
        self.assertEqual(view["counts"]["blocked"], 1)
        entry = view["blocked"][0]
        self.assertEqual(entry["task_id"], task["task_id"])
        self.assertIn("governance denied", entry["error"])
        self.assertIn("处置入口", entry["note"])

    def test_empty_store_is_all_zero(self):
        view = build_todo_view(self.config, store=self.store)
        self.assertEqual(view["awaiting_human"], [])
        self.assertEqual(view["blocked"], [])
        self.assertEqual(view["counts"], {"awaiting_human": 0, "blocked": 0})
        self.assertFalse(view["review_queue"]["available"])

    def test_review_queue_snapshot_surfaced_read_only(self):
        report_dir = Path(self.config.get("review_queue", {}).get(
            "report_dir", "reports/review_queue"))
        (self.root / report_dir).mkdir(parents=True, exist_ok=True)
        (self.root / report_dir / "latest.json").write_text(json.dumps({
            "generated_at": NOW.isoformat(), "items": [
                {"signal_id": "s1", "status": "pending"},
                {"signal_id": "s2", "status": "completed"},
                {"signal_id": "s3", "status": "pending"},
            ]}, ensure_ascii=False), encoding="utf-8")
        view = build_todo_view(self.config, store=self.store)
        self.assertTrue(view["review_queue"]["available"])
        self.assertEqual(view["review_queue"]["pending_count"], 2)
        self.assertEqual(view["review_queue"]["generated_at"], NOW.isoformat())


class TodoHttpCliTests(TodoViewTestCase):
    def test_board_serves_todo_endpoint(self):
        import threading
        import time
        import urllib.request
        from stock_agent.task_board import create_task_board_server

        self.make_awaiting("待办任务", "需要人工确认吗？")
        server = create_task_board_server(self.store, host="127.0.0.1", port=0, config=self.config)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        deadline = time.time() + 5
        while True:
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:{0}/health".format(port), timeout=1) as probe:
                    if probe.status == 200:
                        break
            except Exception:
                if time.time() >= deadline:
                    raise
                time.sleep(0.05)
        with urllib.request.urlopen(
                "http://127.0.0.1:{0}/api/v1/todo".format(port), timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["counts"]["awaiting_human"], 1)
        self.assertEqual(payload["awaiting_human"][0]["question"], "需要人工确认吗？")
        self.assertIn("scope_note", payload)

    def test_cli_todo_view_prints_json(self):
        import contextlib
        import io
        from unittest.mock import patch

        from stock_agent.cli import main

        self.make_awaiting("CLI 待办", "CLI 问题？")
        config_file = self.root / "config.json"
        config_file.write_text(json.dumps(self.config), encoding="utf-8")
        with patch.object(sys, "argv", ["stock_agent", "todo-view", "--config", str(config_file)]), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            main()
        payload = json.loads(captured.getvalue())
        self.assertEqual(payload["counts"]["awaiting_human"], 1)


if __name__ == "__main__":
    unittest.main()
