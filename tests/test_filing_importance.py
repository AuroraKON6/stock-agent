import unittest
from unittest.mock import patch

from stock_agent.analysis import analyze_one
from stock_agent.llm_agent import enrich_signals
from stock_agent.models import RawSignal, now_iso
from stock_agent.pipeline import _filing_evidence_stats


BASE_CONFIG = {
    "topics": [],
    "priority_rules": {
        "p0_keywords": [],
        "p1_keywords": [],
        "positive_keywords": [],
        "negative_keywords": [],
        "review_keywords": [],
        "min_priority_for_alert": "P1",
        "filing_importance_rules": {
            "enabled": True,
            "high_types": [
                "performance_forecast",
                "buyback",
                "shareholder_reduction",
                "dividend",
                "regulatory_inquiry",
                "major_contract",
            ],
            "medium_types": ["annual_report", "semiannual_report", "quarterly_report", "financing"],
            "low_types": ["governance", "legal_opinion", "routine", "monthly_report"],
            "high_score": 70,
            "medium_score": 45,
            "low_score": 10,
            "abnormal_evidence_score": 65,
        },
    },
    "llm_agent": {
        "enabled": True,
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-pro",
        "timeout": 10,
        "temperature": 0.2,
        "min_priority": "P2",
        "max_signals_per_run": 10,
        "auto_execute_actions": True,
        "allowed_actions": ["export_review", "write_action_log"],
    },
}


def _filing_raw(title, announcement_type, evidence_status="ok", missing_fields=None):
    missing_fields = missing_fields or []
    return RawSignal(
        source="eastmoney_filing",
        source_type="filing",
        title=title,
        summary="公告摘要",
        url="https://example.test/ann.html",
        published_at=now_iso(),
        payload={
            "announcement_type": announcement_type,
            "report_period": "2026A" if announcement_type in ("annual_report", "performance_forecast") else "",
            "filing_evidence": {
                "provider": "eastmoney_filing",
                "status": evidence_status,
                "announcement_type": announcement_type,
                "report_period": "2026A" if announcement_type in ("annual_report", "performance_forecast") else "",
                "missing_fields": missing_fields,
                "human_review_required": bool(missing_fields),
            },
            "financial_evidence": {"status": "missing", "metrics": {}},
        },
    )


class FilingImportanceAnalysisTests(unittest.TestCase):
    def test_high_value_announcement_is_promoted_and_marked_high(self):
        raw = _filing_raw("某公司业绩预告大幅增长", "performance_forecast")
        signal = analyze_one(raw, BASE_CONFIG)

        self.assertEqual(signal.priority, "P1")
        self.assertEqual(signal.payload["filing_importance_level"], "high")
        self.assertGreaterEqual(signal.payload["filing_importance_score"], 70)
        self.assertTrue(any("公告重要性" in reason for reason in signal.reasons))
        self.assertTrue(signal.needs_review)

    def test_procedural_announcement_stays_low_priority(self):
        raw = _filing_raw("某公司董事会秘书聘任公告", "governance")
        signal = analyze_one(raw, BASE_CONFIG)

        self.assertEqual(signal.priority, "P3")
        self.assertEqual(signal.payload["filing_importance_level"], "low")
        self.assertLessEqual(signal.payload["filing_importance_score"], 20)
        self.assertFalse(signal.needs_review)

    def test_medium_announcement_is_tagged_but_not_llm_priority_by_default(self):
        raw = _filing_raw("某公司2026年年度报告", "annual_report")
        signal = analyze_one(raw, BASE_CONFIG)

        self.assertEqual(signal.priority, "P3")
        self.assertEqual(signal.payload["filing_importance_level"], "medium")
        self.assertGreaterEqual(signal.payload["filing_importance_score"], 45)

    def test_abnormal_evidence_is_promoted_for_human_review(self):
        raw = _filing_raw("某公司普通治理公告", "governance", evidence_status="partial", missing_fields=["url"])
        signal = analyze_one(raw, BASE_CONFIG)

        self.assertEqual(signal.priority, "P2")
        self.assertEqual(signal.payload["filing_importance_level"], "unknown")
        self.assertGreaterEqual(signal.payload["filing_importance_score"], 65)
        self.assertTrue(signal.needs_review)
        self.assertTrue(any("公告证据异常" in reason for reason in signal.reasons))


class FilingImportanceLLMTests(unittest.TestCase):
    @patch.dict("os.environ", {"LLM_API_KEY": "test-key"})
    @patch("stock_agent.llm_agent._call_llm")
    def test_llm_skips_low_importance_but_calls_high_and_abnormal(self, mock_call):
        high = analyze_one(_filing_raw("某公司中标重大合同", "major_contract"), BASE_CONFIG)
        low = analyze_one(_filing_raw("某公司法律意见书", "legal_opinion"), BASE_CONFIG)
        medium = analyze_one(_filing_raw("某公司2026年年度报告", "annual_report"), BASE_CONFIG)
        abnormal = analyze_one(
            _filing_raw("某公司普通公告", "governance", evidence_status="partial", missing_fields=["url"]),
            BASE_CONFIG,
        )
        mock_call.return_value = (
            "["
            "{\"signal_id\":\"%s\",\"impact_direction\":\"positive\",\"confidence\":0.8,\"horizon\":\"1-5d\","
            "\"key_thesis\":\"高价值公告需要跟进\",\"risk_points\":[],\"review_questions\":[],"
            "\"human_decision_needed\":true,\"suggested_actions\":[\"export_review\"]},"
            "{\"signal_id\":\"%s\",\"impact_direction\":\"unknown\",\"confidence\":0.5,\"horizon\":\"unknown\","
            "\"key_thesis\":\"证据异常需补充\",\"risk_points\":[],\"review_questions\":[],"
            "\"human_decision_needed\":true,\"suggested_actions\":[\"export_review\"]}"
            "]"
        ) % (high.signal_id, abnormal.signal_id)

        result, stats = enrich_signals([high, low, medium, abnormal], BASE_CONFIG)

        self.assertEqual(stats["llm_candidates"], 2)
        self.assertEqual(stats["llm_success"], 2)
        self.assertEqual(high.llm_analysis["key_thesis"], "高价值公告需要跟进")
        self.assertEqual(low.llm_analysis, {})
        self.assertEqual(medium.llm_analysis, {})
        self.assertEqual(abnormal.llm_analysis["key_thesis"], "证据异常需补充")


class FilingImportanceStatsTests(unittest.TestCase):
    def test_pipeline_stats_count_importance_levels(self):
        high = analyze_one(_filing_raw("某公司中标重大合同", "major_contract"), BASE_CONFIG)
        low = analyze_one(_filing_raw("某公司法律意见书", "legal_opinion"), BASE_CONFIG)

        stats = _filing_evidence_stats([high, low])

        self.assertEqual(stats["filing_importance_level_counts"], {"high": 1, "low": 1})


if __name__ == "__main__":
    unittest.main()
