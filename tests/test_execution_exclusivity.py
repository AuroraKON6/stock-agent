"""GLM-04 remainder: one research engine per event stream (batch vs task exclusivity)."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_agent.config import load_config, project_path_override
from stock_agent.execution_mode import (
    EVENT_INTAKE_TASK,
    EVENT_INTAKE_BATCH,
    resolve_event_intake_mode,
    assert_agent_run_allowed,
    assert_event_rescan_allowed,
)
from stock_agent.models import AnalyzedSignal
from stock_agent.storage import SignalStore
from stock_agent.task_intake import TaskIntakeError, ensure_question_task, rescan_pending_events
from stock_agent.task_runtime import TaskStore

sys.path.insert(0, str(Path(__file__).resolve().parent))

NOW = datetime.now(timezone.utc)


class ExclusivityTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        base = load_config(str(Path(__file__).resolve().parents[1] / "config/watchlist.example.json"))
        self.config = dict(base)
        self.config["storage"] = {"sqlite_path": str(self.root / "signals.sqlite3")}
        self.config["task_runtime"] = {"db_path": str(self.root / "tasks.sqlite3")}
        self.store = TaskStore(self.config["task_runtime"]["db_path"])
        self.addCleanup(self.store.close)


class ModeResolutionTests(ExclusivityTestCase):
    def test_default_mode_is_batch(self):
        self.assertEqual(resolve_event_intake_mode(self.config), EVENT_INTAKE_BATCH)

    def test_explicit_task_mode(self):
        self.config["task_runtime"]["event_intake_mode"] = "task"
        self.assertEqual(resolve_event_intake_mode(self.config), EVENT_INTAKE_TASK)

    def test_invalid_mode_rejected(self):
        self.config["task_runtime"]["event_intake_mode"] = "both"
        with self.assertRaises(ValueError):
            resolve_event_intake_mode(self.config)


class AgentRunGateTests(ExclusivityTestCase):
    def test_agent_run_allowed_in_batch_mode(self):
        assert_agent_run_allowed(self.config)

    def test_agent_run_refused_in_task_mode(self):
        self.config["task_runtime"]["event_intake_mode"] = "task"
        with self.assertRaises(PermissionError) as raised:
            assert_agent_run_allowed(self.config)
        message = str(raised.exception)
        self.assertIn("task", message.lower())
        self.assertIn("event_intake_mode", message)

    def test_refusal_does_not_depend_on_signal_state(self):
        self.config["task_runtime"]["event_intake_mode"] = "task"
        signal_store = SignalStore(Path(self.config["storage"]["sqlite_path"]))
        try:
            signal_store.upsert_many([AnalyzedSignal(
                signal_id="sig-x", source="manual", source_type="filing",
                title="t", summary="s", url="https://www.cninfo.com.cn/x",
                published_at=(NOW - timedelta(hours=1)).isoformat(),
                collected_at=NOW.isoformat(), symbols=["600519"],
                payload={"data_classification": "public"})])
        finally:
            signal_store.close()
        with self.assertRaises(PermissionError):
            assert_agent_run_allowed(self.config)


class RescanGateTests(ExclusivityTestCase):
    def seed_signal(self, sid, url="https://www.cninfo.com.cn/x/1", title="某公告"):
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

    def test_rescan_allowed_in_task_mode(self):
        self.config["task_runtime"]["event_intake_mode"] = "task"
        self.seed_signal("sig-a")
        report = rescan_pending_events(self.store, config=self.config)
        self.assertEqual(report["created"], 1)

    def test_rescan_refused_in_batch_mode(self):
        self.seed_signal("sig-a")
        with self.assertRaises(TaskIntakeError) as raised:
            rescan_pending_events(self.store, config=self.config)
        self.assertIn("batch", str(raised.exception).lower())

    def test_question_tasks_are_mode_independent(self):
        # User-initiated research questions are not the event stream; they
        # never double-analyze scheduled signals and stay available in both
        # modes.
        first = ensure_question_task(self.store, research_question="独立问题",
                                     symbols=["600519"], config=self.config)
        self.assertEqual(first["status"], "RECORDED")
        self.config["task_runtime"]["event_intake_mode"] = "task"
        second = ensure_question_task(self.store, research_question="另一问题",
                                      symbols=["600519"], config=self.config)
        self.assertEqual(second["status"], "RECORDED")

    def test_batch_mode_rescan_refusal_is_not_bypassable_by_purpose_renaming(self):
        with self.assertRaises(TaskIntakeError):
            rescan_pending_events(self.store, config=self.config, purpose="other_lane")


if __name__ == "__main__":
    unittest.main()
