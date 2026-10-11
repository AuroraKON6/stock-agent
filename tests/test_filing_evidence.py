import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.models import AnalyzedSignal, RawSignal, now_iso


try:
    from stock_agent.sources.filing_jsonl import (
        collect_filing_jsonl,
        _row_to_signal,
    )
except ImportError:
    collect_filing_jsonl = None
    _row_to_signal = None


class FilingJsonlSourceTests(unittest.TestCase):
    def setUp(self):
        if collect_filing_jsonl is None:
            self.fail("stock_agent.sources.filing_jsonl is not implemented")

    def test_disabled_source_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "filings.jsonl"
            path.write_text("{}", encoding="utf-8")
            signals = collect_filing_jsonl([{"enabled": False, "path": str(path)}])

        self.assertEqual(signals, [])

    def test_missing_file_is_non_blocking(self):
        signals = collect_filing_jsonl([{"enabled": True, "path": "missing-file.jsonl"}])
        self.assertEqual(signals, [])

    def test_successful_collect_creates_filing_signal_with_evidence(self):
        row = {
            "source": "cninfo",
            "source_type": "filing",
            "symbol": "300750",
            "company": "宁德时代",
            "title": "宁德时代2025年年度报告",
            "announcement_type": "annual_report",
            "report_period": "2025A",
            "published_at": "2026-04-20T20:00:00+08:00",
            "url": "https://example.test/report.pdf",
            "summary": "公司收入增长，海外业务占比提升。",
            "key_points": ["收入增长", "海外业务占比提升"],
            "risk_points": ["原材料价格波动"],
            "metrics": {
                "revenue_yoy": 0.18,
                "net_profit_yoy": 0.12,
                "gross_margin": 0.22,
            },
            "raw_text": "very long pdf text should stay out of prompt",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "filings.jsonl"
            path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
            signals = collect_filing_jsonl([{"enabled": True, "path": str(path), "name": "公告证据池"}])

        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertEqual(signal.source, "cninfo")
        self.assertEqual(signal.source_type, "filing")
        self.assertEqual(signal.title, "宁德时代2025年年度报告")
        self.assertEqual(signal.url, "https://example.test/report.pdf")
        filing = signal.payload["filing_evidence"]
        self.assertEqual(filing["provider"], "公告证据池")
        self.assertEqual(filing["status"], "ok")
        self.assertEqual(filing["symbol"], "300750")
        self.assertEqual(filing["announcement_type"], "annual_report")
        self.assertIn("url", filing["fields_present"])
        financial = signal.payload["financial_evidence"]
        self.assertEqual(financial["metrics"]["revenue_yoy"], 0.18)
        self.assertEqual(financial["status"], "ok")
        self.assertEqual(signal.payload["raw_text"], "very long pdf text should stay out of prompt")

    def test_partial_evidence_marks_missing_fields_for_human_review(self):
        signal = _row_to_signal(
            {
                "symbol": "600519",
                "company": "贵州茅台",
                "title": "贵州茅台业绩预告",
                "summary": "业绩预告摘要",
            },
            {"name": "公告证据池"},
        )

        filing = signal.payload["filing_evidence"]
        self.assertEqual(filing["status"], "partial")
        self.assertIn("url", filing["missing_fields"])
        self.assertIn("announcement_type", filing["missing_fields"])
        financial = signal.payload["financial_evidence"]
        self.assertEqual(financial["status"], "missing")
        self.assertTrue(filing["human_review_required"])

    def test_invalid_jsonl_reports_file_and_line(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bad.jsonl"
            path.write_text("{bad json", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                collect_filing_jsonl([{"enabled": True, "path": str(path)}])

        self.assertIn("bad.jsonl:1", str(ctx.exception))

    def test_empty_directory_returns_no_signals_and_no_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "filings"
            path.mkdir()
            signals = collect_filing_jsonl([{"enabled": True, "path": str(path)}])

        self.assertEqual(signals, [])

    def test_directory_reads_jsonl_files_in_path_name_order(self):
        row_a = {"symbol": "000001", "title": "first"}
        row_b = {"symbol": "000002", "title": "second"}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "filings"
            path.mkdir()
            (path / "b.jsonl").write_text(json.dumps(row_b, ensure_ascii=False) + "\n", encoding="utf-8")
            (path / "a.jsonl").write_text(json.dumps(row_a, ensure_ascii=False) + "\n", encoding="utf-8")
            signals = collect_filing_jsonl([{"enabled": True, "path": str(path)}])

        self.assertEqual(len(signals), 2)
        self.assertEqual([signal.payload["symbol"] for signal in signals], ["000001", "000002"])

    def test_directory_ignores_non_jsonl_and_subdirectories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "filings"
            path.mkdir()
            (path / "notes.txt").write_text("{not json", encoding="utf-8")
            sub = path / "sub"
            sub.mkdir()
            (sub / "nested.jsonl").write_text(json.dumps({"symbol": "999999", "title": "nested"}) + "\n", encoding="utf-8")
            (path / "real.jsonl").write_text(json.dumps({"symbol": "300750", "title": "real"}) + "\n", encoding="utf-8")
            signals = collect_filing_jsonl([{"enabled": True, "path": str(path)}])

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].payload["symbol"], "300750")

    def test_malformed_file_in_directory_reports_path_and_line(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "filings"
            path.mkdir()
            (path / "good.jsonl").write_text(json.dumps({"symbol": "300750", "title": "good"}) + "\n", encoding="utf-8")
            (path / "bad.jsonl").write_text("{bad json", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                collect_filing_jsonl([{"enabled": True, "path": str(path)}])

        self.assertIn("bad.jsonl:1", str(ctx.exception))


class PipelineFilingJsonlTests(unittest.TestCase):
    def test_collect_sources_includes_filing_jsonl_source(self):
        from stock_agent import pipeline

        if not hasattr(pipeline, "collect_filing_jsonl"):
            self.fail("pipeline does not import collect_filing_jsonl")

        filing_signal = RawSignal(source="cninfo", source_type="filing", title="公告证据")
        config = {
            "sources": {
                "local_jsonl": [],
                "filing_jsonl": [{"enabled": True, "path": "filings.jsonl"}],
                "rss": [],
                "eastmoney": {"enabled": False},
                "alltick": {"enabled": False},
                "pysnowball": {"enabled": False},
                "akshare": {"enabled": False},
            }
        }

        with patch.object(pipeline, "collect_filing_jsonl", return_value=[filing_signal]) as collect:
            signals = pipeline.collect_sources(config)

        collect.assert_called_once_with(config["sources"]["filing_jsonl"])
        self.assertEqual(signals, [filing_signal])


class LLMFilingEvidencePromptTests(unittest.TestCase):
    def test_llm_prompt_includes_filing_and_financial_evidence_without_raw_text(self):
        from stock_agent.llm_agent import _build_user_prompt

        signal = AnalyzedSignal(
            signal_id="filing_sid",
            source="cninfo",
            source_type="filing",
            title="宁德时代2025年年度报告",
            summary="公司收入增长，海外业务占比提升。",
            url="https://example.test/report.pdf",
            published_at=now_iso(),
            collected_at=now_iso(),
            priority="P1",
            priority_score=60.0,
            payload={
                "filing_evidence": {
                    "provider": "cninfo",
                    "status": "ok",
                    "symbol": "300750",
                    "announcement_type": "annual_report",
                    "report_period": "2025A",
                    "key_points": ["收入增长"],
                    "risk_points": ["原材料价格波动"],
                },
                "financial_evidence": {
                    "status": "ok",
                    "metrics": {"revenue_yoy": 0.18, "net_profit_yoy": 0.12},
                },
                "raw_text": "very long pdf text should stay out of prompt",
            },
        )

        prompt = _build_user_prompt([signal])

        self.assertIn('"filing_evidence"', prompt)
        self.assertIn('"financial_evidence"', prompt)
        self.assertIn('"revenue_yoy": 0.18', prompt)
        self.assertIn('"announcement_type": "annual_report"', prompt)
        self.assertNotIn("very long pdf text should stay out of prompt", prompt)


if __name__ == "__main__":
    unittest.main()
