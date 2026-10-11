import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.models import AnalyzedSignal, RawSignal, now_iso


try:
    from stock_agent.evidence_chain import attach_evidence_chains, build_evidence_chain, summarize_evidence_chains
except ImportError:
    attach_evidence_chains = None
    build_evidence_chain = None
    summarize_evidence_chains = None


def _signal(signal_id="s001"):
    return AnalyzedSignal(
        signal_id=signal_id,
        source="manual",
        source_type="news",
        title="major AI server order",
        summary="Company won a major AI server order; official filing must be verified.",
        url="http://example.com/news",
        published_at=now_iso(),
        collected_at=now_iso(),
        topics=["AI"],
        symbols=["600000"],
        priority="P1",
        priority_score=86,
        needs_review=True,
        reasons=["large order", "missing official filing"],
        payload={
            "pct_change": 3.6,
            "main_net_inflow": 120000000,
            "source_evidence": {"provider": "manual", "url": "http://example.com/news"},
            "filing_evidence": {"status": "missing", "provider": "eastmoney_filing"},
            "financial_evidence": {"metrics": {"revenue": 123.4}},
        },
        llm_analysis={
            "key_thesis": "Order may improve expectations.",
            "risk_points": ["margin unknown"],
            "review_questions": ["Verify official filing"],
            "human_decision_needed": True,
        },
        role_analyses=[
            {
                "role_id": "fundamental_analyst",
                "stance": "positive",
                "confidence": 0.8,
                "thesis": "Revenue visibility improves.",
                "evidence_refs": ["url", "summary", "financial_evidence.metrics.revenue", "official_filing_url"],
                "missing_info": ["contract margin"],
                "review_questions": ["Is there an official filing?"],
                "human_decision_needed": True,
            },
            {
                "role_id": "capital_flow_analyst",
                "stance": "mixed",
                "confidence": 0.6,
                "thesis": "Market attention is rising.",
                "evidence_refs": ["pct_change", "main_net_inflow"],
                "missing_info": ["volume persistence"],
                "review_questions": ["Does inflow persist?"],
                "human_decision_needed": True,
            },
            {
                "role_id": "contrarian_challenger",
                "stance": "negative",
                "confidence": 0.55,
                "thesis": "The news may be priced in.",
                "evidence_refs": ["valuation_context"],
                "missing_info": ["valuation context"],
                "review_questions": ["What is the base expectation?"],
                "human_decision_needed": True,
            },
        ],
        role_synthesis={
            "consensus_direction": "mixed_positive",
            "conflict_level": "high",
            "key_disagreement": "Order upside versus valuation risk.",
            "decision_gate": "human_review_required",
            "next_human_actions": ["Verify filing", "Check margin"],
            "forbidden_actions_note": "No auto trading, order placement, or position rebalance.",
        },
    )


class EvidenceChainModuleTests(unittest.TestCase):
    def setUp(self):
        if build_evidence_chain is None:
            self.fail("stock_agent.evidence_chain is not implemented")

    def test_maps_role_evidence_refs_to_signal_payload_and_links(self):
        chain = build_evidence_chain(_signal())

        resolved = {item["ref"]: item for item in chain["resolved_refs"]}
        self.assertEqual(resolved["url"]["category"], "raw_signal")
        self.assertEqual(resolved["pct_change"]["category"], "market")
        self.assertEqual(resolved["main_net_inflow"]["category"], "market")
        self.assertEqual(resolved["financial_evidence.metrics.revenue"]["category"], "financial")
        self.assertIn("official_filing_url", chain["unresolved_refs"])
        self.assertGreater(chain["quality"]["score"], 0)
        self.assertLess(chain["quality"]["score"], 1)

    def test_missing_info_becomes_open_evidence_gap_tasks(self):
        signal = _signal()
        [signal], stats = attach_evidence_chains([signal])

        task_titles = [task["gap"] for task in signal.evidence_gap_tasks]
        self.assertIn("contract margin", task_titles)
        self.assertIn("valuation context", task_titles)
        self.assertTrue(all(task["status"] == "open" for task in signal.evidence_gap_tasks))
        self.assertEqual(signal.evidence_chain["signal_id"], "s001")
        self.assertEqual(stats["evidence_chain_signal_count"], 1)
        self.assertGreater(stats["evidence_gap_task_count"], 0)

    def test_summary_counts_quality_and_gap_tasks(self):
        signal = _signal()
        [signal], _stats = attach_evidence_chains([signal])
        summary = summarize_evidence_chains([signal])

        self.assertEqual(summary["signal_count"], 1)
        self.assertGreater(summary["gap_task_count"], 0)
        self.assertIn("WARN", summary["status_counts"])


