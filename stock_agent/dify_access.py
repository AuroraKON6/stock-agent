"""DF-02: Dify service-credential registry — service auth vs human authorization.

The Dify adoption plan separates service authentication from human
authorization: a Dify app key, an API ``user`` field, or a form-submitter name
is NEVER a project approval identity. This module only decides whether a
CALLER (a Dify workflow holding a backend-issued service token) may invoke a
given operation class — it never decides whether a human decision is valid.

The registry is a JSON file (``config/dify/service_credentials.json`` by
default, path overridable via ``dify.service_credentials_path``) holding ONLY
sha-256 hashes of tokens; plaintext tokens are never written, logged, or
echoed. A missing registry file means an empty registry and every call is
denied (fail-closed, same doctrine as the source registry).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

# Operation classes a service credential may be granted. "submit_feedback"
# covers both human-feedback relay and cancellation requests — both still
# require a backend-issued principal verification at the gateway layer, so
# this scope alone never executes a protected action.
SCOPES = frozenset({
    "create_research",
    "read_research",
    "read_fact_bundle",
    "submit_result",
    "submit_feedback",
})

CREDENTIAL_FIELDS = frozenset({
    "credential_id", "token_sha256", "scopes", "case_scope",
    "active", "created_at", "expires_at", "note",
})

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_CREDENTIALS_PATH = "config/dify/service_credentials.json"


class DifyAccessError(Exception):
    """Authentication/authorization denial for a Dify-facing call.

    ``reason`` is a stable machine-readable code (dify_disabled,
    credential_unknown, credential_inactive, credential_expired,
    scope_not_granted, case_out_of_scope, task_not_found_or_out_of_scope,
    fact_bundle_not_found) so the future HTTP layer can map to status codes
    without parsing prose.
    """

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


def dify_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Extract and validate the ``dify`` config section (fail-closed).

    Missing section = disabled with defaults; unknown keys are rejected so a
    typo cannot silently widen behavior.
    """
    raw = (config or {}).get("dify") or {}
    if not isinstance(raw, dict):
        raise ValueError("dify config must be an object")
    allowed = {"enabled", "service_credentials_path", "approved_workflow_versions", "poll"}
    unexpected = set(raw) - allowed
    if unexpected:
        raise ValueError("unknown dify config fields: {0}".format(sorted(unexpected)))
    enabled = bool(raw.get("enabled", False))
    path = str(raw.get("service_credentials_path") or DEFAULT_CREDENTIALS_PATH)
    versions = raw.get("approved_workflow_versions") or []
    if not isinstance(versions, list) or not all(isinstance(v, str) and v.strip() for v in versions):
        raise ValueError("approved_workflow_versions must be a list of non-empty strings")
    poll_raw = raw.get("poll") or {}
    if not isinstance(poll_raw, dict):
        raise ValueError("dify.poll must be an object")
    poll_unexpected = set(poll_raw) - {"suggest_interval_seconds", "max_polls"}
    if poll_unexpected:
        raise ValueError("unknown dify.poll fields: {0}".format(sorted(poll_unexpected)))
    suggest = poll_raw.get("suggest_interval_seconds", 30)
    max_polls = poll_raw.get("max_polls", 40)
    for value in (suggest, max_polls):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("dify.poll values must be positive integers")
    return {
        "enabled": enabled,
        "service_credentials_path": path,
        "approved_workflow_versions": list(versions),
        "poll": {"suggest_interval_seconds": suggest, "max_polls": max_polls},
    }


def _validate_entry(entry: Any) -> None:
    if not isinstance(entry, dict):
        raise ValueError("credential entry must be an object")
    unexpected = set(entry) - CREDENTIAL_FIELDS
    if unexpected:
        raise ValueError("unknown credential fields: {0}".format(sorted(unexpected)))
    missing = CREDENTIAL_FIELDS - {"expires_at", "note"} - set(entry)
    if missing:
        raise ValueError("credential entry missing fields: {0}".format(sorted(missing)))
    if not str(entry.get("credential_id") or "").strip():
        raise ValueError("credential_id must not be empty")
    digest = str(entry.get("token_sha256") or "")
    if not _SHA256_HEX.match(digest):
        raise ValueError("token_sha256 must be a 64-char lowercase hex digest")
    scopes = entry.get("scopes")
    if not isinstance(scopes, list) or not scopes:
        raise ValueError("scopes must be a non-empty list")
    unknown = set(scopes) - SCOPES
    if unknown:
        raise ValueError("unknown scopes: {0} (allowed: {1})".format(
            sorted(unknown), sorted(SCOPES)))
    case_scope = entry.get("case_scope")
    if not isinstance(case_scope, list) or not case_scope or \
            not all(isinstance(item, str) and item.strip() for item in case_scope):
        raise ValueError("case_scope must be a non-empty list of case ids (or ['*'])")
    if not isinstance(entry.get("active"), bool):
        raise ValueError("active must be a boolean")
    expires_at = entry.get("expires_at", "")
    if expires_at and not str(expires_at).strip():
        raise ValueError("expires_at must be an ISO timestamp or empty")


