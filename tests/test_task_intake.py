"""GLM-04 unified task intake: event-identity dedup, revision chain, crash-window-safe consumption."""
import contextlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_agent.config import load_config, project_path_override
from stock_agent.event_contract import event_identity
from stock_agent.models import AnalyzedSignal
from stock_agent.storage import SignalStore
from stock_agent.task_intake import (
    ensure_event_task,
    ensure_question_task,
    rescan_pending_events,
)
from stock_agent.task_runtime import TaskStore

sys.path.insert(0, str(Path(__file__).resolve().parent))

NOW = datetime.now(timezone.utc)


class TaskIntakeTestCase(unittest.TestCase):
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
            # Rescan tests exercise the task engine; the single-engine gate
            # requires the event stream to be declared as task-owned.
            "event_intake_mode": "task",
        }
        self.store = TaskStore(self.config["task_runtime"]["db_path"])
        self.addCleanup(self.store.close)

    def seed_signal(self, sid, *, title="某公司发布年度报告", url="https://www.cninfo.com.cn/x/1",
                    source="manual", kind="filing", symbols=("600519",), hours=1,
                    payload=None):
        signal_store = SignalStore(Path(self.config["storage"]["sqlite_path"]))
        try:
            signal_store.upsert_many([AnalyzedSignal(
                signal_id=sid, source=source, source_type=kind, title=title,
                summary="fixture", url=url,
                published_at=(NOW - timedelta(hours=hours)).isoformat(),
                collected_at=NOW.isoformat(), symbols=list(symbols),
                payload=payload if payload is not None else {"data_classification": "public"},
            )])
        finally:
            signal_store.close()

    def event(self, **overrides):
        base = {
            "source_id": "manual",
            "url": "https://www.cninfo.com.cn/x/1",
            "title": "某公司发布年度报告",
            "content_version": "1",
            "symbols": ["600519"],
            "signal_id": "sig-a",
        }
        base.update(overrides)
        return base


class EventTaskDedupTests(TaskIntakeTestCase):
    def test_same_event_twice_returns_existing_task(self):
        first = ensure_event_task(self.store, event=self.event(), config=self.config)
        second = ensure_event_task(self.store, event=self.event(), config=self.config)
        self.assertEqual(first["status"], "RECORDED")
        self.assertEqual(second["status"], "DUPLICATE")
        self.assertEqual(first["task"]["task_id"], second["task"]["task_id"])
        self.assertEqual(len(self.store.list_tasks()), 1)
        self.assertEqual(self.store.consumption_summary()["consumed_events"], 1)

    def test_revision_creates_traceable_new_task(self):
        first = ensure_event_task(self.store, event=self.event(), config=self.config)
        revised = ensure_event_task(self.store, event=self.event(content_version="2"),
                                    config=self.config)
        self.assertEqual(revised["status"], "RECORDED")
        self.assertNotEqual(first["task"]["task_id"], revised["task"]["task_id"])
        self.assertEqual(revised["supersedes_task_id"], first["task"]["task_id"])
        origin = revised["task"]["input_payload"]["origin"]
        self.assertEqual(origin["kind"], "event")
        self.assertEqual(origin["content_version"], "2")
        self.assertEqual(self.store.consumption_summary()["consumed_events"], 2)

    def test_different_purpose_is_a_separate_lane(self):
        first = ensure_event_task(self.store, event=self.event(), config=self.config)
        second = ensure_event_task(self.store, event=self.event(), config=self.config,
                                   purpose="event_monitoring_v1")
        self.assertEqual(second["status"], "RECORDED")
        self.assertNotEqual(first["task"]["task_id"], second["task"]["task_id"])

    def test_event_identity_matches_contract(self):
        result = ensure_event_task(self.store, event=self.event(), config=self.config)
        expected = event_identity(source_id="manual", url="https://www.cninfo.com.cn/x/1",
                                  title="某公司发布年度报告", content_version="1")
        self.assertEqual(result["event_key"], expected)

    def test_task_input_stays_public_with_origin(self):
        result = ensure_event_task(self.store, event=self.event(), config=self.config)
        payload = result["task"]["input_payload"]
        self.assertEqual(payload["data_classification"], "public")
        self.assertIn("public_context", payload)
        self.assertEqual(payload["origin"]["kind"], "event")


