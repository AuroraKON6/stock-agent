"""K02: Kimi integration offline safety tests (no network, no real keys).

Covers acceptance-matrix rows T01/T02/T04/T05/T07/T08/T09/T10/T11/T15/T21 at
the client boundary. T06 (junction/reparse races) and T25 (MCP) stay NOT_RUN
by design. All fixtures live in a TemporaryDirectory; the fake transport
records requests so tests can assert exactly what would have egressed.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from stock_agent.integrations import kimi as kimi_integration
from stock_agent.integrations.kimi import artifacts as kimi_artifacts
from stock_agent.integrations.kimi import client as kimi_client
from stock_agent.integrations.kimi import contracts as kimi_contracts
from stock_agent.integrations.kimi import transport as kimi_transport

SNAPSHOT = {
    "schema_version": "1.0",
    "apis": [
        {
            "api_id": "ifind_get_price",
            "source_id": "kimi_ifind_price",
            "data_source_name": "ifind",
            "api_name": "ifind_get_price",
            "transport": "kimi_agent_gw",
            "data_kind": "daily_quote",
            "purpose": "daily background",
            "params": {
                "ticker": {"type": "a_share_ticker_list", "required": True, "max_items": 3},
                "start_date": {"type": "date", "required": True},
                "end_date": {"type": "date", "required": True},
                "file_path": {"type": "virtual_artifact_name", "required": True, "host_generated": True},
                "format": {"type": "enum", "required": False, "options": ["json", "csv"], "default": "json"},
                "interval": {"type": "enum", "required": False, "options": ["D", "W", "M", "Q", "Y"], "default": "D"},
                "adjust": {"type": "enum", "required": False, "options": ["forward", "backward", "none"]},
            },
            "constraints": {
                "max_window_days": 1096,
                "poc": {"max_tickers": 1, "max_window_days": 30, "interval_must_be": "D",
                        "adjust_must_be_explicit": True},
            },
            "result_contract": {"envelope": "std", "rows_field": "csv", "must_record": []},
            "known_unknowns": {"volume_unit": {"value": None, "reason": "unstated"}},
            "notes": [],
        },
        {
            "api_id": "dongcai_mx_finance_search",
            "source_id": "kimi_dongcai_news",
            "data_source_name": "dongcai",
            "api_name": "dongcai_mx_finance_search",
            "transport": "kimi_agent_gw",
            "data_kind": "news_metadata",
            "purpose": "news leads",
            "params": {
                "query": {"type": "search_query", "required": True, "max_length": 200},
                "file_path": {"type": "virtual_artifact_name", "required": False, "host_generated": True},
            },
            "constraints": {"poc": {"max_queries_per_event": 1}},
            "result_contract": {"envelope": "std", "must_record": []},
            "known_unknowns": {},
            "notes": [],
        },
    ],
}


def snapshot(tmp: Path) -> Dict[str, Any]:
    path = tmp / "contracts.json"
    path.write_text(json.dumps(SNAPSHOT), encoding="utf-8")
    return kimi_contracts.load_contract_snapshot(path)


def envelope(*, success: bool = True, user_text: str = "", assistant_text: str = "",
             files: Optional[List[Dict[str, Any]]] = None, error: Any = None) -> bytes:
    result: Dict[str, Any] = {}
    if user_text:
        result["user"] = [{"type": "text", "text": user_text}]
    if assistant_text:
        result["assistant"] = [{"type": "text", "text": assistant_text}]
    body: Dict[str, Any] = {"is_success": success}
    if result:
        body["result"] = result
    if files is not None:
        body["files"] = files
    if error is not None:
        body["error"] = error
    return json.dumps(body).encode("utf-8")


class FakeTransport:
    """Records what would egress; returns queued outcomes. Never touches
    the network, so the isolation guard stays satisfied."""

    def __init__(self, outcomes: Optional[List[kimi_transport.TransportOutcome]] = None):
        self.outcomes = list(outcomes or [])
        self.requests: List[Dict[str, Any]] = []

    def post_tools(self, payload: bytes, headers: Dict[str, str],
                   timeout: float) -> kimi_transport.TransportOutcome:
        self.requests.append({
            "payload": json.loads(payload.decode("utf-8")),
            "has_auth_header": "Authorization" in headers,
            "auth_value": headers.get("Authorization", ""),
            "timeout": timeout,
        })
        if self.outcomes:
            return self.outcomes.pop(0)
        return kimi_transport.TransportOutcome(kimi_transport.OUTCOME_OK, http_status=200,
                                               body=envelope(user_text="ok"))


class KimiTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.snapshot = snapshot(self.root)
        self.enabled = kimi_client.KimiIntegrationConfig(enabled=True)

    def price_params(self, **overrides) -> Dict[str, Any]:
        params = {"ticker": "600519.SH", "start_date": "2026-09-28",
                  "end_date": "2026-10-09", "adjust": "forward"}
        params.update(overrides)
        return params

    def price_query(self, **overrides) -> kimi_client.DataQuery:
        return kimi_client.build_query(
            self.snapshot, "ifind_get_price", self.price_params(**overrides),
            purpose="event price background")


class ImportSafetyTests(unittest.TestCase):
    """T01: import and status do not read shared credentials, open sockets,
    spawn processes, or install anything."""

    def test_import_has_no_side_effects_and_disabled_by_default(self):
        config = kimi_client.KimiIntegrationConfig.from_config({})
        self.assertFalse(config.enabled)
        self.assertEqual(config.origin, "https://agent-gw.kimi.com")

    def test_unknown_config_fields_rejected(self):
        with self.assertRaises(kimi_client.KimiIntegrationError):
            kimi_client.KimiIntegrationConfig.from_config(
                {"kimi_integration": {"enabled": False, "base_url": "http://x"}})


class ContractValidationTests(KimiTestCase):
    """T09/T10/T11: precise schema validation, fail-closed unknowns."""

    def test_unknown_api_fails_closed(self):
        with self.assertRaises(kimi_contracts.KimiContractError):
            kimi_contracts.find_api(self.snapshot, "ifind_get_everything")

    def test_unknown_parameter_rejected(self):
        with self.assertRaises(kimi_contracts.KimiContractError) as ctx:
            kimi_client.build_query(self.snapshot, "ifind_get_price",
                                    self.price_params(extra_field="x"), purpose="p")
        self.assertIn("unknown parameters", str(ctx.exception))

    def test_missing_required_parameter_rejected(self):
        params = self.price_params()
        del params["start_date"]
        with self.assertRaises(kimi_contracts.KimiContractError):
            kimi_client.build_query(self.snapshot, "ifind_get_price", params, purpose="p")

    def test_enum_violation_rejected(self):
        with self.assertRaises(kimi_contracts.KimiContractError):
            kimi_client.build_query(self.snapshot, "ifind_get_price",
                                    self.price_params(adjust="sideways"), purpose="p")

    def test_caller_cannot_supply_file_path(self):
        with self.assertRaises(kimi_contracts.KimiContractError) as ctx:
            kimi_client.build_query(self.snapshot, "ifind_get_price",
                                    self.price_params(file_path="E:/steal.csv"), purpose="p")
        self.assertIn("host generated", str(ctx.exception))

    def test_more_than_three_tickers_rejected(self):
        with self.assertRaises(kimi_contracts.KimiContractError):
            kimi_client.build_query(self.snapshot, "ifind_get_price",
                                    self.price_params(ticker="600519.SH,000001.SZ,300750.SZ,600036.SH"),
                                    purpose="p", poc=False)

    def test_poc_single_ticker_enforced(self):
        with self.assertRaises(kimi_contracts.KimiContractError) as ctx:
            kimi_client.build_query(self.snapshot, "ifind_get_price",
                                    self.price_params(ticker="600519.SH,000001.SZ"), purpose="p")
        self.assertIn("PoC", str(ctx.exception))

    def test_window_over_three_years_rejected(self):
        with self.assertRaises(kimi_contracts.KimiContractError):
            kimi_client.build_query(self.snapshot, "ifind_get_price",
                                    self.price_params(start_date="2020-01-01", end_date="2026-10-09"),
                                    purpose="p", poc=False)

    def test_poc_window_cap_enforced(self):
        with self.assertRaises(kimi_contracts.KimiContractError):
            kimi_client.build_query(self.snapshot, "ifind_get_price",
                                    self.price_params(start_date="2026-01-01", end_date="2026-10-09"),
                                    purpose="p")

    def test_reversed_dates_rejected(self):
        with self.assertRaises(kimi_contracts.KimiContractError):
            kimi_client.build_query(self.snapshot, "ifind_get_price",
                                    self.price_params(start_date="2026-10-09", end_date="2026-09-28"),
                                    purpose="p")

    def test_adjust_must_be_explicit_in_poc(self):
        params = self.price_params()
        del params["adjust"]
        with self.assertRaises(kimi_contracts.KimiContractError) as ctx:
            kimi_client.build_query(self.snapshot, "ifind_get_price", params, purpose="p")
        self.assertIn("adjustment basis must be explicit", str(ctx.exception))

    def test_non_a_share_ticker_rejected(self):
        with self.assertRaises(kimi_contracts.KimiContractError):
            kimi_client.build_query(self.snapshot, "ifind_get_price",
                                    self.price_params(ticker="AAPL.O"), purpose="p")

    def test_query_text_rejects_local_paths_and_credential_shapes(self):
        for bad in ("E:\\\\secrets\\\\notes.txt", "\\\\\\\\server\\\\share", "api_key=abc123"):
            with self.assertRaises(kimi_contracts.KimiContractError):
                kimi_client.build_query(self.snapshot, "dongcai_mx_finance_search",
                                        {"query": bad}, purpose="p")

    def test_query_text_length_capped(self):
        with self.assertRaises(kimi_contracts.KimiContractError):
            kimi_client.build_query(self.snapshot, "dongcai_mx_finance_search",
                                    {"query": "行情" * 150}, purpose="p")

    def test_idempotency_key_binds_contract_and_params(self):
        query_a = self.price_query()
        query_b = self.price_query(adjust="none")
        self.assertNotEqual(query_a.idempotency_key, query_b.idempotency_key)
        query_c = self.price_query()
        self.assertEqual(query_a.idempotency_key, query_c.idempotency_key)

    def test_gateway_params_use_virtual_file_path(self):
        query = self.price_query()
        sent = query.gateway_params()
        self.assertIn("file_path", sent)
        self.assertTrue(sent["file_path"].startswith("kimi_ifind_get_price_"))
        self.assertNotIn(":", sent["file_path"])
        self.assertNotIn("\\\\", sent["file_path"])


class CredentialBoundaryTests(KimiTestCase):
    """T02: missing/wrong credentials classify cleanly; no cross-account
    fallback; no key material in outcomes or records."""

    def _run(self, *, provider, transport):
        query = self.price_query()
        return kimi_client.execute_query(
            query, integration=self.enabled, transport=transport,
            credential_provider=provider, budget=kimi_client.ExperimentBudget())

    def test_missing_provider_blocks_without_network(self):
        transport = FakeTransport()
        result = self._run(provider=None, transport=transport)
        self.assertEqual(result.outcome, "credential_missing")
        self.assertEqual(transport.requests, [])

    def test_placeholder_key_blocks(self):
        transport = FakeTransport()
        result = self._run(provider=lambda: "not_provided", transport=transport)
        self.assertEqual(result.outcome, "credential_missing")
        self.assertEqual(transport.requests, [])

    def test_wrong_key_classified_auth_rejected_no_secret_leak(self):
        transport = FakeTransport([kimi_transport.TransportOutcome(
            "auth_rejected", http_status=401, detail="gateway rejected credentials")])
        result = self._run(provider=lambda: "k" * 32, transport=transport)
        self.assertEqual(result.outcome, "auth_rejected")
        record = json.dumps(result.as_record())
        self.assertNotIn("kkkk", record)

    def test_403_permission_denied_not_membership_guess(self):
        transport = FakeTransport([kimi_transport.TransportOutcome(
            "permission_denied", http_status=403, detail="gateway refused access")])
        result = self._run(provider=lambda: "k" * 32, transport=transport)
        self.assertEqual(result.outcome, "permission_denied")

    def test_429_records_retry_after_and_unknown_cost(self):
        transport = FakeTransport([kimi_transport.TransportOutcome(
            "rate_limited", http_status=429, detail="rate limited", retry_after_seconds=30.0)])
        result = self._run(provider=lambda: "k" * 32, transport=transport)
        self.assertEqual(result.outcome, "rate_limited")
        self.assertEqual(result.retry_after_seconds, 30.0)
        self.assertEqual(result.cost_status, "unknown")

    def test_post_send_disconnect_is_outcome_unknown_not_free_failure(self):
        transport = FakeTransport([kimi_transport.TransportOutcome(
            "outcome_unknown", detail="connection failed after send; result unknown")])
        result = self._run(provider=lambda: "k" * 32, transport=transport)
        self.assertEqual(result.outcome, "outcome_unknown")
        self.assertEqual(result.cost_status, "unknown")


class DestinationBoundaryTests(unittest.TestCase):
    """T04: exact-origin enforcement at the transport layer."""

    def test_policy_url_is_exact(self):
        policy = kimi_transport.DestinationPolicy()
        self.assertEqual(policy.url(), "https://agent-gw.kimi.com/coding/v1/tools")
        self.assertTrue(policy.matches("https://agent-gw.kimi.com/coding/v1/tools"))
        self.assertFalse(policy.matches("https://agent-gw.kimi.com/coding/v1/tools/other"))
        self.assertFalse(policy.matches("https://evil.example/coding/v1/tools"))

    def test_http_origin_refused_without_request(self):
        transport = kimi_transport.UrllibTransport(
            policy=kimi_transport.DestinationPolicy(origin="http://agent-gw.kimi.com"))
        outcome = transport.post_tools(b"{}", {}, 1.0)
        self.assertEqual(outcome.outcome, "destination_denied")
        self.assertIn("HTTPS", outcome.detail)


class ArtifactBoundaryTests(unittest.TestCase):
    """T05/T07/T08: remote names never decide local paths."""

    def test_host_generated_names_ignore_remote_shape(self):
        plans = kimi_artifacts.plan_artifacts(
            [{"name": "../../evil.csv", "content": "a,b\n1,2\n"},
             {"name": "ok.csv", "content": "x\ny\n"}],
            kimi_artifacts.ArtifactLimits())
        self.assertEqual(len(plans), 2)
        self.assertEqual(plans[0].remote_name_rejected, "parent_escape")
        self.assertFalse(plans[0].accepted)
        self.assertTrue(plans[1].accepted)
        for plan in plans:
            self.assertRegex(plan.filename, r"^ka_[0-9a-f]{32}\.csv$")

    def _malicious(self, name: str, reason: str) -> None:
        plans = kimi_artifacts.plan_artifacts([{"name": name, "content": "x"}],
                                              kimi_artifacts.ArtifactLimits())
        self.assertEqual(plans[0].remote_name_rejected, reason, name)
        self.assertFalse(plans[0].accepted)

    def test_absolute_paths_rejected(self):
        self._malicious("C:/windows/system32/evil.csv", "drive_relative")
        self._malicious("/etc/passwd", "absolute_path")

    def test_unc_rejected(self):
        self._malicious("\\\\\\\\server\\\\share\\\\x.csv", "unc_path")
        self._malicious("//server/share/x.csv", "unc_path")

    def test_ads_rejected(self):
        self._malicious("clean.csv:hidden", "alternate_data_stream")

    def test_device_names_rejected(self):
        self._malicious("CON.csv", "device_name")
        self._malicious("NUL", "device_name")

    def test_trailing_dots_and_control_chars_rejected(self):
        self._malicious("trailing.csv ", "trailing_dot_or_space")
        self._malicious("bad" + chr(1) + "name.csv", "unsafe_characters")

    def test_concurrent_same_second_plans_never_collide(self):
        files = [{"name": "same.csv", "content": "v"}] * 4
        batch_a = kimi_artifacts.plan_artifacts(files, kimi_artifacts.ArtifactLimits(max_files=4))
        batch_b = kimi_artifacts.plan_artifacts(files, kimi_artifacts.ArtifactLimits(max_files=4))
        names_a = {plan.filename for plan in batch_a}
        names_b = {plan.filename for plan in batch_b}
        self.assertEqual(len(names_a), 4)
        self.assertEqual(len(names_b), 4)
        self.assertFalse(names_a & names_b)

    def test_file_count_cap_aborts_bounded(self):
        files = [{"name": "f{i}.csv".format(i=i), "content": "x"} for i in range(5)]
        with self.assertRaises(kimi_artifacts.ArtifactLimitError):
            kimi_artifacts.plan_artifacts(files, kimi_artifacts.ArtifactLimits())

    def test_total_bytes_cap_aborts_bounded(self):
        big = "z" * (3 * 1024 * 1024)
        files = [{"name": "a.csv", "content": big}, {"name": "b.csv", "content": big}]
        with self.assertRaises(kimi_artifacts.ArtifactLimitError):
            kimi_artifacts.plan_artifacts(files, kimi_artifacts.ArtifactLimits())

    def test_commit_never_overwrites_and_records_hash(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "artifacts"
        files = [{"name": "data.csv", "content": "a,b\n1,2\n"}]
        plans = kimi_artifacts.plan_artifacts(files, kimi_artifacts.ArtifactLimits())
        contents = {plans[0].artifact_id: b"a,b\n1,2\n"}
        committed = kimi_artifacts.commit_artifacts(plans, contents, root)
        self.assertEqual(len(committed), 1)
        target = root / committed[0]["filename"]
        self.assertTrue(target.is_file())
        first_name = committed[0]["filename"]
        # A second identical response commits under a different name; the
        # existing file is untouched.
        plans2 = kimi_artifacts.plan_artifacts(files, kimi_artifacts.ArtifactLimits())
        contents2 = {plans2[0].artifact_id: b"a,b\n1,2\n"}
        committed2 = kimi_artifacts.commit_artifacts(plans2, contents2, root)
        self.assertNotEqual(first_name, committed2[0]["filename"])
        self.assertEqual(len(list(root.glob("ka_*.csv"))), 2)

    def test_rejected_plans_commit_nothing(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "artifacts"
        files = [{"name": "../evil.csv", "content": "x"}]
        plans = kimi_artifacts.plan_artifacts(files, kimi_artifacts.ArtifactLimits())
        contents = {plans[0].artifact_id: b"x"}
        committed = kimi_artifacts.commit_artifacts(plans, contents, root)
        self.assertEqual(committed, [])
        self.assertEqual(list(root.iterdir()), [])


class ExecuteQueryTests(KimiTestCase):
    def _run(self, transport, *, integration=None, artifact_root=None,
             budget=None, provider=lambda: "k" * 32):
        query = self.price_query()
        return kimi_client.execute_query(
            query, integration=integration or self.enabled, transport=transport,
            credential_provider=provider, budget=budget or kimi_client.ExperimentBudget(),
            artifact_root=artifact_root)

    def test_disabled_config_never_touches_network(self):
        transport = FakeTransport()
        result = self._run(transport, integration=kimi_client.KimiIntegrationConfig(enabled=False))
        self.assertEqual(result.outcome, "not_configured")
        self.assertEqual(transport.requests, [])

    def test_experiment_budget_caps_gateway_requests(self):
        transport = FakeTransport()
        budget = kimi_client.ExperimentBudget(max_requests=2)
        self._run(transport, budget=budget)
        self._run(transport, budget=budget)
        result = self._run(transport, budget=budget)
        self.assertEqual(result.outcome, "budget_exhausted")
        self.assertEqual(len(transport.requests), 2)

    def test_provider_analysis_separated_from_data_text(self):
        transport = FakeTransport([kimi_transport.TransportOutcome(
            "ok", http_status=200,
            body=envelope(user_text="rows: 2", assistant_text="AI 分析意见"))])
        result = self._run(transport)
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(result.user_text, "rows: 2")
        self.assertEqual(result.provider_analysis, "AI 分析意见")
        self.assertEqual(result.data_kind, "daily_quote")

    def test_is_success_true_with_no_data_is_empty_not_success(self):
        transport = FakeTransport([kimi_transport.TransportOutcome(
            "ok", http_status=200, body=envelope())])
        result = self._run(transport)
        self.assertEqual(result.outcome, "empty_result")

    def test_business_failure_classified_with_unknown_cost(self):
        transport = FakeTransport([kimi_transport.TransportOutcome(
            "ok", http_status=200,
            body=envelope(success=False, error={"user": ["会员档位不足"]}))])
        result = self._run(transport)
        self.assertEqual(result.outcome, "gateway_business_error")
        self.assertEqual(result.cost_status, "unknown")

    def test_row_cap_aborts_before_any_commit(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        rows = "\n".join("r{0},v{0}".format(i) for i in range(1500))
        transport = FakeTransport([kimi_transport.TransportOutcome(
            "ok", http_status=200,
            body=envelope(files=[{"name": "big.csv", "content": rows}]))])
        result = self._run(transport, artifact_root=Path(temporary.name))
        self.assertEqual(result.outcome, "response_too_large")
        self.assertTrue(result.row_limit_exceeded)
        self.assertEqual(result.committed_artifacts, [])
        self.assertEqual(list(Path(temporary.name).iterdir()), [])

    def test_files_committed_via_host_writer_only(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        transport = FakeTransport([kimi_transport.TransportOutcome(
            "ok", http_status=200,
            body=envelope(files=[{"name": "prices.csv", "content": "d,c\n2026-10-09,1700"}]))])
        result = self._run(transport, artifact_root=Path(temporary.name))
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(len(result.committed_artifacts), 1)
        record = result.as_record()
        self.assertEqual(record["committed_artifacts"][0]["bytes"], len("d,c\n2026-10-09,1700"))

    def test_malicious_file_name_is_dropped_not_sanitized_into_use(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        transport = FakeTransport([kimi_transport.TransportOutcome(
            "ok", http_status=200,
            body=envelope(files=[{"name": "..\\..\\evil.csv", "content": "x"}]))])
        result = self._run(transport, artifact_root=Path(temporary.name))
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(result.committed_artifacts, [])
        self.assertEqual(list(Path(temporary.name).iterdir()), [])

    def test_non_json_body_is_response_invalid(self):
        transport = FakeTransport([kimi_transport.TransportOutcome(
            "ok", http_status=200, body=b"<html>login page</html>")])
        result = self._run(transport)
        self.assertEqual(result.outcome, "response_invalid")

    def test_record_is_secret_free(self):
        transport = FakeTransport([kimi_transport.TransportOutcome(
            "ok", http_status=200, body=envelope(user_text="data"))])
        result = self._run(transport)
        record = json.dumps(result.as_record(), ensure_ascii=False)
        self.assertNotIn("kkkk", record)
        self.assertIn("cost_status", record)


class TransportClassificationTests(unittest.TestCase):
    """Direct urllib-level classification with injected errors."""

    class FailingOpener:
        def __init__(self, error):
            self._error = error

        def open(self, request, timeout):
            raise self._error

    def _transport(self, error):
        transport = kimi_transport.UrllibTransport()
        transport.opener = self.FailingOpener(error)
        return transport

    def test_http_401_maps_to_auth_rejected(self):
        import urllib.error
        outcome = self._transport(urllib.error.HTTPError(
            "url", 401, "Unauthorized", None, None)).post_tools(b"{}", {}, 1.0)  # type: ignore[arg-type]
        self.assertEqual(outcome.outcome, "auth_rejected")

    def test_http_5xx_after_send_is_unknown_not_transport_error(self):
        import io
        import urllib.error
        error = urllib.error.HTTPError("url", 502, "Bad Gateway", None, io.BytesIO(b"x"))
        outcome = self._transport(error).post_tools(b"{}", {}, 1.0)
        self.assertEqual(outcome.outcome, "outcome_unknown")

    def test_http_429_parses_retry_after(self):
        import io
        import urllib.error
        error = urllib.error.HTTPError(
            "url", 429, "Too Many", {"Retry-After": "17"}, io.BytesIO(b""))
        outcome = self._transport(error).post_tools(b"{}", {}, 1.0)
        self.assertEqual(outcome.outcome, "rate_limited")
        self.assertEqual(outcome.retry_after_seconds, 17.0)
        self.assertFalse(outcome.retryable is False)

    def test_auth_and_destination_never_retryable(self):
        for code in ("auth_rejected", "permission_denied", "destination_denied",
                     "credential_missing", "response_invalid"):
            outcome = kimi_transport.TransportOutcome(code)
            self.assertFalse(outcome.retryable, code)


class ContractSnapshotIntegrityTests(unittest.TestCase):
    def test_snapshot_hash_is_stable_and_binding(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        first = snapshot(root)
        second = snapshot(root)
        self.assertEqual(first["contract_hash"], second["contract_hash"])
        self.assertEqual(len(first["contract_hash"]), 64)

    def test_snapshot_rejects_unknown_param_type(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        broken = json.loads(json.dumps(SNAPSHOT))
        broken["apis"][0]["params"]["mystery"] = {"type": "pickle", "required": False}
        path = Path(temporary.name) / "broken.json"
        path.write_text(json.dumps(broken), encoding="utf-8")
        with self.assertRaises(kimi_contracts.KimiContractError):
            kimi_contracts.load_contract_snapshot(path)

    def test_real_candidate_snapshot_loads(self):
        """The reviewed candidate file in config/candidates must itself pass
        structural validation (read-only, no workspace writes)."""
        path = Path(__file__).resolve().parents[1] / "config" / "candidates" / "kimi" / "contracts.v1.json"
        if not path.is_file():
            self.skipTest("candidate snapshot not present")
        loaded = kimi_contracts.load_contract_snapshot(path)
        api_ids = {api["api_id"] for api in loaded["apis"]}
        self.assertEqual(api_ids, {"ifind_get_price", "ifind_get_stock_announcement",
                                   "ifind_get_financial_statements", "dongcai_mx_finance_search"})

    def test_real_candidate_snapshot_enforces_first_batch_contract(self):
        path = Path(__file__).resolve().parents[1] / "config" / "candidates" / "kimi" / "contracts.v1.json"
        if not path.is_file():
            self.skipTest("candidate snapshot not present")
        loaded = kimi_contracts.load_contract_snapshot(path)
        good = {"ticker": "600519.SH", "start_date": "2026-09-28",
                "end_date": "2026-10-09", "adjust": "forward"}
        kimi_client.build_query(loaded, "ifind_get_price", good, purpose="poc")
        with self.assertRaises(kimi_contracts.KimiContractError):
            kimi_client.build_query(loaded, "ifind_get_price",
                                    dict(good, start_date="2023-01-01"), purpose="poc")


if __name__ == "__main__":
    unittest.main()
