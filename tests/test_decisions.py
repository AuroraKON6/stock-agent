import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.models import AnalyzedSignal, now_iso


try:
    from stock_agent.decisions import (
        VALID_DECISIONS,
        apply_decision_context,
        compute_decision_summary,
        list_pending_decisions,
        load_decision_context,
        record_decision,
        write_decision_summary,
    )
except ImportError:
    VALID_DECISIONS = set()
    apply_decision_context = None
    compute_decision_summary = None
    list_pending_decisions = None
    load_decision_context = None
    record_decision = None
    write_decision_summary = None


def _signal(signal_id="s001"):
    return AnalyzedSignal(
        signal_id=signal_id,
        source="manual",
        source_type="news",
        title="test signal",
        summary="summary",
        url="",
        published_at=now_iso(),
        collected_at=now_iso(),
        priority="P1",
        priority_score=55,
        needs_review=True,
    )


def _write_latest_team_queue(root: Path):
    team_dir = root / "reports" / "team"
    team_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "human_intervention_queue": [
            {
                "signal_id": "s001",
                "title": "test signal",
                "priority": "P1",
                "decision_owner": "user",
                "status": "waiting_for_supervisor",
                "allowed_decisions": sorted(VALID_DECISIONS or {"request_more_data"}),
            },
            {
                "signal_id": "s002",
                "title": "other signal",
                "priority": "P2",
                "decision_owner": "user",
                "status": "waiting_for_supervisor",
                "allowed_decisions": ["request_more_data"],
            },
        ]
    }
    (team_dir / "latest.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class DecisionModuleTests(unittest.TestCase):
    def setUp(self):
        if record_decision is None:
            self.fail("stock_agent.decisions is not implemented")

    def test_valid_decision_recorded_to_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("stock_agent.decisions.project_path", side_effect=lambda p: root / p):
                record = record_decision("s001", "request_more_data", notes="补公告和资金流")

            files = list((root / "data" / "decisions").glob("decision_log_*.jsonl"))
            self.assertEqual(len(files), 1)
            saved = json.loads(files[0].read_text(encoding="utf-8").strip())
            self.assertEqual(saved["signal_id"], "s001")
            self.assertEqual(saved["decision"], "request_more_data")
            self.assertEqual(saved["notes"], "补公告和资金流")
            self.assertEqual(record["actor"], "user")

    def test_invalid_trading_decision_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("stock_agent.decisions.project_path", side_effect=lambda p: root / p):
                with self.assertRaises(ValueError):
                    record_decision("s001", "place_order", notes="should never happen")

    def test_pending_list_excludes_decided_signal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_latest_team_queue(root)
            with patch("stock_agent.decisions.project_path", side_effect=lambda p: root / p):
                record_decision("s001", "mark_as_noise", notes="噪声")
                pending = list_pending_decisions()

            self.assertEqual([item["signal_id"] for item in pending], ["s002"])

    def test_decision_summary_and_latest_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("stock_agent.decisions.project_path", side_effect=lambda p: root / p):
                record_decision("s001", "request_more_data", notes="补数据")
                record_decision("s002", "defer_until_more_evidence", notes="等待公告")
                summary = compute_decision_summary()
                paths = write_decision_summary(summary)

            self.assertEqual(summary["total"], 2)
            self.assertEqual(summary["decision_counts"]["request_more_data"], 1)
            self.assertTrue(Path(paths["decision_summary_path"]).exists())
            self.assertTrue(Path(paths["decision_summary_json_path"]).exists())
            self.assertTrue((root / "reports" / "decisions" / "latest.md").exists())

    def test_apply_decision_context_makes_decision_visible_to_llm_prompt(self):
        from stock_agent.llm_agent import _build_user_prompt

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("stock_agent.decisions.project_path", side_effect=lambda p: root / p):
                record_decision("s001", "request_more_data", notes="补资金流")
                context = load_decision_context()

        signal = _signal("s001")
        [signal] = apply_decision_context([signal], context)
        prompt = _build_user_prompt([signal])

        self.assertEqual(signal.human_decision["decision"], "request_more_data")
        self.assertIn("human_decision", prompt)
        self.assertIn("补资金流", prompt)


if __name__ == "__main__":
    unittest.main()
