import os
import unittest
from unittest.mock import patch

from stock_agent.models import AnalyzedSignal, RawSignal, now_iso


try:
    from stock_agent.sources.pysnowball_adapter import (
        collect_pysnowball,
        _row_to_signal,
        _to_xueqiu_symbol,
    )
except ImportError:
    collect_pysnowball = None
    _row_to_signal = None
    _to_xueqiu_symbol = None


class PySnowballAdapterTests(unittest.TestCase):
    def setUp(self):
        if collect_pysnowball is None:
            self.fail("stock_agent.sources.pysnowball_adapter is not implemented")

    def test_disabled_source_returns_empty_without_token_or_network(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("stock_agent.sources.pysnowball_adapter._fetch_quote_rows") as fetch:
                signals = collect_pysnowball({"enabled": False, "symbols": ["600519"]})

        self.assertEqual(signals, [])
        fetch.assert_not_called()

    def test_missing_token_returns_source_error(self):
        with patch.dict(os.environ, {}, clear=True):
            signals = collect_pysnowball({"enabled": True, "symbols": ["600519"]})

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].source, "pysnowball")
        self.assertEqual(signals[0].source_type, "source_error")
        self.assertIn("XUEQIU_TOKEN", signals[0].summary)

    def test_pysnowball_token_env_is_fallback(self):
        with patch.dict(os.environ, {"PYSNOWBALL_TOKEN": "token"}, clear=True):
            with patch("stock_agent.sources.pysnowball_adapter._fetch_quote_rows", return_value=[]) as fetch:
                collect_pysnowball({"enabled": True, "symbols": ["600519"]})

        self.assertEqual(fetch.call_args.kwargs["token"], "token")

    def test_a_share_symbol_mapping_for_xueqiu(self):
        cases = {
            "600519": "SH600519",
            "688981": "SH688981",
            "000001": "SZ000001",
            "300750": "SZ300750",
            "430047": "BJ430047",
            "SH600519": "SH600519",
            "sz300750": "SZ300750",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(_to_xueqiu_symbol(raw), expected)

    def test_successful_collect_creates_price_signal_with_evidence(self):
        rows = [
            {
                "symbol": "SH600519",
                "name": "贵州茅台",
                "current": 1510.5,
                "percent": 3.21,
                "chg": 46.9,
                "amount": 2300000000,
                "volume": 154000,
                "timestamp": 1781248800000,
            }
        ]

        with patch.dict(os.environ, {"XUEQIU_TOKEN": "token"}, clear=True):
            with patch("stock_agent.sources.pysnowball_adapter._fetch_quote_rows", return_value=rows) as fetch:
                signals = collect_pysnowball({"enabled": True, "symbols": ["600519"], "timeout": 9})

        fetch.assert_called_once()
        args, kwargs = fetch.call_args
        self.assertEqual(args[0], ["SH600519"])
        self.assertEqual(kwargs["token"], "token")
        self.assertEqual(kwargs["timeout"], 9)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].source, "pysnowball")
        self.assertEqual(signals[0].source_type, "price")
        self.assertEqual(signals[0].payload["code"], "SH600519")
        self.assertEqual(signals[0].payload["name"], "贵州茅台")
        self.assertEqual(signals[0].payload["latest"], 1510.5)
        self.assertEqual(signals[0].payload["pct_change"], 3.21)
        evidence = signals[0].payload["source_evidence"]
        self.assertEqual(evidence["provider"], "pysnowball")
        self.assertEqual(evidence["status"], "ok")
        self.assertIn("current", evidence["fields_present"])

    def test_api_failure_returns_source_error(self):
        with patch.dict(os.environ, {"XUEQIU_TOKEN": "token"}, clear=True):
            with patch("stock_agent.sources.pysnowball_adapter._fetch_quote_rows", side_effect=RuntimeError("boom")):
                signals = collect_pysnowball({"enabled": True, "symbols": ["600519"]})

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].source_type, "source_error")
        self.assertIn("boom", signals[0].summary)

    def test_row_to_signal_handles_missing_fields_as_partial_evidence(self):
        signal = _row_to_signal({"symbol": "SZ300750", "name": "宁德时代"}, {})

        self.assertEqual(signal.payload["code"], "SZ300750")
        self.assertIsNone(signal.payload["latest"])
        evidence = signal.payload["source_evidence"]
        self.assertEqual(evidence["status"], "partial")
        self.assertIn("current", evidence["missing_fields"])


class PipelinePySnowballTests(unittest.TestCase):
    def test_collect_sources_includes_pysnowball_source(self):
        from stock_agent import pipeline

        if not hasattr(pipeline, "collect_pysnowball"):
            self.fail("pipeline does not import collect_pysnowball")

        snowball_signal = RawSignal(source="pysnowball", source_type="price", title="雪球行情快照")
        config = {
            "sources": {
                "local_jsonl": [],
                "rss": [],
                "eastmoney": {"enabled": False},
                "akshare": {"enabled": False},
                "alltick": {"enabled": False},
                "pysnowball": {"enabled": True, "symbols": ["600519"]},
            }
        }
        with patch.object(pipeline, "collect_pysnowball", return_value=[snowball_signal]) as collect:
            signals = pipeline.collect_sources(config)

        collect.assert_called_once_with(config["sources"]["pysnowball"])
        self.assertEqual(signals, [snowball_signal])


class LLMPromptEvidenceTests(unittest.TestCase):
    def test_llm_prompt_includes_source_evidence_without_full_raw_payload(self):
        from stock_agent.llm_agent import _build_user_prompt

        signal = AnalyzedSignal(
            signal_id="sid",
            source="pysnowball",
            source_type="price",
            title="贵州茅台(SH600519) 雪球行情快照",
            summary="最新价=1510.5; 涨跌幅=3.21%; 成交额=2300000000",
            url="",
            published_at=now_iso(),
            collected_at=now_iso(),
            priority="P2",
            priority_score=30.0,
            payload={
                "code": "SH600519",
                "latest": 1510.5,
                "pct_change": 3.21,
                "amount": 2300000000,
                "source_evidence": {
                    "provider": "pysnowball",
                    "status": "ok",
                    "fields_present": ["current", "percent", "amount"],
                    "missing_fields": [],
                },
                "raw": {"large": "raw data should stay out of prompt"},
            },
        )

        prompt = _build_user_prompt([signal])

        self.assertIn('"source_evidence"', prompt)
        self.assertIn('"provider": "pysnowball"', prompt)
        self.assertIn('"latest": 1510.5', prompt)
        self.assertNotIn("raw data should stay out of prompt", prompt)


if __name__ == "__main__":
    unittest.main()
