"""GLM-09: research trigger contract - thresholds, caps, revisions."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_agent.config import load_config, project_path_override
from stock_agent.models import AnalyzedSignal
from stock_agent.research_trigger import (
    decide_trigger,
    research_trigger_config,
)
from stock_agent.storage import SignalStore
from stock_agent.task_intake import rescan_pending_events
from stock_agent.task_runtime import TaskStore

sys.path.insert(0, str(Path(__file__).resolve().parent))

NOW = datetime.now(timezone.utc)


class TriggerDecisionTests(unittest.TestCase):
    def row(self, *, source_type="filing", priority="P2", content_version="1", symbol="600519"):
        return {
            "signal_id": "sig-t",
            "source": "eastmoney_filing",
            "source_type": source_type,
            "title": "某公告",
            "summary": "",
            "url": "https://www.cninfo.com.cn/x/1",
            "published_at": (NOW - timedelta(hours=1)).isoformat(),
            "collected_at": NOW.isoformat(),
            "symbols": [symbol],
            "payload": {"data_classification": "public",
                        "content_version": content_version},
            "priority": priority,
        }

    def test_filing_always_triggers_regardless_of_priority(self):
        for priority in ("P0", "P1", "P2", "P3"):
            decision = decide_trigger(self.row(priority=priority), config={})
            self.assertTrue(decision["trigger"], priority)

    def test_news_needs_at_least_p2_by_default(self):
        self.assertFalse(decide_trigger(self.row(source_type="news", priority="P3"), config={})["trigger"])
        self.assertTrue(decide_trigger(self.row(source_type="news", priority="P2"), config={})["trigger"])
        self.assertTrue(decide_trigger(self.row(source_type="news", priority="P1"), config={})["trigger"])

    def test_unknown_source_type_never_triggers(self):
        decision = decide_trigger(self.row(source_type="price"), config={})
        self.assertFalse(decision["trigger"])
        self.assertIn("source_type", decision["reason"])

    def test_revision_always_triggers_even_when_capped_or_low_priority(self):
        state = {"symbol_cycle_counts": {"600519": 5},
                 "symbol_last_trigger": {"600519": (NOW + timedelta(hours=1)).isoformat()}}
        decision = decide_trigger(
            self.row(source_type="news", priority="P3", content_version="2"),
            config={}, state=state)
        self.assertTrue(decision["trigger"])
        self.assertEqual(decision["reason"], "content revision")

    def test_per_symbol_cycle_cap(self):
        config = {"task_runtime": {"research_trigger": {"max_per_symbol_per_cycle": 2}}}
        state = {"symbol_cycle_counts": {"600519": 2}}
        decision = decide_trigger(self.row(), config=config, state=state)
        self.assertFalse(decision["trigger"])
        self.assertIn("cap", decision["reason"])

    def test_symbol_cooldown_blocks_but_records(self):
        config = {"task_runtime": {"research_trigger": {"symbol_cooldown_minutes": 60}}}
        now_iso = NOW.isoformat()
        state = {"symbol_last_trigger": {"600519": now_iso}}
        decision = decide_trigger(self.row(), config=config, state=state, now=NOW)
        self.assertFalse(decision["trigger"])
        self.assertIn("cooldown", decision["reason"])

    def test_config_validation_rejects_bad_values(self):
        with self.assertRaises(ValueError):
            research_trigger_config({"task_runtime": {"research_trigger": {"news_min_priority": "P9"}}})
        with self.assertRaises(ValueError):
            research_trigger_config({"task_runtime": {"research_trigger": {"max_per_symbol_per_cycle": -1}}})
        defaults = research_trigger_config({})
        self.assertEqual(defaults["news_min_priority"], "P2")
        self.assertEqual(defaults["max_per_symbol_per_cycle"], 0)
        self.assertEqual(defaults["symbol_cooldown_minutes"], 0)


class RescanTriggerIntegrationTests(unittest.TestCase):
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

    def seed(self, sid, *, source_type, priority, url, title="某新闻", content_version=None, symbol="600519"):
        signal_store = SignalStore(Path(self.config["storage"]["sqlite_path"]))
        try:
            payload = {"data_classification": "public"}
            if content_version:
                payload["content_version"] = content_version
            signal_store.upsert_many([AnalyzedSignal(
                signal_id=sid, source="manual", source_type=source_type, title=title,
                summary="s", url=url,
                published_at=(NOW - timedelta(hours=1)).isoformat(),
                collected_at=NOW.isoformat(), symbols=[symbol],
                priority=priority, payload=payload)])
        finally:
            signal_store.close()

    def test_rescan_applies_trigger_policy(self):
        self.seed("sig-filing", source_type="filing", priority="P3",
                  url="https://www.cninfo.com.cn/x/f", title="甲公司公告")   # filing: triggers
        self.seed("sig-low", source_type="news", priority="P3",
                  url="https://www.cninfo.com.cn/x/1", title="低优新闻")     # news P3: skipped
        self.seed("sig-high", source_type="news", priority="P1",
                  url="https://www.cninfo.com.cn/x/2", title="高优新闻")     # news P1: triggers
        report = rescan_pending_events(self.store, config=self.config)
        self.assertEqual(report["created"], 2)
        self.assertEqual(report["skipped_by_trigger"], 1)
        self.assertEqual(report["skipped_by_cluster"], 0)
        tasks = self.store.list_tasks()
        self.assertEqual(len(tasks), 2)

    def test_per_symbol_cap_in_one_cycle(self):
        self.config["task_runtime"]["research_trigger"] = {"max_per_symbol_per_cycle": 1}
        self.seed("sig-a", source_type="filing", priority="P1", url="https://x.example/a", title="甲事项公告")
        self.seed("sig-b", source_type="filing", priority="P1", url="https://x.example/b", title="乙事项公告")
        self.seed("sig-c", source_type="filing", priority="P1", url="https://x.example/c", title="丙事项公告")
        report = rescan_pending_events(self.store, config=self.config)
        self.assertEqual(report["created"], 1)
        self.assertGreaterEqual(report["skipped_by_trigger"], 2)
        self.assertEqual(report["skipped_by_cluster"], 0)

    def test_revision_bypasses_cap(self):
        self.config["task_runtime"]["research_trigger"] = {"max_per_symbol_per_cycle": 1}
        self.seed("sig-a", source_type="filing", priority="P1", url="https://x.example/a", title="丁事项公告")
        self.seed("sig-rev", source_type="filing", priority="P3", url="https://x.example/a",
                  content_version="2", title="丁事项公告")
        report = rescan_pending_events(self.store, config=self.config)
        self.assertEqual(report["created"], 2)


if __name__ == "__main__":
    unittest.main()
