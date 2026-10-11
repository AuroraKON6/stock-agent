import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from stock_agent.dashboard import _render_dashboard_html
from stock_agent.models import AnalyzedSignal, now_iso
from stock_agent.reports import _render_markdown, write_outputs


def _filing_signal_dict():
    return {
        "signal_id": "f001",
        "title": "贵州茅台2025年年度报告",
        "priority": "P2",
        "source": "eastmoney_filing",
        "source_type": "filing",
        "topics": [],
        "symbols": ["贵州茅台"],
        "llm_analysis": {},
        "reasons": [],
        "needs_review": True,
        "payload": {
            "filing_evidence": {
                "provider": "eastmoney_filing",
                "status": "ok",
                "symbol": "600519",
                "company": "贵州茅台",
                "announcement_type": "annual_report",
                "report_period": "2025A",
                "url": "https://data.eastmoney.com/notices/detail/600519/AN.html",
                "missing_fields": [],
                "importance_level": "medium",
                "importance_score": 45,
            },
            "financial_evidence": {
                "status": "missing",
                "metrics": {},
            },
            "filing_importance_level": "medium",
            "filing_importance_score": 45,
        },
    }


def _filing_signal_model():
    data = _filing_signal_dict()
    return AnalyzedSignal(
        signal_id=data["signal_id"],
        source=data["source"],
        source_type=data["source_type"],
        title=data["title"],
        summary="年度报告摘要",
        url=data["payload"]["filing_evidence"]["url"],
        published_at=now_iso(),
        collected_at=now_iso(),
        priority=data["priority"],
        priority_score=30.0,
        needs_review=True,
        payload=data["payload"],
    )


class FilingEvidenceMarkdownTests(unittest.TestCase):
    def test_daily_report_shows_filing_evidence_status(self):
        signal = _filing_signal_model()
        markdown = _render_markdown(
            {"project_name": "A股智能投研系统", "review": {"fields": []}},
            now_iso(),
            [signal],
            [],
            [],
            {"inserted": 1, "duplicates": 0, "decision_summary": {}},
        )

        self.assertIn("公告/财报证据", markdown)
        self.assertIn("贵州茅台2025年年度报告", markdown)
        self.assertIn("eastmoney_filing", markdown)
        self.assertIn("annual_report", markdown)
        self.assertIn("2025A", markdown)
        self.assertIn("ok", markdown)
        self.assertIn("importance=medium", markdown)

    def test_write_outputs_keeps_low_priority_filing_evidence_section(self):
        signal = _filing_signal_model()
        signal.priority = "P3"
        signal.priority_score = 5.0

        with tempfile.TemporaryDirectory() as tmpdir:
            for subdir in ["reports/daily", "reports/json", "reports/alerts"]:
                (Path(tmpdir) / subdir).mkdir(parents=True, exist_ok=True)
            with patch("stock_agent.reports.project_path", side_effect=lambda p: Path(tmpdir) / p):
                report_path, _json_path, _alert_path, _alerts = write_outputs(
                    [signal],
                    {"project_name": "A股智能投研系统", "report": {"include_low_priority": False}, "review": {"fields": []}},
                    {"inserted": 1, "duplicates": 0, "filing_evidence_count": 1},
                )
                markdown = Path(report_path).read_text(encoding="utf-8-sig")
        self.assertIn("公告/财报证据", markdown)
        self.assertIn("贵州茅台2025年年度报告", markdown)
        self.assertIn("eastmoney_filing", markdown)

    def test_write_outputs_keeps_low_priority_llm_analysis_section(self):
        signal = _filing_signal_model()
        signal.priority = "P3"
        signal.priority_score = 5.0
        signal.llm_analysis = {
            "impact_direction": "neutral",
            "confidence": 0.6,
            "horizon": "1-5d",
            "key_thesis": "公告偏治理事项，短期影响中性。",
            "risk_points": ["需核对公告原文"],
            "review_questions": ["是否涉及后续治理变化？"],
            "human_decision_needed": True,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            for subdir in ["reports/daily", "reports/json", "reports/alerts"]:
                (Path(tmpdir) / subdir).mkdir(parents=True, exist_ok=True)
            with patch("stock_agent.reports.project_path", side_effect=lambda p: Path(tmpdir) / p):
                report_path, _json_path, _alert_path, _alerts = write_outputs(
                    [signal],
                    {"project_name": "A股智能投研系统", "report": {"include_low_priority": False}, "review": {"fields": []}},
                    {"inserted": 1, "duplicates": 0, "llm_enabled": True, "llm_model": "deepseek-v4-pro"},
                )
                markdown = Path(report_path).read_text(encoding="utf-8-sig")

        self.assertIn("公告偏治理事项，短期影响中性。", markdown)
        self.assertIn("deepseek-v4-pro", markdown)


class FilingEvidenceDashboardTests(unittest.TestCase):
    def test_dashboard_shows_filing_evidence_table(self):
        ctx = {
            "generated_at": "2026-06-16T10:00:00+08:00",
            "signals": [_filing_signal_dict()],
            "stats": {"team_enabled": False},
            "team": {},
            "team_ops": {},
            "alerts_list": [],
            "pending_review": [],
            "review_summary": None,
            "decision_summary": None,
        }

        html = _render_dashboard_html(ctx)

        self.assertIn("公告/财报证据", html)
        self.assertIn("贵州茅台2025年年度报告", html)
        self.assertIn("annual_report", html)
        self.assertIn("2025A", html)
        self.assertIn("eastmoney_filing", html)
        self.assertIn("medium", html)


if __name__ == "__main__":
    unittest.main()
