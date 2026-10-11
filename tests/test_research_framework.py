import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.models import AnalyzedSignal, now_iso


try:
    from stock_agent.research_framework import apply_research_framework
except ImportError:
    apply_research_framework = None


def _signal(
    signal_id="s001",
    source="manual",
    source_type="news",
    title="test signal",
    priority="P2",
    priority_score=30.0,
    needs_review=False,
    reasons=None,
    topics=None,
    symbols=None,
    payload=None,
):
    return AnalyzedSignal(
        signal_id=signal_id,
        source=source,
        source_type=source_type,
        title=title,
        summary="summary",
        url="http://example.com",
        published_at=now_iso(),
        collected_at=now_iso(),
        topics=topics or [],
        symbols=symbols or [],
        matched_keywords=[],
        sentiment_score=0.0,
        priority_score=priority_score,
        priority=priority,
        needs_review=needs_review,
        reasons=reasons or [],
        payload=payload or {},
    )


class ResearchFrameworkTests(unittest.TestCase):
    def setUp(self):
        if apply_research_framework is None:
            self.fail("stock_agent.research_framework is not implemented")

    def test_price_signal_routes_to_market_playbook(self):
        signal = _signal(source_type="price", priority="P3", payload={"latest": 10.5})
        signals, stats = apply_research_framework([signal], {})

        track_ids = [track["id"] for track in signals[0].research_tracks]
        self.assertIn("market_price_action", track_ids)
        self.assertGreaterEqual(stats["research_track_counts"]["market_price_action"], 1)
        self.assertGreater(len(signals[0].research_questions), 0)

    def test_high_priority_review_signal_gets_multiple_tracks(self):
        signal = _signal(
            priority="P1",
            needs_review=True,
            reasons=["missing source", "risk keyword"],
            topics=["AI"],
            symbols=["ABC"],
        )
        signals, stats = apply_research_framework([signal], {})

        track_ids = {track["id"] for track in signals[0].research_tracks}
        self.assertIn("event_impact", track_ids)
        self.assertIn("risk_challenge", track_ids)
        self.assertIn("llm_deep_dive", track_ids)
        self.assertEqual(stats["research_signal_count"], 1)

    def test_custom_playbook_can_be_added_from_config(self):
        signal = _signal(reasons=["large order mentioned"])
        config = {
            "research_framework": {
                "enabled": True,
                "mode": "config_only",
                "playbooks": [
                    {
                        "id": "order_verification",
                        "label": "Order Verification",
                        "reason_contains": ["large order"],
                        "questions": ["Verify customer and delivery schedule"],
                        "focus": ["order_quality"],
                    }
                ],
            }
        }
        signals, stats = apply_research_framework([signal], config)

        track_ids = [track["id"] for track in signals[0].research_tracks]
        self.assertIn("order_verification", track_ids)
        self.assertIn("Verify customer and delivery schedule", signals[0].research_questions)
        self.assertEqual(stats["research_playbooks"], 1)

    def test_disabled_framework_leaves_signals_unchanged(self):
        signal = _signal()
        signals, stats = apply_research_framework([signal], {"research_framework": {"enabled": False}})

        self.assertEqual(signals[0].research_tracks, [])
        self.assertEqual(signals[0].research_questions, [])
        self.assertFalse(stats["research_framework_enabled"])


class ResearchFrameworkIntegrationTests(unittest.TestCase):
    def test_llm_prompt_contains_research_context(self):
        from stock_agent.llm_agent import _build_user_prompt

        signal = _signal(priority="P1", needs_review=True)
        signals, _stats = apply_research_framework([signal], {})
        prompt = _build_user_prompt(signals)

        self.assertIn("research_tracks", prompt)
        self.assertIn("research_questions", prompt)
        self.assertIn("risk_challenge", prompt)

    @patch.dict(os.environ, {"LLM_API_KEY": "", "DEEPSEEK_API_KEY": ""}, clear=False)
    @patch("stock_agent.pipeline.collect_sources")
    def test_agent_pipeline_stats_include_research_framework(self, mock_collect):
        from stock_agent.models import RawSignal
        from stock_agent.pipeline import run_agent_pipeline

        mock_collect.return_value = [
            RawSignal(source="manual", source_type="news", title="major test order", summary="large order")
        ]
        config = {
            "topics": [],
            "priority_rules": {
                "p0_keywords": [],
                "p1_keywords": ["major"],
                "positive_keywords": ["order"],
                "negative_keywords": [],
                "review_keywords": [],
                "min_priority_for_alert": "P1",
            },
            "llm_agent": {"enabled": True, "min_priority": "P3"},
            "report": {"daily_top_n": 20},
            "review": {"fields": ["signal_id"]},
            "storage": {"sqlite_path": "data/test_research_framework.sqlite3"},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            for sub in ["reports/daily", "reports/json", "reports/alerts", "reports/actions", "data/review", "data"]:
                (tmp / sub).mkdir(parents=True, exist_ok=True)

            def project_path_side(path_value):
                return tmp / path_value

            with patch("stock_agent.pipeline.project_path", side_effect=project_path_side), \
                 patch("stock_agent.reports.project_path", side_effect=project_path_side), \
                 patch("stock_agent.market_resilience.project_path", side_effect=project_path_side), \
                 patch("stock_agent.source_health.project_path", side_effect=project_path_side), \
                 patch("stock_agent.source_remediation.project_path", side_effect=project_path_side), \
                 patch("stock_agent.pipeline.ensure_directories"):
                result = run_agent_pipeline(config)

            self.assertIn("research_track_counts", result.stats)
            self.assertIn("research_agenda", result.stats)
            with open(result.json_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertIn("research_tracks", data["signals"][0])


if __name__ == "__main__":
    unittest.main()
