import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.models import AnalyzedSignal, RawSignal, now_iso


try:
    from stock_agent.source_remediation import (
        build_remediation_summary,
        split_source_degraded_signals,
    )
except ImportError:
    build_remediation_summary = None
    split_source_degraded_signals = None


def _source_error(source="eastmoney_filing", message="timeout"):
    return RawSignal(
        source=source,
        source_type="source_error",
        title="{0} failed".format(source),
        summary=message,
        published_at=now_iso(),
    )


def _price_signal(source="eastmoney", code="600519"):
    return RawSignal(
        source=source,
        source_type="price",
        title="{0} quote".format(code),
        summary="quote",
        published_at=now_iso(),
        payload={"code": code, "latest": 100.0, "pct_change": 1.2},
    )


def _analyzed_with_gaps(signal_id="s001"):
    return AnalyzedSignal(
        signal_id=signal_id,
        source="eastmoney_filing",
        source_type="filing",
        title="major contract filing",
        summary="filing needs evidence",
        url="",
        published_at=now_iso(),
        collected_at=now_iso(),
        priority="P2",
        needs_review=True,
        payload={
            "symbol": "600519",
            "filing_evidence": {
                "status": "partial",
                "provider": "eastmoney_filing",
                "missing_fields": ["url", "announcement_type"],
            },
            "source_quality": {
                "source_id": "eastmoney_filing",
                "status": "FAIL",
                "evidence_gaps": ["missing_fields", "url"],
            },
        },
        evidence_gap_tasks=[
            {"task_id": "{0}:gap:001".format(signal_id), "gap": "missing_url", "status": "open"},
            {"task_id": "{0}:gap:002".format(signal_id), "gap": "missing_fields", "status": "open"},
            {"task_id": "{0}:gap:003".format(signal_id), "gap": "official_filing_url", "status": "open"},
        ],
    )


class SourceRemediationCoreTests(unittest.TestCase):
    def setUp(self):
        if build_remediation_summary is None or split_source_degraded_signals is None:
            self.fail("stock_agent.source_remediation is not implemented")

    def test_splits_source_errors_into_degraded_events_not_research_signals(self):
        normal, degraded = split_source_degraded_signals([
            _source_error("eastmoney_filing", "WinError 10013"),
            _price_signal("akshare"),
        ])

        self.assertEqual([signal.source_type for signal in normal], ["price"])
        self.assertEqual(len(degraded), 1)
        self.assertEqual(degraded[0]["event_type"], "source_degraded")
        self.assertEqual(degraded[0]["source_id"], "eastmoney_filing")
        self.assertIn("retry_source", degraded[0]["allowed_actions"])
        serialized = json.dumps(degraded, ensure_ascii=False).lower()
        self.assertNotIn("buy", serialized)
        self.assertNotIn("sell", serialized)

    def test_evidence_gaps_become_executable_remediation_tasks(self):
        signal = _analyzed_with_gaps()
        source_health = {
            "source_degraded_events": [
                {
                    "event_type": "source_degraded",
                    "source_id": "eastmoney_filing",
                    "source_kind": "eastmoney_filing",
                    "severity": "FAIL",
                    "detail": "timeout",
                    "fallback_candidates": ["filing_jsonl"],
                }
            ]
        }

        summary = build_remediation_summary([signal], source_health, {"sources": {}})

        task_types = {task["task_type"] for task in summary["remediation_queue"]}
        self.assertIn("fill_missing_url", task_types)
        self.assertIn("fill_missing_fields", task_types)
        self.assertIn("verify_official_filing", task_types)
        self.assertIn("repair_source", task_types)
        self.assertEqual(summary["remediation_gap_type_counts"]["missing_url"], 1)
        self.assertGreater(summary["remediation_queue_count"], 0)
        self.assertTrue(all(task["status"] == "open" for task in summary["remediation_queue"]))


class SourceRemediationSourceHealthTests(unittest.TestCase):
    def test_source_health_exposes_source_degraded_events_with_fallback_candidates(self):
        from stock_agent.source_health import build_source_health_summary

        summary = build_source_health_summary([
            {
                "source_id": "eastmoney_filing",
                "source_kind": "eastmoney_filing",
                "enabled": True,
                "attempted": True,
                "latency_ms": 50,
                "signals": [_source_error("eastmoney_filing", "timeout")],
                "expected_symbols": ["600519"],
                "exception": "timeout",
            }
        ], {"sources": {"filing_jsonl": [{"enabled": True, "path": "data/inbox/filings"}]}})

        self.assertEqual(summary["source_degraded_event_count"], 1)
        event = summary["source_degraded_events"][0]
        self.assertEqual(event["event_type"], "source_degraded")
        self.assertEqual(event["source_id"], "eastmoney_filing")
        self.assertIn("filing_jsonl", event["fallback_candidates"])

    def test_low_quality_source_rows_create_source_level_remediation_tasks(self):
        source_health = {
            "source_degraded_events": [],
            "sources": [
                {
                    "source_id": "market_cache",
                    "source_kind": "market_cache",
                    "status": "WARN",
                    "score": 79.1,
                    "coverage_gap_symbols": ["000001", "300750"],
                    "anomaly_reasons": ["coverage_gap", "field_missing", "duplicates", "low_trust"],
                    "evidence_gaps": ["coverage:000001", "coverage:300750", "missing_fields"],
                    "fallback_resolved": False,
                }
            ],
        }

        summary = build_remediation_summary([], source_health, {"sources": {"eastmoney": {"enabled": True}}})

        tasks = summary["remediation_queue"]
        task_types = {task["task_type"] for task in tasks}
        self.assertIn("fill_source_coverage", task_types)
        self.assertIn("fill_missing_fields", task_types)
        self.assertIn("reduce_source_duplicates", task_types)
        self.assertIn("verify_source_trust", task_types)
        self.assertTrue(all(task["signal_id"] == "" for task in tasks))
        self.assertTrue(any("000001" in task.get("gap", "") for task in tasks))
        self.assertEqual(summary["remediation_gap_type_counts"]["coverage_gap"], 1)
        self.assertEqual(summary["remediation_gap_type_counts"]["duplicates"], 1)
        self.assertEqual(summary["remediation_gap_type_counts"]["low_trust"], 1)


