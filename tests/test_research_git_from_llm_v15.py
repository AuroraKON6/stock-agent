import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from stock_agent.models import AnalyzedSignal, RawSignal, now_iso


def _patch_project_root(root: Path):
    stack = ExitStack()
    stack.enter_context(patch("stock_agent.config.PROJECT_ROOT", root))
    for target in [
        "stock_agent.research_git.project_path",
        "stock_agent.brokerage_architecture.project_path",
        "stock_agent.pipeline.project_path",
        "stock_agent.reports.project_path",
        "stock_agent.market_resilience.project_path",
        "stock_agent.source_health.project_path",
        "stock_agent.source_remediation.project_path",
        "stock_agent.cli.project_path",
    ]:
        stack.enter_context(patch(target, side_effect=lambda p, r=root: r / p))
    return stack


def _make_signal(signal_id="sig001", priority="P1", confidence=0.82, human_needed=True):
    signal = AnalyzedSignal(
        signal_id=signal_id,
        source="manual",
        source_type="news",
        title="AI server order signed",
        summary="Large AI server contract needs verification.",
        url="https://example.com/news/ai-order",
        published_at=now_iso(),
        collected_at=now_iso(),
        topics=["AI compute"],
        symbols=["TEST"],
        matched_keywords=["order"],
        sentiment_score=0.6,
        priority_score=88.0,
        priority=priority,
        needs_review=True,
        reasons=["high priority test signal"],
    )
    signal.llm_analysis = {
        "impact_direction": "positive",
        "confidence": confidence,
        "horizon": "1-4w",
        "key_thesis": "The contract may improve revenue visibility, but the source document must be checked.",
        "risk_points": ["execution uncertainty", "margin pressure"],
        "review_questions": ["Is there an original filing?", "What is the delivery schedule?"],
        "human_decision_needed": human_needed,
    }
    signal.llm_actions = ["export_review"]
    return signal


def _make_config(**research_git_overrides):
    research_git = {
        "enabled": True,
        "auto_stage_from_llm": True,
        "min_priority": "P2",
        "min_confidence": 0.7,
        "require_human_decision_needed": True,
    }
    research_git.update(research_git_overrides)
    return {
        "topics": [{"name": "AI compute", "symbols": ["TEST"], "keywords": ["order"]}],
        "priority_rules": {
            "p0_keywords": [],
            "p1_keywords": ["contract"],
            "positive_keywords": ["order", "contract"],
            "negative_keywords": [],
            "review_keywords": ["needs verification"],
            "min_priority_for_alert": "P1",
        },
        "llm_agent": {"enabled": True, "auto_execute_actions": True, "min_priority": "P3"},
        "research_git": research_git,
        "report": {"daily_top_n": 20},
        "review": {"fields": ["signal_id"]},
        "storage": {"sqlite_path": "data/test.sqlite3"},
    }


