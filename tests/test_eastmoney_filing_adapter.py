import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.models import RawSignal


try:
    from stock_agent.sources.eastmoney_filing_adapter import (
        collect_eastmoney_filings,
        _fetch_announcements,
        _row_to_signal,
        _to_filing_jsonl_record,
    )
except ImportError:
    collect_eastmoney_filings = None
    _fetch_announcements = None
    _row_to_signal = None
    _to_filing_jsonl_record = None


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


def _eastmoney_row():
    return {
        "art_code": "AN202606111823465368",
        "title": "贵州茅台:贵州茅台2025年年度报告",
        "title_ch": "贵州茅台:贵州茅台2025年年度报告",
        "notice_date": "2026-06-12 00:00:00",
        "display_time": "2026-06-11 20:55:07:357",
        "columns": [{"column_code": "001002", "column_name": "年度报告"}],
        "codes": [{"stock_code": "600519", "short_name": "贵州茅台", "market_code": "1"}],
    }


class EastmoneyFilingAdapterTests(unittest.TestCase):
    def setUp(self):
        if collect_eastmoney_filings is None:
            self.fail("stock_agent.sources.eastmoney_filing_adapter is not implemented")

    def test_disabled_source_returns_empty_without_network(self):
        with patch("stock_agent.sources.eastmoney_filing_adapter._fetch_announcements") as fetch:
            signals = collect_eastmoney_filings({"enabled": False, "symbols": ["600519"]})

        self.assertEqual(signals, [])
        fetch.assert_not_called()

    def test_fetch_announcements_uses_eastmoney_security_ann_endpoint(self):
        payload = {"data": {"list": [_eastmoney_row()]}}
        with patch("stock_agent.sources.eastmoney_filing_adapter.urllib.request.urlopen", return_value=FakeResponse(payload)) as urlopen:
            rows = _fetch_announcements("600519", page_size=3, timeout=11, ann_type="A")

        self.assertEqual(len(rows), 1)
        request = urlopen.call_args[0][0]
        self.assertIn("np-anotice-stock.eastmoney.com/api/security/ann", request.full_url)
        self.assertIn("stock_list=600519", request.full_url)
        self.assertIn("page_size=3", request.full_url)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 11)

    def test_successful_collect_creates_filing_signal_and_landing_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.sources.eastmoney_filing_adapter._fetch_announcements", return_value=[_eastmoney_row()]) as fetch, \
                 patch("stock_agent.sources.eastmoney_filing_adapter.project_path", side_effect=lambda p: Path(tmpdir) / p):
                signals = collect_eastmoney_filings({
                    "enabled": True,
                    "symbols": ["600519"],
                    "page_size": 5,
                    "max_announcements_per_symbol": 2,
                    "write_jsonl": True,
                    "output_dir": "data/inbox/filings",
                })

            fetch.assert_called_once()
            self.assertEqual(len(signals), 1)
            signal = signals[0]
            self.assertEqual(signal.source, "eastmoney_filing")
            self.assertEqual(signal.source_type, "filing")
            self.assertIn("年度报告", signal.title)
            self.assertIn("data.eastmoney.com/notices/detail/600519/AN202606111823465368.html", signal.url)
            filing = signal.payload["filing_evidence"]
            self.assertEqual(filing["provider"], "eastmoney_filing")
            self.assertEqual(filing["status"], "ok")
            self.assertEqual(filing["announcement_type"], "annual_report")
            self.assertEqual(filing["report_period"], "2025A")
            self.assertIn("importance_level", filing)
            self.assertIn("importance_score", filing)

            files = list((Path(tmpdir) / "data" / "inbox" / "filings").glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            record = json.loads(files[0].read_text(encoding="utf-8").strip())
            self.assertEqual(record["source"], "eastmoney_filing")
            self.assertEqual(record["symbol"], "600519")
            self.assertEqual(record["announcement_type"], "annual_report")
            self.assertEqual(record["report_period"], "2025A")
            self.assertIn("filing_importance_level", record)
            self.assertIn("filing_importance_score", record)

    def test_api_failure_returns_source_error_and_does_not_raise(self):
        with patch("stock_agent.sources.eastmoney_filing_adapter._fetch_announcements", side_effect=RuntimeError("boom")):
            signals = collect_eastmoney_filings({"enabled": True, "symbols": ["600519"]})

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].source, "eastmoney_filing")
        self.assertEqual(signals[0].source_type, "source_error")
        self.assertIn("boom", signals[0].summary)

    def test_row_to_signal_marks_partial_when_financial_report_lacks_period(self):
        row = _eastmoney_row()
        row["title"] = "贵州茅台:贵州茅台年度报告"
        row["title_ch"] = "贵州茅台:贵州茅台年度报告"
        signal = _row_to_signal(row, {})

        filing = signal.payload["filing_evidence"]
        self.assertEqual(filing["announcement_type"], "annual_report")
        self.assertEqual(filing["status"], "partial")
        self.assertIn("report_period", filing["missing_fields"])
        self.assertTrue(filing["human_review_required"])

    def test_to_filing_jsonl_record_excludes_raw_eastmoney_payload(self):
        signal = _row_to_signal(_eastmoney_row(), {})
        record = _to_filing_jsonl_record(signal)

        self.assertEqual(record["source"], "eastmoney_filing")
        self.assertIn("key_points", record)
        self.assertIn("filing_importance_level", record)
        self.assertIn("filing_importance_score", record)
        self.assertNotIn("raw", record)
        self.assertNotIn("eastmoney_raw", record)


class PipelineEastmoneyFilingTests(unittest.TestCase):
    def test_collect_sources_includes_eastmoney_filing_source(self):
        from stock_agent import pipeline

        if not hasattr(pipeline, "collect_eastmoney_filings"):
            self.fail("pipeline does not import collect_eastmoney_filings")

        filing_signal = RawSignal(source="eastmoney_filing", source_type="filing", title="公告")
        config = {
            "sources": {
                "local_jsonl": [],
                "filing_jsonl": [],
                "eastmoney_filing": {"enabled": True, "symbols": ["600519"]},
                "rss": [],
                "eastmoney": {"enabled": False},
                "alltick": {"enabled": False},
                "pysnowball": {"enabled": False},
                "akshare": {"enabled": False},
            }
        }

        with patch.object(pipeline, "collect_eastmoney_filings", return_value=[filing_signal]) as collect:
            signals = pipeline.collect_sources(config)

        collect.assert_called_once_with(config["sources"]["eastmoney_filing"])
        self.assertEqual(signals, [filing_signal])


if __name__ == "__main__":
    unittest.main()
