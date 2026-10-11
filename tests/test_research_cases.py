import json
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.models import AnalyzedSignal, RawSignal, now_iso


try:
    from stock_agent.research_cases import (
        VALID_CASE_DECISIONS,
        build_research_cases,
        compute_case_summary,
        enrich_research_cases,
        record_case_decision,
    )
except ImportError:
    VALID_CASE_DECISIONS = set()
    build_research_cases = None
    compute_case_summary = None
    enrich_research_cases = None
    record_case_decision = None


def _signal(
    signal_id,
    *,
    title="AI server order",
    symbol="688888",
    topic="AI compute",
    announcement_type="major_contract",
    priority="P1",
):
    return AnalyzedSignal(
        signal_id=signal_id,
        source="filing_importance_verify",
        source_type="filing",
        title=title,
        summary="summary",
        url="https://example.test/ann",
        published_at=now_iso(),
        collected_at=now_iso(),
        topics=[topic] if topic else [],
        symbols=[symbol] if symbol else [],
        priority=priority,
        priority_score=80 if priority == "P1" else 30,
        needs_review=priority in ("P0", "P1", "P2"),
        payload={
            "symbol": symbol,
            "company": "VerificationCo",
            "announcement_type": announcement_type,
            "filing_evidence": {
                "announcement_type": announcement_type,
                "status": "ok",
                "importance_level": "high",
            },
        },
        llm_analysis={
            "key_thesis": "Contract supports revenue visibility.",
            "risk_points": ["delivery risk"],
            "review_questions": ["verify contract terms"],
        },
    )


class ResearchCaseBuildTests(unittest.TestCase):
    def setUp(self):
        if build_research_cases is None:
            self.fail("stock_agent.research_cases is not implemented")

    def test_groups_signals_by_entity_topic_and_catalyst_path(self):
        s1 = _signal("s001")
        s2 = _signal("s002", title="AI server order follow-up")

        cases = build_research_cases([s1, s2], {})

        self.assertEqual(len(cases), 1)
        case = cases[0]
        self.assertEqual(case["signal_count"], 2)
        self.assertEqual(case["entity"], "688888")
        self.assertEqual(case["topic"], "AI compute")
        self.assertEqual(case["catalyst_path"], "filing:major_contract")
        self.assertEqual(case["status"], "active")
        self.assertEqual(case["signal_ids"], ["s001", "s002"])

    def test_splits_different_catalyst_paths(self):
        contract = _signal("s001", announcement_type="major_contract")
        buyback = _signal("s002", announcement_type="buyback", title="buyback plan")

        cases = build_research_cases([contract, buyback], {})

        self.assertEqual(len(cases), 2)
        self.assertEqual(
            sorted(case["catalyst_path"] for case in cases),
            ["filing:buyback", "filing:major_contract"],
        )


