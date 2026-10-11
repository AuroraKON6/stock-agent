import json
import os
import urllib.parse
import unittest
from unittest.mock import patch

from stock_agent.models import RawSignal


try:
    from stock_agent.sources.alltick_adapter import (
        collect_alltick,
        _fetch_ticks,
        _to_alltick_code,
    )
except ImportError:
    collect_alltick = None
    _fetch_ticks = None
    _to_alltick_code = None


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class AllTickAdapterTests(unittest.TestCase):
    def setUp(self):
        if collect_alltick is None:
            self.fail("stock_agent.sources.alltick_adapter is not implemented")

    def test_disabled_source_returns_empty_without_network(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("stock_agent.sources.alltick_adapter._fetch_ticks") as fetch:
                signals = collect_alltick({"enabled": False, "symbols": ["600519"]})
        self.assertEqual(signals, [])
        fetch.assert_not_called()

    def test_missing_token_returns_source_error(self):
        with patch.dict(os.environ, {}, clear=True):
            signals = collect_alltick({"enabled": True, "symbols": ["600519"]})

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].source, "alltick")
        self.assertEqual(signals[0].source_type, "source_error")
        self.assertIn("ALLTICK_TOKEN", signals[0].summary)

    def test_a_share_symbol_suffix_mapping(self):
        cases = {
            "600519": "600519.SH",
            "688981": "688981.SH",
            "000001": "000001.SZ",
            "300750": "300750.SZ",
            "430047": "430047.BJ",
            "600519.sh": "600519.SH",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(_to_alltick_code(raw), expected)

    def test_successful_collect_creates_price_signal(self):
        rows = [
            {
                "code": "600519.SH",
                "price": 1510.5,
                "volume": 1200,
                "turnover": 1812600,
                "trade_direction": 1,
                "tick_time": "2026-06-12 10:00:00",
            }
        ]

        with patch.dict(os.environ, {"ALLTICK_TOKEN": "token"}, clear=True):
            with patch("stock_agent.sources.alltick_adapter._fetch_ticks", return_value=rows) as fetch:
                signals = collect_alltick(
                    {
                        "enabled": True,
                        "symbols": ["600519"],
                        "timeout": 9,
                        "base_url": "https://example.test/quote-stock-b-api",
                    }
                )

        fetch.assert_called_once()
        args, kwargs = fetch.call_args
        self.assertEqual(args[0], ["600519.SH"])
        self.assertEqual(kwargs["token"], "token")
        self.assertEqual(kwargs["timeout"], 9)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].source, "alltick")
        self.assertEqual(signals[0].source_type, "price")
        self.assertIn("600519.SH", signals[0].title)
        self.assertEqual(signals[0].payload["code"], "600519.SH")
        self.assertEqual(signals[0].payload["latest"], 1510.5)
        self.assertIsNone(signals[0].payload["pct_change"])
        self.assertEqual(signals[0].payload["raw"], rows[0])

    def test_api_failure_returns_source_error(self):
        with patch.dict(os.environ, {"ALLTICK_TOKEN": "token"}, clear=True):
            with patch("stock_agent.sources.alltick_adapter._fetch_ticks", side_effect=RuntimeError("boom")):
                signals = collect_alltick({"enabled": True, "symbols": ["600519"]})

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].source_type, "source_error")
        self.assertIn("boom", signals[0].summary)

    def test_max_symbols_per_run_defaults_to_five(self):
        rows = [{"code": "600519.SH", "price": 1}]
        with patch.dict(os.environ, {"ALLTICK_TOKEN": "token"}, clear=True):
            with patch("stock_agent.sources.alltick_adapter._fetch_ticks", return_value=rows) as fetch:
                collect_alltick(
                    {
                        "enabled": True,
                        "symbols": ["600519", "300750", "000001", "688981", "601398", "601318"],
                    }
                )

        args, _kwargs = fetch.call_args
        self.assertEqual(
            args[0],
            ["600519.SH", "300750.SZ", "000001.SZ", "688981.SH", "601398.SH"],
        )

    def test_fetch_ticks_uses_trade_tick_endpoint(self):
        payload = {"ret": 200, "data": {"tick_list": [{"code": "600519.SH", "price": 1510.5}]}}
        with patch("stock_agent.sources.alltick_adapter.urllib.request.urlopen", return_value=FakeResponse(payload)) as urlopen:
            rows = _fetch_ticks(
                ["600519.SH"],
                token="token",
                base_url="https://example.test/quote-stock-b-api",
                timeout=11,
            )

        self.assertEqual(rows, [{"code": "600519.SH", "price": 1510.5}])
        request = urlopen.call_args[0][0]
        self.assertIn("/trade-tick?", request.full_url)
        parsed = urllib.parse.urlparse(request.full_url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(query["token"], ["token"])
        body = json.loads(query["query"][0])
        self.assertEqual(body["data"]["symbol_list"], [{"code": "600519.SH"}])
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 11)


class PipelineAllTickTests(unittest.TestCase):
    def test_collect_sources_includes_alltick_source(self):
        from stock_agent import pipeline

        if not hasattr(pipeline, "collect_alltick"):
            self.fail("pipeline does not import collect_alltick")

        alltick_signal = RawSignal(source="alltick", source_type="price", title="AllTick snapshot")
        config = {
            "sources": {
                "local_jsonl": [],
                "rss": [],
                "eastmoney": {"enabled": False},
                "akshare": {"enabled": False},
                "alltick": {"enabled": True, "symbols": ["600519"]},
            }
        }
        with patch.object(pipeline, "collect_alltick", return_value=[alltick_signal]) as collect:
            signals = pipeline.collect_sources(config)

        collect.assert_called_once_with(config["sources"]["alltick"])
        self.assertEqual(signals, [alltick_signal])


if __name__ == "__main__":
    unittest.main()
