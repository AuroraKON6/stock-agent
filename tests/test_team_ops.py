import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.models import AnalyzedSignal, RawSignal, now_iso


try:
    from stock_agent.team_ops import build_team_ops_snapshot, write_team_ops_artifacts
except ImportError:
    build_team_ops_snapshot = None
    write_team_ops_artifacts = None


def _signal(
    signal_id="s001",
    title="price signal",
    source_type="price",
    priority="P2",
    needs_review=True,
    payload=None,
    team_assignments=None,
):
    return AnalyzedSignal(
        signal_id=signal_id,
        source="manual",
        source_type=source_type,
        title=title,
        summary="summary",
        url="",
        published_at=now_iso(),
        collected_at=now_iso(),
        priority=priority,
        priority_score=30,
        needs_review=needs_review,
        reasons=["缺少URL或原始来源"] if needs_review else [],
        payload=payload or {"latest": 10.5, "pct_change": 3.2},
        team_assignments=team_assignments or [
            {"role_id": "market_watcher", "role_name": "盯盘员", "task": "检查K-line", "status": "queued"}
        ],
    )


def _team():
    return {
        "supervisor": {"id": "user", "name": "上司"},
        "supervisor_brief": {
            "headline": "今日有 1 条高优先级信号需要上司确认。",
            "decision_required": True,
            "needs_human_decision": [{"signal_id": "s001", "title": "price signal", "priority": "P1"}],
            "next_actions": ["人工确认来源"],
        },
        "messages": [
            {"from": "market_watcher", "to": "news_filing_analyst", "body": "请核对K线异动是否有事件催化。"}
        ],
        "strategy_feedback": {
            "suggestions": ["价格信号应结合K-line持续性和事件催化。"],
            "output_only": True,
            "config_auto_modified": False,
        },
        "guardrails": ["no_auto_trading"],
    }


class TeamOpsSnapshotTests(unittest.TestCase):
    def setUp(self):
        if build_team_ops_snapshot is None:
            self.fail("stock_agent.team_ops is not implemented")

    def test_snapshot_contains_supervisor_kline_review_and_strategy_sections(self):
        signal = _signal(priority="P1")
        snapshot = build_team_ops_snapshot([signal], _team(), {"inserted": 1}, {})

        self.assertEqual(snapshot["supervisor_brief"]["headline"], _team()["supervisor_brief"]["headline"])
        self.assertEqual(snapshot["kline_watchlist"][0]["signal_id"], "s001")
        self.assertEqual(snapshot["review_checklist"][0]["signal_id"], "s001")
        self.assertEqual(snapshot["human_intervention_queue"][0]["decision_owner"], "user")
        self.assertIn("request_more_data", snapshot["human_intervention_queue"][0]["allowed_decisions"])
        self.assertIn("价格信号应结合K-line持续性和事件催化。", snapshot["strategy_backlog"][0]["suggestion"])
        self.assertEqual(snapshot["guardrails"], ["no_auto_trading"])

    def test_write_team_ops_artifacts_creates_markdown_json_and_latest(self):
        signal = _signal(priority="P1")
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.team_ops.project_path", side_effect=lambda p: Path(tmpdir) / p):
                paths = write_team_ops_artifacts([signal], _team(), {"inserted": 1}, {})

            md_path = Path(paths["team_brief_path"])
            json_path = Path(paths["team_brief_json_path"])
            latest_md = Path(paths["team_brief_latest_path"])
            self.assertTrue(md_path.exists())
            self.assertTrue(json_path.exists())
            self.assertTrue(latest_md.exists())
            md = md_path.read_text(encoding="utf-8-sig")
            self.assertIn("投研团队晨会纪要", md)
            self.assertIn("人工介入队列", md)
            self.assertIn("K线盯盘清单", md)
            self.assertIn("策略优化台账", md)
            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(data["kline_watchlist"][0]["title"], "price signal")
            self.assertEqual(data["human_intervention_queue"][0]["status"], "waiting_for_supervisor")


class TeamOpsPipelineTests(unittest.TestCase):
    @patch.dict(os.environ, {"LLM_API_KEY": "", "DEEPSEEK_API_KEY": ""}, clear=False)
    @patch("stock_agent.pipeline.collect_sources")
    def test_pipeline_writes_team_ops_paths_to_json_stats(self, mock_collect):
        from stock_agent.pipeline import run_agent_pipeline

        mock_collect.return_value = [
            RawSignal(source="manual", source_type="news", title="major team signal", summary="major order")
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
            "storage": {"sqlite_path": "data/test_team_ops.sqlite3"},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            for sub in ["reports/daily", "reports/json", "reports/alerts", "reports/team", "data"]:
                (tmp / sub).mkdir(parents=True, exist_ok=True)

            def project_path_side(path_value):
                return tmp / path_value

            with patch("stock_agent.pipeline.project_path", side_effect=project_path_side), \
                 patch("stock_agent.reports.project_path", side_effect=project_path_side), \
                 patch("stock_agent.market_resilience.project_path", side_effect=project_path_side), \
                 patch("stock_agent.source_health.project_path", side_effect=project_path_side), \
                 patch("stock_agent.source_remediation.project_path", side_effect=project_path_side), \
                 patch("stock_agent.team_ops.project_path", side_effect=project_path_side), \
                 patch("stock_agent.pipeline.ensure_directories"):
                result = run_agent_pipeline(config)

            data = json.loads(Path(result.json_path).read_text(encoding="utf-8"))
            self.assertTrue(data["stats"]["team_brief_path"].endswith(".md"))
            self.assertTrue(Path(data["stats"]["team_brief_path"]).exists())
            self.assertIn("team_ops", data)
            self.assertIn("strategy_backlog", data["team_ops"])
            self.assertIn("human_intervention_queue", data["team_ops"])


class TeamOpsDashboardTests(unittest.TestCase):
    def test_dashboard_loads_and_renders_team_brief_path(self):
        from stock_agent.dashboard import _load_dashboard_context, _render_dashboard_html

        report = {
            "generated_at": "2026-06-05T10:00:00+08:00",
            "signals": [],
            "stats": {
                "team_enabled": True,
                "team_role_count": 6,
                "team_assignment_count": 1,
                "team_message_count": 1,
                "team_brief_path": "reports/team/team_brief.md",
            },
            "team": _team(),
            "team_ops": {"strategy_backlog": [{"suggestion": "优化阈值"}]},
            "alerts": [],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "reports/json").mkdir(parents=True)
            (root / "reports/alerts").mkdir(parents=True)
            (root / "data/review").mkdir(parents=True)
            (root / "reports/review").mkdir(parents=True)
            (root / "reports/json/20260605T100000_0800.json").write_text(
                json.dumps(report, ensure_ascii=False), encoding="utf-8"
            )
            with patch("stock_agent.dashboard.project_path", side_effect=lambda p: root / p):
                ctx = _load_dashboard_context()
                html = _render_dashboard_html(ctx)

        self.assertIn("reports/team/team_brief.md", html)
        self.assertIn("策略优化台账", html)
        self.assertIn("优化阈值", html)


if __name__ == "__main__":
    unittest.main()
