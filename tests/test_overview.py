"""GLM-10 slice 1: read-only workbench overview aggregation."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_agent.config import load_config, project_path_override
from stock_agent.models import AnalyzedSignal
from stock_agent.overview import build_overview
from stock_agent.storage import SignalStore
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


class OverviewTestCase(unittest.TestCase):
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

    def seed_signal(self, sid, *, source_type="news", priority="P1", provenance=""):
        store = SignalStore(Path(self.config["storage"]["sqlite_path"]))
        try:
            store.upsert_many([AnalyzedSignal(
                signal_id=sid, source="manual", source_type=source_type, title="事件_" + sid,
                summary="s", url="https://www.cninfo.com.cn/x/" + sid,
                published_at=(NOW - timedelta(hours=1)).isoformat(),
                collected_at=NOW.isoformat(), symbols=["600519"],
                priority=priority,
                payload={"data_classification": "public", "data_provenance": provenance})])
        finally:
            store.close()


class PureOverviewTests(OverviewTestCase):
    def test_market_symbols_from_topics_and_honest_missing_quotes(self):
        overview = build_overview(self.config, store=None)
        symbols = overview["market"]["symbols"]
        self.assertTrue(symbols, "topic symbols must be surfaced")
        self.assertEqual(len(symbols), len(set(symbols)))
        self.assertEqual(overview["market"]["quotes"], {})
        self.assertEqual(overview["market"]["quotes_missing"], symbols)

    def test_engine_mode_reported(self):
        self.assertEqual(build_overview(self.config, store=None)["engine"]["event_intake_mode"], "task")
        batch = dict(self.config)
        batch["task_runtime"] = {"db_path": "x", "event_intake_mode": "batch"}
        self.assertEqual(build_overview(batch, store=None)["engine"]["event_intake_mode"], "batch")

    def test_events_section_counts_eligible_and_excludes_example(self):
        self.seed_signal("s1")
        self.seed_signal("s2", source_type="filing")
        self.seed_signal("s-ex", provenance="example")
        overview = build_overview(self.config, store=None)
        self.assertEqual(overview["events"]["eligible_count"], 2)
        titles = [item["title"] for item in overview["events"]["recent"]]
        self.assertIn("事件_s1", titles)
        self.assertNotIn("事件_s-ex", titles)


class StoreOverviewTests(OverviewTestCase):
    def test_tasks_budget_and_awaiting_human(self):
        store = TaskStore(self.config["task_runtime"]["db_path"])
        self.addCleanup(store.close)
        done, _ = store.create_task(title="done", input_payload=_spec("问题甲"),
                                    execution_mode="replay")
        claimed = store.claim_next("w1")
        store.mark_running(done["task_id"], worker_id="w1",
                           fencing_token=claimed["fencing_token"], expected_version=claimed["version"])
        store.complete(done["task_id"], worker_id="w1", fencing_token=claimed["fencing_token"],
                       output=final_replay_response(done["task_id"]), model_call={"called": False})

        waiting, _ = store.create_task(title="waiting", input_payload=_spec("问题乙"),
                                       execution_mode="replay")
        claimed = store.claim_next("w2")
        store.mark_running(waiting["task_id"], worker_id="w2",
                           fencing_token=claimed["fencing_token"], expected_version=claimed["version"])
        store.request_awaiting_human(
            waiting["task_id"], worker_id="w2", fencing_token=claimed["fencing_token"],
            human_request={"question": "需要补充哪季报？", "why_needed": "", "options": []},
            output=final_replay_response(waiting["task_id"]),
            model_call={"called": False, "rounds": [],
                        "context_delivery": {"delivered_quotes": [], "dropped_evidence": []}})
        # One budgeted model attempt today (audit event).
        store.record_research_attempt_start(waiting["task_id"], worker_id="w2",
                                            fencing_token=claimed["fencing_token"],
                                            attempt={"round": 1})

        overview = build_overview(self.config, store=store)
        self.assertEqual(overview["tasks"]["status_counts"].get("succeeded"), 1)
        self.assertEqual(overview["tasks"]["status_counts"].get("awaiting_human"), 1)
        awaiting = overview["tasks"]["awaiting_human"]
        self.assertEqual(awaiting[0]["task_id"], waiting["task_id"])
        self.assertEqual(awaiting[0]["question"], "需要补充哪季报？")
        self.assertEqual(overview["budget"]["calls_today"], 1)
        self.assertEqual(overview["budget"]["limit"], 0)
        self.assertFalse(overview["budget"]["enabled"])
        self.assertEqual(overview["tasks"]["consumption"]["consumed_events"], 0)

    def test_missing_task_store_reports_unavailable(self):
        overview = build_overview(self.config, store=None)
        self.assertFalse(overview["tasks"]["available"])
        self.assertEqual(overview["budget"]["calls_today"], 0)


class OverviewHttpCliTests(OverviewTestCase):
    def test_board_serves_overview_endpoint(self):
        import threading
        import urllib.request
        from stock_agent.task_board import create_task_board_server

        store = TaskStore(self.config["task_runtime"]["db_path"])
        self.addCleanup(store.close)
        self.seed_signal("s1")
        server = create_task_board_server(store, host="127.0.0.1", port=0, config=self.config)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        with urllib.request.urlopen(
                "http://127.0.0.1:{0}/api/v1/overview".format(port), timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        for section in ("engine", "market", "events", "tasks", "budget"):
            self.assertIn(section, payload)
        self.assertEqual(payload["engine"]["event_intake_mode"], "task")

    def test_cli_overview_prints_json(self):
        import contextlib
        import io
        from unittest.mock import patch

        from stock_agent.cli import main

        config_file = self.root / "config.json"
        config_file.write_text(json.dumps(self.config), encoding="utf-8")
        with patch.object(sys, "argv", ["stock_agent", "overview", "--config", str(config_file)]), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            main()
        payload = json.loads(captured.getvalue())
        self.assertIn("engine", payload)
        self.assertIn("scope_note", payload)


if __name__ == "__main__":
    unittest.main()