class SourceRemediationPipelineTests(unittest.TestCase):
    def test_pipeline_keeps_source_degraded_out_of_signals_and_writes_remediation_stats(self):
        from stock_agent.pipeline import run_pipeline

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
                "reports/source_health",
                "reports/remediation",
                "logs/run_ops",
            ]:
                (root / rel).mkdir(parents=True, exist_ok=True)
            config = {
                "storage": {"sqlite_path": str(root / "data" / "signals.sqlite3")},
                "sources": {
                    "eastmoney_filing": {"enabled": True, "symbols": ["600519"]},
                    "filing_jsonl": [{"enabled": True, "path": str(root / "data" / "inbox" / "filings")}],
                },
                "market_resilience": {"enabled": False},
                "topics": [],
                "priority_rules": {
                    "p0_keywords": [],
                    "p1_keywords": [],
                    "positive_keywords": [],
                    "negative_keywords": [],
                    "review_keywords": [],
                },
                "research_framework": {"enabled": False},
                "research_team": {"enabled": False},
                "research_cases": {"enabled": False},
                "report": {"include_low_priority": True},
            }
            with patch("stock_agent.pipeline.collect_eastmoney_filings", return_value=[_source_error("eastmoney_filing", "timeout")]), \
                 patch("stock_agent.pipeline.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.reports.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.run_ops.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.market_resilience.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.decisions.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.team_ops.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.research_cases.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.source_health.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.source_remediation.project_path", side_effect=lambda p: root / p):
                result = run_pipeline(config)

            data = json.loads(Path(result.json_path).read_text(encoding="utf-8"))

        self.assertEqual(result.stats["source_errors"], 0)
        self.assertEqual(data["signals"], [])
        self.assertEqual(data["stats"]["source_degraded_event_count"], 1)
        self.assertGreater(data["stats"]["remediation_queue_count"], 0)
        self.assertIn("source_degraded_events", data["source_health"])


class SourceRemediationSurfaceTests(unittest.TestCase):
    def test_dashboard_renders_source_degraded_and_remediation_queue(self):
        from stock_agent.dashboard import _render_dashboard_html

        html = _render_dashboard_html({
            "generated_at": now_iso(),
            "signals": [],
            "stats": {
                "source_health": {
                    "overall_status": "FAIL",
                    "overall_score": 20,
                    "source_degraded_events": [
                        {"source_id": "eastmoney_filing", "severity": "FAIL", "detail": "timeout"}
                    ],
                    "sources": [],
                },
                "remediation_queue": [
                    {
                        "task_id": "rem-001",
                        "task_type": "fill_missing_url",
                        "source_id": "eastmoney_filing",
                        "signal_id": "s001",
                        "gap": "missing_url",
                        "status": "open",
                        "allowed_actions": ["attach_evidence_url"],
                    }
                ],
                "remediation_gap_type_counts": {"missing_url": 1},
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

        self.assertIn("Source Remediation Queue", html)
        self.assertIn("source_degraded", html)
        self.assertIn("fill_missing_url", html)
        self.assertIn("eastmoney_filing", html)

    def test_review_summary_includes_source_remediation_quality(self):
        from stock_agent.review import compute_review_summary, write_review_summary

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "reports" / "json").mkdir(parents=True)
            (root / "data" / "review").mkdir(parents=True)
            (root / "reports" / "json" / "latest.json").write_text(json.dumps({
                "signals": [_analyzed_with_gaps().to_dict()],
                "stats": {
                    "source_degraded_events": [
                        {"source_id": "eastmoney_filing", "severity": "FAIL", "detail": "timeout"}
                    ],
                    "remediation_queue": [
                        {"task_type": "fill_missing_url", "source_id": "eastmoney_filing", "status": "open"},
                        {"task_type": "verify_official_filing", "source_id": "eastmoney_filing", "status": "resolved"},
                    ],
                    "remediation_gap_type_counts": {"missing_url": 1, "official_filing": 1},
                },
            }, ensure_ascii=False), encoding="utf-8")
            with patch("stock_agent.review.project_path", side_effect=lambda p: root / p):
                summary = compute_review_summary()
                md_path, _json_path = write_review_summary(summary)
                md = Path(md_path).read_text(encoding="utf-8-sig")

        remediation = summary["source_remediation_summary"]
        self.assertEqual(remediation["source_degraded_event_count"], 1)
        self.assertEqual(remediation["open_count"], 1)
        self.assertEqual(remediation["resolved_count"], 1)
        self.assertEqual(remediation["gap_type_counts"]["missing_url"], 1)
        self.assertIn("Source Remediation", md)
        self.assertIn("eastmoney_filing", md)


if __name__ == "__main__":
    unittest.main()
