import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.models import AnalyzedSignal, RawSignal, now_iso


try:
    from stock_agent.source_health import (
        attach_source_quality,
        build_source_health_summary,
        build_source_specs,
    )
except ImportError:
    attach_source_quality = None
    build_source_health_summary = None
    build_source_specs = None


def _price_signal(source="eastmoney", code="600519", latest=1680.0, pct_change=1.2):
    return RawSignal(
        source=source,
        source_type="price",
        title="贵州茅台行情快照",
        summary="price",
        url="",
        published_at=now_iso(),
        payload={
            "code": code,
            "name": "贵州茅台",
            "latest": latest,
            "pct_change": pct_change,
        },
    )


def _filing_signal(source="eastmoney_filing", symbol="600519", missing_fields=None):
    missing_fields = missing_fields or []
    return RawSignal(
        source=source,
        source_type="filing",
        title="贵州茅台重大合同公告",
        summary="filing",
        url="" if "url" in missing_fields else "https://example.test/ann",
        published_at=now_iso(),
        payload={
            "symbol": symbol,
            "announcement_type": "major_contract",
            "filing_evidence": {
                "provider": source,
                "status": "partial" if missing_fields else "ok",
                "symbol": symbol,
                "announcement_type": "major_contract",
                "missing_fields": missing_fields,
                "human_review_required": bool(missing_fields),
            },
        },
    )


def _source_error_signal(source="eastmoney", message="timeout"):
    return RawSignal(
        source=source,
        source_type="source_error",
        title="{0} failed".format(source),
        summary=message,
        published_at=now_iso(),
    )


class SourceHealthCoreTests(unittest.TestCase):
    def setUp(self):
        if build_source_health_summary is None:
            self.fail("stock_agent.source_health is not implemented")

    def test_scores_field_completeness_coverage_duplicates_and_trust(self):
        config = {
            "sources": {
                "eastmoney": {"enabled": True, "symbols": ["600519", "300750"]},
            },
            "source_health": {
                "trust_scores": {"eastmoney": 0.9},
                "warn_score_below": 80,
                "fail_score_below": 50,
            },
        }
        events = [
            {
                "source_id": "eastmoney",
                "source_kind": "eastmoney",
                "enabled": True,
                "attempted": True,
                "latency_ms": 120,
                "signals": [_price_signal(latest=None)],
                "expected_symbols": ["600519", "300750"],
            }
        ]

        summary = build_source_health_summary(events, config, duplicate_counts={"eastmoney": 1})
        item = summary["sources"][0]

        self.assertEqual(item["source_id"], "eastmoney")
        self.assertEqual(item["status"], "WARN")
        self.assertLess(item["field_completeness"], 1.0)
        self.assertEqual(item["coverage_gap_symbols"], ["300750"])
        self.assertEqual(item["duplicate_count"], 1)
        self.assertGreater(item["trust_score"], 0.8)
        self.assertIn("coverage_gap", item["anomaly_reasons"])
        self.assertEqual(summary["overall_status"], "WARN")

    def test_source_error_marks_source_fail_and_needs_human_review(self):
        config = {"source_health": {"fail_score_below": 50}}
        events = [
            {
                "source_id": "rss:finance",
                "source_kind": "rss",
                "enabled": True,
                "attempted": True,
                "latency_ms": 40,
                "signals": [
                    RawSignal(
                        source="finance",
                        source_type="source_error",
                        title="RSS failed",
                        summary="timeout",
                    )
                ],
                "expected_symbols": [],
            }
        ]

        summary = build_source_health_summary(events, config)
        item = summary["sources"][0]

        self.assertEqual(item["status"], "FAIL")
        self.assertEqual(item["success_rate"], 0.0)
        self.assertTrue(item["needs_human_review"])
        self.assertIn("source_error", item["anomaly_reasons"])

    def test_warn_only_source_error_keeps_external_provider_outage_non_fatal(self):
        config = {"source_health": {"fail_score_below": 50, "warn_only_source_errors": ["eastmoney_filing"]}}
        events = [
            {
                "source_id": "eastmoney_filing",
                "source_kind": "eastmoney_filing",
                "enabled": True,
                "attempted": True,
                "latency_ms": 40,
                "signals": [_source_error_signal("eastmoney_filing", "temporary provider outage")],
                "expected_symbols": ["600519"],
            }
        ]

        summary = build_source_health_summary(events, config)
        item = summary["sources"][0]

        self.assertEqual(item["status"], "WARN")
        self.assertEqual(summary["overall_status"], "WARN")
        self.assertTrue(item["needs_human_review"])
        self.assertIn("source_error", item["anomaly_reasons"])

    def test_resolved_fallback_keeps_overall_health_ok_but_auditable(self):
        config = {
            "source_health": {
                "warn_score_below": 80,
                "fail_score_below": 50,
                "trust_scores": {"eastmoney": 0.85, "market_cache": 0.55},
            }
        }
        events = [
            {
                "source_id": "eastmoney",
                "source_kind": "eastmoney",
                "enabled": True,
                "attempted": True,
                "latency_ms": 1500,
                "signals": [_source_error_signal("eastmoney", "WinError 10013")],
                "expected_symbols": ["600519"],
                "fallback_resolved": True,
            },
            {
                "source_id": "market_cache",
                "source_kind": "market_cache",
                "enabled": True,
                "attempted": True,
                "latency_ms": 0,
                "signals": [_price_signal(source="market_cache", code="600519")],
                "expected_symbols": ["600519"],
            },
        ]

        summary = build_source_health_summary(events, config)
        by_source = {item["source_id"]: item for item in summary["sources"]}

        self.assertEqual(summary["overall_status"], "OK")
        self.assertEqual(summary["low_quality_sources"], [])
        self.assertEqual(summary["source_degraded_event_count"], 1)
        self.assertEqual(by_source["eastmoney"]["status"], "WARN")
        self.assertTrue(by_source["eastmoney"]["fallback_resolved"])
        self.assertIn("fallback_resolved", by_source["eastmoney"]["anomaly_reasons"])
        self.assertEqual(by_source["market_cache"]["status"], "OK")
        self.assertIn("low_trust", by_source["market_cache"]["anomaly_reasons"])

    def test_attach_source_quality_copies_quality_to_signal_payload(self):
        signal = _filing_signal(missing_fields=["url"])
        summary = build_source_health_summary([
            {
                "source_id": "eastmoney_filing",
                "source_kind": "eastmoney_filing",
                "enabled": True,
                "attempted": True,
                "latency_ms": 20,
                "signals": [signal],
                "expected_symbols": ["600519"],
            }
        ], {})

        attach_source_quality([signal], summary)

        quality = signal.payload["source_quality"]
        self.assertEqual(quality["source_id"], "eastmoney_filing")
        self.assertIn("url", quality["evidence_gaps"])
        self.assertTrue(quality["needs_human_review"])