class ResearchCaseLLMTests(unittest.TestCase):
    def setUp(self):
        if enrich_research_cases is None:
            self.fail("stock_agent.research_cases is not implemented")

    @patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"})
    @patch("stock_agent.research_cases._call_llm")
    def test_llm_adds_multi_hypothesis_case_analysis(self, mock_call):
        case = build_research_cases([_signal("s001")], {})[0]
        mock_call.return_value = json.dumps([
            {
                "case_id": case["case_id"],
                "hypotheses": [
                    {
                        "name": "bull",
                        "thesis": "contract adds revenue visibility",
                        "probability": 0.6,
                        "evidence_for": ["large order"],
                        "evidence_against": ["delivery risk"],
                    },
                    {
                        "name": "bear",
                        "thesis": "margin pressure offsets revenue",
                        "probability": 0.4,
                        "evidence_for": ["hardware margin risk"],
                        "evidence_against": ["visible backlog"],
                    },
                ],
                "evidence_gaps": ["original filing terms"],
                "counter_questions": ["what is gross margin?"],
                "next_decision_options": ["continue_tracking", "needs_evidence"],
                "human_decision_needed": True,
            }
        ])

        enriched, stats = enrich_research_cases([case], {
            "llm_agent": {
                "enabled": True,
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-v4-pro",
                "timeout": 10,
                "temperature": 0.2,
            },
            "research_cases": {"enabled": True, "llm_enabled": True, "min_priority": "P2"},
        })

        self.assertEqual(stats["research_case_llm_candidates"], 1)
        self.assertEqual(stats["research_case_llm_success"], 1)
        analysis = enriched[0]["case_analysis"]
        self.assertEqual(len(analysis["hypotheses"]), 2)
        self.assertEqual(analysis["evidence_gaps"], ["original filing terms"])
        self.assertEqual(analysis["counter_questions"], ["what is gross margin?"])
        self.assertEqual(analysis["next_decision_options"], ["continue_tracking", "needs_evidence"])

    @patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"})
    @patch("stock_agent.research_cases._call_llm")
    def test_llm_defaults_to_single_attempt_and_caps_case_timeout(self, mock_call):
        case = build_research_cases([_signal("s001")], {})[0]
        mock_call.side_effect = OSError("temporary provider read failure")

        enriched, stats = enrich_research_cases([case], {
            "llm_agent": {
                "enabled": True,
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-v4-pro",
                "timeout": 60,
                "temperature": 0.2,
            },
            "research_cases": {"enabled": True, "llm_enabled": True, "min_priority": "P2"},
        })

        self.assertEqual(mock_call.call_count, 1)
        self.assertEqual(mock_call.call_args.kwargs["timeout"], 30)
        self.assertEqual(stats["research_case_llm_failed"], 1)
        self.assertIn("case LLM call failed", enriched[0]["case_error"])

    @patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"})
    @patch("stock_agent.research_cases._call_llm")
    def test_transient_case_llm_failure_recovered_by_retry_does_not_degrade_runtime(self, mock_call):
        from stock_agent.run_ops import RunOpsContext

        case = build_research_cases([_signal("s001")], {})[0]
        mock_call.side_effect = [
            OSError("temporary provider read failure"),
            json.dumps([{
                "case_id": case["case_id"],
                "hypotheses": [],
                "evidence_gaps": [],
                "counter_questions": [],
                "next_decision_options": ["continue_tracking"],
                "human_decision_needed": True,
            }]),
        ]
        ops = RunOpsContext({"run_ops": {"enabled": True, "llm_max_calls": 3, "llm_max_seconds": 30}})

        enriched, stats = enrich_research_cases([case], {
            "llm_agent": {
                "enabled": True,
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-v4-pro",
                "timeout": 10,
                "temperature": 0.2,
            },
            "research_cases": {
                "enabled": True,
                "llm_enabled": True,
                "min_priority": "P2",
                "llm_max_attempts": 2,
                "llm_timeout": 10,
            },
        }, ops=ops)

        self.assertEqual(stats["research_case_llm_success"], 1)
        self.assertEqual(enriched[0].get("case_error", ""), "")
        self.assertNotIn("llm_call_failed", [item["reason"] for item in ops.summary()["degradations"]])


