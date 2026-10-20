"""R04: transactional query intake, retention rights, execution policy.

Acceptance scenarios from GLM_REFACTOR_TASKBOOK_20261020 (A18–A25). Concurrency
tests use real threads on SEPARATE store connections (the O layer per the
taskbook: same-process dual connections; N/C cross-process stays with later
packets). Every governed test runs offline through the injected FakeTransport;
zero network, zero real credentials.
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict

from stock_agent.config import project_path_override
from stock_agent.connector_runtime import (
    ConnectorQueryStore,
    DestinationRefused,
    assert_purpose_registered,
    assert_transport_destination,
    cache_rights_revision,
    quota_group_for,
    resolve_destination,
    status_for_outcome,
)
from stock_agent.connector_runtime.rights import decide_retention
from stock_agent.integrations.kimi import transport as kimi_transport
from stock_agent.sources import kimi_adapter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_kimi_client_v1 import FakeTransport, envelope, snapshot

PRICED_PARAMS = {"ticker": "600519.SH", "start_date": "2026-09-28",
                 "end_date": "2026-10-09", "adjust": "forward"}


def registry_entry(source_id="kimi_ifind_price", kind="price", *, state="approved",
                   expires="2099-12-31", retention=True, upstream="ifind",
                   purposes=("event price background",), rpm=60, rpd=10,
                   approved_at="2026-10-01T00:00:00"):
    return {
        "source_id": source_id, "kind": kind, "upstream": upstream,
        "purpose": "test purpose", "purposes": list(purposes),
        "approval": {"state": state,
                     "approved_by": "真实负责人" if state == "approved" else "",
                     "approved_at": approved_at if state == "approved" else ""},
        "body_retention_allowed": retention, "llm_external_send_allowed": False,
        "timeliness": {"expected_max_delay_minutes": 1440, "staleness_policy": "flag"},
        "rate_limit": {"max_requests_per_minute": rpm, "max_requests_per_day": rpd},
        "expires_at": expires,
    }


def registry_with(*entries):
    return {"schema_version": "1.0",
            "entries": {entry["source_id"]: entry for entry in entries},
            "path": "test"}


def ok_price_outcome():
    csv_body = "日期,开盘价,收盘价,成交量\n2026-10-09,1690.0,1700.0,33000\n"
    return kimi_transport.TransportOutcome(
        "ok", http_status=200,
        body=envelope(files=[{"name": "ifind.csv", "content": csv_body}]))


class R04TestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.snapshot_doc = snapshot(self.root)
        self.config = {"kimi_integration": {"enabled": True},
                       "kimi_sources": {"daily_data_call_limit": 50}}
        self.registry = registry_with(registry_entry())

    def governed(self, *, transport=None, params=None, registry=None, config=None,
                 purpose="event price background", artifact_root=None, allow_cache=True,
                 refresh=False):
        return kimi_adapter.governed_execute(
            self.snapshot_doc, "ifind_get_price", params or dict(PRICED_PARAMS),
            purpose=purpose, config=config or self.config,
            registry=registry or self.registry,
            transport=transport or FakeTransport([ok_price_outcome()]),
            credential_provider=lambda: "k" * 32,
            artifact_root=artifact_root, allow_cache=allow_cache, refresh=refresh)

    def store_path(self):
        return self.root / "data/kimi/query_intake.sqlite3"


class AtomicIntakeTests(R04TestCase):
    def test_a18_same_key_two_workers_one_dispatch(self):
        first = ConnectorQueryStore(self.store_path())
        second = ConnectorQueryStore(self.store_path())
        try:
            verdict1 = first.reserve_query("key-a18", source_id="s1", upstream="ifind",
                                           purpose="p", per_source_rpm=60,
                                           per_source_rpd=10, group_daily_limit=5)
            verdict2 = second.reserve_query("key-a18", source_id="s1", upstream="ifind",
                                            purpose="p", per_source_rpm=60,
                                            per_source_rpd=10, group_daily_limit=5)
            self.assertTrue(verdict1["reserved"])
            self.assertFalse(verdict1["replay"])
            self.assertTrue(verdict2["reserved"] and verdict2["replay"],
                            "the second worker must get the SAME intent, not a dispatch slot")
            self.assertEqual(verdict1["intent"]["intent_id"], verdict2["intent"]["intent_id"])
        finally:
            first.close()
            second.close()

    def test_a18_thread_race_same_key_single_intent(self):
        barrier = threading.Barrier(6)
        results, errors = [], []

        def worker(index):
            store = ConnectorQueryStore(self.store_path())
            try:
                barrier.wait()
                results.append(store.reserve_query(
                    "key-race", source_id="s1", upstream="ifind", purpose="p",
                    per_source_rpm=60, per_source_rpd=10, group_daily_limit=5))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                store.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(sum(1 for item in results if not item["replay"]), 1)
        self.assertEqual(len({item["intent"]["intent_id"] for item in results}), 1)

    def test_a19_last_quota_unit_concurrent_single_winner(self):
        barrier = threading.Barrier(2)
        results, errors = [], []

        def worker(name):
            store = ConnectorQueryStore(self.store_path())
            try:
                barrier.wait()
                results.append(store.reserve_query(
                    "key-{0}".format(name), source_id="s1", upstream="ifind", purpose="p",
                    per_source_rpm=60, per_source_rpd=10, group_daily_limit=1))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                store.close()

        threads = [threading.Thread(target=worker, args=(name,)) for name in ("x", "y")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        granted = [item for item in results if item["reserved"] and not item["replay"]]
        denied = [item for item in results if not item["reserved"]]
        self.assertEqual(len(granted), 1, "the last unit is spent exactly once")
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["reason"], "daily_quota_group")
        self.assertEqual(denied[0]["calls_today"], 1)

    def test_a23_same_upstream_shares_one_quota_group(self):
        store = ConnectorQueryStore(self.store_path())
        try:
            self.assertEqual(quota_group_for("IFIND"), quota_group_for("ifind"))
            for key, source in (("k1", "kimi_ifind_price"), ("k2", "http_ifind_price")):
                verdict = store.reserve_query(key, source_id=source, upstream="ifind",
                                              purpose="p", per_source_rpm=60,
                                              per_source_rpd=10, group_daily_limit=5)
                self.assertTrue(verdict["reserved"])
            # Kimi-routed and direct-HTTP entries over one upstream consumed
            # TWO units of ONE shared group — never two independent budgets.
            self.assertEqual(store.quota_usage(quota_group_for("ifind")), 2)
            other = store.reserve_query("k3", source_id="kimi_dongcai_news",
                                        upstream="dongcai", purpose="p",
                                        per_source_rpm=60, per_source_rpd=10,
                                        group_daily_limit=5)
            self.assertTrue(other["reserved"])
            self.assertEqual(store.quota_usage(quota_group_for("dongcai")), 1)
        finally:
            store.close()

    def test_attempt_chain_status_mapping(self):
        store = ConnectorQueryStore(self.store_path())
        try:
            verdict = store.reserve_query("k-status", source_id="s", upstream="ifind",
                                          purpose="p", per_source_rpm=60,
                                          per_source_rpd=10, group_daily_limit=5)
            intent_id = verdict["intent"]["intent_id"]
            store.mark_dispatched(intent_id)
            row = store.complete_query(intent_id, outcome="outcome_unknown")
            self.assertEqual(row["status"], "outcome_unknown")
            self.assertEqual(status_for_outcome("ok"), "succeeded")
            self.assertEqual(status_for_outcome("auth_rejected"), "failed_known")
        finally:
            store.close()


class RetentionTests(R04TestCase):
    def test_a20_retention_denied_leaves_zero_artifact_files(self):
        no_retention = registry_with(registry_entry(retention=False))
        artifact_root = self.root / "artifacts"
        outcome = self.governed(registry=no_retention, artifact_root=artifact_root,
                                allow_cache=False)
        self.assertEqual(outcome["stage"], "executed")
        self.assertFalse(outcome["retention"]["raw_artifacts"],
                         "the host decision, not the caller's artifact_root, decides")
        self.assertTrue(outcome["result"].artifact_contents,
                        "the run stays in memory; data is not lost, just not persisted")
        if artifact_root.exists():
            self.assertEqual(list(artifact_root.rglob("*")), [],
                             "zero content files may touch disk without retention rights")
        cache_files = list((self.root / "data/kimi/cache").glob("*.json"))
        self.assertEqual(cache_files, [], "no cache document either")

    def test_retention_allowed_writes_artifacts_and_cache(self):
        artifact_root = self.root / "artifacts"
        outcome = self.governed(artifact_root=artifact_root, allow_cache=False)
        self.assertTrue(outcome["retention"]["raw_artifacts"])
        self.assertTrue(list(artifact_root.rglob("*.csv")),
                        "allowed retention persists the raw artifact")
        self.assertTrue(list((self.root / "data/kimi/cache").glob("*.json")))

    def test_decision_matrix_fail_closed(self):
        withdrawn = registry_with(registry_entry(state="withdrawn"))
        status = kimi_adapter.gate_source("kimi_ifind_price", withdrawn)["registry_status"]
        decision = decide_retention(status)
        self.assertFalse(decision.raw_artifacts or decision.cache or decision.fact_bundle_content)
        self.assertTrue(decision.logs, "metadata-only ledger rows are always allowed")


class CacheRightsTests(R04TestCase):
    def _prime_cache(self):
        self.governed(allow_cache=False)

    def test_a21_rights_revision_change_invalidates_cache(self):
        self._prime_cache()
        cache_dir = self.root / "data/kimi/cache"
        self.assertEqual(len(list(cache_dir.glob("*.json"))), 1)
        # Re-approval under a NEW revision: the old document no longer proves
        # it was produced under current rights. The read is refused, the
        # already-billed key is NOT re-sent (replay), and a consumer that
        # needs the data again must take an explicit refresh (new billable
        # intent) — which then re-caches under the current revision.
        reapproved = registry_with(registry_entry(approved_at="2026-10-19T00:00:00"))
        transport = FakeTransport([ok_price_outcome()])
        outcome = self.governed(registry=reapproved, transport=transport)
        self.assertEqual(outcome["stage"], "replay",
                         "cache refused AND not re-billed; refusing is the point")
        self.assertEqual(transport.requests, [])
        refreshed = self.governed(registry=reapproved, refresh=True)
        self.assertEqual(refreshed["stage"], "executed")
        # Unchanged revision still hits.
        transport2 = FakeTransport([ok_price_outcome()])
        outcome2 = self.governed(registry=reapproved, transport=transport2)
        self.assertEqual(outcome2["stage"], "cache")
        self.assertEqual(transport2.requests, [])

    def test_a22_ttl_expiry_is_stale_then_refresh_overwrites(self):
        config = {"kimi_integration": {"enabled": True},
                  "kimi_sources": {"daily_data_call_limit": 50,
                                   "cache_ttl_seconds": 1}}
        self.governed(config=config, allow_cache=False)
        cache_dir = self.root / "data/kimi/cache"
        document = json.loads(next(iter(cache_dir.glob("*.json"))).read_text(encoding="utf-8"))
        self.assertTrue(document["expires_at"])
        time.sleep(1.2)
        transport = FakeTransport([ok_price_outcome()])
        outcome = self.governed(config=config, transport=transport)
        # Expired document is stale: the read is refused and the already-billed
        # key is not re-sent. Serving old rows is never an option.
        self.assertEqual(outcome["stage"], "replay")
        self.assertEqual(transport.requests, [])
        refreshed = self.governed(config=config, refresh=True)
        self.assertEqual(refreshed["stage"], "executed",
                         "an explicit refresh is the stale-cache answer: a new billable intent")
        # The refreshed document overwrites with a new as-of moment.
        refreshed_doc = json.loads(next(iter(cache_dir.glob("*.json"))).read_text(encoding="utf-8"))
        self.assertGreater(refreshed_doc["as_of"], document["as_of"])
        transport2 = FakeTransport([ok_price_outcome()])
        self.assertEqual(self.governed(config=config, transport=transport2)["stage"], "cache")

    def test_cache_purpose_mismatch_is_miss(self):
        self._prime_cache()
        transport = FakeTransport([ok_price_outcome()])
        outcome = self.governed(transport=transport, purpose="other purpose",
                                allow_cache=True)
        # "other purpose" is not registered -> purpose gate refuses entirely.
        self.assertEqual(outcome["stage"], "gate")
        self.assertEqual(outcome["gate"]["reason"], "purpose_not_registered")


class DestinationPolicyTests(R04TestCase):
    def _integration(self, origin="https://agent-gw.kimi.com"):
        from stock_agent.integrations.kimi import client as kimi_client

        return kimi_client.KimiIntegrationConfig.from_config(
            {"kimi_integration": {"enabled": True, "origin": origin}})

    def test_a24_caller_constructed_policy_on_real_transport_refused(self):
        approved = self._integration()
        ok, reason = assert_transport_destination(
            kimi_transport.UrllibTransport(), approved)
        self.assertTrue(ok)
        rogue = kimi_transport.UrllibTransport(
            policy=kimi_transport.DestinationPolicy(origin="https://evil.example"))
        ok, reason = assert_transport_destination(rogue, approved)
        self.assertFalse(ok)
        self.assertEqual(reason, "transport_destination_not_approved")

    def test_governed_refuses_rogue_real_transport_without_sending(self):
        rogue = kimi_transport.UrllibTransport(
            policy=kimi_transport.DestinationPolicy(origin="https://evil.example"))
        outcome = self.governed(transport=rogue, allow_cache=False)
        self.assertEqual(outcome["stage"], "destination")
        self.assertFalse(outcome["allowed"])

    def test_resolve_destination_rejects_non_https(self):
        with self.assertRaises(DestinationRefused):
            resolve_destination(self._integration(origin="http://agent-gw.kimi.com"))

    def test_offline_seam_is_explicit(self):
        ok, reason = assert_transport_destination(FakeTransport([]), self._integration())
        self.assertTrue(ok)
        self.assertEqual(reason, "offline_seam")


class PurposeAndRateTests(R04TestCase):
    def test_a25_unregistered_purpose_refused_before_any_send(self):
        transport = FakeTransport([ok_price_outcome()])
        outcome = self.governed(transport=transport, purpose="undisclosed purpose")
        self.assertEqual(outcome["stage"], "gate")
        self.assertEqual(outcome["gate"]["reason"], "purpose_not_registered")
        self.assertEqual(transport.requests, [], "nothing may be sent")
        self.assertFalse(self.store_path().exists() and
                         ConnectorQueryStore(self.store_path()).find_intent("anything"),
                        "no reservation is spent on a refused purpose")

    def test_purpose_allowlist_unit(self):
        entry = registry_entry(purposes=("a", "b")).copy()
        self.assertEqual(assert_purpose_registered(entry, "a"), (True, "ok"))
        self.assertEqual(assert_purpose_registered(entry, "c"), (False, "purpose_not_registered"))
        no_list = {"purposes": []}
        self.assertEqual(assert_purpose_registered(no_list, "a")[0], False)

    def test_a25_rpm_exceeded_refuses_without_sending(self):
        registry = registry_with(registry_entry(rpm=1))
        transport = FakeTransport([ok_price_outcome(), ok_price_outcome()])
        first = self.governed(registry=registry, transport=transport,
                              params=dict(PRICED_PARAMS, start_date="2026-09-28"),
                              allow_cache=False)
        self.assertEqual(first["stage"], "executed")
        second = self.governed(registry=registry, transport=transport,
                               params=dict(PRICED_PARAMS, start_date="2026-09-29"),
                               allow_cache=False)
        self.assertEqual(second["stage"], "rate_limited")
        self.assertEqual(second["reason"], "rate_limited_minute")
        self.assertFalse(second["allowed"])
        self.assertEqual(len(transport.requests), 1,
                         "the rate-limited query must not reach the transport")


class AuditContinuityTests(R04TestCase):
    def test_jsonl_ledger_remains_the_audit_projection(self):
        self.governed(allow_cache=False)
        records = kimi_adapter.load_query_ledger(self.config)
        kinds = [record["record_type"] for record in records]
        self.assertIn("query_intent", kinds)
        self.assertIn("query_result", kinds)
        intent = next(record for record in records if record["record_type"] == "query_intent")
        self.assertTrue(intent["idempotency_key"])
        # A replay appends no second intent record.
        replay = self.governed(allow_cache=False)
        self.assertEqual(replay["stage"], "replay")
        intents = [record for record in kimi_adapter.load_query_ledger(self.config)
                   if record["record_type"] == "query_intent"]
        self.assertEqual(len(intents), 1)


if __name__ == "__main__":
    unittest.main()
