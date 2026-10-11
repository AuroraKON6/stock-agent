import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.models import AnalyzedSignal, RawSignal, now_iso


try:
    from stock_agent.research_team import run_research_team
except ImportError:
    run_research_team = None


def _signal(
    signal_id="s001",
    source="manual",
    source_type="news",
    title="test signal",
    priority="P2",
    priority_score=30.0,
    needs_review=False,
    reasons=None,
    research_tracks=None,
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
        topics=[],
        symbols=[],
        matched_keywords=[],
        sentiment_score=0.0,
        priority_score=priority_score,
        priority=priority,
        needs_review=needs_review,
        reasons=reasons or [],
        payload=payload or {},
        research_tracks=research_tracks or [],
    )


class ResearchTeamTests(unittest.TestCase):
    def setUp(self):
        if run_research_team is None:
            self.fail("stock_agent.research_team is not implemented")

    def test_default_team_has_realistic_roles_and_supervisor(self):
        _signals, team, stats = run_research_team([], {}, {})

        role_ids = {role["id"] for role in team["roles"]}
        self.assertIn("chief_research_coordinator", role_ids)
        self.assertIn("information_collector", role_ids)
        self.assertIn("market_watcher", role_ids)
        self.assertIn("risk_manager", role_ids)
        self.assertEqual(team["supervisor"]["id"], "user")
        self.assertEqual(stats["team_role_count"], len(team["roles"]))

    def test_price_signal_is_assigned_to_market_watcher_with_kline_watch(self):
        signal = _signal(
            source_type="price",
            title="ABC price snapshot",
            research_tracks=[{"id": "market_price_action", "label": "Market Price Action"}],
            payload={"latest": 10.5, "pct_change": 4.1},
        )
        signals, team, _stats = run_research_team([signal], {}, {})

        assignments = signals[0].team_assignments
        role_ids = {item["role_id"] for item in assignments}
        self.assertIn("market_watcher", role_ids)
        self.assertTrue(any("K-line" in item["task"] for item in assignments))
        self.assertTrue(any(msg["from"] == "market_watcher" for msg in team["messages"]))

    def test_high_priority_signal_triggers_risk_and_strategy_discussion(self):
        signal = _signal(
            priority="P1",
            needs_review=True,
            reasons=["missing source"],
            research_tracks=[
                {"id": "risk_challenge", "label": "Risk Challenge"},
                {"id": "llm_deep_dive", "label": "LLM Deep Dive"},
            ],
        )
        signals, team, _stats = run_research_team([signal], {}, {})

        role_ids = {item["role_id"] for item in signals[0].team_assignments}
        self.assertIn("risk_manager", role_ids)
        self.assertIn("strategy_reviewer", role_ids)
        self.assertTrue(team["supervisor_brief"]["decision_required"])
        self.assertTrue(any(msg["to"] == "strategy_reviewer" for msg in team["messages"]))

    def test_team_output_never_contains_auto_trade_actions(self):
        signal = _signal(priority="P1", needs_review=True)
        _signals, team, _stats = run_research_team([signal], {}, {})

        self.assertIn("no_auto_trading", team["guardrails"])
        serialized = json.dumps(team, ensure_ascii=False).lower()
        self.assertNotIn("place_order", serialized)
        self.assertNotIn("auto_execute_trade", serialized)

    def test_can_override_team_roles_from_config(self):
        config = {
            "research_team": {
                "enabled": True,
                "roles": [
                    {
                        "id": "sector_specialist",
                        "name": "Sector Specialist",
                        "responsibilities": ["track sector chain"],
                        "source_types": ["news"],
                    }
                ],
            }
        }
        signal = _signal(source_type="news")
        signals, team, _stats = run_research_team([signal], config, {})

        role_ids = {role["id"] for role in team["roles"]}
        assigned_ids = {item["role_id"] for item in signals[0].team_assignments}
        self.assertIn("sector_specialist", role_ids)
        self.assertIn("sector_specialist", assigned_ids)


class ResearchTeamPipelineTests(unittest.TestCase):
    @patch.dict(os.environ, {"LLM_API_KEY": "", "DEEPSEEK_API_KEY": ""}, clear=False)
    @patch("stock_agent.pipeline.collect_sources")
    def test_pipeline_json_and_report_include_team_layer(self, mock_collect):
        from stock_agent.pipeline import run_agent_pipeline

        mock_collect.return_value = [
            RawSignal(
                source="manual",
                source_type="news",
                title="major test order",
                summary="large order needs review",
            )
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
            "research_team": {"enabled": True},
            "report": {"daily_top_n": 20},
            "review": {"fields": ["signal_id"]},
            "storage": {"sqlite_path": "data/test_research_team.sqlite3"},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            for sub in [
                "reports/daily",
                "reports/json",
                "reports/alerts",
                "reports/actions",
                "reports/team",
                "data/review",
                "data",
            ]:
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

            with open(result.json_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            report_text = Path(result.report_path).read_text(encoding="utf-8-sig")

        self.assertIn("team", data)
        self.assertIn("supervisor_brief", data["team"])
        self.assertIn("team_assignments", data["signals"][0])
        self.assertIn("投研团队", report_text)


if __name__ == "__main__":
    unittest.main()