def load_service_credentials(config: Dict[str, Any]) -> Dict[str, Any]:
    """Load (and strictly validate) the credential registry; missing = empty."""
    settings = dify_config(config)
    from .config import project_path

    path = project_path(settings["service_credentials_path"])
    if not path.exists():
        return {"schema_version": "1.0", "credentials": [], "path": settings["service_credentials_path"]}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("service credential registry unreadable: {0}".format(exc)) from exc
    if not isinstance(document, dict):
        raise ValueError("service credential registry must be a JSON object")
    unexpected = set(document) - {"schema_version", "credentials"}
    if unexpected:
        raise ValueError("unknown registry fields: {0}".format(sorted(unexpected)))
    if str(document.get("schema_version") or "") != "1.0":
        raise ValueError("unsupported registry schema_version")
    entries = document.get("credentials")
    if not isinstance(entries, list):
        raise ValueError("credentials must be a list")
    seen_ids = set()
    for entry in entries:
        _validate_entry(entry)
        credential_id = str(entry["credential_id"])
        if credential_id in seen_ids:
            raise ValueError("duplicate credential_id: {0}".format(credential_id))
        seen_ids.add(credential_id)
    return {"schema_version": "1.0", "credentials": entries,
            "path": settings["service_credentials_path"]}


def is_credential_expired(entry: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    expires_at = str((entry or {}).get("expires_at") or "")
    if not expires_at:
        return False
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    if expiry.tzinfo is None:
        expiry = expiry.astimezone()
    return expiry <= (now or datetime.now().astimezone())


def _is_expired(entry: Dict[str, Any], now: Optional[datetime]) -> bool:
    return is_credential_expired(entry, now)


def verify_service_credential(registry: Dict[str, Any], presented_token: str,
                              *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Verify a presented service token against the registry (timing-safe).

    Returns the caller identity {credential_id, scopes, case_scope} or raises
    DifyAccessError with a stable reason code. Only hashes are compared; the
    plaintext token is never stored, logged, or echoed.
    """
    presented = str(presented_token or "")
    if not presented:
        raise DifyAccessError("credential_unknown", "no service token presented")
    digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    matched: Optional[Dict[str, Any]] = None
    for entry in (registry or {}).get("credentials", []):
        # Compare every entry's digest so verification time does not leak
        # which credential matched.
        if hmac.compare_digest(str(entry.get("token_sha256") or ""), digest):
            matched = entry
            # keep scanning: uniform timing over the full registry
    if matched is None:
        raise DifyAccessError("credential_unknown", "service token does not match any credential")
    if not matched.get("active"):
        raise DifyAccessError("credential_inactive", str(matched.get("credential_id")))
    if _is_expired(matched, now):
        raise DifyAccessError("credential_expired", str(matched.get("credential_id")))
    return {
        "credential_id": str(matched["credential_id"]),
        "scopes": list(matched["scopes"]),
        "case_scope": list(matched["case_scope"]),
    }


def ensure_scope(identity: Dict[str, Any], scope: str) -> None:
    if scope not in SCOPES:
        raise ValueError("unknown scope: {0}".format(scope))
    if scope not in set(identity.get("scopes") or []):
        raise DifyAccessError("scope_not_granted",
                              "credential {0} lacks scope {1}".format(
                                  identity.get("credential_id"), scope))


def ensure_case_scope(identity: Dict[str, Any], case_id: str) -> None:
    scope: List[str] = list(identity.get("case_scope") or [])
    if "*" not in scope and str(case_id or "") not in scope:
        raise DifyAccessError("case_out_of_scope",
                              "credential {0} may not touch case {1}".format(
                                  identity.get("credential_id"), case_id))
