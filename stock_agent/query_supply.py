"""R11: governed queries feeding the task supply — offline file provider first.

Wires ``sources.kimi_adapter.governed_execute`` into task preparation so a
research task can carry data that arrived through the FULL governed path
(gate → purpose → retention → destination → atomic reserve → execute →
normalize), with the query→result→consumed chain bound in ONE authority
(the connector intake store, R04/R11).

- **File fixture provider** (the "Fake/file first" step the taskbook names):
  canned outcomes live in a host-approved JSON file (no network, no
  credentials); live providers remain injection-only until their approvals
  exist.
- **Distinct supply statuses (A52)**: ``data_delivered`` / ``no_data`` /
  ``permission_denied`` / ``auth_rejected`` / ``suspended`` / ``replay`` /
  gate refusals — never collapsed into one "empty".
- **Announcement metadata stays leads (A54)**: normalized announcement rows
  join the context as URL leads; they never create evidence_summaries
  entries — the original is still missing until a vault import happens.
- **Replay never re-bills or re-binds (A53)**: an identical api+params
  request resolves to the same idempotency key → intake replay → no new
  reservation, no second consumption.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import project_path
from .connector_runtime import queries as connector_queries
from .integrations.kimi import transport as kimi_transport
from .sources import kimi_adapter

SUPPLY_SCHEMA = "1.0"
DEFAULT_FIXTURE_PATH = "config/candidates/sources/file_fixtures.json"

_OUTCOME_TO_SUPPLY = {
    "ok": "data_delivered",
    "empty_result": "no_data",
    "permission_denied": "permission_denied",
    "auth_rejected": "auth_rejected",
    "suspended": "suspended",
}


class QuerySupplyError(ValueError):
    pass


class FileFixtureTransport:
    """Offline transport: looks up (api_id) in the host's fixture file and
    returns the canned outcome. Zero network, zero credentials."""

    def __init__(self, fixture_path: Path):
        self.fixture_path = Path(fixture_path)
        self.requests: List[str] = []

    def post_tools(self, payload: bytes, headers: Dict[str, str],
                   timeout: float) -> "kimi_transport.TransportOutcome":
        request = json.loads(payload.decode("utf-8"))
        params = dict(request.get("params") or {})
        api_name = str(params.get("api_name") or "")
        self.requests.append(api_name)
        document = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        entry = (document.get("apis") or {}).get(api_name)
        if entry is None:
            return kimi_transport.TransportOutcome(
                kimi_transport.OUTCOME_PERMISSION_DENIED,
                detail="fixture provider has no entry for {0}".format(api_name))
        outcome = str(entry.get("outcome") or "ok")
        body = entry.get("body")
        payload_body = (json.dumps(body, ensure_ascii=False).encode("utf-8")
                        if body is not None else None)
        return kimi_transport.TransportOutcome(
            outcome, http_status=int(entry.get("http_status") or 200),
            body=payload_body,
            detail=str(entry.get("detail") or ""))


def supply_status_for(outcome: str) -> str:
    return _OUTCOME_TO_SUPPLY.get(str(outcome or ""), str(outcome or "unknown"))


def attach_query_to_spec(config: Dict[str, Any], spec: Dict[str, Any], *,
                         source_id: str, api_id: str, params: Dict[str, Any],
                         purpose: str, task_id: str,
                         transport: Optional[Any] = None,
                         data_kind: str = "daily_quote",
                         allow_cache: bool = True) -> Dict[str, Any]:
    """Run ONE governed query and bind its result into the task spec.

    Offline default: the file fixture provider. A live transport may be
    injected by an approved entry point later — the governed path is the
    same either way. Returns a receipt dict; ``spec`` is mutated only on a
    delivered result (context merge + receipt + consumption binding)."""
    config = dict(config or {})
    kimi_sources = dict(config.get("kimi_sources") or {})
    kimi_sources.setdefault("file_fixture_path", DEFAULT_FIXTURE_PATH)
    integration = dict(config.get("kimi_integration") or {})
    integration.setdefault("enabled", True)
    config["kimi_sources"] = kimi_sources
    config["kimi_integration"] = integration

    if transport is None:
        fixture = project_path(str(kimi_sources["file_fixture_path"]))
        if not fixture.is_file():
            raise QuerySupplyError(
                "file fixture provider not configured: {0}".format(fixture))
        transport = FileFixtureTransport(fixture)

    from .sources.registry import load_source_registry

    registry = load_source_registry(config)
    snapshot = _snapshot_for(source_id, api_id, data_kind=data_kind)
    result = kimi_adapter.governed_execute(
        snapshot, api_id, params, purpose=purpose, config=config,
        registry=registry, transport=transport,
        credential_provider=lambda: "file-fixture-offline",
        allow_cache=allow_cache)

    receipt: Dict[str, Any] = {
        "schema_version": SUPPLY_SCHEMA,
        "source_id": source_id, "api_id": api_id, "purpose": purpose,
        "task_id": task_id,
    }
    stage = str(result.get("stage") or "")
    if stage == "cache":
        # A cache hit reuses a prior billable query: nothing is reserved or
        # consumed again; the cached normalization still feeds the supply.
        cached = dict(result.get("cached") or {})
        normalized = cached.get("normalized")
        receipt["supply_status"] = "data_delivered"
        receipt["cache_hit"] = True
        if isinstance(normalized, dict):
            spec.setdefault("query_results", {})[api_id] = normalized
            _merge_context(spec, api_id, normalized, params)
        spec.setdefault("query_receipts", []).append(receipt)
        return receipt
    if stage == "gate":
        receipt["supply_status"] = "gate_refused"
        receipt["detail"] = str((result.get("gate") or {}).get("reason") or "")
        return receipt
    if stage in ("budget", "rate_limited", "destination", "integration_disabled"):
        receipt["supply_status"] = stage
        receipt["detail"] = str(result.get("reason")
                                or (result.get("budget") or {}).get("reason") or "")
        return receipt
    if stage == "replay":
        prior = result.get("prior_intent") or {}
        receipt["supply_status"] = "replay"
        receipt["intent_id"] = str(prior.get("intent_id") or "")
        receipt["idempotency_key"] = str(result.get("idempotency_key") or "")
        return receipt

    # stage == "executed": normalize the outcome into a supply status.
    outcome = str((result.get("result") or {}).outcome or "")
    receipt["supply_status"] = supply_status_for(outcome)
    receipt["intent_id"] = str(result.get("intent_id") or "")
    receipt["idempotency_key"] = str(result.get("idempotency_key") or "")
    receipt["outcome"] = outcome
    normalized = result.get("normalized")
    if normalized is not None and receipt["supply_status"] == "data_delivered":
        context = spec.setdefault("public_context", {})
        results = spec.setdefault("query_results", {})
        results[api_id] = normalized
        if normalized.get("data_kind") == "daily_quote":
            ticker = str((params or {}).get("ticker") or "")
            if ticker:
                context.setdefault("quote_snapshots", {})[ticker] = {
                    "price": _first_close(normalized),
                    "provider": "file_fixture",
                    "source_time": "",
                }
        # A54: announcement metadata joins as LEADS ONLY — no
        # evidence_summaries entry is ever created here; the original stays
        # missing until an explicit vault import.
        if normalized.get("data_kind") == "announcement_metadata":
            context.setdefault("announcement_leads", []).extend(
                normalized.get("rows") or [])
    # Bind the consumption for ANY executed outcome (delivered or honest
    # empty/denied) — the chain records what the query produced, and one
    # intent binds at most once.
    intake_path = kimi_adapter._query_store_path(config)
    with connector_queries.ConnectorQueryStore(intake_path) as intake:
        verdict = intake.record_query_consumption(
            receipt.get("intent_id") or "", task_id,
            supply_status=receipt["supply_status"])
    receipt["consumption_recorded"] = bool(verdict.get("recorded"))
    receipts = spec.setdefault("query_receipts", [])
    receipts.append(receipt)
    return receipt


def _merge_context(spec: Dict[str, Any], api_id: str, normalized: Dict[str, Any],
                   params: Optional[Dict[str, Any]]) -> None:
    """Merge a DELIVERED normalization into the task context.

    A54: announcement metadata joins as LEADS ONLY — no evidence_summaries
    entry is ever created here; the original stays missing until an explicit
    vault import happens."""
    context = spec.setdefault("public_context", {})
    kind = str(normalized.get("data_kind") or "")
    if kind == "daily_quote":
        ticker = str((params or {}).get("ticker") or "")
        if ticker:
            context.setdefault("quote_snapshots", {})[ticker] = {
                "price": _first_close(normalized),
                "provider": "file_fixture",
                "source_time": "",
            }
    elif kind == "announcement_metadata":
        context.setdefault("announcement_leads", []).extend(
            normalized.get("rows") or [])


def _first_close(normalized: Dict[str, Any]) -> Optional[float]:
    for bar in normalized.get("bars") or []:
        if bar.get("close") is not None:
            return float(bar["close"])
    return None


def _snapshot_for(source_id: str, api_id: str, data_kind: str = "daily_quote") -> Dict[str, Any]:
    """A minimal frozen contract snapshot for the fixture provider, carrying
    the same api shape the reviewed contract files use (so governed_execute,
    build_query and normalize_params run unchanged). The fixture provider is
    offline-by-design: its snapshot is versioned in-code, not a substitute
    for a reviewed live contract."""
    import hashlib

    params: Dict[str, Any] = {}
    if data_kind == "daily_quote":
        params = {
            "ticker": {"type": "a_share_ticker_list", "required": True, "max_items": 3},
            "start_date": {"type": "date", "required": True},
            "end_date": {"type": "date", "required": True},
            "adjust": {"type": "enum", "required": False,
                       "options": ["forward", "backward", "none"]},
        }
    elif data_kind == "announcement_metadata":
        params = {"query": {"type": "search_query", "required": True, "max_length": 200}}
    contract_hash = hashlib.sha256(
        "{0}:{1}:fixture-v1".format(source_id, api_id).encode("utf-8")).hexdigest()
    return {
        "schema_version": "1.0",
        "contract_hash": contract_hash,
        "apis": [{
            "api_id": api_id, "source_id": source_id,
            "data_source_name": "file_fixture", "api_name": api_id,
            "transport": "file_fixture", "data_kind": data_kind,
            "purpose": "offline fixture supply",
            "params": params,
            "constraints": {},
            "result_contract": {"envelope": "std"},
            "known_unknowns": {}, "notes": [],
        }],
    }
