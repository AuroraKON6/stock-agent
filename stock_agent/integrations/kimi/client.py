"""Kimi integration client: assemble, send, classify, and record one query.

The client binds the frozen contract (contracts), the fixed-origin transport
(transport) and the host-decided artifact writer (artifacts) into one
fail-closed flow: validate -> budget check -> credential check -> send ->
classify -> parse envelope -> plan artifacts. Live traffic requires an
explicitly enabled configuration; the default is disabled and every entry
point degrades to ``not_configured`` without touching the network.

Envelope semantics (from the external gateway, treated as untrusted input):
``is_success`` separates business success from failure; ``result.user[].text``
is the data-facing text; ``result.assistant[].text`` is provider analysis and
is kept apart as ``provider_analysis`` (never official fact, plan F07);
``files[]`` content is never written by this layer — only the host-controlled
writer commits artifacts, and only when the caller supplies a root.
``is_success=true`` with neither text nor files is classified ``empty_result``
rather than success (T21).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import artifacts as artifacts_module
from . import contracts as contracts_module
from . import transport as transport_module

MAX_ROWS_PER_RESPONSE = 1000
MAX_EXPERIMENT_REQUESTS = 5


class KimiIntegrationError(RuntimeError):
    pass


@dataclass
class KimiIntegrationConfig:
    enabled: bool = False
    origin: str = transport_module.DEFAULT_ORIGIN
    tools_path: str = transport_module.DEFAULT_TOOLS_PATH
    timeout_seconds: float = transport_module.DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = transport_module.MAX_RESPONSE_BYTES
    poc: bool = True

    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]]) -> "KimiIntegrationConfig":
        raw = (config or {}).get("kimi_integration") or {}
        if not isinstance(raw, dict):
            raise KimiIntegrationError("kimi_integration must be an object when present")
        known = {"enabled", "origin", "tools_path", "timeout_seconds",
                 "max_response_bytes", "poc"}
        unexpected = set(raw) - known
        if unexpected:
            raise KimiIntegrationError(
                "unknown kimi_integration fields: {0}".format(sorted(unexpected)))
        merged = cls()
        merged.enabled = bool(raw.get("enabled", False))
        merged.origin = str(raw.get("origin") or transport_module.DEFAULT_ORIGIN)
        merged.tools_path = str(raw.get("tools_path") or transport_module.DEFAULT_TOOLS_PATH)
        try:
            merged.timeout_seconds = float(raw.get("timeout_seconds") or transport_module.DEFAULT_TIMEOUT_SECONDS)
            merged.max_response_bytes = int(raw.get("max_response_bytes") or transport_module.MAX_RESPONSE_BYTES)
        except (TypeError, ValueError) as exc:
            raise KimiIntegrationError("kimi_integration limits must be numeric") from exc
        merged.poc = bool(raw.get("poc", True))
        return merged


@dataclass
class ExperimentBudget:
    """Per-experiment request cap shared by describe and call attempts
    (plan K02: at most 5 gateway requests including retries). In-process
    only; the persistent ledger lives in the sources adapter (K03)."""
    max_requests: int = MAX_EXPERIMENT_REQUESTS
    used: int = 0

    def take(self) -> None:
        if self.used >= self.max_requests:
            raise BudgetExhaustedError(
                "experiment budget exhausted: {0}/{1} gateway requests".format(self.used, self.max_requests))
        self.used += 1


class BudgetExhaustedError(RuntimeError):
    pass


@dataclass
class DataQuery:
    request_id: str
    api_id: str
    source_id: str
    data_source_name: str
    api_name: str
    data_kind: str
    purpose: str
    normalized_params: Dict[str, Any]
    idempotency_key: str
    contract_hash: str
    task_id: Optional[str] = None

    def gateway_params(self) -> Dict[str, Any]:
        """Serialize typed params into the wire form: the only string-typed
        field the gateway demands (file_path) is filled with a host-generated
        virtual name here, never with a caller value."""
        params = dict(self.normalized_params)
        params["file_path"] = contracts_module.virtual_artifact_name(
            _api_stub(self), _preferred_ext(self.data_kind))
        params.setdefault("format", "json")
        return params


def _api_stub(query: DataQuery) -> Dict[str, Any]:
    return {"api_name": query.api_name}


def _preferred_ext(data_kind: str) -> str:
    return "csv" if data_kind in {"daily_quote", "financial_statement",
                                  "announcement_metadata"} else "json"


def build_query(snapshot: Dict[str, Any], api_id: str, raw_params: Dict[str, Any], *,
                purpose: str, task_id: Optional[str] = None,
                poc: bool = True, request_id: str = "") -> DataQuery:
    """Validate caller input against the frozen contract and produce the
    host-owned query record. Raises KimiContractError on any violation."""
    api = contracts_module.find_api(snapshot, api_id)
    normalized = contracts_module.normalize_params(api, raw_params, poc=poc)
    import uuid
    return DataQuery(
        request_id=request_id or "kq_{0}".format(uuid.uuid4().hex),
        api_id=api_id,
        source_id=api["source_id"],
        data_source_name=api["data_source_name"],
        api_name=api["api_name"],
        data_kind=api["data_kind"],
        purpose=purpose,
        normalized_params=normalized,
        idempotency_key=contracts_module.idempotency_key(snapshot, api, normalized),
        contract_hash=snapshot["contract_hash"],
        task_id=task_id,
    )


@dataclass
class DataResult:
    request_id: str
    outcome: str
    data_kind: str
    source_id: str
    transport_provider: str = "kimi_agent_gw"
    data_provider: Optional[str] = None
    user_text: str = ""
    provider_analysis: str = ""
    artifact_plans: List[Dict[str, Any]] = field(default_factory=list)
    committed_artifacts: List[Dict[str, Any]] = field(default_factory=list)
    row_count: Optional[int] = None
    row_limit_exceeded: bool = False
    http_status: Optional[int] = None
    error_class: Optional[str] = None
    retry_after_seconds: Optional[float] = None
    cost_status: str = "unknown"
    attempts: int = 0
    detail: str = ""
    observed_at: str = ""
    # Accepted artifact bytes by artifact_id, in memory only: the sources
    # adapter normalizes and caches from here; ledger records and displays
    # never include body content.
    artifact_contents: Dict[str, bytes] = field(default_factory=dict, repr=False)

    def as_record(self) -> Dict[str, Any]:
        """Secret-free ledger record for the query attempt (plan 7.2 subset;
        unknowns stay null with a reason, never faked)."""
        return {
            "request_id": self.request_id,
            "outcome": self.outcome,
            "data_kind": self.data_kind,
            "source_id": self.source_id,
            "transport_provider": self.transport_provider,
            "data_provider": self.data_provider,
            "user_text_chars": len(self.user_text),
            "provider_analysis_chars": len(self.provider_analysis),
            "artifact_plans": [dict(plan) for plan in self.artifact_plans],
            "committed_artifacts": [dict(item) for item in self.committed_artifacts],
            "row_count": self.row_count,
            "row_limit_exceeded": self.row_limit_exceeded,
            "http_status": self.http_status,
            "error_class": self.error_class,
            "retry_after_seconds": self.retry_after_seconds,
            "cost_status": self.cost_status,
            "attempts": self.attempts,
            "detail": self.detail[:200],
        }


def execute_query(query: DataQuery, *,
                  integration: KimiIntegrationConfig,
                  transport: transport_module.GatewayTransport,
                  credential_provider: Optional[Callable[[], str]],
                  budget: ExperimentBudget,
                  artifact_root: Optional[Any] = None,
                  artifact_limits: Optional[artifacts_module.ArtifactLimits] = None,
                  now: Optional[Callable[[], str]] = None) -> DataResult:
    """Run one governed query end to end. The transport is injected, so tests
    drive this fully offline; production wires UrllibTransport."""
    from datetime import datetime, timezone

    def _now() -> str:
        return (now or (lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")))()

    result = DataResult(
        request_id=query.request_id, outcome=transport_module.OUTCOME_OK,
        data_kind=query.data_kind, source_id=query.source_id,
        data_provider=query.data_source_name, observed_at=_now())
    if not integration.enabled:
        result.outcome = transport_module.OUTCOME_NOT_CONFIGURED
        result.detail = "kimi integration disabled; no network attempted"
        return result
    try:
        budget.take()
    except BudgetExhaustedError as exc:
        result.outcome = transport_module.OUTCOME_BUDGET_EXHAUSTED
        result.detail = str(exc)
        return result
    try:
        api_key = transport_module.resolve_credential(credential_provider)
    except transport_module.CredentialMissing as exc:
        result.outcome = transport_module.OUTCOME_CREDENTIAL_MISSING
        result.detail = str(exc)
        return result
    payload = transport_module.build_tools_payload(
        "call_data_source_tool",
        transport_module.call_data_source_params(
            query.data_source_name, query.api_name, query.gateway_params()))
    headers = transport_module.authorization_header(api_key)
    result.attempts = 1
    outcome = transport.post_tools(payload, headers, integration.timeout_seconds)
    result.http_status = outcome.http_status
    result.retry_after_seconds = outcome.retry_after_seconds
    if not outcome.ok:
        result.outcome = outcome.outcome
        result.detail = outcome.detail
        # 429 and post-send unknowns may have consumed plan quota.
        result.cost_status = "unknown" if outcome.outcome in {
            transport_module.OUTCOME_RATE_LIMITED, transport_module.OUTCOME_OUTCOME_UNKNOWN} else "none"
        return result
    parsed = _parse_envelope(outcome.body)
    if parsed is None:
        result.outcome = transport_module.OUTCOME_RESPONSE_INVALID
        result.detail = "gateway body was not a JSON envelope"
        return result
    envelope, error_class = parsed
    if not envelope.get("is_success"):
        result.outcome = _business_outcome_for_error(error_class)
        result.error_class = error_class
        result.detail = "gateway reported business failure"
        result.cost_status = "unknown"
        return result
    user_text, provider_text = _envelope_texts(envelope)
    remote_files = [f for f in (envelope.get("files") or []) if isinstance(f, dict)]
    limits = artifact_limits or artifacts_module.ArtifactLimits()
    try:
        plans = artifacts_module.plan_artifacts(remote_files, limits)
    except artifacts_module.ArtifactLimitError as exc:
        result.outcome = transport_module.OUTCOME_RESPONSE_TOO_LARGE
        result.detail = str(exc)
        result.cost_status = "unknown"
        return result
    contents = _artifact_contents(remote_files, plans)
    row_count, limit_exceeded = _count_rows(query, contents)
    if limit_exceeded:
        result.outcome = transport_module.OUTCOME_RESPONSE_TOO_LARGE
        result.row_count = row_count
        result.row_limit_exceeded = True
        result.detail = "row cap {0} exceeded; bounded abort before any commit".format(MAX_ROWS_PER_RESPONSE)
        result.cost_status = "unknown"
        return result
    result.user_text = user_text
    result.provider_analysis = provider_text
    result.row_count = row_count
    result.artifact_plans = [_plan_record(plan) for plan in plans]
    for plan in plans:
        if plan.accepted:
            result.artifact_contents[plan.artifact_id] = contents[plan.artifact_id]
    if not user_text.strip() and not plans:
        result.outcome = "empty_result"
        result.detail = "is_success true but no data text and no files"
        result.cost_status = "unknown"
        return result
    if artifact_root is not None:
        accepted = [plan for plan in plans if plan.accepted]
        committed = artifacts_module.commit_artifacts(accepted, contents, artifact_root)
        result.committed_artifacts = committed
    result.cost_status = "unknown"  # in-plan calls still count; price unknown
    return result


def _plan_record(plan: artifacts_module.ArtifactPlan) -> Dict[str, Any]:
    return {
        "artifact_id": plan.artifact_id,
        "filename": plan.filename,
        "bytes": plan.byte_size,
        "sha256": plan.sha256,
        "extension": plan.extension,
        "accepted": plan.accepted,
        "remote_name_rejected": plan.remote_name_rejected,
        "remote_name_record": plan.remote_name_record,
    }


def _artifact_contents(remote_files: List[Dict[str, Any]],
                       plans: List[artifacts_module.ArtifactPlan]) -> Dict[str, bytes]:
    contents: Dict[str, bytes] = {}
    plan_iter = iter(plans)
    for item in remote_files:
        content = item.get("content")
        if content is None:
            continue
        data = content if isinstance(content, bytes) else str(content).encode("utf-8", "replace")
        try:
            plan = next(plan_iter)
        except StopIteration:
            break
        contents[plan.artifact_id] = data
    return contents


def _count_rows(query: DataQuery, contents: Dict[str, bytes]) -> "tuple[Optional[int], bool]":
    total = 0
    counted = False
    for data in contents.values():
        if not data:
            continue
        counted = True
        total += data.decode("utf-8", "replace").count("\n")
    if not counted:
        return None, False
    return total, total > MAX_ROWS_PER_RESPONSE


def _parse_envelope(body: Optional[bytes]) -> "Optional[tuple[Dict[str, Any], Optional[str]]]":
    if body is None:
        return None
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        # describe-style markdown strings and any non-object are not call
        # envelopes; callers must treat this as response_invalid.
        return None
    return parsed, _error_class(parsed.get("error"))


def _error_class(error: Any) -> Optional[str]:
    if error is None:
        return None
    if isinstance(error, dict):
        for side in ("user", "assistant"):
            value = error.get(side)
            if isinstance(value, list) and value:
                return "gateway_{0}_error".format(side)
        if any(error.get(side) for side in ("user", "assistant")):
            return "gateway_structured_error"
        return "gateway_error"
    return "gateway_error"


def _business_outcome_for_error(error_class: Optional[str]) -> str:
    # Gateway business errors are not membership guesses (F06): classify by
    # transport-level signals where possible, otherwise a distinct code.
    return "gateway_business_error"


def _envelope_texts(envelope: Dict[str, Any]) -> "tuple[str, str]":
    result = envelope.get("result") or {}
    user_text = "\n".join(
        (item or {}).get("text", "")
        for item in (result.get("user") or []) if isinstance(item, dict))
    assistant_text = "\n".join(
        (item or {}).get("text", "")
        for item in (result.get("assistant") or []) if isinstance(item, dict))
    return user_text, assistant_text
