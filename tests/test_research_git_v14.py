import io
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


def _patch_paths(root: Path):
    stack = ExitStack()
    for target in [
        "stock_agent.research_git.project_path",
        "stock_agent.brokerage_architecture.project_path",
        "stock_agent.cli.project_path",
    ]:
        stack.enter_context(patch(target, side_effect=lambda p, r=root: r / p))
    stack.enter_context(patch("stock_agent.config.PROJECT_ROOT", root))
    return stack


class ResearchGitV14Tests(unittest.TestCase):
    def test_stage_and_commit_project_into_event_state(self):
        from stock_agent.brokerage_architecture import project_brokerage_state
        from stock_agent.research_git import commit_research_draft, project_research_git_state, stage_research_draft

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                draft = stage_research_draft(
                    entity_type="signal",
                    entity_id="sig-ai-order",
                    title="中科曙光大额订单",
                    thesis="订单提升未来两年收入可见度，但需要核验公告原文。",
                    evidence_refs=["report:daily.md", "filing:url"],
                    review_questions=["合同是否已签署？"],
                    actor="agent",
                )
                committed = commit_research_draft(draft["draft_id"], "提交人工复核", actor="analyst")
                research_state = project_research_git_state()
                brokerage_state = project_brokerage_state()

        self.assertEqual(committed["status"], "committed")
        self.assertTrue(committed["commit_hash"])
        self.assertEqual(research_state["drafts"][draft["draft_id"]]["status"], "committed")
        self.assertEqual(research_state["status_counts"]["committed"], 1)
        self.assertIn(draft["draft_id"], brokerage_state["objects"]["research_drafts"])
        self.assertEqual(brokerage_state["objects"]["research_drafts"][draft["draft_id"]]["status"], "committed")
        self.assertEqual(brokerage_state["domains"]["research_front_office"]["research_draft_count"], 1)

    def test_approve_requires_commit_and_records_human_gate(self):
        from stock_agent.research_git import approve_research_draft, commit_research_draft, project_research_git_state, stage_research_draft

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                draft = stage_research_draft(
                    entity_type="research_case",
                    entity_id="case001",
                    title="AI 算力订单 case",
                    thesis="基本面方向偏正面，但证据链未闭合。",
                )
                with self.assertRaises(ValueError):
                    approve_research_draft(draft["draft_id"], notes="不能跳过 commit")
                commit_research_draft(draft["draft_id"], "case 进入复核")
                approved = approve_research_draft(draft["draft_id"], notes="证据充足，纳入持续跟踪", actor="user")
                state = project_research_git_state()

        self.assertEqual(approved["status"], "approved")
        self.assertEqual(state["drafts"][draft["draft_id"]]["status"], "approved")
        self.assertIn("No auto trading", approved["forbidden_actions_note"])
        self.assertEqual(state["human_gate_counts"]["approved"], 1)

    def test_rejects_forbidden_research_action(self):
        from stock_agent.research_git import stage_research_draft

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                with self.assertRaises(ValueError):
                    stage_research_draft(
                        entity_type="signal",
                        entity_id="sig-risk",
                        title="unsafe",
                        thesis="建议自动下单买入。",
                    )

    def test_write_summary_outputs_latest_files(self):
        from stock_agent.research_git import commit_research_draft, stage_research_draft, write_research_git_summary

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _patch_paths(root):
                draft = stage_research_draft(
                    entity_type="signal",
                    entity_id="sig001",
                    title="订单信号",
                    thesis="需要人工复核订单真实性。",
                )
                commit_research_draft(draft["draft_id"], "提交")
                paths = write_research_git_summary()
                summary_path_exists = Path(paths["research_git_summary_path"]).exists()
                latest_json = json.loads((root / "reports" / "research_git" / "latest.json").read_text(encoding="utf-8"))
                latest_md = (root / "reports" / "research_git" / "latest.md").read_text(encoding="utf-8-sig")

        self.assertTrue(summary_path_exists)
        self.assertEqual(latest_json["status_counts"]["committed"], 1)
        self.assertIn("Research-as-Git", latest_md)
        self.assertIn("订单信号", latest_md)

    def test_cli_research_stage_and_status(self):
        from stock_agent.cli import main

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "config").mkdir(parents=True, exist_ok=True)
            (root / "config" / "watchlist.local.json").write_text("{}", encoding="utf-8")
            with _patch_paths(root):
                out = io.StringIO()
                argv = [
                    "stock_agent",
                    "research-stage",
                    "--entity-type",
                    "signal",
                    "--entity-id",
                    "sig-cli",
                    "--title",
                    "CLI 信号",
                    "--thesis",
                    "先形成研判草稿，等待人工复核。",
                    "--review-question",
                    "是否有公告原文？",
                ]
                with patch.object(sys, "argv", argv), patch("sys.stdout", out):
                    main()
                stage_output = out.getvalue()
                draft_id = stage_output.strip().splitlines()[0].split(": ", 1)[1]

                out = io.StringIO()
                argv = ["stock_agent", "research-commit", "--draft-id", draft_id, "--message", "提交复核"]
                with patch.object(sys, "argv", argv), patch("sys.stdout", out):
                    main()
                commit_output = out.getvalue()

                out = io.StringIO()
                argv = ["stock_agent", "research-status", "--status", "committed"]
                with patch.object(sys, "argv", argv), patch("sys.stdout", out):
                    main()
                status_output = out.getvalue()

        self.assertIn("research draft:", stage_output)
        self.assertIn("research commit:", commit_output)
        self.assertIn(draft_id, status_output)
        self.assertIn("committed", status_output)


if __name__ == "__main__":
    unittest.main()
