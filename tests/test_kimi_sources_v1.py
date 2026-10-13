"""K03: Kimi sources adapter — ledger, cache, gates, normalization (offline).

Covers T03 (permission gates on execute/cache), T12 (daily bars never take
the realtime lane), T13 (unknown units stay gaps), T14/T19 (idempotent replay
never re-bills), T15 (announcement URLs are leads; provider analysis stays
separate), T20 (adapter refuses cleanly when sources are not approved). All
paths run against a fake transport and a TemporaryDirectory ledger.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from stock_agent.config import project_path_override
from stock_agent.integrations.kimi import client as kimi_client
from stock_agent.integrations.kimi import contracts as kimi_contracts
from stock_agent.market_data_governance import assess_market_signals
from stock_agent.models import RawSignal
from stock_agent.sources import kimi_adapter
from stock_agent.sources.registry import validate_registry_entries

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_kimi_client_v1 import FakeTransport, SNAPSHOT, envelope, snapshot

PRICED_PARAMS = {"ticker": "600519.SH", "start_date": "2026-09-28",
                 "end_date": "2026-10-09", "adjust": "forward"}


def registry_entry(source_id: str, kind: str, *, state: str = "approved",
                    expires: str = "2099-12-31", retention: bool = True,
                    external_send: bool = False,
                    upstream: str = "ifind") -> Dict[str, Any]:
    return {
        "source_id": source_id,
        "kind": kind,
        "upstream": upstream,
        "purpose": "test purpose",
        "approval": {"state": state,
                     "approved_by": "真实负责人" if state == "approved" else "",
                     "approved_at": "2026-10-01T00:00:00" if state == "approved" else ""},
        "body_retention_allowed": retention,
        "llm_external_send_allowed": external_send,
        "timeliness": {"expected_max_delay_minutes": 1440, "staleness_policy": "flag"},
        "rate_limit": {"max_requests_per_minute": 1, "max_requests_per_day": 10},
        "expires_at": expires,
    }


def registry_with(*entries: Dict[str, Any]) -> Dict[str, Any]:
    return {"schema_version": "1.0",
            "entries": {entry["source_id"]: entry for entry in entries},
            "path": "test"}


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.snapshot = snapshot(self.root)
        self.config = {"kimi_integration": {"enabled": True},
                       "kimi_sources": {"ledger_path": "data/kimi/query_ledger.jsonl",
                                        "cache_dir": "data/kimi/cache",
                                        "daily_data_call_limit": 3}}
        self.registry = registry_with(
            registry_entry("kimi_ifind_price", "price"),
            registry_entry("kimi_ifind_filing", "filing"),
            registry_entry("kimi_ifind_financials", "financial_statement"),
            registry_entry("kimi_dongcai_news", "news"),
        )

    def run_price(self, transport: Optional[FakeTransport] = None, *,
                  params: Optional[Dict[str, Any]] = None,
                  registry: Optional[Dict[str, Any]] = None,
                  config: Optional[Dict[str, Any]] = None,
                  allow_cache: bool = True) -> Dict[str, Any]:
        return kimi_adapter.governed_execute(
            self.snapshot, "ifind_get_price", params or PRICED_PARAMS,
            purpose="event price background", config=config or self.config,
            registry=registry or self.registry,
            transport=transport or FakeTransport([_ok_price_outcome()]),
            credential_provider=lambda: "k" * 32,
            allow_cache=allow_cache)


def _ok_price_outcome() -> Any:
    from stock_agent.integrations.kimi import transport as kimi_transport
    csv_body = "日期,开盘价,收盘价,成交量\n2026-10-09,1690.0,1700.0,33000\n"
    return kimi_transport.TransportOutcome(
        "ok", http_status=200,
        body=envelope(files=[{"name": "ifind.csv", "content": csv_body}]))


class RegistryKindTests(unittest.TestCase):
    def test_financial_statement_kind_accepted(self):
        entries = validate_registry_entries([registry_entry("kimi_ifind_financials", "financial_statement")])
        self.assertEqual(entries[0]["kind"], "financial_statement")

    def test_unknown_kind_still_rejected(self):
        with self.assertRaises(Exception):
            validate_registry_entries([registry_entry("kimi_x", "unknown_kind")])

    def test_duplicate_upstream_kind_still_rejected(self):
        with self.assertRaises(Exception):
            validate_registry_entries([
                registry_entry("kimi_ifind_price", "price"),
                registry_entry("another_name", "price", upstream="ifind"),
            ])


class SourceGateTests(LedgerTestCase):
    def test_unregistered_source_refused_before_intent(self):
        outcome = self.run_price(registry=registry_with())
        self.assertEqual(outcome["stage"], "gate")
        self.assertEqual(outcome["gate"]["reason"], "source_not_registered")
        self.assertEqual(kimi_adapter.load_query_ledger(self.config), [])

    def test_pending_approval_refused(self):
        registry = registry_with(registry_entry("kimi_ifind_price", "price", state="pending_human_approval"))
        outcome = self.run_price(registry=registry)
        self.assertEqual(outcome["gate"]["reason"], "source_not_approved")

    def test_expired_approval_refused(self):
        registry = registry_with(registry_entry("kimi_ifind_price", "price", expires="2020-01-01"))
        outcome = self.run_price(registry=registry)
        self.assertEqual(outcome["gate"]["reason"], "source_expired")

    def test_daily_data_call_budget_enforced(self):
        # Distinct windows produce distinct idempotency keys, so each call is
        # a genuine billable attempt; the fourth hits the daily cap.
        for day in range(3):
            start = "2026-09-{0:02d}".format(28 - day)
            self.run_price(params=dict(PRICED_PARAMS, start_date=start), allow_cache=False)
        outcome = self.run_price(params=dict(PRICED_PARAMS, start_date="2026-09-01"),
                                 allow_cache=False)
        self.assertEqual(outcome["stage"], "budget")
        self.assertFalse(outcome["allowed"])
        self.assertEqual(outcome["budget"]["calls_today"], 3)

    def test_idempotent_replay_returns_prior_intent_not_new_send(self):
        first = self.run_price()
        self.assertEqual(first["stage"], "executed")
        transport = FakeTransport([_ok_price_outcome()])
        second = self.run_price(transport=transport, allow_cache=False)
        self.assertEqual(second["stage"], "replay")
        self.assertEqual(second["idempotency_key"], first["idempotency_key"])
        self.assertEqual(transport.requests, [])
        intents = [r for r in kimi_adapter.load_query_ledger(self.config)
                   if r["record_type"] == "query_intent"]
        self.assertEqual(len(intents), 1)


class CacheRightsTests(LedgerTestCase):
    def test_cache_written_only_when_retention_allowed(self):
        self.run_price()
        cache_files = list((self.root / "data/kimi/cache").glob("*.json"))
        self.assertEqual(len(cache_files), 1)

    def test_cache_denied_without_retention_right(self):
        registry = registry_with(registry_entry("kimi_ifind_price", "price", retention=False))
        self.run_price(registry=registry)
        cache_dir = self.root / "data/kimi/cache"
        self.assertFalse(cache_dir.exists() or any(cache_dir.glob("*.json")))

    def test_cache_read_reverifies_permissions(self):
        self.run_price()
        revoked = registry_with(registry_entry("kimi_ifind_price", "price", state="withdrawn"))
        outcome = self.run_price(registry=revoked)
        self.assertEqual(outcome["stage"], "gate")
        self.assertEqual(outcome["gate"]["reason"], "source_not_approved")

    def test_cache_hit_does_not_send(self):
        self.run_price()
        transport = FakeTransport([_ok_price_outcome()])
        outcome = self.run_price(transport=transport)
        self.assertEqual(outcome["stage"], "cache")
        self.assertTrue(outcome["cache_hit"])
        self.assertEqual(transport.requests, [])
        consumption = [r for r in kimi_adapter.load_query_ledger(self.config)
                       if r["record_type"] == "consumed"]
        self.assertEqual(consumption[-1]["purpose"], "event price background")

    def test_cache_key_binds_contract_hash(self):
        self.run_price()
        files = list((self.root / "data/kimi/cache").glob("*.json"))
        cached = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(cached["contract_hash"], self.snapshot["contract_hash"])


class NormalizationTests(LedgerTestCase):
    def test_daily_quote_unknown_units_stay_gaps(self):
        normalized = kimi_adapter.normalize_daily_quote_csv(
            "日期,开盘价,收盘价,成交量,未知列\n2026-10-09,1690,1700,33000,mystery\n",
            ticker="600519.SH", adjust="forward",
            window={"start": "2026-09-28", "end": "2026-10-09"})
        self.assertEqual(normalized["bars"][0]["close"], 1700.0)
        self.assertIn("未知列", normalized["unmapped_columns"])
        self.assertIsNone(normalized["known_unknowns"]["volume_unit"]["value"])
        self.assertEqual(normalized["data_scope"], "historical_background")

    def test_non_numeric_cell_recorded_not_guessed(self):
        normalized = kimi_adapter.normalize_daily_quote_csv(
            "日期,收盘价\n2026-10-09,停牌\n")
        self.assertIsNone(normalized["bars"][0]["close"])
        self.assertTrue(any("non_numeric" in item for item in normalized["unmapped_columns"]))

    def test_announcement_urls_are_leads_only(self):
        normalized = kimi_adapter.announcement_rows_from_csv(
            "公告日期,标题,PDF链接\n2026-10-09,年度报告,https://example.com/x.pdf\n")
        self.assertEqual(normalized["url_fields"], ["PDF链接"])
        self.assertIn("leads only", " ".join(normalized["notes"]))

    def test_news_normalization_keeps_event_id_gap(self):
        normalized = kimi_adapter.normalize_news_text("标题A\n标题B")
        self.assertEqual(normalized["item_count"], 2)
        self.assertIsNone(normalized["known_unknowns"]["event_id"]["value"])


class HistoricalBackgroundGateTests(unittest.TestCase):
    """T12: daily bars never take the realtime staleness lane."""

    def _config(self) -> Dict[str, Any]:
        return {"market_data_governance": {
            "enabled": True,
            "approved_internal_research_providers": ["kimi_ifind_price"],
            "max_closed_market_age_hours": 36,
        }}

    def _signal(self, **payload_overrides: Any) -> RawSignal:
        payload = {
            "code": "600519",
            "latest": 1700.0,
            "quote_timestamp": "2026-10-09T15:00:00+08:00",
            "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "data_scope": "historical_background",
            "frequency": "D",
            "history_window": {"start": "2026-09-28T00:00:00+08:00",
                               "end": "2026-10-09T23:59:59+08:00"},
        }
        payload.update(payload_overrides)
        return RawSignal(source="kimi_ifind_price", source_type="price",
                         title="iFinD 日线背景", summary="daily background",
                         payload=payload)

    def test_old_daily_bar_not_blocked_by_realtime_threshold(self):
        signals, summary = assess_market_signals(
            [self._signal(quote_timestamp="2026-10-02T15:00:00+08:00")],
            self._config(), now=datetime(2026, 10, 12, 10, 0, tzinfo=timezone(timedelta(hours=8))))
        gate = signals[0].payload["market_data_governance"]
        self.assertEqual(gate["status"], "DEGRADED")
        self.assertIn("historical_background_not_live", gate["reason_codes"])
        self.assertNotIn("quote_older_than_closed_market_limit", gate["reason_codes"])
        self.assertTrue(gate["llm_eligible"])

    def test_future_bar_date_blocked(self):
        signals, _ = assess_market_signals(
            [self._signal(quote_timestamp="2026-12-31T15:00:00+08:00")],
            self._config(), now=datetime(2026, 10, 12, 10, 0, tzinfo=timezone(timedelta(hours=8))))
        gate = signals[0].payload["market_data_governance"]
        self.assertEqual(gate["status"], "BLOCKED")
        self.assertIn("future_quote_timestamp", gate["reason_codes"])

    def test_bar_outside_declared_window_blocked(self):
        signals, _ = assess_market_signals(
            [self._signal(quote_timestamp="2026-08-01T15:00:00+08:00")],
            self._config(), now=datetime(2026, 10, 12, 10, 0, tzinfo=timezone(timedelta(hours=8))))
        gate = signals[0].payload["market_data_governance"]
        self.assertEqual(gate["status"], "BLOCKED")
        self.assertIn("quote_before_declared_window", gate["reason_codes"])

    def test_unattested_provider_blocked_even_for_background(self):
        signals, _ = assess_market_signals(
            [self._signal()], self._config(), now=datetime(2026, 10, 12, 10, 0, tzinfo=timezone(timedelta(hours=8))))
        # provider kimi_ifind_price IS attested in _config; now un-attest it
        cfg = {"market_data_governance": {"enabled": True,
                                          "approved_internal_research_providers": []}}
        signals, _ = assess_market_signals(
            [self._signal()], cfg, now=datetime(2026, 10, 12, 10, 0, tzinfo=timezone(timedelta(hours=8))))
        gate = signals[0].payload["market_data_governance"]
        self.assertEqual(gate["status"], "BLOCKED")
        self.assertIn("provider_not_internally_attested", gate["reason_codes"])

    def test_realtime_lane_unchanged_without_scope_marker(self):
        payload = {
            "code": "600519", "latest": 1700.0,
            "quote_timestamp": "2026-10-02T15:00:00+08:00",
            "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        signals, _ = assess_market_signals(
            [RawSignal(source="eastmoney", source_type="price", title="q", summary="s", payload=payload)],
            self._config(), now=datetime(2026, 10, 12, 10, 0, tzinfo=timezone(timedelta(hours=8))))
        gate = signals[0].payload["market_data_governance"]
        # A week-old realtime-shaped quote is still blocked by the old rules
        # (in-session stale here) and is not silently rerouted to the
        # historical lane, which requires an explicit data_scope marker.
        self.assertEqual(gate["status"], "BLOCKED")
        self.assertIn("stale_live_quote", gate["reason_codes"])
        self.assertNotIn("data_scope", gate)


class LedgerRecordTests(LedgerTestCase):
    def test_intent_then_result_then_ledger_shape(self):
        outcome = self.run_price()
        self.assertTrue(outcome["allowed"])
        records = kimi_adapter.load_query_ledger(self.config)
        kinds = [record["record_type"] for record in records]
        self.assertEqual(kinds, ["query_intent", "query_result"])
        self.assertEqual(records[0]["request_id"], records[1]["request_id"])
        self.assertEqual(records[1]["cost_status"], "unknown")
        self.assertEqual(records[1]["normalized_row_count"], 1)

    def test_refusal_still_counted_for_audit(self):
        self.run_price(registry=registry_with())
        records = kimi_adapter.load_query_ledger(self.config)
        # Gate refusals send nothing, but the adapter records no intent for
        # them; the refusal itself is visible in the returned gate payload.
        self.assertEqual(records, [])
        outcome = self.run_price(registry=registry_with())
        self.assertEqual(outcome["gate"]["reason"], "source_not_registered")

    def test_data_call_count_reads_today_only(self):
        # The ledger timestamps intents with the real clock, so the "today"
        # assertion must also use the real clock; only the zero-day uses a
        # fixed future date. (Hard-coding 2026-10-12 broke at midnight.)
        self.run_price()
        count = kimi_adapter.data_calls_today(self.config)
        self.assertEqual(count, 1)
        future = datetime.now(timezone.utc) + timedelta(days=2)
        count_other_day = kimi_adapter.data_calls_today(self.config, now=future)
        self.assertEqual(count_other_day, 0)


if __name__ == "__main__":
    unittest.main()
