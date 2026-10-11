from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from stock_agent.config import project_path_override
from stock_agent.market_data_governance import (
    assess_market_signals,
    market_signal_llm_allowed,
    write_market_data_governance_summary,
)
from stock_agent.models import RawSignal
from stock_agent.analysis import analyze_signals


TZ = timezone(timedelta(hours=8))


def _config(**overrides):
    governance = {
        "enabled": True,
        "timezone": "Asia/Shanghai",
        "max_live_age_seconds": 120,
        "max_closed_market_age_hours": 36,
        "require_quote_timestamp_during_session": True,
        "block_cache_without_quote_timestamp": True,
        "allow_degraded_offhours_llm": False,
        "require_authorized_provider_for_llm": True,
        "approved_internal_research_providers": ["provider_a", "provider_b"],
        "require_consensus_for_llm": False,
        "min_consensus_sources": 2,
        "max_cross_source_deviation_pct": 0.5,
    }
    governance.update(overrides)
    return {"market_data_governance": governance}


def _price(provider="provider_a", latest=100.0, quote_at="2026-08-13T10:00:00+08:00", cache=False):
    evidence = {"provider": "market_cache", "cached_from": provider, "cached_at": "2026-08-13T10:00:30+08:00"} if cache else {}
    return RawSignal(
        source="market_cache" if cache else provider,
        source_type="price",
        title="quote",
        published_at="2026-08-13T10:00:30+08:00",
        payload={
            "code": "600000",
            "latest": latest,
            "pct_change": 1.0,
            "quote_timestamp": quote_at,
            "observed_at": "2026-08-13T10:00:30+08:00",
            "source_evidence": evidence,
        },
    )


