import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO

from stock_agent.decisions import compute_decision_summary, record_decision
from stock_agent.review import compute_review_summary, write_review_summary


class DecisionLedgerV2Tests(unittest.TestCase):
    def test_records_confirm_reject_defer_style_decision_with_outcome_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("stock_agent.decisions.project_path", side_effect=lambda p: root / p):
                record = record_decision(
                    "s001",
                    "confirm_signal",
                    notes="official filing verified",
                    actor="analyst",
                    outcome="effective",
                    followup_result="price moved +4% after filing",
                    evidence_refs=["filing_url", "role:f fundamental_analyst"],
                )
                summary = compute_decision_summary()

            files = list((root / "data" / "decisions").glob("decision_log_*.jsonl"))
            saved = json.loads(files[0].read_text(encoding="utf-8").strip())

        self.assertEqual(record["decision"], "confirm_signal")
        self.assertEqual(saved["outcome"], "effective")
        self.assertEqual(saved["followup_result"], "price moved +4% after filing")
        self.assertIn("filing_url", saved["evidence_refs"])
        self.assertEqual(summary["decision_counts"]["confirm_signal"], 1)
        self.assertEqual(summary["outcome_counts"]["effective"], 1)

    def test_rejects_trading_decision_even_with_outcome_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("stock_agent.decisions.project_path", side_effect=lambda p: root / p):
                with self.assertRaises(ValueError):
                    record_decision(
                        "s001",
                        "buy",
                        outcome="effective",
                        followup_result="unsafe",
                    )

    def test_review_summary_includes_decision_ledger_outcomes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("stock_agent.decisions.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.review.project_path", side_effect=lambda p: root / p):
                record_decision("s001", "confirm_signal", outcome="effective")
                record_decision("s002", "reject_signal", outcome="false_positive")
                summary = compute_review_summary()
                md_path, _json_path = write_review_summary(summary)

            md_text = Path(md_path).read_text(encoding="utf-8-sig")

        self.assertEqual(summary["decision_ledger_summary"]["total"], 2)
        self.assertEqual(summary["decision_ledger_summary"]["outcome_counts"]["effective"], 1)
        self.assertIn("Decision Ledger", md_text)
        self.assertIn("confirm_signal", md_text)

    def test_review_summary_cli_writes_summary_when_only_decision_ledger_exists(self):
        from stock_agent.cli import _review_summary

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("stock_agent.decisions.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.review.project_path", side_effect=lambda p: root / p):
                record_decision("s001", "defer_signal", outcome="pending")
                out = StringIO()
                with redirect_stdout(out):
                    _review_summary()

                files = list((root / "reports" / "review").glob("review_summary_*.md"))
                self.assertEqual(len(files), 1)
                self.assertIn("Decision Ledger", files[0].read_text(encoding="utf-8-sig"))


if __name__ == "__main__":
    unittest.main()
