import io
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from stock_agent.models import RawSignal


def _patch_paths(root: Path):
    stack = ExitStack()
    for target in [
        "stock_agent.workspace_inbox.project_path",
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
    stack.enter_context(patch("stock_agent.config.PROJECT_ROOT", root))
    return stack


def _write_report(root: Path) -> Path:
    report_dir = root / "reports" / "json"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "20260702T090000_0800.json"
    report = {
        "generated_at": "2026-07-02T09:00:00+08:00",
        "stats": {"llm_model": "deepseek-v4-pro"},
        "signals": [
            {
                "signal_id": "sig001",
                "title": "AI server order needs verification",
                "summary": "Large AI server order reported by a public source.",
                "source": "manual",
                "source_type": "news",
                "url": "https://example.com/news/ai-order",
                "priority": "P1",
                "topics": ["AI compute"],
                "symbols": ["TEST"],
                "reasons": ["high priority"],
                "research_questions": ["Does the order have a filing source?"],
                "llm_analysis": {
                    "impact_direction": "positive",
                    "confidence": 0.82,
                    "horizon": "1-4w",
                    "key_thesis": "Revenue visibility may improve if the order is confirmed.",
                    "risk_points": ["execution uncertainty"],
                    "review_questions": ["Is there an original filing?"],
                    "human_decision_needed": True,
                },
                "role_synthesis": {"decision_gate": "human_review", "key_disagreement": "margin impact"},
                "evidence_chain": {
                    "status": "partial",
                    "refs": ["url:https://example.com/news/ai-order", "filing:missing"],
                },
                "evidence_gap_tasks": [
                    {"task_id": "gap001", "gap": "missing original filing", "status": "open"}
                ],
                "payload": {
                    "filing_evidence": {
                        "status": "missing",
                        "provider": "eastmoney",
                        "missing_fields": ["filing_url"],
                    },
                    "source_quality": {"status": "medium", "score": 65},
                    "raw_blob": "x" * 5000,
                },
            }
        ],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return report_path


class WorkspaceInboxV16Tests(unittest.TestCase):
    def test_build_workspace_inbox_links_signal_evidence_comments_and_research_draft(self):
        from stock_agent.research_git import stage_research_draft
        from stock_agent.workspace_inbox import build_workspace_inbox_from_report

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report_path = _write_report(root)
            with _patch_paths(root):
                draft = stage_research_draft(
                    entity_type="signal",
                    entity_id="sig001",
                    title="LLM research draft: AI server order needs verification",
                    thesis="Revenue visibility may improve if the order is confirmed.",
                    evidence_refs=["signal:sig001", "url:https://example.com/news/ai-order"],
                    review_questions=["Is there an original filing?"],
                )
                inbox = build_workspace_inbox_from_report(str(report_path))

        self.assertEqual(inbox["object_counts"]["signals"], 1)
        self.assertGreaterEqual(inbox["object_counts"]["evidence_items"], 3)
        signal = inbox["signals"]["sig001"]
        self.assertIn(draft["draft_id"], signal["research_draft_ids"])
        evidence_kinds = {item["kind"] for item in inbox["evidence_items"]}
        self.assertIn("source_url", evidence_kinds)
        self.assertIn("filing_evidence", evidence_kinds)
        self.assertIn("evidence_chain", evidence_kinds)
        comment_kinds = {item["kind"] for item in inbox["comments"]}
        self.assertIn("llm_thesis", comment_kinds)
        self.assertIn("review_question", comment_kinds)
        text = json.dumps(inbox, ensure_ascii=False)
        self.assertNotIn("raw_blob", text)
        self.assertNotIn("x" * 100, text)

    def test_write_workspace_inbox_outputs_latest_json_and_markdown(self):
        from stock_agent.research_git import stage_research_draft
        from stock_agent.workspace_inbox import build_workspace_inbox_from_report, write_workspace_inbox

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report_path = _write_report(root)
            with _patch_paths(root):
                draft = stage_research_draft(
                    entity_type="signal",
                    entity_id="sig001",
                    title="LLM research draft: AI server order needs verification",
                    thesis="Revenue visibility may improve if the order is confirmed.",
                )
                inbox = build_workspace_inbox_from_report(str(report_path))
                paths = write_workspace_inbox(inbox)
                workspace_path_exists = Path(paths["workspace_inbox_path"]).exists()
                latest_json = json.loads((root / "reports" / "workspace" / "latest.json").read_text(encoding="utf-8"))
                latest_md = (root / "reports" / "workspace" / "latest.md").read_text(encoding="utf-8-sig")

        self.assertTrue(workspace_path_exists)
        self.assertEqual(latest_json["object_counts"]["signals"], 1)
        self.assertIn("Workspace Inbox", latest_md)
        self.assertIn("AI server order needs verification", latest_md)
        self.assertIn(draft["draft_id"], latest_md)

    def test_cli_workspace_sync_from_latest_report(self):
        from stock_agent.cli import main

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_report(root)
            with _patch_paths(root):
                out = io.StringIO()
                argv = ["stock_agent", "workspace-sync"]
                with patch.object(sys, "argv", argv), patch("sys.stdout", out):
                    main()
                output = out.getvalue()
                latest_json = json.loads((root / "reports" / "workspace" / "latest.json").read_text(encoding="utf-8"))

        self.assertIn("workspace inbox:", output)
        self.assertIn("workspace latest:", output)
        self.assertEqual(latest_json["object_counts"]["signals"], 1)

    @patch("stock_agent.pipeline.enrich_signals")
    @patch("stock_agent.pipeline.collect_sources")
    def test_agent_pipeline_writes_workspace_inbox_after_json_report(self, mock_collect, mock_enrich):
        from stock_agent.pipeline import run_agent_pipeline

        raw = RawSignal(
            source="manual",
            source_type="news",
            title="AI server order needs verification",
            summary="Large AI server order reported by a public source.",
            url="https://example.com/news/ai-order",
        )
        mock_collect.return_value = [raw]

        def fake_enrich(signals, config, ops=None):
            for signal in signals:
                signal.llm_analysis = {
                    "impact_direction": "positive",
                    "confidence": 0.82,
                    "horizon": "1-4w",
                    "key_thesis": "Revenue visibility may improve if the order is confirmed.",
                    "risk_points": ["execution uncertainty"],
                    "review_questions": ["Is there an original filing?"],
                    "human_decision_needed": True,
                }
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
        config = {
            "topics": [{"name": "AI compute", "symbols": ["TEST"], "keywords": ["order"]}],
            "priority_rules": {
                "p0_keywords": [],
                "p1_keywords": ["order"],
                "positive_keywords": ["order"],
                "negative_keywords": [],
                "review_keywords": ["verification"],
                "min_priority_for_alert": "P1",
            },
            "llm_agent": {"enabled": True, "auto_execute_actions": True, "min_priority": "P3"},
            "research_git": {"enabled": True, "auto_stage_from_llm": True, "min_priority": "P2"},
            "multi_role_agent": {"enabled": False},
            "research_cases": {"enabled": False},
            "report": {"daily_top_n": 20},
            "review": {"fields": ["signal_id"]},
            "storage": {"sqlite_path": "data/test.sqlite3"},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                result = run_agent_pipeline(config)
                latest = json.loads((root / "reports" / "workspace" / "latest.json").read_text(encoding="utf-8"))

        self.assertIn("workspace_latest_path", result.stats)
        self.assertEqual(latest["object_counts"]["signals"], 1)
        self.assertGreaterEqual(latest["object_counts"]["comments"], 1)

    def test_daily_report_shows_workspace_inbox_paths(self):
        from stock_agent.models import AnalyzedSignal, now_iso
        from stock_agent.reports import write_outputs

        signal = AnalyzedSignal(
            signal_id="sig001",
            source="manual",
            source_type="news",
            title="AI server order needs verification",
            summary="Large AI server order reported by a public source.",
            url="https://example.com/news/ai-order",
            published_at=now_iso(),
            collected_at=now_iso(),
            priority="P1",
            priority_score=80,
            topics=["AI compute"],
            symbols=["TEST"],
        )
        stats = {
            "inserted": 1,
            "duplicates": 0,
            "workspace_latest_path": "E:/stock agent/reports/workspace/latest.md",
            "workspace_latest_json_path": "E:/stock agent/reports/workspace/latest.json",
            "workspace_object_counts": {
                "signals": 1,
                "evidence_items": 3,
                "comments": 4,
                "research_drafts": 1,
            },
        }
        config = {
            "project_name": "A股智能投研系统",
            "priority_rules": {"min_priority_for_alert": "P1"},
            "report": {"daily_top_n": 20},
            "review": {"fields": ["signal_id"]},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for sub in ["reports/daily", "reports/json", "reports/alerts"]:
                (root / sub).mkdir(parents=True, exist_ok=True)
            with patch("stock_agent.reports.project_path", side_effect=lambda p: root / p):
                report_path, _json_path, _alert_path, _alerts = write_outputs([signal], config, stats)
                text = Path(report_path).read_text(encoding="utf-8-sig")

        self.assertIn("## Workspace Inbox", text)
        self.assertIn("signals=1", text)
        self.assertIn("evidence=3", text)
        self.assertIn("latest.md", text)


if __name__ == "__main__":
    unittest.main()