class MarketDataGovernanceV23Tests(unittest.TestCase):
    def test_non_price_signal_is_not_gated(self):
        signal = RawSignal(source="filing", source_type="filing", title="filing")
        rows, summary = assess_market_signals([signal], _config(), now=datetime(2026, 8, 13, 10, 1, tzinfo=TZ))
        self.assertEqual(0, summary["price_signal_count"])
        self.assertNotIn("market_data_governance", rows[0].payload)
        self.assertEqual((True, ""), market_signal_llm_allowed(rows[0]))

    def test_fresh_authorized_live_quote_passes(self):
        rows, summary = assess_market_signals(
            [_price()], _config(), now=datetime(2026, 8, 13, 10, 1, tzinfo=TZ)
        )
        gate = rows[0].payload["market_data_governance"]
        self.assertEqual("PASS", gate["status"])
        self.assertTrue(gate["llm_eligible"])
        self.assertEqual("continuous_am", gate["session"])
        self.assertEqual("OK", summary["status"])

    def test_stale_live_quote_is_blocked(self):
        rows, _ = assess_market_signals(
            [_price(quote_at="2026-08-13T09:55:00+08:00")],
            _config(),
            now=datetime(2026, 8, 13, 10, 1, tzinfo=TZ),
        )
        gate = rows[0].payload["market_data_governance"]
        self.assertEqual("BLOCKED", gate["status"])
        self.assertIn("stale_live_quote", gate["reason_codes"])
        self.assertFalse(market_signal_llm_allowed(rows[0])[0])

    def test_missing_timestamp_during_session_is_blocked(self):
        signal = _price(quote_at="")
        rows, _ = assess_market_signals([signal], _config(), now=datetime(2026, 8, 13, 10, 1, tzinfo=TZ))
        self.assertIn(
            "missing_quote_timestamp_during_session",
            rows[0].payload["market_data_governance"]["reason_codes"],
        )

    def test_cache_without_original_quote_timestamp_is_blocked(self):
        signal = _price(cache=True, quote_at="")
        rows, _ = assess_market_signals([signal], _config(), now=datetime(2026, 8, 13, 18, tzinfo=TZ))
        gate = rows[0].payload["market_data_governance"]
        self.assertEqual("BLOCKED", gate["status"])
        self.assertIn("cache_missing_quote_timestamp", gate["reason_codes"])

    def test_off_hours_quote_is_degraded_and_not_sent_to_llm_by_default(self):
        signal = _price(quote_at="2026-08-13T14:59:30+08:00")
        rows, summary = assess_market_signals([signal], _config(), now=datetime(2026, 8, 13, 16, tzinfo=TZ))
        gate = rows[0].payload["market_data_governance"]
        self.assertEqual("DEGRADED", gate["status"])
        self.assertFalse(gate["llm_eligible"])
        self.assertEqual("WARN", summary["status"])

    def test_explicit_policy_can_allow_recent_off_hours_context(self):
        signal = _price(quote_at="2026-08-13T14:59:30+08:00")
        rows, _ = assess_market_signals(
            [signal], _config(allow_degraded_offhours_llm=True), now=datetime(2026, 8, 13, 16, tzinfo=TZ)
        )
        self.assertTrue(rows[0].payload["market_data_governance"]["llm_eligible"])

    def test_unattested_provider_is_blocked_without_claiming_legal_status(self):
        rows, _ = assess_market_signals(
            [_price("unknown_provider")], _config(), now=datetime(2026, 8, 13, 10, 1, tzinfo=TZ)
        )
        gate = rows[0].payload["market_data_governance"]
        self.assertIn("provider_not_internally_attested", gate["reason_codes"])
        self.assertFalse(gate["provider_internally_attested"])
        self.assertNotIn("licensed", json.dumps(gate).lower())

    def test_consistent_two_source_observations_satisfy_consensus(self):
        diagnostics = {"observations": [
            {"symbol": "600000", "provider": "provider_a", "latest": 100.0},
            {"symbol": "600000", "provider": "provider_b", "latest": 100.2},
        ]}
        rows, _ = assess_market_signals(
            [_price()], _config(require_consensus_for_llm=True), diagnostics,
            now=datetime(2026, 8, 13, 10, 1, tzinfo=TZ),
        )
        gate = rows[0].payload["market_data_governance"]
        self.assertEqual("CONSISTENT", gate["consistency"]["status"])
        self.assertTrue(gate["llm_eligible"])

    def test_cross_source_conflict_blocks_quote(self):
        diagnostics = {"observations": [
            {"symbol": "600000", "provider": "provider_a", "latest": 100.0},
            {"symbol": "600000", "provider": "provider_b", "latest": 103.0},
        ]}
        rows, _ = assess_market_signals(
            [_price()], _config(), diagnostics, now=datetime(2026, 8, 13, 10, 1, tzinfo=TZ)
        )
        gate = rows[0].payload["market_data_governance"]
        self.assertEqual("BLOCKED", gate["status"])
        self.assertIn("cross_source_price_conflict", gate["reason_codes"])

    def test_required_consensus_without_second_source_blocks(self):
        rows, _ = assess_market_signals(
            [_price()], _config(require_consensus_for_llm=True),
            now=datetime(2026, 8, 13, 10, 1, tzinfo=TZ),
        )
        gate = rows[0].payload["market_data_governance"]
        self.assertIn("insufficient_cross_source_consensus", gate["reason_codes"])

    def test_weekend_quote_is_off_market_context(self):
        signal = _price(quote_at="2026-08-15T10:00:00+08:00")
        rows, _ = assess_market_signals([signal], _config(), now=datetime(2026, 8, 15, 10, 1, tzinfo=TZ))
        gate = rows[0].payload["market_data_governance"]
        self.assertEqual("closed_market", gate["session"])
        self.assertEqual("DEGRADED", gate["status"])

    def test_disabled_policy_preserves_backward_compatible_llm_eligibility(self):
        rows, summary = assess_market_signals(
            [_price(quote_at="")], {"market_data_governance": {"enabled": False}},
            now=datetime(2026, 8, 13, 10, 1, tzinfo=TZ),
        )
        self.assertEqual("DISABLED", summary["status"])
        self.assertTrue(rows[0].payload["market_data_governance"]["llm_eligible"])

    def test_writer_creates_timestamped_and_latest_audit_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with project_path_override(lambda value: root / value):
                _rows, summary = assess_market_signals(
                    [_price()], _config(), now=datetime(2026, 8, 13, 10, 1, tzinfo=TZ)
                )
                paths = write_market_data_governance_summary(summary, _config())
            for path in paths.values():
                self.assertTrue(Path(path).exists())
            text = Path(paths["market_data_governance_latest_path"]).read_text(encoding="utf-8-sig")
            self.assertIn("internal_research_operating_control_not_regulatory_deadline", text)

    def test_blocked_price_never_reaches_main_or_multi_role_llm_calls(self):
        raw = _price()
        rows, _ = assess_market_signals(
            [raw], _config(approved_internal_research_providers=[]),
            now=datetime(2026, 8, 13, 10, 1, tzinfo=TZ),
        )
        analyzed = analyze_signals([rows[0]], {
            "topics": [],
            "priority_rules": {"p0_keywords": [], "p1_keywords": [], "positive_keywords": [], "negative_keywords": [], "review_keywords": []},
        })[0]
        analyzed.priority = "P1"
        config = {
            "llm_agent": {"enabled": True, "min_priority": "P2", "max_signals_per_run": 5},
            "multi_role_agent": {"enabled": True, "min_priority": "P2", "max_signals_per_run": 5},
        }
        from stock_agent.llm_agent import enrich_signals
        from stock_agent.role_analyst import enrich_signals_with_role_analysts
        with patch("stock_agent.llm_agent._call_llm") as main_call, patch("stock_agent.role_analyst._call_role_llm") as role_call:
            signals, main_stats = enrich_signals([analyzed], config)
            signals, role_stats = enrich_signals_with_role_analysts(signals, config)
        main_call.assert_not_called()
        role_call.assert_not_called()
        self.assertEqual(1, main_stats["llm_market_data_blocked"])
        self.assertEqual(1, role_stats["role_llm_market_data_blocked"])
        self.assertTrue(signals[0].needs_review)
        self.assertIn("Market data governance blocked", signals[0].llm_error)


if __name__ == "__main__":
    unittest.main()
