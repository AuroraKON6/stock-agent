"""GLM-10 ops view: run health from existing projections, honestly offline."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_agent.config import load_config, project_path_override
from stock_agent.ops_view import build_ops_view
from stock_agent.task_runtime import TaskStore
from stock_agent.task_supply import build_task_spec

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


class OpsViewTestCase(unittest.TestCase):
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


class EngineAndBudgetTests(OpsViewTestCase):
    def test_engine_and_budget_sections(self):
        task, _ = self.store.create_task(title="t", input_payload=_spec("运行视图问题"),
                                         execution_mode="live")
        claimed = self.store.claim_next("w1")
        self.store.mark_running(task["task_id"], worker_id="w1",
                                fencing_token=claimed["fencing_token"],
                                expected_version=claimed["version"])
        self.store.record_research_attempt_start(
            task["task_id"], worker_id="w1", fencing_token=claimed["fencing_token"],
            attempt={"round": 1})
        view = build_ops_view(self.config, store=self.store)
        self.assertEqual(view["engine"]["event_intake_mode"], "batch")
        self.assertEqual(view["engine"]["status_counts"].get("running"), 1)
        self.assertEqual(view["engine"]["outbox_events"] >= 1, True)
        self.assertEqual(view["budget"]["calls_today"], 1)
        self.assertEqual(view["budget"]["limit"], 0)

    def test_recent_failures_newest_first_with_errors(self):
        older, _ = self.store.create_task(title="旧失败", input_payload=_spec("旧问题"),
                                          execution_mode="replay")
        newer, _ = self.store.create_task(title="新失败", input_payload=_spec("新问题"),
                                          execution_mode="replay")
        for task, error, minutes_ago in ((older, "first error", 60), (newer, "second error", 1)):
            claimed = self.store.claim_next("w{0}".format(task["task_id"][-4:]))
            self.store.mark_running(task["task_id"], worker_id=claimed["worker_id"],
                                    fencing_token=claimed["fencing_token"],
                                    expected_version=claimed["version"])
            self.store.fail(task["task_id"], worker_id=claimed["worker_id"],
                            fencing_token=claimed["fencing_token"],
                            error=error, retryable=False)
            # Second-granularity timestamps tie within one test; age the rows
            # explicitly so newest-first ordering is observable.
            stamp = (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
            self.store._conn.execute(
                "UPDATE research_tasks SET updated_at = ? WHERE task_id = ?",
                (stamp, task["task_id"]))
            self.store._conn.commit()
        view = build_ops_view(self.config, store=self.store)
        failures = view["recent_failures"]
        self.assertEqual(len(failures), 2)
        self.assertEqual(failures[0]["title"], "新失败")  # newest first
        self.assertIn("second error", failures[0]["error"])


class SourceRegistryTests(OpsViewTestCase):
    def test_missing_registry_reported_honestly(self):
        view = build_ops_view(self.config, store=self.store)
        self.assertFalse(view["source_registry"]["configured"])
        self.assertEqual(view["source_registry"]["approval_counts"], {})
        self.assertIn("BLOCKED", view["source_registry"]["note"])

    def test_example_registry_summarized(self):
        example = Path(__file__).resolve().parents[1] / "config" / "source_registry.example.json"
        self.config["source_registry"] = {"path": str(self.root / "registry.json")}
        (self.root / "registry.json").write_text(example.read_text(encoding="utf-8"),
                                                 encoding="utf-8")
        view = build_ops_view(self.config, store=self.store)
        section = view["source_registry"]
        self.assertTrue(section["configured"])
        self.assertEqual(section["approval_counts"].get("pending_human_approval"), 3)
        self.assertEqual(section["entry_count"], 3)
        self.assertFalse(all(entry["external_send_allowed"] for entry in section["entries"]))


class ProjectionTests(OpsViewTestCase):
    def test_absent_projections_reported(self):
        view = build_ops_view(self.config, store=self.store)
        projections = {item["name"]: item for item in view["projections"]}
        for name in ("run_coordination", "evidence_vault", "production_pilot"):
            self.assertFalse(projections[name]["present"], name)

    def test_present_projection_with_age(self):
        target = self.root / "reports" / "run_coordination"
        target.mkdir(parents=True)
        stamp = (NOW - timedelta(minutes=30)).isoformat()
        (target / "latest.json").write_text(
            json.dumps({"generated_at": stamp, "status": "PASS"}), encoding="utf-8")
        view = build_ops_view(self.config, store=self.store)
        entry = next(item for item in view["projections"] if item["name"] == "run_coordination")
        self.assertTrue(entry["present"])
        self.assertEqual(entry["generated_at"], stamp)
        self.assertGreaterEqual(entry["age_minutes"], 29)
        self.assertIn("投影", view["projections_note"])


class OpsHttpCliTests(OpsViewTestCase):
    def test_board_serves_ops_endpoint(self):
        import threading
        import time
        import urllib.request
        from stock_agent.task_board import create_task_board_server

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
                "http://127.0.0.1:{0}/api/v1/ops".format(port), timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        for section in ("engine", "budget", "recent_failures", "source_registry", "projections"):
            self.assertIn(section, payload)

    def test_cli_ops_view_prints_json(self):
        import contextlib
        import io
        from unittest.mock import patch

        from stock_agent.cli import main

        config_file = self.root / "config.json"
        config_file.write_text(json.dumps(self.config), encoding="utf-8")
        with patch.object(sys, "argv", ["stock_agent", "ops-view", "--config", str(config_file)]), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            main()
        payload = json.loads(captured.getvalue())
        self.assertIn("engine", payload)
        self.assertIn("scope_note", payload)


if __name__ == "__main__":
    unittest.main()