class SourceHealthPipelineTests(unittest.TestCase):
    def test_finalize_source_health_does_not_penalize_historical_duplicates(self):
        from stock_agent.pipeline import _finalize_source_health

        raw = _filing_signal()
        source_health = build_source_health_summary([
            {
                "source_id": "eastmoney_filing",
                "source_kind": "eastmoney_filing",
                "enabled": True,
                "attempted": True,
                "latency_ms": 10,
                "signals": [raw],
                "expected_symbols": ["600519"],
            }
        ], {"sources": {"eastmoney_filing": {"enabled": True, "symbols": ["600519"]}}})
        analyzed = [
            AnalyzedSignal(
                signal_id="already-seen",
                source="eastmoney_filing",
                source_type="filing",
                title=raw.title,
                summary=raw.summary,
                url=raw.url,
                published_at=raw.published_at or now_iso(),
                collected_at=now_iso(),
                payload={"source_quality": {"source_id": "eastmoney_filing"}},
            )
        ]

        updated = _finalize_source_health(source_health, analyzed, inserted=[], config={
            "sources": {"eastmoney_filing": {"enabled": True, "symbols": ["600519"]}},
        })
        item = updated["sources"][0]

        self.assertEqual(item["duplicate_count"], 0)
        self.assertNotIn("duplicates", item["anomaly_reasons"])

    def test_pipeline_writes_source_health_to_stats_json_and_daily_report(self):
        from stock_agent.pipeline import run_pipeline

        raw = _price_signal(latest=None)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for rel in [
                "data",
                "reports/daily",
                "reports/json",
                "reports/alerts",
                "reports/team",
                "reports/decisions",
                "reports/cases",
            ]:
                (root / rel).mkdir(parents=True, exist_ok=True)
            config = {
                "storage": {"sqlite_path": str(root / "data" / "signals.sqlite3")},
                "sources": {"eastmoney": {"enabled": True, "symbols": ["600519", "300750"]}},
                "topics": [],
                "priority_rules": {"p0_keywords": [], "p1_keywords": [], "positive_keywords": [], "negative_keywords": [], "review_keywords": []},
                "source_health": {"enabled": True, "warn_score_below": 80, "fail_score_below": 50},
                "research_framework": {"enabled": False},
                "research_team": {"enabled": False},
                "research_cases": {"enabled": False},
                "report": {"include_low_priority": True},
            }
            with patch("stock_agent.pipeline.collect_eastmoney", return_value=[raw]), \
                 patch("stock_agent.pipeline.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.reports.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.market_resilience.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.source_health.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.source_remediation.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.decisions.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.team_ops.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.research_cases.project_path", side_effect=lambda p: root / p):
                result = run_pipeline(config)

            data = json.loads(Path(result.json_path).read_text(encoding="utf-8"))
            report = Path(result.report_path).read_text(encoding="utf-8-sig")

        self.assertIn("source_health", data)
        self.assertEqual(result.stats["source_health"]["overall_status"], "WARN")
        self.assertIn("Data Source Health", report)
        self.assertIn("coverage_gap", report)

    def test_llm_prompt_includes_source_quality_context(self):
        from stock_agent.analysis import analyze_one
        from stock_agent.llm_agent import _build_user_prompt

        raw = _filing_signal(missing_fields=["url"])
        summary = build_source_health_summary([
            {
                "source_id": "eastmoney_filing",
                "source_kind": "eastmoney_filing",
                "enabled": True,
                "attempted": True,
                "latency_ms": 1,
                "signals": [raw],
                "expected_symbols": ["600519"],
            }
        ], {})
        attach_source_quality([raw], summary)
        signal = analyze_one(raw, {
            "topics": [],
            "priority_rules": {"p0_keywords": [], "p1_keywords": [], "positive_keywords": [], "negative_keywords": [], "review_keywords": []},
        })

        prompt = _build_user_prompt([signal])

        self.assertIn("source_quality", prompt)
        self.assertIn("evidence_gaps", prompt)
        self.assertIn("url", prompt)

    def test_research_case_prompt_includes_source_quality_context(self):
        from stock_agent.analysis import analyze_one
        from stock_agent.research_cases import build_research_cases, _build_case_prompt

        raw = _filing_signal(missing_fields=["url"])
        summary = build_source_health_summary([
            {
                "source_id": "eastmoney_filing",
                "source_kind": "eastmoney_filing",
                "enabled": True,
                "attempted": True,
                "latency_ms": 1,
                "signals": [raw],
                "expected_symbols": ["600519"],
            }
        ], {})
        attach_source_quality([raw], summary)
        signal = analyze_one(raw, {
            "topics": [],
            "priority_rules": {"p0_keywords": [], "p1_keywords": [], "positive_keywords": [], "negative_keywords": [], "review_keywords": []},
        })
        case = build_research_cases([signal], {})[0]

        prompt = _build_case_prompt([case])

        self.assertIn("source_quality", prompt)
        self.assertIn("needs_human_review", prompt)


