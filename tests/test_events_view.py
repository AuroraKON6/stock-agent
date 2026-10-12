"""GLM-10 events view: deduped events with sources, revisions, linked tasks."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_agent.config import load_config, project_path_override
from stock_agent.events_view import build_events_view
from stock_agent.models import AnalyzedSignal
from stock_agent.storage import SignalStore
from stock_agent.task_intake import rescan_pending_events
from stock_agent.task_runtime import TaskStore

sys.path.insert(0, str(Path(__file__).resolve().parent))

NOW = datetime.now(timezone.utc)


class EventsViewTestCase(unittest.TestCase):
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

    def seed(self, sid, *, title, url, source="manual", source_type="filing",
             priority="P1", symbol="600519", content_version=None, hours_ago=1,
             provenance=""):
        signal_store = SignalStore(Path(self.config["storage"]["sqlite_path"]))
        try:
            payload = {"data_classification": "public"}
            if provenance:
                payload["data_provenance"] = provenance
            if content_version:
                payload["content_version"] = content_version
            signal_store.upsert_many([AnalyzedSignal(
                signal_id=sid, source=source, source_type=source_type, title=title,
                summary="s", url=url,
                published_at=(NOW - timedelta(hours=hours_ago)).isoformat(),
                collected_at=NOW.isoformat(), symbols=[symbol],
                priority=priority, payload=payload)])
        finally:
            signal_store.close()


class ClusterDedupTests(EventsViewTestCase):
    def test_same_story_multiple_sources_dedup_into_one_event(self):
        self.seed("n1", title="茅台发布年度报告", url="https://a.example/1", source="eastmoney",
                  source_type="news", hours_ago=2)
        self.seed("n2", title="【快讯】茅台发布年度报告。", url="https://b.example/2", source="sina",
                  source_type="news", hours_ago=1)
        view = build_events_view(self.config, store=None)
        self.assertEqual(len(view["events"]), 1)
        event = view["events"][0]
        self.assertEqual(event["report_count"], 2)
        self.assertEqual(sorted(event["sources"]), ["eastmoney", "sina"])
        self.assertFalse(event["revision"])
        self.assertEqual(event["symbols"], ["600519"])

    def test_revision_flagged_with_versions(self):
        self.seed("f1", title="业绩预告", url="https://a.example/1", hours_ago=3)
        self.seed("f2", title="业绩预告", url="https://a.example/1v2", content_version="2",
                  hours_ago=1)
        view = build_events_view(self.config, store=None)
        event = view["events"][0]
        self.assertTrue(event["revision"])
        self.assertEqual(event["versions"], ["1", "2"])

    def test_distinct_stories_stay_separate_and_ordered(self):
        self.seed("old", title="甲事项公告", url="https://a.example/a", hours_ago=5)
        self.seed("new", title="乙事项公告", url="https://a.example/b", hours_ago=1)
        view = build_events_view(self.config, store=None)
        self.assertEqual(len(view["events"]), 2)
        self.assertEqual(view["events"][0]["title"], "乙事项公告")

    def test_example_provenance_excluded(self):
        self.seed("ex", title="样例事件", url="https://a.example/x", provenance="example")
        view = build_events_view(self.config, store=None)
        self.assertEqual(view["events"], [])


class LinkedTaskTests(EventsViewTestCase):
    def test_linked_tasks_with_status_from_consumption(self):
        self.seed("f1", title="关联任务事件", url="https://a.example/f1")
        report = rescan_pending_events(self.store, config=self.config)
        self.assertEqual(report["created"], 1)
        view = build_events_view(self.config, store=self.store)
        event = view["events"][0]
        self.assertEqual(len(event["linked_tasks"]), 1)
        self.assertEqual(event["linked_tasks"][0]["status"], "queued")

    def test_unlinked_event_has_no_tasks(self):
        self.seed("n1", title="未触发事件", url="https://a.example/n", source_type="news",
                  priority="P3")  # news below threshold -> never triggered
        view = build_events_view(self.config, store=self.store)
        self.assertEqual(view["events"][0]["linked_tasks"], [])


class HttpCliTests(EventsViewTestCase):
    def test_board_serves_events_endpoint(self):
        import threading
        import time
        import urllib.request
        from stock_agent.task_board import create_task_board_server

        self.seed("f1", title="端点事件", url="https://a.example/e1")
        server = create_task_board_server(self.store, host="127.0.0.1", port=0, config=self.config)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        # Readiness poll: connect-before-accept shows as ConnectionRefused
        # under full-suite load on Windows.
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
                "http://127.0.0.1:{0}/api/v1/events-view".format(port), timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["events"][0]["title"], "端点事件")
        self.assertIn("scope_note", payload)

    def test_cli_events_view_prints_json(self):
        import contextlib
        import io
        from unittest.mock import patch

        from stock_agent.cli import main

        self.seed("f1", title="CLI 事件", url="https://a.example/c1")
        config_file = self.root / "config.json"
        config_file.write_text(json.dumps(self.config), encoding="utf-8")
        with patch.object(sys, "argv", ["stock_agent", "events-view", "--config", str(config_file)]), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            main()
        payload = json.loads(captured.getvalue())
        self.assertEqual(payload["events"][0]["title"], "CLI 事件")


if __name__ == "__main__":
    unittest.main()
