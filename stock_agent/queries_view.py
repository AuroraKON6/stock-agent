"""K05: Kimi data-query ledger view — who asked what, what came back, what it cost.

Pure projection over the append-only query ledger written by
``sources/kimi_adapter.py``. Read-only: building this view never contacts
the gateway, never consumes quota, and never writes anywhere. Outcomes are
reported with their distinct classes (auth/permission/rate-limit/empty/
contract/transport-unknown), never collapsed into a single "failed" (plan
section 8-K05 table).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

QUERIES_VIEW_SCHEMA_VERSION = "1.0"
QUERIES_VIEW_SCOPE_NOTE = "personal_research_workbench_not_institutional_approval"
DEFAULT_QUERY_LIMIT = 50

_OUTCOME_LABELS = {
    "ok": "成功（数据已回）",
    "empty_result": "空结果（is_success=true 但无数据）",
    "auth_rejected": "认证被拒（401）",
    "permission_denied": "权限被拒（403，不推断会员档位）",
    "rate_limited": "限流（429，带 Retry-After）",
    "gateway_business_error": "网关业务失败",
    "credential_missing": "凭据缺失（不回退其他账号）",
    "destination_denied": "出站目标被拒（origin 白名单/HTTPS）",
    "response_too_large": "响应超限（字节/行数有界中止）",
    "response_invalid": "响应非法（非 JSON 信封）",
    "outcome_unknown": "结果未知（发送后中断，费用未知）",
    "budget_exhausted": "预算耗尽（实验/每日上限）",
    "not_configured": "集成未启用（未联网）",
    "transport_error": "传输错误",
}

_GATE_REASONS = {
    "source_not_registered": "源未登记",
    "source_not_approved": "源未获真实人工批准",
    "source_expired": "源登记已过期",
    "body_retention_not_allowed": "无正文保留权（不缓存内容）",
}


def outcome_label(outcome: str) -> str:
    return _OUTCOME_LABELS.get(str(outcome or ""), str(outcome or "unknown"))


def gate_reason_label(reason: str) -> str:
    return _GATE_REASONS.get(str(reason or ""), str(reason or "unknown"))


def build_queries_view(
    config: Dict[str, Any],
    *,
    limit: int = DEFAULT_QUERY_LIMIT,
    purpose: str = "",
) -> Dict[str, Any]:
    from .models import now_iso
    from .sources import kimi_adapter

    ledger = kimi_adapter.load_query_ledger(config, limit=100000)
    budget_state = kimi_adapter.may_start_data_call(config)
    intents = {str(record.get("request_id") or ""): record
               for record in ledger if record.get("record_type") == "query_intent"}
    results: Dict[str, Dict[str, Any]] = {}
    for record in ledger:
        if record.get("record_type") == "query_result":
            results[str(record.get("request_id") or "")] = record
    consumption: List[Dict[str, Any]] = [record for record in ledger
                                         if record.get("record_type") == "consumed"]

    queries: List[Dict[str, Any]] = []
    for record in ledger:
        if record.get("record_type") != "query_intent":
            continue
        request_id = str(record.get("request_id") or "")
        result = results.get(request_id)
        entry: Dict[str, Any] = {
            "request_id": request_id,
            "created_at": str(record.get("created_at") or ""),
            "task_id": str(record.get("task_id") or ""),
            "source_id": str(record.get("source_id") or ""),
            "api_id": str(record.get("api_id") or ""),
            "data_kind": str(record.get("data_kind") or ""),
            "purpose": str(record.get("purpose") or ""),
            "params": _sanitized_params(record.get("normalized_params")),
            "contract_hash": str(record.get("contract_hash") or "")[:16],
            "idempotency_key": str(record.get("idempotency_key") or "")[:16],
            "result": None,
        }
        if result is not None:
            entry["result"] = {
                "outcome": str(result.get("outcome") or ""),
                "outcome_label": outcome_label(str(result.get("outcome") or "")),
                "cost_status": str(result.get("cost_status") or "unknown"),
                "attempts": int(result.get("attempts") or 0),
                "row_count": result.get("row_count"),
                "http_status": result.get("http_status"),
                "artifact_count": len(result.get("artifact_plans") or []),
                "rejected_artifact_names": [
                    str(plan.get("remote_name_record") or "")
                    for plan in (result.get("artifact_plans") or [])
                    if plan.get("remote_name_rejected")],
            }
        if purpose and purpose not in str(record.get("purpose") or ""):
            continue
        queries.append(entry)
    queries.sort(key=lambda item: item["created_at"], reverse=True)

    outcome_counts: Dict[str, int] = {}
    for result in results.values():
        outcome = str(result.get("outcome") or "unknown")
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1

    return {
        "schema_version": QUERIES_VIEW_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "scope_note": QUERIES_VIEW_SCOPE_NOTE,
        "ledger_record_count": len(ledger),
        "query_count": len(queries),
        "consumption_count": len(consumption),
        "daily_data_call_budget": budget_state,
        "outcome_counts": dict(sorted(outcome_counts.items())),
        "queries": queries[:max(1, int(limit))],
    }


def _sanitized_params(params: Any) -> Dict[str, Any]:
    """Display params only: allowlist the contract parameter names, cap
    lengths; file_path never appears (host generated and not recorded)."""
    if not isinstance(params, dict):
        return {}
    allowed = {"ticker", "start_date", "end_date", "adjust", "interval",
               "statement", "financial_parameter", "query", "format",
               "request_time", "category"}
    sanitized: Dict[str, Any] = {}
    for name, value in params.items():
        if name not in allowed:
            continue
        text = str(value)
        sanitized[name] = text[:80]
    return sanitized


def build_kimi_data_status(config: Dict[str, Any]) -> Dict[str, Any]:
    """Read-only health layers for the kimi data path (plan K05). Never
    opens the network: 'health check' must not cost quota. Layers are
    reported separately; NOT_RUN means the thing never happened."""
    from .integrations.kimi import client as kimi_client
    from .sources import kimi_adapter
    from .sources.registry import load_source_registry

    integration = kimi_client.KimiIntegrationConfig.from_config(config)
    kimi_sources = (config or {}).get("kimi_sources") or {}
    snapshot_path = str(kimi_sources.get("contract_snapshot") or "")
    contract_state = {"status": "NOT_CONFIGURED", "path": snapshot_path,
                      "detail": "no contract snapshot configured"}
    if snapshot_path:
        from pathlib import Path
        from .config import project_path
        path = project_path(snapshot_path)
        if not path.is_file():
            contract_state = {"status": "MISSING", "path": snapshot_path,
                              "detail": "configured snapshot file absent"}
        else:
            from .integrations.kimi import contracts as kimi_contracts
            try:
                loaded = kimi_contracts.load_contract_snapshot(path)
                contract_state = {"status": "VALID", "path": snapshot_path,
                                  "contract_hash": loaded["contract_hash"][:16],
                                  "api_count": len(loaded["apis"])}
            except kimi_contracts.KimiContractError as exc:
                contract_state = {"status": "INVALID", "path": snapshot_path,
                                  "detail": str(exc)[:160]}

    registry = load_source_registry(config)
    kimi_entries = {source_id: entry for source_id, entry in (registry.get("entries") or {}).items()
                    if source_id.startswith("kimi_")}
    sources_report = {}
    for source_id, entry in sorted(kimi_entries.items()):
        from .sources.registry import registry_status
        status = registry_status(registry, source_id)
        sources_report[source_id] = {
            "kind": entry.get("kind"),
            "approval_state": status.get("approval_state"),
            "expired": status.get("expired"),
            "body_retention_allowed": status.get("body_retention_allowed"),
            "llm_external_send_allowed": status.get("external_send_allowed"),
        }
    if not kimi_entries:
        sources_report = {"detail": "no kimi_* sources registered in the production registry "
                                    "(candidate example file exists; not loaded)"}

    ledger = kimi_adapter.load_query_ledger(config, limit=100000)
    data_results = [record for record in ledger if record.get("record_type") == "query_result"]
    outcomes = sorted({str(record.get("outcome") or "") for record in data_results})
    budget_state = kimi_adapter.may_start_data_call(config)

    return {
        "schema_version": "1.0",
        "generated_at": _now_iso(),
        "scope_note": "technical_status_only_not_business_go",
        "layers": {
            "configured": integration.enabled,
            "contract_valid": contract_state,
            "access_verified": {"status": "NOT_RUN",
                                "detail": "requires a real credential check; blocked by K01 rights matrix"},
            "data_valid": {"status": "NOT_RUN" if not data_results else "PARTIAL",
                           "observed_outcomes": outcomes,
                           "detail": "quality validation only meaningful after a governed query"},
            "internally_approved": {"status": "BLOCKED" if not kimi_entries else "PARTIAL",
                                    "sources": sources_report},
            "llm_eligible": {"status": "BLOCKED",
                             "detail": "no kimi source has llm_external_send_allowed=true; "
                                       "rights matrix item 'external send' stays BLOCKED"},
        },
        "daily_data_call_budget": budget_state,
        "live_calls_made": len(data_results),
        "reminder": "no network is contacted by this status command; live stays off until K06 gates",
    }


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
