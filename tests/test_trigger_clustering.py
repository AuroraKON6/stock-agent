"""GLM-09 slice 2: same-story clustering and cross-cycle cooldowns."""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_agent.config import load_config, project_path_override
from stock_agent.models import AnalyzedSignal
from stock_agent.research_trigger import cluster_key, research_trigger_config
from stock_agent.storage import SignalStore
from stock_agent.task_intake import rescan_pending_events
from stock_agent.task_runtime import TaskStore

sys.path.insert(0, str(Path(__file__).resolve().parent))

NOW = datetime.now(timezone.utc)


class ClusterKeyTests(unittest.TestCase):
    def row(self, title, symbols=("600519",)):
        return {"title": title, "symbols": list(symbols)}

    def test_same_story_variants_share_key(self):
        base = cluster_key(self.row("茅台发布2026年年度报告！"))
        variants = [
            cluster_key(self.row("茅台 发布 2026年年度报告!")),
            cluster_key(self.row("茅台发布2026年年度报告")),
            cluster_key(self.row("【快讯】茅台发布2026年年度报告。")),
        ]
        for key in variants:
            self.assertEqual(key, base)

    def test_different_stories_and_symbols_differ(self):
        base = cluster_key(self.row("茅台发布年度报告"))
        self.assertNotEqual(cluster_key(self.row("茅台发布季度报告")), base)
        self.assertNotEqual(cluster_key(self.row("茅台发布年度报告", symbols=("000001",))), base)

    def test_config_fields_validated(self):
        defaults = research_trigger_config({})
        self.assertTrue(defaults["cluster_within_cycle"])
        self.assertEqual(defaults["cluster_cooldown_minutes"], 0)
        with self.assertRaises(ValueError):
            research_trigger_config({"task_runtime": {"research_trigger": {"cluster_cooldown_minutes": -5}}})
        with self.assertRaises(ValueError):
            research_trigger_config({"task_runtime": {"research_trigger": {"cluster_within_cycle": "yes"}}})


class ClusteringIntegrationTests(unittest.TestCase):
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

    def seed(self, sid, *, title, url, source="manual", source_type="news",
             priority="P2", symbol="600519", content_version=None):
        signal_store = SignalStore(Path(self.config["storage"]["sqlite_path"]))
        try:
            payload = {"data_classification": "public"}
            if content_version:
                payload["content_version"] = content_version
            signal_store.upsert_many([AnalyzedSignal(
                signal_id=sid, source=source, source_type=source_type, title=title,
                summary="s", url=url,
                published_at=(NOW - timedelta(hours=1)).isoformat(),
                collected_at=NOW.isoformat(), symbols=[symbol],
                priority=priority, payload=payload)])
        finally:
            signal_store.close()

    def test_same_story_multiple_sources_one_task(self):
        self.seed("n1", title="茅台发布2026年年度报告", url="https://a.example/1", source="eastmoney")
        self.seed("n2", title="茅台 发布 2026年年度报告!", url="https://b.example/2", source="sina")
        self.seed("n3", title="【快讯】茅台发布2026年年度报告。", url="https://c.example/3", source="xinhua")
        report = rescan_pending_events(self.store, config=self.config)
        self.assertEqual(report["created"], 1)
        self.assertEqual(report["skipped_by_cluster"], 2)
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_filing_wins_cluster_over_news(self):
        self.seed("n1", title="某公司重大资产重组公告", url="https://a.example/1", source_type="news")
        self.seed("f1", title="某公司重大资产重组公告", url="https://cninfo.example/2",
                  source_type="filing", priority="P3")
        report = rescan_pending_events(self.store, config=self.config)
        self.assertEqual(report["created"], 1)
        task = self.store.list_tasks()[0]
        self.assertIn("cninfo.example", task["input_payload"]["origin"]["url"])

    def test_revision_same_cluster_reopens(self):
        self.seed("n1", title="某公司业绩预告", url="https://a.example/1")
        rescan_pending_events(self.store, config=self.config)
        self.seed("n1v2", title="某公司业绩预告", url="https://a.example/1v2",
                  source_type="filing", content_version="2")
        report = rescan_pending_events(self.store, config=self.config)
        self.assertEqual(report["created"], 1)
        self.assertEqual(report["skipped_by_cluster"], 0)
        self.assertEqual(len(self.store.list_tasks()), 2)

    def test_cross_cycle_cluster_cooldown_skips_new_same_story(self):
        self.config["task_runtime"]["research_trigger"] = {"cluster_cooldown_minutes": 60}
        self.seed("n1", title="某公司获得大额订单", url="https://a.example/1")
        first = rescan_pending_events(self.store, config=self.config)
        self.assertEqual(first["created"], 1)
        # A different outlet carries the same story AFTER the first cycle.
        self.seed("n9", title="某公司获得大额订单", url="https://z.example/9", source="late-outlet")
        second = rescan_pending_events(self.store, config=self.config)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["skipped_by_cluster"], 1)

    def test_cross_cycle_symbol_cooldown_uses_recent_tasks(self):
        self.config["task_runtime"]["research_trigger"] = {"symbol_cooldown_minutes": 60}
        self.seed("f1", title="某公司完全不同的一则公告", url="https://a.example/f1",
                  source_type="filing")
        first = rescan_pending_events(self.store, config=self.config)
        self.assertEqual(first["created"], 1)
        # A DIFFERENT story for the SAME symbol within the window.
        self.seed("f2", title="另一件毫不相关的事", url="https://a.example/f2",
                  source_type="filing")
        second = rescan_pending_events(self.store, config=self.config)
        self.assertEqual(second["created"], 0)
        self.assertGreaterEqual(second.get("skipped_by_trigger", 0), 1)


if __name__ == "__main__":
    unittest.main()