class ResearchCaseDecisionTests(unittest.TestCase):
    def setUp(self):
        if record_case_decision is None:
            self.fail("stock_agent.research_cases is not implemented")

    def test_case_decision_updates_case_summary_and_rejects_trading(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cases_dir = root / "data" / "research_cases"
            cases_dir.mkdir(parents=True, exist_ok=True)
            case = build_research_cases([_signal("s001")], {})[0]
            (cases_dir / "cases.json").write_text(
                json.dumps({"cases": [case]}, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch("stock_agent.research_cases.project_path", side_effect=lambda p: root / p):
                record = record_case_decision(case["case_id"], "needs_evidence", notes="need original filing")
                with self.assertRaises(ValueError):
                    record_case_decision(case["case_id"], "buy", notes="unsafe")
                summary = compute_case_summary()

        self.assertIn("continue_tracking", VALID_CASE_DECISIONS)
        self.assertEqual(record["decision"], "needs_evidence")
        self.assertEqual(summary["total_cases"], 1)
        self.assertEqual(summary["status_counts"]["needs_evidence"], 1)
        self.assertEqual(summary["latest_decisions"][case["case_id"]]["notes"], "need original filing")

    def test_review_summary_includes_research_case_status_changes(self):
        from stock_agent.review import compute_review_summary

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cases_dir = root / "data" / "research_cases"
            cases_dir.mkdir(parents=True, exist_ok=True)
            case = build_research_cases([_signal("s001")], {})[0]
            (cases_dir / "cases.json").write_text(
                json.dumps({"cases": [case]}, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch("stock_agent.research_cases.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.review.project_path", side_effect=lambda p: root / p):
                record_case_decision(case["case_id"], "needs_evidence", notes="case needs source")
                summary = compute_review_summary()

        case_summary = summary["research_case_summary"]
        self.assertEqual(case_summary["status_counts"]["needs_evidence"], 1)
        self.assertEqual(case_summary["status_changes"][0]["decision"], "needs_evidence")

    def test_cli_case_record_writes_manual_case_decision(self):
        from stock_agent.cli import main

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            argv = [
                "stock_agent",
                "case-record",
                "--case-id",
                "case001",
                "--decision",
                "needs_evidence",
                "--notes",
                "need source filing",
            ]
            out = io.StringIO()
            with patch.object(sys, "argv", argv), \
                 patch("sys.stdout", out), \
                 patch("stock_agent.research_cases.project_path", side_effect=lambda p: root / p):
                main()

            files = list((root / "data" / "research_cases").glob("case_decisions_*.jsonl"))
            self.assertEqual(len(files), 1)
            saved = json.loads(files[0].read_text(encoding="utf-8").strip())

        self.assertEqual(saved["case_id"], "case001")
        self.assertEqual(saved["decision"], "needs_evidence")
        self.assertIn("case001", out.getvalue())

    def test_cli_review_summary_writes_file_when_only_case_status_changed(self):
        from stock_agent.cli import main

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cases_dir = root / "data" / "research_cases"
            cases_dir.mkdir(parents=True, exist_ok=True)
            (cases_dir / "cases.json").write_text(
                json.dumps({"cases": [{"case_id": "case001", "title": "case", "status": "active", "signal_count": 1}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch("stock_agent.research_cases.project_path", side_effect=lambda p: root / p):
                record_case_decision("case001", "needs_evidence", notes="need source")
            argv = ["stock_agent", "review-summary"]
            out = io.StringIO()
            with patch.object(sys, "argv", argv), \
                 patch("sys.stdout", out), \
                 patch("stock_agent.review.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.research_cases.project_path", side_effect=lambda p: root / p):
                main()

            files = list((root / "reports" / "review").glob("review_summary_*.md"))

        self.assertEqual(len(files), 1)
        self.assertIn("review_summary", out.getvalue())


class ResearchCasePipelineTests(unittest.TestCase):
    def test_pipeline_writes_research_cases_to_json_and_daily_report(self):
        from stock_agent.pipeline import run_pipeline

        raw = RawSignal(
            source="manual",
            source_type="filing",
            title="VerificationCo wins major AI server contract",
            summary="major contract",
            url="https://example.test/ann",
            published_at=now_iso(),
            payload={
                "symbol": "688888",
                "announcement_type": "major_contract",
                "filing_evidence": {"announcement_type": "major_contract", "status": "ok"},
            },
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = {
                "storage": {"sqlite_path": str(root / "data" / "signals.sqlite3")},
                "sources": {},
                "topics": [{"name": "AI compute", "symbols": ["VerificationCo"], "keywords": ["major contract"], "risk_keywords": []}],
                "priority_rules": {"p0_keywords": [], "p1_keywords": ["major contract"], "positive_keywords": [], "negative_keywords": [], "review_keywords": []},
                "research_cases": {"enabled": True, "llm_enabled": False},
                "research_framework": {"enabled": False},
                "research_team": {"enabled": False},
                "report": {"include_low_priority": True},
            }
            with patch("stock_agent.pipeline.collect_sources", return_value=[raw]), \
                 patch("stock_agent.config.PROJECT_ROOT", root), \
                 patch("stock_agent.pipeline.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.market_resilience.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.source_health.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.source_remediation.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.research_cases.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.reports.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.decisions.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.team_ops.project_path", side_effect=lambda p: root / p):
                result = run_pipeline(config)

            data = json.loads(Path(result.json_path).read_text(encoding="utf-8"))
            report_text = Path(result.report_path).read_text(encoding="utf-8")

        self.assertEqual(result.stats["research_case_count"], 1)
        self.assertEqual(len(data["research_cases"]), 1)
        self.assertIn("Research Cases", report_text)
        self.assertIn("VerificationCo", report_text)


class ResearchCaseDashboardTests(unittest.TestCase):
    def test_dashboard_renders_research_cases_section(self):
        from stock_agent.dashboard import _render_dashboard_html

        case = build_research_cases([_signal("s001")], {})[0]
        html = _render_dashboard_html({
            "generated_at": now_iso(),
            "report_json_path": "",
            "alert_path": "",
            "signals": [],
            "stats": {"research_case_count": 1, "research_case_status_counts": {"active": 1}},
            "team": {},
            "team_ops": {},
            "alerts_list": [],
            "pending_review": [],
            "review_summary": None,
            "decision_summary": None,
            "research_cases": [case],
            "notification_path": "",
            "last_scheduled_run": None,
        })

        self.assertIn("Research Cases", html)
        self.assertIn(case["case_id"][:12], html)
        self.assertIn("active", html)


if __name__ == "__main__":
    unittest.main()