class SourceHealthSurfaceTests(unittest.TestCase):
    def test_alert_trigger_for_unhealthy_source(self):
        from stock_agent.alerts import _check_triggers

        triggers = _check_triggers(
            signals=[],
            alerts=[],
            pending_review=[],
            actions=[],
            source_health={"overall_status": "WARN", "low_quality_sources": ["eastmoney"]},
        )

        self.assertIn("source_health", [name for name, _ in triggers])

    def test_doctor_warns_on_latest_source_health_warning(self):
        from stock_agent.doctor import _check_source_health

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            d = root / "reports" / "source_health"
            d.mkdir(parents=True)
            (d / "latest.json").write_text(json.dumps({
                "overall_status": "WARN",
                "overall_score": 62,
                "low_quality_sources": ["eastmoney"],
            }), encoding="utf-8")
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: root / p):
                status, detail, _ = _check_source_health()

        self.assertEqual(status, "WARN")
        self.assertIn("eastmoney", detail)

    def test_dashboard_renders_source_health_section(self):
        from stock_agent.dashboard import _render_dashboard_html

        html = _render_dashboard_html({
            "generated_at": now_iso(),
            "report_json_path": "",
            "alert_path": "",
            "signals": [],
            "stats": {
                "source_health": {
                    "overall_status": "WARN",
                    "overall_score": 62,
                    "sources": [
                        {
                            "source_id": "eastmoney",
                            "status": "WARN",
                            "score": 62,
                            "field_completeness": 0.5,
                            "coverage_gap_symbols": ["300750"],
                            "latency_ms": 120,
                            "anomaly_reasons": ["coverage_gap"],
                        }
                    ],
                }
            },
            "team": {},
            "team_ops": {},
            "research_cases": [],
            "alerts_list": [],
            "pending_review": [],
            "review_summary": None,
            "decision_summary": None,
            "notification_path": "",
            "last_scheduled_run": None,
        })

        self.assertIn("Data Source Health", html)
        self.assertIn("eastmoney", html)
        self.assertIn("coverage_gap", html)


if __name__ == "__main__":
    unittest.main()
