"""W04-lite tests: automatic public-context supply for research tasks.

Fixtures exercise the market cache, the signals store, and the vault index in
an isolated workspace. No network, no model calls.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.models import AnalyzedSignal, now_iso
from stock_agent.storage import SignalStore
from stock_agent.task_supply import build_public_context, build_task_spec


class SupplyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def scoped(self):
        return project_path_override(lambda value, root=self.root: root / value)

    def _write_cache(self, quotes):
        path = self.root / "data" / "cache" / "market_quotes" / "latest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"generated_at": now_iso(), "quotes": quotes}, ensure_ascii=False), encoding="utf-8")

    def _quote(self, symbol="600519", latest=1500.0, with_time=False):
        payload = {
            "code": symbol,
            "latest": latest,
            "pct_change": 1.2,
            "market_resilience": {"provider": "eastmoney", "fetched_at": now_iso()},
            "observed_at": now_iso(),
        }
        if with_time:
            payload["quote_timestamp"] = "2026-09-18T10:00:00+08:00"
        return {"source": "eastmoney", "source_type": "price", "payload": payload}

    def _signal(self, signal_id, title, url, symbols, example=False, hours_ago=0):
        payload = {"data_classification": "public", "data_provenance": "example" if example else ""}
        published = (datetime.now().astimezone() - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
        return AnalyzedSignal(
            signal_id=signal_id, source="manual", source_type="news", title=title, summary="s",
            url=url, published_at=published, collected_at=now_iso(),
            symbols=list(symbols), payload=payload,
        )

    def test_quote_source_time_honesty(self):
        self._write_cache([self._quote("600519", with_time=True), self._quote("000001", with_time=False)])
        with self.scoped():
            context = build_public_context(["600519", "000001"])
        verified = context["quote_snapshots"]["600519"]
        self.assertEqual(verified["source_time_status"], "verified")
        self.assertEqual(verified["source_time"], "2026-09-18T10:00:00+08:00")
        unverified = context["quote_snapshots"]["000001"]
        self.assertEqual(unverified["source_time_status"], "unverified_missing_upstream_time")
        self.assertIsNone(unverified["source_time"])
        # The fetch/observed time must not be smuggled in as the quote time.
        self.assertNotEqual(unverified["source_time"], unverified["observed_at"])

    def test_missing_quote_reported_not_faked(self):
        self._write_cache([self._quote("600519")])
        with self.scoped():
            context = build_public_context(["600519", "300750"])
        self.assertIn("300750", context["context_notes"]["quotes_missing"])
        self.assertNotIn("300750", context["quote_snapshots"])

    def test_news_dedup_example_exclusion_and_recency(self):
        self._write_cache([])
        store = SignalStore(self.root / "data" / "stock_agent.sqlite3")
        store.upsert_many([
            self._signal("n1", "真实新闻A", "https://news.example/a", ["600519"]),
            self._signal("n2", "真实新闻A转载", "https://news.example/a", ["600519"]),  # same URL -> dedup
            self._signal("n3", "样例新闻", "https://example.com/x", ["600519"], example=True),
            self._signal("n4", "过期新闻", "https://news.example/old", ["600519"], hours_ago=72),
            self._signal("n5", "其他标的", "https://news.example/b", ["000001"]),
        ])
        store.close()
        with self.scoped():
            context = build_public_context(["600519"], lookback_hours=48)
        items = context["news_clusters"].get("news-600519", {}).get("items", [])
        titles = [item["title"] for item in items]
        self.assertEqual(titles, ["真实新闻A"])
        self.assertNotIn("样例新闻", titles)
        self.assertNotIn("过期新闻", titles)

    def test_task_spec_shapes_and_default_replay(self):
        self._write_cache([self._quote("600519")])
        with self.scoped():
            spec = build_task_spec(research_question="评估短期影响", symbols=["600519"])
        self.assertEqual(spec["data_classification"], "public")
        self.assertIn("600519", spec["public_context"]["quote_snapshots"])
        self.assertEqual(spec["replay_response"]["schema_version"], "1.0")
        self.assertTrue(spec["replay_response"]["human_questions"])
        with self.scoped():
            live_spec = build_task_spec(research_question="q", symbols=["600519"], execution_mode="live")
        self.assertNotIn("replay_response", live_spec)
        with self.assertRaises(ValueError):
            build_task_spec(research_question="", symbols=["600519"])


if __name__ == "__main__":
    unittest.main()
