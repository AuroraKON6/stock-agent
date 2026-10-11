"""GLM-02 source registry: approved-source contract with fail-closed lookups.

The registry records, per source: upstream identity, purpose, human approval
state, body-retention and LLM external-send rights, timeliness expectations,
rate limits, and expiry. Developers build and validate the structure only;
real approvals are entered by humans. Lookups fail closed: an unregistered,
expired, or unapproved source is never eligible for external send.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from ..config import project_path

SCHEMA_VERSION = "1.0"
DEFAULT_REGISTRY_PATH = "config/source_registry.json"
APPROVAL_STATES = {"pending_human_approval", "approved", "denied", "withdrawn"}
SOURCE_KINDS = {"news", "filing", "price", "manual_intel"}
STALENESS_POLICIES = {"reject", "flag"}
# Approval principals must look human; service/system accounts are rejected.
_NON_HUMAN_PRINCIPAL_MARKERS = ("stock_agent_", "service", "system", "bot", "agent:", "_agent")

ENTRY_FIELDS = {
    "source_id", "kind", "upstream", "purpose", "approval",
    "body_retention_allowed", "llm_external_send_allowed",
    "timeliness", "rate_limit", "expires_at",
}
TIMELINESS_FIELDS = {"expected_max_delay_minutes", "staleness_policy"}
RATE_LIMIT_FIELDS = {"max_requests_per_minute", "max_requests_per_day"}
APPROVAL_FIELDS = {"state", "approved_by", "approved_at"}


class SourceRegistryError(ValueError):
    pass


def _require_str(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise SourceRegistryError("field {0} must be a string".format(field))
    if not allow_empty and not value.strip():
        raise SourceRegistryError("field {0} must not be empty".format(field))
    return value


def _require_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SourceRegistryError("field {0} must be a positive integer".format(field))
    return value


def _parse_expiry(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SourceRegistryError("expires_at must be an ISO date (YYYY-MM-DD)") from exc


def _validate_approval(approval: Any) -> Dict[str, Any]:
    if not isinstance(approval, dict) or set(approval) != APPROVAL_FIELDS:
        raise SourceRegistryError(
            "approval must have exactly {0}".format(sorted(APPROVAL_FIELDS)))
    state = approval["state"]
    if state not in APPROVAL_STATES:
        raise SourceRegistryError(
            "approval.state must be one of {0}".format(sorted(APPROVAL_STATES)))
    approved_by = _require_str(approval["approved_by"], "approval.approved_by", allow_empty=True)
    approved_at = _require_str(approval["approved_at"], "approval.approved_at", allow_empty=True)
    if state == "approved":
        if not approved_by.strip():
            raise SourceRegistryError("approved state requires approval.approved_by")
        lowered = approved_by.lower()
        if any(marker in lowered for marker in _NON_HUMAN_PRINCIPAL_MARKERS):
            raise SourceRegistryError(
                "approval.approved_by must be a real human principal, not a service/system account")
        if not approved_at.strip():
            raise SourceRegistryError("approved state requires approval.approved_at")
        try:
            datetime.fromisoformat(approved_at)
        except ValueError as exc:
            raise SourceRegistryError("approval.approved_at must be an ISO timestamp") from exc
    elif approved_by.strip() or approved_at.strip():
        raise SourceRegistryError("approval records may only name a principal in approved state")
    return {"state": state, "approved_by": approved_by, "approved_at": approved_at}


def validate_registry_entries(raw_entries: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_entries, list):
        raise SourceRegistryError("registry entries must be a list")
    entries: List[Dict[str, Any]] = []
    seen_ids: set = set()
    seen_upstreams: set = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise SourceRegistryError("each registry entry must be an object")
        unexpected = set(raw) - ENTRY_FIELDS
        if unexpected:
            raise SourceRegistryError(
                "unexpected registry fields: {0}".format(sorted(unexpected)))
        missing = ENTRY_FIELDS - set(raw)
        if missing:
            raise SourceRegistryError(
                "missing registry fields: {0}".format(sorted(missing)))
        source_id = _require_str(raw["source_id"], "source_id")
        if source_id in seen_ids:
            raise SourceRegistryError("duplicate source_id: {0}".format(source_id))
        seen_ids.add(source_id)
        kind = _require_str(raw["kind"], "kind")
        if kind not in SOURCE_KINDS:
            raise SourceRegistryError(
                "source_id {0}: kind must be one of {1}".format(source_id, sorted(SOURCE_KINDS)))
        upstream = _require_str(raw["upstream"], "upstream")
        # Two registered sources over the same upstream+kind would masquerade as
        # independent corroboration; one registration per upstream reality.
        upstream_key = (upstream.lower(), kind)
        if upstream_key in seen_upstreams:
            raise SourceRegistryError(
                "source_id {0}: upstream {1!r} already registered for kind {2}; "
                "re-registering the same upstream under another name is pseudo-multi-source".format(
                    source_id, upstream, kind))
        seen_upstreams.add(upstream_key)
        _require_str(raw["purpose"], "purpose")
        approval = _validate_approval(raw["approval"])
        body_retention = raw["body_retention_allowed"]
        if not isinstance(body_retention, bool):
            raise SourceRegistryError("body_retention_allowed must be boolean")
        external_send = raw["llm_external_send_allowed"]
        if not isinstance(external_send, bool):
            raise SourceRegistryError("llm_external_send_allowed must be boolean")
        if external_send and approval["state"] != "approved":
            raise SourceRegistryError(
                "source_id {0}: llm_external_send_allowed requires approval.state 'approved' "
                "by a real human; developers may not enable external send".format(source_id))
        timeliness = raw["timeliness"]
        if not isinstance(timeliness, dict) or set(timeliness) != TIMELINESS_FIELDS:
            raise SourceRegistryError(
                "source_id {0}: timeliness must have exactly {1}".format(
                    source_id, sorted(TIMELINESS_FIELDS)))
        _require_positive_int(
            timeliness["expected_max_delay_minutes"], "timeliness.expected_max_delay_minutes")
        if timeliness["staleness_policy"] not in STALENESS_POLICIES:
            raise SourceRegistryError(
                "source_id {0}: staleness_policy must be one of {1}".format(
                    source_id, sorted(STALENESS_POLICIES)))
        rate_limit = raw["rate_limit"]
        if not isinstance(rate_limit, dict) or set(rate_limit) != RATE_LIMIT_FIELDS:
            raise SourceRegistryError(
                "source_id {0}: rate_limit must have exactly {1}".format(
                    source_id, sorted(RATE_LIMIT_FIELDS)))
        _require_positive_int(rate_limit["max_requests_per_minute"],
                              "rate_limit.max_requests_per_minute")
        _require_positive_int(rate_limit["max_requests_per_day"],
                              "rate_limit.max_requests_per_day")
        _parse_expiry(_require_str(raw["expires_at"], "expires_at"))
        entries.append(dict(raw))
    return entries


def load_source_registry(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw = (config or {}).get("source_registry") or {}
    path_value = str(raw.get("path") or DEFAULT_REGISTRY_PATH)
    path = project_path(path_value)
    if not path.exists():
        if not (config or {}).get("source_registry"):
            # No registry configured: nothing is registered, nothing approved.
            return {"schema_version": SCHEMA_VERSION, "entries": {}, "path": path_value}
        raise SourceRegistryError(
            "source registry configured but missing: {0}".format(path_value))
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceRegistryError("source registry unreadable: {0}".format(exc)) from exc
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise SourceRegistryError(
            "source registry schema_version must be {0!r}".format(SCHEMA_VERSION))
    entries = validate_registry_entries(document.get("entries"))
    return {
        "schema_version": SCHEMA_VERSION,
        "entries": {entry["source_id"]: entry for entry in entries},
        "path": path_value,
    }


def _is_expired(entry: Dict[str, Any], today: Optional[date] = None) -> bool:
    return _parse_expiry(entry["expires_at"]) < (today or date.today())


def registry_status(registry: Dict[str, Any], source_name: str) -> Dict[str, Any]:
    entry = (registry.get("entries") or {}).get(source_name)
    if entry is None:
        return {
            "registered": False, "expired": False,
            "approval_state": "not_registered", "reason": "not_registered",
            "external_send_allowed": False,
        }
    expired = _is_expired(entry)
    external_send_allowed = (
        entry["llm_external_send_allowed"]
        and entry["approval"]["state"] == "approved"
        and not expired
    )
    return {
        "registered": True,
        "expired": expired,
        "approval_state": entry["approval"]["state"],
        "reason": "expired" if expired else "ok",
        "external_send_allowed": external_send_allowed,
        "kind": entry["kind"],
        "upstream": entry["upstream"],
        "body_retention_allowed": entry["body_retention_allowed"],
        "timeliness": dict(entry["timeliness"]),
        "rate_limit": dict(entry["rate_limit"]),
    }


def may_send_to_llm(registry: Dict[str, Any], source_name: str) -> bool:
    return bool(registry_status(registry, source_name)["external_send_allowed"])
