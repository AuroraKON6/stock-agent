import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.models import RawSignal, now_iso


def _price_signal(source, code="600519", latest=100.0):
    return RawSignal(
        source=source,
        source_type="price",
        title="{0} {1} quote".format(source, code),
        summary="latest={0}".format(latest),
        published_at=now_iso(),
        payload={
            "code": code,
            "name": code,
            "latest": latest,
            "pct_change": 1.2,
            "price_change": 1.0,
        },
    )


def _source_error(source, message="network denied"):
    return RawSignal(
        source=source,
        source_type="source_error",
        title="{0} failed".format(source),
        summary=message,
        published_at=now_iso(),
    )


class MarketResilienceFallbackTests(unittest.TestCase):
    def setUp(self):
        from stock_agent.config import project_path_override

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.enterContext(project_path_override(lambda value: Path(temporary.name) / value))

    def test_verification_provider_is_observed_without_duplicating_output(self):
        from stock_agent.market_resilience import collect_market_quotes_with_resilience

        config = {
            "sources": {
                "eastmoney": {"enabled": True, "symbols": ["600519"], "source_type": "price"},
                "akshare": {"enabled": True, "symbols": ["600519"], "source_type": "price"},
            },
            "market_resilience": {
                "enabled": True,
                "fallback_priority": ["eastmoney", "akshare", "cache"],
                "verification_providers": ["akshare"],
            },
        }
        signals, _events, diagnostics = collect_market_quotes_with_resilience(config, collectors={
            "eastmoney": lambda cfg: [_price_signal("eastmoney", "600519", 100.0)],
            "akshare": lambda cfg: [_price_signal("akshare", "600519", 100.2)],
        })
        self.assertEqual(1, len(signals))
        self.assertEqual("eastmoney", signals[0].source)
        self.assertEqual(["eastmoney", "akshare"], diagnostics["providers_attempted"])
        self.assertEqual({"eastmoney", "akshare"}, {item["provider"] for item in diagnostics["observations"]})

    def test_eastmoney_exchange_timestamp_is_preserved_separately_from_observation(self):
        from stock_agent.sources.eastmoney_adapter import collect_eastmoney

        with patch("stock_agent.sources.eastmoney_adapter._fetch_quotes", return_value=[{
            "f12": "600519", "f14": "Moutai", "f2": 100, "f3": 1.0,
            "f4": 1.0, "f6": 1000, "f62": 100, "f124": 1786928400,
        }]):
            signal = collect_eastmoney({"enabled": True, "symbols": ["600519"]})[0]
        self.assertTrue(signal.payload["quote_timestamp"])
        self.assertTrue(signal.payload["observed_at"])
        self.assertEqual(signal.payload["quote_timestamp"], signal.published_at)
        self.assertNotEqual(signal.payload["quote_timestamp"], signal.payload["observed_at"])

    def test_uses_fallback_provider_and_marks_primary_failure_resolved(self):
        from stock_agent.market_resilience import collect_market_quotes_with_resilience
        from stock_agent.source_health import build_source_health_summary

        config = {
            "sources": {
                "eastmoney": {"enabled": True, "symbols": ["600519", "300750"], "source_type": "price"},
                "akshare": {"enabled": False, "symbols": ["600519", "300750"], "source_type": "price"},
            },
            "market_resilience": {
                "enabled": True,
                "fallback_priority": ["eastmoney", "akshare", "cache"],
                "allow_disabled_fallbacks": True,
            },
            "source_health": {"warn_score_below": 80, "fail_score_below": 50},
        }
        collectors = {
            "eastmoney": lambda cfg: [_source_error("eastmoney", "WinError 10013")],
            "akshare": lambda cfg: [_price_signal("akshare", "600519"), _price_signal("akshare", "300750")],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("stock_agent.market_resilience.project_path", side_effect=lambda p: root / p):
                signals, events, diagnostics = collect_market_quotes_with_resilience(config, collectors=collectors)
        summary = build_source_health_summary(events, config)

        self.assertEqual([s.source for s in signals], ["akshare", "akshare"])
        self.assertEqual(diagnostics["status"], "WARN")
        self.assertTrue(diagnostics["fallback_used"])
        self.assertEqual(diagnostics["covered_symbols"], ["600519", "300750"])
        eastmoney_event = [e for e in events if e["source_id"] == "eastmoney"][0]
        self.assertTrue(eastmoney_event["fallback_resolved"])
        self.assertEqual(summary["overall_status"], "OK")
        self.assertEqual(summary["source_degraded_event_count"], 1)
        self.assertNotEqual(summary["overall_status"], "FAIL")

    def test_uses_recent_cache_when_all_live_providers_fail(self):
        from stock_agent.market_resilience import collect_market_quotes_with_resilience, write_quote_cache

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = {
                "sources": {"eastmoney": {"enabled": True, "symbols": ["600519"], "source_type": "price"}},
                "market_resilience": {
                    "enabled": True,
                    "fallback_priority": ["eastmoney", "cache"],
                    "cache_ttl_minutes": 60,
                },
            }
            with patch("stock_agent.market_resilience.project_path", side_effect=lambda p: root / p):
                write_quote_cache([_price_signal("eastmoney", "600519", latest=188.8)])
                signals, _events, diagnostics = collect_market_quotes_with_resilience(
                    config,
                    collectors={"eastmoney": lambda cfg: [_source_error("eastmoney", "temporary outage")]},
                )

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].source, "market_cache")
        self.assertEqual(signals[0].payload["latest"], 188.8)
        self.assertTrue(diagnostics["cache_used"])
        self.assertEqual(diagnostics["status"], "WARN")

    def test_write_quote_cache_skips_incomplete_quotes(self):
        from stock_agent.market_resilience import write_quote_cache

        incomplete = RawSignal(
            source="pysnowball",
            source_type="price",
            title="incomplete quote",
            summary="no latest price",
            published_at=now_iso(),
            payload={"code": "600519"},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("stock_agent.market_resilience.project_path", side_effect=lambda p: root / p):
                path = write_quote_cache([incomplete])

            self.assertEqual(path, "")
            self.assertFalse((root / "data" / "cache" / "market_quotes" / "latest.json").exists())

    def test_seeds_quote_cache_from_latest_complete_json_reports(self):
        from stock_agent.market_resilience import load_quote_cache, seed_quote_cache_from_reports

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report_dir = root / "reports" / "json"
            report_dir.mkdir(parents=True)
            (report_dir / "20260701T010000_0800.json").write_text(json.dumps({
                "signals": [
                    {
                        "source": "eastmoney",
                        "source_type": "price",
                        "title": "old 600519",
                        "summary": "old quote",
                        "published_at": now_iso(),
                        "payload": {"code": "600519", "latest": 1200.0, "pct_change": -1.0},
                    }
                ]
            }, ensure_ascii=False), encoding="utf-8")
            (report_dir / "20260702T010000_0800.json").write_text(json.dumps({
                "signals": [
                    {
                        "source": "eastmoney",
                        "source_type": "price",
                        "title": "new 600519",
                        "summary": "new quote",
                        "published_at": now_iso(),
                        "payload": {"code": "600519", "latest": 1210.0, "pct_change": 0.5},
                    },
                    {
                        "source": "eastmoney",
                        "source_type": "price",
                        "title": "new 300750",
                        "summary": "new quote",
                        "published_at": now_iso(),
                        "payload": {"code": "300750", "latest": 390.0, "pct_change": 0.2},
                    },
                    {
                        "source": "eastmoney",
                        "source_type": "price",
                        "title": "bad 000001",
                        "summary": "bad quote",
                        "published_at": now_iso(),
                        "payload": {"code": "000001", "latest": None, "pct_change": 0.1},
                    },
                ]
            }, ensure_ascii=False), encoding="utf-8")
            config = {
                "sources": {"eastmoney": {"enabled": True, "symbols": ["600519", "300750", "000001"]}},
                "market_resilience": {"enabled": True},
            }
            with patch("stock_agent.market_resilience.project_path", side_effect=lambda p: root / p):
                result = seed_quote_cache_from_reports(config)
                cached = load_quote_cache(["600519", "300750", "000001"], ttl_minutes=60)

        self.assertEqual(result["seeded_count"], 2)
        self.assertEqual(result["missing_symbols"], ["000001"])
        by_code = {item.payload["code"]: item for item in cached}
        self.assertEqual(by_code["600519"].payload["latest"], 1210.0)
        self.assertEqual(by_code["300750"].payload["latest"], 390.0)
        self.assertIn("cache_seed_report", by_code["600519"].payload)

    def test_seed_quote_cache_ignores_existing_market_cache_reports(self):
        from stock_agent.market_resilience import load_quote_cache, seed_quote_cache_from_reports

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report_dir = root / "reports" / "json"
            report_dir.mkdir(parents=True)
            (report_dir / "20260701T010000_0800.json").write_text(json.dumps({
                "signals": [
                    {
                        "source": "eastmoney",
                        "source_type": "price",
                        "title": "trusted 600519",
                        "summary": "trusted quote",
                        "published_at": now_iso(),
                        "payload": {"code": "600519", "latest": 1210.0, "pct_change": 0.5},
                    }
                ]
            }, ensure_ascii=False), encoding="utf-8")
            (report_dir / "20260702T010000_0800.json").write_text(json.dumps({
                "signals": [
                    {
                        "source": "market_cache",
                        "source_type": "price",
                        "title": "cached 600519",
                        "summary": "cached quote",
                        "published_at": now_iso(),
                        "payload": {
                            "code": "600519",
                            "latest": 100.0,
                            "pct_change": 1.2,
                            "source_evidence": {"provider": "market_cache"},
                        },
                    }
                ]
            }, ensure_ascii=False), encoding="utf-8")
            config = {
                "sources": {"eastmoney": {"enabled": True, "symbols": ["600519"]}},
                "market_resilience": {"enabled": True},
            }
            with patch("stock_agent.market_resilience.project_path", side_effect=lambda p: root / p):
                seed_quote_cache_from_reports(config)
                cached = load_quote_cache(["600519"], ttl_minutes=60)

        self.assertEqual(len(cached), 1)
        self.assertEqual(cached[0].payload["latest"], 1210.0)
        self.assertEqual(cached[0].payload["cached_from"], "eastmoney")


class MarketResiliencePipelineTests(unittest.TestCase):
    def test_pipeline_uses_fallback_quotes_without_source_error_signals(self):
        from stock_agent.pipeline import run_pipeline

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = {
                "storage": {"sqlite_path": str(root / "data" / "signals.sqlite3")},
                "sources": {
                    "eastmoney": {"enabled": True, "symbols": ["600519"], "source_type": "price"},
                    "akshare": {"enabled": False, "symbols": ["600519"], "source_type": "price"},
                },
                "market_resilience": {
                    "enabled": True,
                    "fallback_priority": ["eastmoney", "akshare", "cache"],
                    "allow_disabled_fallbacks": True,
                },
                "topics": [{"name": "market", "symbols": ["600519"], "keywords": [], "risk_keywords": []}],
                "priority_rules": {
                    "p0_keywords": [],
                    "p1_keywords": [],
                    "positive_keywords": [],
                    "negative_keywords": [],
                    "review_keywords": [],
                },
                "research_cases": {"enabled": False},
                "research_framework": {"enabled": False},
                "research_team": {"enabled": False},
                "report": {"include_low_priority": True},
            }
            with patch("stock_agent.pipeline.collect_eastmoney", return_value=[_source_error("eastmoney", "WinError 10013")]), \
                 patch("stock_agent.pipeline.collect_akshare", return_value=[_price_signal("akshare", "600519")]), \
                 patch("stock_agent.config.PROJECT_ROOT", root), \
                 patch("stock_agent.pipeline.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.reports.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.run_ops.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.market_resilience.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.source_health.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.source_remediation.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.decisions.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.team_ops.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.research_cases.project_path", side_effect=lambda p: root / p):
                result = run_pipeline(config)

            data = json.loads(Path(result.json_path).read_text(encoding="utf-8"))
            report_text = Path(result.report_path).read_text(encoding="utf-8")

        self.assertEqual(result.stats["source_errors"], 0)
        self.assertEqual(result.stats["market_resilience"]["status"], "WARN")
        self.assertIn("akshare", result.signals[0].source)
        self.assertIn("External Dependencies", report_text)
        self.assertIn("market_resilience", data["stats"])

    def test_cli_market_cache_seed_writes_cache_from_reports(self):
        from stock_agent.cli import main

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "config").mkdir(parents=True)
            (root / "reports" / "json").mkdir(parents=True)
            (root / "config" / "watchlist.local.json").write_text(json.dumps({
                "sources": {"eastmoney": {"enabled": True, "symbols": ["600519"]}},
                "market_resilience": {"enabled": True},
            }), encoding="utf-8")
            (root / "reports" / "json" / "20260702T010000_0800.json").write_text(json.dumps({
                "signals": [
                    {
                        "source": "eastmoney",
                        "source_type": "price",
                        "title": "600519 quote",
                        "summary": "quote",
                        "published_at": now_iso(),
                        "payload": {"code": "600519", "latest": 1210.0, "pct_change": 0.5},
                    }
                ]
            }), encoding="utf-8")
            out = []
            argv = ["stock_agent", "market-cache-seed", "--config", "config/watchlist.local.json"]
            with patch.object(__import__("sys"), "argv", argv), \
                 patch("builtins.print", side_effect=lambda *args, **kwargs: out.append(" ".join(str(a) for a in args))), \
                 patch("stock_agent.config.PROJECT_ROOT", root), \
                 patch("stock_agent.config.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.cli.PROJECT_ROOT", root), \
                 patch("stock_agent.cli.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.market_resilience.project_path", side_effect=lambda p: root / p):
                main()

            cache_path = root / "data" / "cache" / "market_quotes" / "latest.json"

            self.assertTrue(cache_path.exists())
            self.assertTrue(any("seeded_count: 1" in line for line in out))