class HumanWorkbenchTests(unittest.TestCase):
    def test_team_ops_snapshot_contains_human_workbench_with_evidence_gaps_and_commands(self):
        from stock_agent.team_ops import build_team_ops_snapshot

        signal = _signal()
        [signal], evidence_stats = attach_evidence_chains([signal])
        team = {
            "supervisor_brief": {
                "needs_human_decision": [{"signal_id": "s001", "title": signal.title, "priority": "P1"}],
                "headline": "needs review",
            },
            "messages": [],
            "guardrails": ["no_auto_trading"],
            "strategy_feedback": {"suggestions": []},
        }
        snapshot = build_team_ops_snapshot([signal], team, evidence_stats, {})

        workbench = snapshot["human_workbench"]
        self.assertEqual(workbench[0]["signal_id"], "s001")
        self.assertEqual(workbench[0]["role_conflict"]["conflict_level"], "high")
        self.assertTrue(workbench[0]["evidence_gap_tasks"])
        commands = workbench[0]["allowed_decision_commands"]
        self.assertTrue(any("decision-confirm" in command for command in commands))
        self.assertTrue(any("--evidence-ref" in command for command in commands))
        serialized = json.dumps(workbench, ensure_ascii=False).lower()
        self.assertNotIn("buy", serialized)
        self.assertNotIn("sell", serialized)

    def test_review_export_includes_evidence_chain_and_gap_tasks(self):
        from stock_agent.review import _build_review_record

        signal = _signal()
        [signal], _stats = attach_evidence_chains([signal])
        record = _build_review_record(signal.to_dict())

        self.assertIn("evidence_chain", record)
        self.assertIn("evidence_gap_tasks", record)
        self.assertTrue(record["evidence_gap_tasks"])