class ResearchGitFromLLMTests(unittest.TestCase):
    def test_stages_high_confidence_llm_signal_and_writes_summary(self):
        from stock_agent.brokerage_architecture import project_brokerage_state
        from stock_agent.research_git import project_research_git_state, stage_llm_research_drafts

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_project_root(root):
                stats = stage_llm_research_drafts([_make_signal()], _make_config())
                state = project_research_git_state()
                brokerage_state = project_brokerage_state()
                latest = json.loads((root / "reports" / "research_git" / "latest.json").read_text(encoding="utf-8"))

        self.assertEqual(stats["research_git_auto_staged_count"], 1)
        self.assertEqual(state["draft_count"], 1)
        draft = next(iter(state["drafts"].values()))
        self.assertEqual(draft["status"], "draft")
        self.assertIn("AI server order", draft["title"])
        self.assertIn("revenue visibility", draft["thesis"])
        self.assertIn("signal:sig001", draft["evidence_refs"])
        self.assertIn("Is there an original filing?", draft["review_questions"])
        self.assertIn(draft["draft_id"], brokerage_state["objects"]["research_drafts"])
        self.assertEqual(latest["draft_count"], 1)

    def test_duplicate_llm_signal_is_not_staged_twice(self):
        from stock_agent.research_git import project_research_git_state, stage_llm_research_drafts

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_project_root(root):
                stage_llm_research_drafts([_make_signal()], _make_config())
                stats = stage_llm_research_drafts([_make_signal()], _make_config())
                state = project_research_git_state()

        self.assertEqual(stats["research_git_auto_staged_count"], 0)
        self.assertEqual(stats["research_git_skipped_existing_count"], 1)
        self.assertEqual(state["draft_count"], 1)

    def test_human_decision_signal_stages_even_below_confidence_floor(self):
        from stock_agent.research_git import project_research_git_state, stage_llm_research_drafts

        signal = _make_signal(confidence=0.55, human_needed=True)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_project_root(root):
                stats = stage_llm_research_drafts([signal], _make_config(min_confidence=0.7))
                state = project_research_git_state()

        self.assertEqual(stats["research_git_auto_staged_count"], 1)
        self.assertEqual(state["draft_count"], 1)

    def test_market_rebalance_context_is_allowed_as_research_question(self):
        from stock_agent.research_git import project_research_git_state, stage_llm_research_drafts

        signal = _make_signal()
        signal.llm_analysis["review_questions"] = ["Could index fund rebalance explain the flow?"]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_project_root(root):
                stats = stage_llm_research_drafts([signal], _make_config())
                state = project_research_git_state()

        self.assertEqual(stats["research_git_auto_staged_count"], 1)
        self.assertEqual(state["draft_count"], 1)

    def test_unsafe_llm_thesis_is_skipped_without_crashing(self):
        from stock_agent.research_git import project_research_git_state, stage_llm_research_drafts

        signal = _make_signal()
        signal.llm_analysis["key_thesis"] = "place_order immediately"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_project_root(root):
                stats = stage_llm_research_drafts([signal], _make_config())
                state = project_research_git_state()

        self.assertEqual(stats["research_git_auto_staged_count"], 0)
        self.assertEqual(stats["research_git_skipped_unsafe_count"], 1)
        self.assertEqual(state["draft_count"], 0)

    @patch.dict(os.environ, {"LLM_API_KEY": ""}, clear=False)
    @patch("stock_agent.pipeline.enrich_signals")
    @patch("stock_agent.pipeline.collect_sources")
    def test_agent_pipeline_auto_stages_llm_draft(self, mock_collect, mock_enrich):
        from stock_agent.pipeline import run_agent_pipeline

        raw = RawSignal(
            source="manual",
            source_type="news",
            title="AI server contract signed",
            summary="The contract needs verification before any conclusion.",
            url="https://example.com/raw",
        )
        mock_collect.return_value = [raw]

        def fake_enrich(signals, config, ops=None):
            for signal in signals:
                signal.llm_analysis = _make_signal(signal.signal_id).llm_analysis
                signal.llm_actions = ["export_review"]
            return signals, {
                "llm_enabled": True,
                "llm_model": "deepseek-v4-pro",
                "llm_base_url": "https://api.deepseek.com/v1",
                "llm_candidates": len(signals),
                "llm_called": 1,
                "llm_success": len(signals),
                "llm_failed": 0,
                "llm_skipped_no_key": 0,
                "llm_skipped_budget": 0,
            }

        mock_enrich.side_effect = fake_enrich
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_project_root(root):
                result = run_agent_pipeline(_make_config())
                latest = json.loads((root / "reports" / "research_git" / "latest.json").read_text(encoding="utf-8"))

        self.assertEqual(result.stats["research_git_auto_staged_count"], 1)
        self.assertTrue(result.stats["research_git_summary_path"].endswith(".md"))
        self.assertEqual(latest["draft_count"], 1)

    def test_auto_stage_from_latest_json_report(self):
        from stock_agent.research_git import project_research_git_state, stage_llm_research_drafts_from_report

        signal = _make_signal()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report_dir = root / "reports" / "json"
            report_dir.mkdir(parents=True, exist_ok=True)
            report_path = report_dir / "20260701T010000_0800.json"
            report_path.write_text(json.dumps({"signals": [signal.to_dict()]}, ensure_ascii=False), encoding="utf-8")
            with _patch_project_root(root):
                stats = stage_llm_research_drafts_from_report("")
                state = project_research_git_state()

        self.assertEqual(stats["research_git_auto_staged_count"], 1)
        self.assertEqual(state["draft_count"], 1)

    def test_cli_research_auto_stage(self):
        from stock_agent.cli import main

        signal = _make_signal()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report_dir = root / "reports" / "json"
            report_dir.mkdir(parents=True, exist_ok=True)
            report_path = report_dir / "20260701T010000_0800.json"
            report_path.write_text(json.dumps({"signals": [signal.to_dict()]}, ensure_ascii=False), encoding="utf-8")
            with _patch_project_root(root), patch("sys.argv", ["stock_agent", "research-auto-stage", "--report", str(report_path)]):
                main()
                latest = json.loads((root / "reports" / "research_git" / "latest.json").read_text(encoding="utf-8"))

        self.assertEqual(latest["draft_count"], 1)


class ResearchGitReportTests(unittest.TestCase):
    def test_daily_report_shows_research_git_handoff(self):
        from stock_agent.reports import write_outputs

        signal = _make_signal()
        stats = {
            "inserted": 1,
            "duplicates": 0,
            "research_git_enabled": True,
            "research_git_auto_staged_count": 1,
            "research_git_draft_count": 2,
            "research_git_pending_review_count": 1,
            "research_git_status_counts": {"draft": 1, "committed": 1},
            "research_git_summary_path": "E:/stock agent/reports/research_git/research_git_demo.md",
            "research_git_summary_latest_path": "E:/stock agent/reports/research_git/latest.md",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for sub in ["reports/daily", "reports/json", "reports/alerts"]:
                (root / sub).mkdir(parents=True, exist_ok=True)
            with patch("stock_agent.reports.project_path", side_effect=lambda p: root / p):
                report_path, _json_path, _alert_path, _alerts = write_outputs([signal], _make_config(), stats)
                text = Path(report_path).read_text(encoding="utf-8-sig")

        self.assertIn("## Research-as-Git", text)
        self.assertIn("auto_staged: 1", text)
        self.assertIn("pending_review: 1", text)
        self.assertIn("latest.md", text)


if __name__ == "__main__":
    unittest.main()