class ExternalDependencyVisibilityTests(unittest.TestCase):
    def test_doctor_reports_llm_connectivity_winerror_with_repair_guidance(self):
        from stock_agent.doctor import _check_llm_connectivity

        with patch("stock_agent.doctor._probe_llm_connectivity", return_value={
            "ok": False,
            "error": "[WinError 10013] access denied",
            "diagnosis": "socket_permission_denied",
            "repair_guide": ["Allow Python outbound HTTPS", "Check firewall or security software"],
        }):
            status, detail, note = _check_llm_connectivity({
                "llm_agent": {"enabled": True, "base_url": "https://api.deepseek.com/v1", "model": "deepseek-v4-pro"}
            })

        self.assertEqual(status, "WARN")
        self.assertIn("socket_permission_denied", detail)
        self.assertIn("Allow Python outbound HTTPS", note)

    def test_alerts_trigger_on_external_dependency_degradation(self):
        from stock_agent.alerts import _check_triggers

        triggers = _check_triggers(
            [],
            [],
            [],
            [],
            external_dependencies={
                "market_resilience": {"status": "WARN", "missing_symbols": ["300750"]},
                "llm_connectivity": {"status": "WARN", "diagnosis": "socket_permission_denied"},
            },
        )

        self.assertIn("external_dependencies", [name for name, _ in triggers])

    def test_dashboard_renders_external_dependencies_section(self):
        from stock_agent.dashboard import _render_dashboard_html

        html = _render_dashboard_html({
            "generated_at": now_iso(),
            "signals": [],
            "stats": {
                "market_resilience": {"status": "WARN", "fallback_used": True, "covered_symbols": ["600519"]},
                "llm_connectivity": {
                    "status": "WARN",
                    "diagnosis": "socket_permission_denied",
                    "repair_guide": ["Allow Python outbound HTTPS"],
                },
            },
            "alerts_list": [],
            "pending_review": [],
            "review_summary": None,
        })

        self.assertIn("External Dependencies", html)
        self.assertIn("socket_permission_denied", html)
        self.assertIn("Allow Python outbound HTTPS", html)


if __name__ == "__main__":
    unittest.main()