class EvidenceChainPipelineTests(unittest.TestCase):
    @patch.dict("os.environ", {"LLM_API_KEY": "test-key", "DEEPSEEK_API_KEY": ""}, clear=False)
    @patch("stock_agent.pipeline.collect_sources")
    @patch("stock_agent.llm_agent._call_llm")
    @patch("stock_agent.role_analyst._call_role_llm")
    def test_agent_pipeline_writes_evidence_chain_and_dashboard_workbench(self, mock_role_llm, mock_signal_llm, mock_collect):
        from stock_agent.analysis import signal_id
        from stock_agent.pipeline import run_agent_pipeline
        from stock_agent.dashboard import _load_dashboard_context, _render_dashboard_html

        raw = RawSignal(
            source="manual",
            source_type="news",
            title="major AI server order evidence test",
            summary="major order needs official filing verification",
            url="http://example.com/evidence",
            published_at="2026-06-19T09:30:00+08:00",
            payload={
                "pct_change": 3.2,
                "main_net_inflow": 110000000,
                "financial_evidence": {"metrics": {"revenue": 1.0}},
            },
        )
        sid = signal_id(raw)
        mock_collect.return_value = [raw]
        mock_signal_llm.return_value = json.dumps([
            {
                "signal_id": sid,
                "impact_direction": "positive",
                "confidence": 0.8,
                "horizon": "1-4w",
                "key_thesis": "Order may improve expectations.",
                "risk_points": ["margin unknown"],
                "review_questions": ["Verify filing"],
                "human_decision_needed": True,
                "suggested_actions": ["export_review", "write_action_log"],
            }
        ])
        mock_role_llm.return_value = json.dumps([
            {
                "signal_id": sid,
                "roles": _signal(sid).role_analyses,
                "synthesis": _signal(sid).role_synthesis,
            }
        ])
        config = {
            "topics": [{"name": "AI", "keywords": ["AI", "server", "order"]}],
            "priority_rules": {
                "p0_keywords": [],
                "p1_keywords": ["major"],
                "positive_keywords": ["order"],
                "negative_keywords": [],
                "review_keywords": ["verification"],
                "min_priority_for_alert": "P1",
            },
            "llm_agent": {"enabled": True, "base_url": "http://llm", "model": "deepseek-v4-pro", "min_priority": "P3"},
            "multi_role_agent": {"enabled": True, "base_url": "http://llm", "model": "deepseek-v4-pro", "min_priority": "P3"},
            "research_framework": {"enabled": True},
            "research_team": {"enabled": True},
            "research_cases": {"enabled": False, "llm_enabled": False},
            "report": {"daily_top_n": 20, "include_low_priority": True},
            "review": {"fields": ["signal_id"]},
            "storage": {"sqlite_path": "data/test_evidence_chain.sqlite3"},
            "run_ops": {"enabled": False},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            for sub in [
                "reports/daily",
                "reports/json",
                "reports/alerts",
                "reports/actions",
                "reports/team",
                "reports/decisions",
                "reports/dashboard",
                "data/review",
                "data/decisions",
                "data",
                "logs/run_ops",
            ]:
                (tmp / sub).mkdir(parents=True, exist_ok=True)

            def pp(path_value):
                return tmp / path_value

            with patch("stock_agent.pipeline.project_path", side_effect=pp), \
                 patch("stock_agent.reports.project_path", side_effect=pp), \
                 patch("stock_agent.market_resilience.project_path", side_effect=pp), \
                 patch("stock_agent.source_health.project_path", side_effect=pp), \
                 patch("stock_agent.source_remediation.project_path", side_effect=pp), \
                 patch("stock_agent.team_ops.project_path", side_effect=pp), \
                 patch("stock_agent.decisions.project_path", side_effect=pp), \
                 patch("stock_agent.dashboard.project_path", side_effect=pp), \
                 patch("stock_agent.run_ops.project_path", side_effect=pp), \
                 patch("stock_agent.pipeline.ensure_directories"):
                result = run_agent_pipeline(config)
                data = json.loads(Path(result.json_path).read_text(encoding="utf-8"))
                ctx = _load_dashboard_context()
                html = _render_dashboard_html(ctx)

        signal = data["signals"][0]
        self.assertIn("evidence_chain", signal)
        self.assertIn("evidence_gap_tasks", signal)
        self.assertGreater(data["stats"]["evidence_gap_task_count"], 0)
        self.assertIn("human_workbench", data["team_ops"])
        self.assertIn("Human Workbench", html)
        self.assertIn("Evidence Gaps", html)


class DecisionShortcutTests(unittest.TestCase):
    def test_cli_shortcuts_record_confirm_reject_defer_with_evidence_refs(self):
        from stock_agent.cli import _decision_confirm, _decision_defer, _decision_reject

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("stock_agent.decisions.project_path", side_effect=lambda p: root / p):
                _decision_confirm("s001", "effective", "confirmed by filing", "user", ["url"])
                _decision_reject("s002", "false_positive", "duplicate rumor", "user", ["source_evidence"])
                _decision_defer("s003", "pending", "waiting for margin data", "user", ["financial_evidence.metrics.margin"])

            records = []
            for path in (root / "data" / "decisions").glob("decision_log_*.jsonl"):
                records.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())

        self.assertEqual([record["decision"] for record in records], ["confirm_signal", "reject_signal", "defer_signal"])
        self.assertEqual(records[0]["outcome"], "effective")
        self.assertEqual(records[2]["evidence_refs"], ["financial_evidence.metrics.margin"])


class ReviewSummaryEvidenceTests(unittest.TestCase):
    def test_review_summary_includes_evidence_chain_quality_from_latest_report(self):
        from stock_agent.review import compute_review_summary, write_review_summary

        signal = _signal()
        [signal], _stats = attach_evidence_chains([signal])
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "reports" / "json").mkdir(parents=True)
            (root / "data" / "review").mkdir(parents=True)
            (root / "reports" / "json" / "latest.json").write_text(
                json.dumps({"signals": [signal.to_dict()]}, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch("stock_agent.review.project_path", side_effect=lambda p: root / p):
                summary = compute_review_summary()
                md_path, _json_path = write_review_summary(summary)
                md = Path(md_path).read_text(encoding="utf-8-sig")

        self.assertEqual(summary["evidence_chain_summary"]["signal_count"], 1)
        self.assertGreater(summary["evidence_chain_summary"]["gap_task_count"], 0)
        self.assertIn("Evidence Chain Quality", md)


if __name__ == "__main__":
    unittest.main()