class CrashWindowRecoveryTests(TaskIntakeTestCase):
    def test_ledger_converges_when_task_committed_without_consumption(self):
        # Simulate the crash window: a task for this event key already exists
        # (created directly), but the consumption row was never written.
        from stock_agent.task_supply import build_task_spec
        spec = build_task_spec(research_question="研究该公告", symbols=["600519"],
                               execution_mode="replay", config=self.config)
        key = event_identity(source_id="manual", url="https://www.cninfo.com.cn/x/1",
                             title="某公司发布年度报告", content_version="1")
        spec["origin"] = {"kind": "event", "event_key": key, "content_version": "1"}
        task, status = self.store.create_task(
            title="研究该公告", input_payload=spec, execution_mode="replay",
            idempotency_key="event::{0}::event_research_v1".format(key))
        self.assertEqual(status, "RECORDED")
        self.assertEqual(self.store.consumption_summary()["consumed_events"], 0)
        result = ensure_event_task(self.store, event=self.event(), config=self.config)
        self.assertEqual(result["status"], "DUPLICATE")
        self.assertEqual(result["task"]["task_id"], task["task_id"])
        self.assertEqual(self.store.consumption_summary()["consumed_events"], 1)

    def test_consumption_row_is_atomic_with_task_creation(self):
        ensure_event_task(self.store, event=self.event(), config=self.config)
        rows = self.store.consumed_event_keys(["nonexistent"])
        self.assertEqual(rows, set())
        summary = self.store.consumption_summary()
        self.assertEqual(summary["consumed_events"], 1)
        self.assertEqual(summary["orphan_tasks"], 0)


class RescanTests(TaskIntakeTestCase):
    def test_rescan_creates_tasks_only_for_unconsumed_signals(self):
        self.seed_signal("sig-a")
        self.seed_signal("sig-b", url="https://www.cninfo.com.cn/x/2", title="另一公告")
        report = rescan_pending_events(self.store, config=self.config)
        self.assertEqual(report["created"], 2)
        self.assertEqual(report["duplicates"], 0)
        second = rescan_pending_events(self.store, config=self.config)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["duplicates"], 0)
        self.assertEqual(second["skipped"], 2)
        self.assertEqual(len(self.store.list_tasks()), 2)

    def test_late_arriving_signal_is_not_lost_by_high_watermark(self):
        self.seed_signal("sig-a")
        first = rescan_pending_events(self.store, config=self.config)
        self.assertEqual(first["created"], 1)
        # The late event arrives AFTER the first scan; no source-time watermark
        # may skip it even though its publication predates the scan.
        self.seed_signal("sig-late", url="https://www.cninfo.com.cn/x/3",
                         title="迟到公告", hours=48)
        second = rescan_pending_events(self.store, config=self.config)
        self.assertEqual(second["created"], 1)
        self.assertIn("sig-late", second["created_signal_ids"])

    def test_example_provenance_and_private_signals_are_skipped(self):
        self.seed_signal("sig-ex", payload={"data_classification": "public",
                                            "data_provenance": "example"})
        self.seed_signal("sig-priv", url="https://www.cninfo.com.cn/x/9",
                         payload={"data_classification": "internal"})
        report = rescan_pending_events(self.store, config=self.config)
        self.assertEqual(report["created"], 0)
        self.assertEqual(report["skipped"], 0)
        self.assertEqual(report["rejected_by_selection"], 2)
        self.assertEqual(len(self.store.list_tasks()), 0)


class QuestionTaskTests(TaskIntakeTestCase):
    def test_same_question_returns_existing_task(self):
        first = ensure_question_task(self.store, research_question="白酒板块龙头近期风险",
                                     symbols=["600519"], config=self.config)
        second = ensure_question_task(self.store, research_question="白酒板块龙头近期风险",
                                      symbols=["600519"], config=self.config)
        self.assertEqual(first["status"], "RECORDED")
        self.assertEqual(second["status"], "DUPLICATE")
        self.assertEqual(first["task"]["task_id"], second["task"]["task_id"])
        self.assertEqual(first["task"]["input_payload"]["origin"]["kind"], "question")

    def test_different_question_creates_new_task(self):
        first = ensure_question_task(self.store, research_question="问题一",
                                     symbols=["600519"], config=self.config)
        second = ensure_question_task(self.store, research_question="问题二",
                                      symbols=["600519"], config=self.config)
        self.assertEqual(second["status"], "RECORDED")
        self.assertNotEqual(first["task"]["task_id"], second["task"]["task_id"])


class TaskRescanCliTests(TaskIntakeTestCase):
    def test_cli_task_rescan_creates_and_is_idempotent(self):
        import contextlib
        import io
        from unittest.mock import patch

        from stock_agent.cli import main

        self.seed_signal("sig-cli")
        config_file = self.root / "config.json"
        config_file.write_text(json.dumps(self.config), encoding="utf-8")
        argv = ["stock_agent", "task-rescan", "--config", str(config_file)]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as captured:
            main()
        report = json.loads(captured.getvalue())["rescan"]
        self.assertEqual(report["created"], 1)
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as captured:
            main()
        report = json.loads(captured.getvalue())["rescan"]
        self.assertEqual(report["created"], 0)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(len(self.store.list_tasks()), 1)


if __name__ == "__main__":
    unittest.main()
