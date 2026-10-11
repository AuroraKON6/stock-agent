from __future__ import annotations

import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import socket
import tempfile
from collections import Counter
from datetime import datetime
from email.message import Message
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .audit_log import (
    append_hash_chained_jsonl,
    atomic_write_text,
    file_hash,
    load_hash_chained_jsonl,
    object_hash,
)
from .config import project_path
from .models import AnalyzedSignal, now_iso
from .research_operations import load_operations_inputs


EVIDENCE_VAULT_VERSION = "28.0"
SAFETY_BOUNDARY = (
    "The vault retains public research evidence only. It does not approve research, "
    "license a data source, publish a report, or authorize any trading action."
)

DEFAULT_OFFICIAL_DOMAINS = {
    "bse.cn",
    "cninfo.com.cn",
    "csrc.gov.cn",
    "sac.net.cn",
    "sse.com.cn",
    "szse.cn",
}
DEFAULT_CONTENT_TYPES = {
    "application/json",
    "application/pdf",
    "application/xml",
    "text/html",
    "text/plain",
    "text/xml",
}
DEFAULT_LLM_REQUIRED_SOURCE_TYPES = {"announcement", "filing", "financial"}
CAPTURE_ROLES = {"information_analyst", "fundamental_analyst", "lead_analyst", "research_coordinator"}
ARCHIVE_REVIEW_ROLES = {"compliance_officer", "information_security_officer"}
ARCHIVE_ACTIVATION_ROLES = {"data_steward", "research_coordinator"}
ARCHIVE_REVOCATION_ROLES = ARCHIVE_REVIEW_ROLES | ARCHIVE_ACTIVATION_ROLES
LOCAL_SHADOW_BACKENDS = {"local_content_addressed_shadow", "local_append_only_shadow", "local"}
EVENT_TYPES = {
    "evidence_captured",
    "evidence_quarantined",
    "evidence_linked",
    "archive_review_recorded",
    "archive_activated",
    "archive_revoked",
}
SECRET_PATTERN = re.compile(r"\b(?:sk|ak)-[A-Za-z0-9_-]{12,}\b", re.IGNORECASE)
CHALLENGE_PATTERNS = (
    re.compile(r"verify\s+(?:that\s+)?you\s+are\s+human", re.IGNORECASE),
    re.compile(r"access\s+denied", re.IGNORECASE),
    re.compile(r"captcha", re.IGNORECASE),
    re.compile(r"(?:安全|人机|滑块)验证"),
    re.compile(r"访问(?:过于)?频繁"),
)


class EvidenceVaultError(ValueError):
    pass


def evidence_vault_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw = (config or {}).get("official_evidence_vault", {}) if isinstance(config, dict) else {}
    archive = raw.get("production_archive", {}) if isinstance(raw.get("production_archive"), dict) else {}
    allowed_domains = _clean_lower_list(raw.get("allowed_official_domains", sorted(DEFAULT_OFFICIAL_DOMAINS)))
    allowed_content_types = _clean_lower_list(raw.get("allowed_content_types", sorted(DEFAULT_CONTENT_TYPES)))
    llm_required = _clean_lower_list(
        raw.get("require_retained_before_external_llm_for", sorted(DEFAULT_LLM_REQUIRED_SOURCE_TYPES))
    )
    return {
        "enabled": bool(raw.get("enabled", False)),
        "enforce": bool(raw.get("enforce", False)),
        "events_path": str(raw.get("events_path") or "data/evidence_vault/events.jsonl"),
        "index_path": str(raw.get("index_path") or "data/evidence_vault/index.json"),
        "objects_dir": str(raw.get("objects_dir") or "data/evidence_vault/objects/sha256"),
        "quarantine_dir": str(raw.get("quarantine_dir") or "data/evidence_vault/quarantine/sha256"),
        "report_dir": str(raw.get("report_dir") or "reports/evidence_vault"),
        "allowed_official_domains": allowed_domains or sorted(DEFAULT_OFFICIAL_DOMAINS),
        "allowed_content_types": allowed_content_types or sorted(DEFAULT_CONTENT_TYPES),
        "require_https": bool(raw.get("require_https", True)),
        "resolve_public_ip_on_fetch": bool(raw.get("resolve_public_ip_on_fetch", True)),
        "allow_network_fetch": bool(raw.get("allow_network_fetch", False)),
        "auto_capture_official_signals": bool(raw.get("auto_capture_official_signals", False)),
        "max_auto_captures_per_run": max(0, min(20, int(raw.get("max_auto_captures_per_run", 3) or 0))),
        "max_document_bytes": max(1024, min(100 * 1024 * 1024, int(raw.get("max_document_bytes", 25 * 1024 * 1024) or 0))),
        "reject_challenge_pages": bool(raw.get("reject_challenge_pages", True)),
        "require_published_at_for_llm": bool(raw.get("require_published_at_for_llm", True)),
        "require_retained_before_external_llm_for": llm_required,
        "retention_years": max(5, int(raw.get("retention_years", 5) or 5)),
        "capture_actor_id": str(raw.get("capture_actor_id") or "stock_agent_source_collector"),
        "capture_actor_role": str(raw.get("capture_actor_role") or "information_analyst"),
        "automatic_approval": bool(raw.get("automatic_approval", False)),
        "automatic_publication": bool(raw.get("automatic_publication", False)),
        "automatic_trading": bool(raw.get("automatic_trading", False)),
        "archive_backend": str(archive.get("backend") or "local_content_addressed_shadow"),
        "archive_evidence_ref": str(archive.get("evidence_ref") or ""),
        "archive_external_immutability_verified": bool(archive.get("external_immutability_verified", False)),
        "archive_required_review_roles": _clean_lower_list(
            archive.get("required_review_roles", sorted(ARCHIVE_REVIEW_ROLES))
        ) or sorted(ARCHIVE_REVIEW_ROLES),
        "archive_activation_roles": _clean_lower_list(
            archive.get("activation_roles", sorted(ARCHIVE_ACTIVATION_ROLES))
        ) or sorted(ARCHIVE_ACTIVATION_ROLES),
    }


def validate_evidence_vault_policy(config: Optional[Dict[str, Any]] = None) -> List[str]:
    cfg = evidence_vault_config(config)
    if not cfg["enabled"]:
        return []
    errors: List[str] = []
    if cfg["enforce"]:
        if not cfg["require_https"]:
            errors.append("official evidence capture must require HTTPS")
        unknown_domains = sorted(set(cfg["allowed_official_domains"]).difference(DEFAULT_OFFICIAL_DOMAINS))
        if unknown_domains:
            errors.append("official domain allowlist contains unreviewed domains: {0}".format(", ".join(unknown_domains)))
        missing_types = sorted(DEFAULT_LLM_REQUIRED_SOURCE_TYPES.difference(cfg["require_retained_before_external_llm_for"]))
        if missing_types:
            errors.append("retained evidence must gate external LLM for: {0}".format(", ".join(missing_types)))
        if cfg["automatic_approval"] or cfg["automatic_publication"] or cfg["automatic_trading"]:
            errors.append("automatic approval, publication, and trading must remain disabled")
    if cfg["auto_capture_official_signals"] and not cfg["allow_network_fetch"]:
        errors.append("auto capture requires explicitly enabled network fetch")
    if cfg["retention_years"] < 5:
        errors.append("official evidence retention must be at least five years")
    required_roles = set(cfg["archive_required_review_roles"])
    if not ARCHIVE_REVIEW_ROLES.issubset(required_roles):
        errors.append("production archive requires compliance and information-security review")
    if cfg["archive_external_immutability_verified"] and not cfg["archive_evidence_ref"]:
        errors.append("external archive immutability verification requires retained evidence")
    return errors


def import_official_document(
    config: Dict[str, Any],
    file_path: str,
    *,
    source_url: str,
    title: str,
    published_at: str,
    actor: str,
    actor_role: str,
    signal_ids: Optional[Sequence[str]] = None,
    issuer: str = "",
    symbol: str = "",
    source_kind: str = "company_disclosure",
    content_type: str = "",
) -> Dict[str, Any]:
    cfg = evidence_vault_config(config)
    _require_enabled(cfg)
    actor_record = _authorize_capture_actor(config, actor, actor_role)
    path = Path(file_path).expanduser()
    if path.is_symlink():
        raise EvidenceVaultError("symbolic-link imports are not accepted")
    if not path.is_file():
        raise EvidenceVaultError("evidence file does not exist: {0}".format(path))
    before = path.stat()
    if before.st_size > cfg["max_document_bytes"]:
        raise EvidenceVaultError("evidence file exceeds configured size limit")
    data = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise EvidenceVaultError("evidence file changed while it was being read")
    guessed = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return ingest_official_document_bytes(
        config,
        data,
        source_url=source_url,
        title=title,
        published_at=published_at,
        actor=actor,
        actor_role=actor_role,
        signal_ids=signal_ids,
        issuer=issuer,
        symbol=symbol,
        source_kind=source_kind,
        content_type=guessed,
        original_filename=path.name,
        capture_mode="manual_import",
        source_binding_status="operator_attested",
    )


def fetch_official_document(
    config: Dict[str, Any],
    *,
    source_url: str,
    title: str,
    published_at: str,
    actor: str,
    actor_role: str,
    signal_ids: Optional[Sequence[str]] = None,
    issuer: str = "",
    symbol: str = "",
    source_kind: str = "company_disclosure",
    opener: Optional[Any] = None,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> Dict[str, Any]:
    cfg = evidence_vault_config(config)
    _require_enabled(cfg)
    if not cfg["allow_network_fetch"]:
        raise EvidenceVaultError("network evidence capture is disabled; use controlled manual import")
    actor_record = _authorize_capture_actor(config, actor, actor_role)
    normalized_url = validate_official_url(source_url, cfg, resolver=resolver)
    redirect_handler = _CheckedRedirectHandler(cfg, resolver)
    active_opener = opener or build_opener(redirect_handler)
    request = Request(
        normalized_url,
        headers={
            "Accept": "application/pdf,application/json,application/xml,text/html,text/plain;q=0.9,*/*;q=0.1",
            "User-Agent": "StockAgentEvidenceVault/28.0 (+controlled-public-research-capture)",
        },
        method="GET",
    )
    response = active_opener.open(request, timeout=30)
    try:
        final_url = validate_official_url(str(response.geturl() or normalized_url), cfg, resolver=resolver)
        headers = response.headers
        declared_length = _integer_header(headers, "Content-Length")
        if declared_length is not None and declared_length > cfg["max_document_bytes"]:
            raise EvidenceVaultError("remote evidence exceeds configured size limit")
        data = response.read(cfg["max_document_bytes"] + 1)
        if len(data) > cfg["max_document_bytes"]:
            raise EvidenceVaultError("remote evidence exceeds configured size limit")
        content_type = _header_content_type(headers)
        original_filename = _response_filename(headers, final_url)
        transport = {
            "http_status": int(getattr(response, "status", 200) or 200),
            "requested_url": normalized_url,
            "final_url": final_url,
            "etag": _safe_header(headers, "ETag"),
            "last_modified": _safe_header(headers, "Last-Modified"),
            "content_length": declared_length,
        }
    finally:
        response.close()
    return ingest_official_document_bytes(
        config,
        data,
        source_url=final_url,
        title=title,
        published_at=published_at,
        actor=actor,
        actor_role=actor_role,
        signal_ids=signal_ids,
        issuer=issuer,
        symbol=symbol,
        source_kind=source_kind,
        content_type=content_type,
        original_filename=original_filename,
        capture_mode="network_fetch",
        source_binding_status="https_transport_verified",
        transport=transport,
    )


def ingest_official_document_bytes(
    config: Dict[str, Any],
    data: bytes,
    *,
    source_url: str,
    title: str,
    published_at: str,
    actor: str,
    actor_role: str,
    signal_ids: Optional[Sequence[str]] = None,
    issuer: str = "",
    symbol: str = "",
    source_kind: str = "company_disclosure",
    content_type: str = "application/octet-stream",
    original_filename: str = "",
    capture_mode: str = "manual_import",
    source_binding_status: str = "operator_attested",
    transport: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = evidence_vault_config(config)
    _require_enabled(cfg)
    actor_record = _authorize_capture_actor(config, actor, actor_role)
    policy_errors = validate_evidence_vault_policy(config)
    if policy_errors:
        raise EvidenceVaultError("evidence vault policy is unsafe: {0}".format("; ".join(policy_errors)))
    normalized_url = validate_official_url(source_url, cfg, resolve_dns=False)
    if not data:
        raise EvidenceVaultError("evidence document is empty")
    if len(data) > cfg["max_document_bytes"]:
        raise EvidenceVaultError("evidence document exceeds configured size limit")
    if not str(title or "").strip():
        raise EvidenceVaultError("evidence title is required")
    effective_type, type_warnings = _effective_content_type(data, content_type)
    quarantine_reasons: List[str] = []
    if effective_type not in set(cfg["allowed_content_types"]):
        quarantine_reasons.append("content type is not approved: {0}".format(effective_type))
    if cfg["reject_challenge_pages"] and effective_type == "text/html" and _looks_like_challenge_page(data):
        quarantine_reasons.append("remote response appears to be an access-control or challenge page")
    published_valid = _valid_datetime(published_at)
    digest = hashlib.sha256(data).hexdigest()
    document_id = "document:sha256:{0}".format(digest)
    status = "quarantined" if quarantine_reasons else "captured"
    object_root = cfg["quarantine_dir"] if quarantine_reasons else cfg["objects_dir"]
    object_path = _content_addressed_path(object_root, digest)
    _atomic_write_binary_once(object_path, data, digest)
    retain_until = _retain_until(now_iso(), cfg["retention_years"])
    event = {
        "schema_version": EVIDENCE_VAULT_VERSION,
        "event_type": "evidence_quarantined" if quarantine_reasons else "evidence_captured",
        "occurred_at": now_iso(),
        "actor": str(actor or "").strip(),
        "actor_role": str(actor_role or "").strip(),
        "principal_type": str(actor_record.get("principal_type") or "unknown"),
        "document_id": document_id,
        "payload": {
            "status": status,
            "sha256": digest,
            "byte_size": len(data),
            "content_type": effective_type,
            "declared_content_type": _normalize_content_type(content_type),
            "original_filename": _safe_filename(original_filename),
            "object_locator": _object_locator(digest, quarantine=bool(quarantine_reasons)),
            "object_path": _relative_project_path(object_path),
            "source_url": normalized_url,
            "source_domain": _url_domain(normalized_url),
            "source_kind": str(source_kind or "official_public_document"),
            "title": str(title).strip(),
            "published_at": str(published_at or "").strip(),
            "published_at_valid": published_valid,
            "captured_at": now_iso(),
            "capture_mode": capture_mode,
            "source_binding_status": source_binding_status,
            "issuer": str(issuer or "").strip(),
            "symbol": str(symbol or "").strip(),
            "signal_ids": _clean_list(signal_ids or []),
            "retention_years": cfg["retention_years"],
            "retain_until": retain_until,
            "type_warnings": type_warnings,
            "quarantine_reasons": quarantine_reasons,
            "transport": _safe_transport(transport or {}),
            "malware_scan_status": "not_configured_raw_bytes_never_sent_to_llm",
            "automatic_approval_executed": False,
            "automatic_publication_executed": False,
            "automatic_trading_executed": False,
        },
        "safety_boundary": SAFETY_BOUNDARY,
    }
    existing = load_evidence_vault(config)
    duplicate = _matching_capture(existing, event)
    if duplicate:
        return {
            "duplicate": True,
            "record": duplicate,
            "document": (existing.get("documents") or {}).get(document_id, {}),
            "vault": existing,
            **write_evidence_vault_reports(existing, config),
        }
    record = append_hash_chained_jsonl(project_path(cfg["events_path"]), event)
    vault = load_evidence_vault(config)
    paths = write_evidence_vault_reports(vault, config)
    return {
        "duplicate": False,
        "record": record,
        "document": (vault.get("documents") or {}).get(document_id, {}),
        "vault": vault,
        **paths,
    }


def link_evidence_to_signal(
    config: Dict[str, Any],
    *,
    document_id: str,
    signal_id: str,
    actor: str,
    actor_role: str,
    notes: str = "",
) -> Dict[str, Any]:
    cfg = evidence_vault_config(config)
    _require_enabled(cfg)
    actor_record = _authorize_capture_actor(config, actor, actor_role)
    vault = load_evidence_vault(config)
    document = (vault.get("documents") or {}).get(str(document_id or ""))
    if not document:
        raise EvidenceVaultError("evidence document not found")
    signal_id = str(signal_id or "").strip()
    if not signal_id:
        raise EvidenceVaultError("signal_id is required")
    if signal_id in set(document.get("signal_ids") or []):
        return {"duplicate": True, "document": document, "vault": vault}
    record = append_hash_chained_jsonl(project_path(cfg["events_path"]), {
        "schema_version": EVIDENCE_VAULT_VERSION,
        "event_type": "evidence_linked",
        "occurred_at": now_iso(),
        "actor": actor,
        "actor_role": actor_role,
        "principal_type": actor_record.get("principal_type", "unknown"),
        "document_id": document_id,
        "payload": {"signal_id": signal_id, "notes": str(notes or "")},
        "safety_boundary": SAFETY_BOUNDARY,
    })
    updated = load_evidence_vault(config)
    return {
        "duplicate": False,
        "record": record,
        "document": (updated.get("documents") or {}).get(document_id, {}),
        "vault": updated,
        **write_evidence_vault_reports(updated, config),
    }


def record_archive_review(
    config: Dict[str, Any],
    *,
    decision: str,
    actor: str,
    actor_role: str,
    notes: str,
    evidence_refs: Sequence[str],
) -> Dict[str, Any]:
    cfg = evidence_vault_config(config)
    _require_enabled(cfg)
    decision = str(decision or "").strip().lower()
    if decision not in {"approve", "reject"}:
        raise EvidenceVaultError("archive review decision must be approve or reject")
    if actor_role not in set(cfg["archive_required_review_roles"]):
        raise EvidenceVaultError("actor role is not a required archive reviewer")
    principal = _authorize_human_role(config, actor, actor_role)
    refs = _safe_references(evidence_refs)
    if not str(notes or "").strip() or not refs:
        raise EvidenceVaultError("archive review requires notes and retained evidence references")
    vault = load_evidence_vault(config)
    current_reviews = ((vault.get("archive_control") or {}).get("reviews") or {})
    for role, review in current_reviews.items():
        if role != actor_role and review.get("decision") == "approve" and review.get("actor") == actor:
            raise EvidenceVaultError("archive review roles must use distinct human actors")
    record = append_hash_chained_jsonl(project_path(cfg["events_path"]), {
        "schema_version": EVIDENCE_VAULT_VERSION,
        "event_type": "archive_review_recorded",
        "occurred_at": now_iso(),
        "actor": actor,
        "actor_role": actor_role,
        "principal_type": principal.get("principal_type", "human"),
        "payload": {
            "archive_policy_fingerprint": archive_policy_fingerprint(config),
            "decision": decision,
            "notes": str(notes).strip(),
            "evidence_refs": refs,
        },
        "safety_boundary": SAFETY_BOUNDARY,
    })
    updated = load_evidence_vault(config)
    return {"record": record, "vault": updated, **write_evidence_vault_reports(updated, config)}


def activate_production_archive(
    config: Dict[str, Any],
    *,
    actor: str,
    actor_role: str,
    notes: str,
) -> Dict[str, Any]:
    cfg = evidence_vault_config(config)
    _require_enabled(cfg)
    if actor_role not in set(cfg["archive_activation_roles"]):
        raise EvidenceVaultError("actor role is not allowed to activate the production archive")
    principal = _authorize_human_role(config, actor, actor_role)
    vault = load_evidence_vault(config)
    control = vault.get("archive_control") or {}
    if cfg["archive_backend"] in LOCAL_SHADOW_BACKENDS:
        raise EvidenceVaultError("local content-addressed storage cannot be activated as a production archive")
    if not cfg["archive_evidence_ref"] or not cfg["archive_external_immutability_verified"]:
        raise EvidenceVaultError("external immutable archive evidence is incomplete")
    missing = list(control.get("missing_approved_review_roles") or [])
    if missing:
        raise EvidenceVaultError("archive reviews are incomplete: {0}".format(", ".join(missing)))
    review_actors = {str(item.get("actor") or "") for item in (control.get("reviews") or {}).values()}
    if actor in review_actors:
        raise EvidenceVaultError("archive activator must be independent from archive reviewers")
    if not str(notes or "").strip():
        raise EvidenceVaultError("archive activation notes are required")
    record = append_hash_chained_jsonl(project_path(cfg["events_path"]), {
        "schema_version": EVIDENCE_VAULT_VERSION,
        "event_type": "archive_activated",
        "occurred_at": now_iso(),
        "actor": actor,
        "actor_role": actor_role,
        "principal_type": principal.get("principal_type", "human"),
        "payload": {
            "archive_policy_fingerprint": archive_policy_fingerprint(config),
            "notes": str(notes).strip(),
            "archive_evidence_ref": cfg["archive_evidence_ref"],
        },
        "safety_boundary": SAFETY_BOUNDARY,
    })
    updated = load_evidence_vault(config)
    return {"record": record, "vault": updated, **write_evidence_vault_reports(updated, config)}


def revoke_production_archive(
    config: Dict[str, Any],
    *,
    actor: str,
    actor_role: str,
    notes: str,
) -> Dict[str, Any]:
    cfg = evidence_vault_config(config)
    _require_enabled(cfg)
    if actor_role not in ARCHIVE_REVOCATION_ROLES:
        raise EvidenceVaultError("actor role is not allowed to revoke the production archive")
    principal = _authorize_human_role(config, actor, actor_role)
    if not str(notes or "").strip():
        raise EvidenceVaultError("archive revocation notes are required")
    record = append_hash_chained_jsonl(project_path(cfg["events_path"]), {
        "schema_version": EVIDENCE_VAULT_VERSION,
        "event_type": "archive_revoked",
        "occurred_at": now_iso(),
        "actor": actor,
        "actor_role": actor_role,
        "principal_type": principal.get("principal_type", "human"),
        "payload": {
            "archive_policy_fingerprint": archive_policy_fingerprint(config),
            "notes": str(notes).strip(),
        },
        "safety_boundary": SAFETY_BOUNDARY,
    })
    updated = load_evidence_vault(config)
    return {"record": record, "vault": updated, **write_evidence_vault_reports(updated, config)}


def archive_policy_fingerprint(config: Dict[str, Any]) -> str:
    cfg = evidence_vault_config(config)
    return object_hash({
        "version": EVIDENCE_VAULT_VERSION,
        "archive_backend": cfg["archive_backend"],
        "archive_evidence_ref": cfg["archive_evidence_ref"],
        "archive_external_immutability_verified": cfg["archive_external_immutability_verified"],
        "archive_required_review_roles": cfg["archive_required_review_roles"],
        "archive_activation_roles": cfg["archive_activation_roles"],
        "retention_years": cfg["retention_years"],
        "objects_dir": cfg["objects_dir"],
    })


def load_evidence_vault(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = evidence_vault_config(config)
    if not cfg["enabled"]:
        return _disabled_vault(cfg)
    chain = load_hash_chained_jsonl(project_path(cfg["events_path"]))
    documents: Dict[str, Dict[str, Any]] = {}
    archive_events: List[Dict[str, Any]] = []
    projection_errors: List[str] = []
    for event in chain.get("records", []):
        event_type = str(event.get("event_type") or "")
        if event_type not in EVENT_TYPES:
            projection_errors.append("unsupported event type: {0}".format(event_type))
            continue
        if event_type in {"archive_review_recorded", "archive_activated", "archive_revoked"}:
            archive_events.append(event)
            continue
        document_id = str(event.get("document_id") or "")
        if not document_id:
            projection_errors.append("evidence event lacks document_id")
            continue
        if event_type in {"evidence_captured", "evidence_quarantined"}:
            payload = dict(event.get("payload") or {})
            supplied_hash = str(payload.get("sha256") or "")
            if document_id != "document:sha256:{0}".format(supplied_hash):
                projection_errors.append("document ID does not match content hash: {0}".format(document_id))
                continue
            current = documents.setdefault(document_id, {
                "document_id": document_id,
                "sha256": supplied_hash,
                "source_urls": [],
                "signal_ids": [],
                "capture_history": [],
            })
            prior_urls = list(current.get("source_urls") or [])
            prior_signals = list(current.get("signal_ids") or [])
            prior_history = list(current.get("capture_history") or [])
            current.update(payload)
            current["source_urls"] = _clean_list(prior_urls + [payload.get("source_url")])
            current["signal_ids"] = _clean_list(prior_signals + list(payload.get("signal_ids") or []))
            current["capture_history"] = prior_history + [{
                "event_id": event.get("event_id", ""),
                "occurred_at": event.get("occurred_at", ""),
                "actor": event.get("actor", ""),
                "actor_role": event.get("actor_role", ""),
                "capture_mode": payload.get("capture_mode", ""),
                "source_url": payload.get("source_url", ""),
            }]
            current["last_event_id"] = event.get("event_id", "")
            current["last_event_hash"] = event.get("event_hash", "")
        elif event_type == "evidence_linked":
            current = documents.get(document_id)
            if not current:
                projection_errors.append("link references unknown document: {0}".format(document_id))
                continue
            signal_id = str((event.get("payload") or {}).get("signal_id") or "").strip()
            if signal_id:
                current["signal_ids"] = _clean_list(list(current.get("signal_ids") or []) + [signal_id])
    archive_control = _project_archive_control(archive_events, config or {}, cfg)
    status_counts = Counter(str(item.get("status") or "unknown") for item in documents.values())
    domain_counts = Counter(str(item.get("source_domain") or "unknown") for item in documents.values())
    signal_links = {
        signal_id: sorted(document_id for document_id, item in documents.items() if signal_id in set(item.get("signal_ids") or []))
        for signal_id in sorted({signal for item in documents.values() for signal in (item.get("signal_ids") or [])})
    }
    url_index: Dict[str, List[str]] = {}
    for document_id, document in documents.items():
        for url in document.get("source_urls") or []:
            key = _normalize_url_for_match(str(url or ""))
            if key:
                url_index.setdefault(key, []).append(document_id)
    for key in list(url_index):
        url_index[key] = sorted(set(url_index[key]))
    vault = {
        "version": EVIDENCE_VAULT_VERSION,
        "generated_at": now_iso(),
        "enabled": True,
        "integrity_status": "FAIL" if chain.get("integrity_status") != "OK" or projection_errors else "OK",
        "event_count": chain.get("record_count", 0),
        "last_event_hash": chain.get("last_hash", ""),
        "event_errors": chain.get("errors", []),
        "projection_errors": projection_errors,
        "documents": dict(sorted(documents.items())),
        "signal_links": signal_links,
        "url_index": url_index,
        "archive_control": archive_control,
        "summary": {
            "document_count": len(documents),
            "captured_count": status_counts.get("captured", 0),
            "quarantined_count": status_counts.get("quarantined", 0),
            "linked_signal_count": len(signal_links),
            "source_domain_counts": dict(sorted(domain_counts.items())),
            "production_archive_ready": archive_control.get("ready", False),
        },
        "policy": _policy_projection(cfg),
        "safety_boundary": SAFETY_BOUNDARY,
    }
    return vault


def verify_evidence_vault(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = evidence_vault_config(config)
    if not cfg["enabled"]:
        return {
            "version": EVIDENCE_VAULT_VERSION,
            "enabled": False,
            "technical_status": "DISABLED",
            "business_status": "DISABLED",
            "integrity_status": "DISABLED",
            "document_count": 0,
            "event_count": 0,
            "production_archive_ready": False,
            "errors": [],
            "business_blockers": [],
            "warnings": [],
            "safety_boundary": SAFETY_BOUNDARY,
        }
    vault = load_evidence_vault(config)
    errors = list(validate_evidence_vault_policy(config))
    warnings: List[str] = []
    blockers: List[str] = []
    if vault.get("integrity_status") != "OK":
        errors.append("evidence event chain or projection integrity failed")
    verified = 0
    missing = 0
    tampered = 0
    for document in (vault.get("documents") or {}).values():
        path = project_path(str(document.get("object_path") or ""))
        if not path.is_file():
            errors.append("evidence object is missing: {0}".format(document.get("document_id", "")))
            missing += 1
            continue
        if file_hash(path) != str(document.get("sha256") or ""):
            errors.append("evidence object hash mismatch: {0}".format(document.get("document_id", "")))
            tampered += 1
            continue
        if path.stat().st_size != int(document.get("byte_size", -1) or -1):
            errors.append("evidence object size mismatch: {0}".format(document.get("document_id", "")))
            tampered += 1
            continue
        verified += 1
        if document.get("status") == "quarantined":
            blockers.append("quarantined evidence requires controlled replacement: {0}".format(document.get("document_id", "")))
        if not document.get("published_at_valid"):
            blockers.append("evidence lacks a valid publication timestamp: {0}".format(document.get("document_id", "")))
    archive_control = vault.get("archive_control") or {}
    if not archive_control.get("ready"):
        blockers.extend(archive_control.get("blockers") or [])
    if not (vault.get("summary") or {}).get("document_count"):
        warnings.append("no official source document has been retained yet")
    result = {
        "version": EVIDENCE_VAULT_VERSION,
        "generated_at": now_iso(),
        "enabled": True,
        "technical_status": "FAIL" if errors else "READY",
        "business_status": "BLOCKED" if errors or blockers else "READY",
        "integrity_status": vault.get("integrity_status", "UNKNOWN"),
        "document_count": (vault.get("summary") or {}).get("document_count", 0),
        "captured_count": (vault.get("summary") or {}).get("captured_count", 0),
        "quarantined_count": (vault.get("summary") or {}).get("quarantined_count", 0),
        "event_count": vault.get("event_count", 0),
        "verified_object_count": verified,
        "missing_object_count": missing,
        "tampered_object_count": tampered,
        "production_archive_ready": bool(archive_control.get("ready")),
        "archive_control": archive_control,
        "errors": _unique(errors),
        "business_blockers": _unique(blockers),
        "warnings": _unique(warnings),
        "safety_boundary": SAFETY_BOUNDARY,
    }
    return result


def evidence_status_for_signal(
    signal: Any,
    config: Dict[str, Any],
    vault: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = evidence_vault_config(config)
    source_type = str(getattr(signal, "source_type", "") or "").strip().lower()
    signal_id = str(getattr(signal, "signal_id", "") or "").strip()
    source_url = str(getattr(signal, "url", "") or "").strip()
    required = source_type in set(cfg["require_retained_before_external_llm_for"])
    if not cfg["enabled"]:
        return {
            "version": EVIDENCE_VAULT_VERSION,
            "status": "DISABLED",
            "required_before_external_llm": required,
            "llm_eligible": not (cfg["enforce"] and required),
            "reason": "official evidence vault is disabled",
            "document_ids": [],
        }
    vault = vault or load_evidence_vault(config)
    document_ids = list((vault.get("signal_links") or {}).get(signal_id, []))
    document_ids.extend((vault.get("url_index") or {}).get(_normalize_url_for_match(source_url), []))
    document_ids = sorted(set(document_ids))
    documents = [
        (vault.get("documents") or {}).get(document_id, {})
        for document_id in document_ids
        if (vault.get("documents") or {}).get(document_id)
    ]
    retained = [item for item in documents if item.get("status") == "captured"]
    eligible = [
        item for item in retained
        if item.get("published_at_valid") or not cfg["require_published_at_for_llm"]
    ]
    if not required:
        status = "NOT_REQUIRED"
        reason = "source type does not require retained original evidence before external LLM use"
        llm_eligible = True
    elif eligible and vault.get("integrity_status") == "OK":
        status = "RETAINED_VERIFIED"
        reason = "official original evidence is retained with a verified event-chain locator"
        llm_eligible = True
    elif documents and all(item.get("status") == "quarantined" for item in documents):
        status = "QUARANTINED"
        reason = "only quarantined evidence is linked"
        llm_eligible = False
    elif retained:
        status = "INCOMPLETE_METADATA"
        reason = "retained evidence lacks required publication-time metadata"
        llm_eligible = False
    else:
        status = "MISSING"
        reason = "required official original evidence is not retained"
        llm_eligible = False
    return {
        "version": EVIDENCE_VAULT_VERSION,
        "status": status,
        "required_before_external_llm": required,
        "llm_eligible": bool(llm_eligible),
        "reason": reason,
        "document_ids": [item.get("document_id", "") for item in documents],
        "documents": [
            {
                "document_id": item.get("document_id", ""),
                "sha256": item.get("sha256", ""),
                "source_url": item.get("source_url", ""),
                "source_domain": item.get("source_domain", ""),
                "published_at": item.get("published_at", ""),
                "captured_at": item.get("captured_at", ""),
                "content_type": item.get("content_type", ""),
                "object_locator": item.get("object_locator", ""),
                "source_binding_status": item.get("source_binding_status", ""),
                "status": item.get("status", ""),
            }
            for item in documents
        ],
        "event_chain_integrity": vault.get("integrity_status", "UNKNOWN"),
        "raw_document_sent_to_external_llm": False,
    }


def attach_evidence_vault_status(
    signals: Sequence[AnalyzedSignal],
    config: Dict[str, Any],
    *,
    allow_auto_capture: bool = True,
) -> Tuple[List[AnalyzedSignal], Dict[str, Any]]:
    cfg = evidence_vault_config(config)
    values = list(signals)
    if not cfg["enabled"]:
        return values, {
            "evidence_vault_enabled": False,
            "evidence_vault_required": 0,
            "evidence_vault_llm_eligible": 0,
            "evidence_vault_blocked": 0,
            "evidence_vault_auto_capture_attempted": 0,
            "evidence_vault_auto_capture_succeeded": 0,
        }
    vault = load_evidence_vault(config)
    attempted = 0
    succeeded = 0
    capture_errors: List[Dict[str, str]] = []
    if allow_auto_capture and cfg["auto_capture_official_signals"] and cfg["allow_network_fetch"]:
        for signal in values:
            if attempted >= cfg["max_auto_captures_per_run"]:
                break
            status = evidence_status_for_signal(signal, config, vault)
            if not status.get("required_before_external_llm") or status.get("llm_eligible"):
                continue
            try:
                validate_official_url(signal.url, cfg, resolve_dns=False)
            except EvidenceVaultError:
                continue
            attempted += 1
            try:
                fetch_official_document(
                    config,
                    source_url=signal.url,
                    title=signal.title,
                    published_at=signal.published_at,
                    actor=cfg["capture_actor_id"],
                    actor_role=cfg["capture_actor_role"],
                    signal_ids=[signal.signal_id],
                    issuer="",
                    symbol=(signal.symbols or [""])[0],
                    source_kind=signal.source_type,
                )
                succeeded += 1
                vault = load_evidence_vault(config)
            except Exception as exc:
                capture_errors.append({
                    "signal_id": signal.signal_id,
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                })
    required = 0
    eligible = 0
    blocked = 0
    status_counts: Counter = Counter()
    for signal in values:
        status = evidence_status_for_signal(signal, config, vault)
        signal.payload = dict(signal.payload or {})
        signal.payload["official_evidence_vault"] = status
        status_counts[str(status.get("status") or "UNKNOWN")] += 1
        if status.get("required_before_external_llm"):
            required += 1
            if status.get("llm_eligible"):
                eligible += 1
            else:
                blocked += 1
    verification = verify_evidence_vault(config)
    paths = write_evidence_vault_reports(vault, config)
    return values, {
        "evidence_vault_enabled": True,
        "evidence_vault_required": required,
        "evidence_vault_llm_eligible": eligible,
        "evidence_vault_blocked": blocked,
        "evidence_vault_status_counts": dict(sorted(status_counts.items())),
        "evidence_vault_auto_capture_attempted": attempted,
        "evidence_vault_auto_capture_succeeded": succeeded,
        "evidence_vault_auto_capture_errors": capture_errors,
        "evidence_vault_verification": verification,
        "official_evidence_vault_enabled": True,
        "official_evidence_vault": vault.get("summary", {}),
        "official_evidence_vault_items": list((vault.get("documents") or {}).values()),
        "official_evidence_vault_verification": verification,
        **paths,
    }


def find_retained_evidence_for_signal(
    signal: Dict[str, Any],
    config: Dict[str, Any],
    vault: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    class _SignalView:
        pass

    view = _SignalView()
    view.signal_id = str(signal.get("signal_id") or "")
    view.source_type = str(signal.get("source_type") or "")
    view.url = str(signal.get("url") or "")
    status = evidence_status_for_signal(view, config, vault=vault)
    retained = [item for item in status.get("documents", []) if item.get("status") == "captured"]
    return retained[0] if retained else {}


def write_evidence_vault_reports(vault: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, str]:
    cfg = evidence_vault_config(config)
    if not cfg["enabled"]:
        return {}
    report_dir = project_path(cfg["report_dir"])
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    latest_json = report_dir / "latest.json"
    latest_md = report_dir / "latest.md"
    timestamped_json = report_dir / "evidence_vault_{0}.json".format(stamp)
    timestamped_md = report_dir / "evidence_vault_{0}.md".format(stamp)
    payload = json.dumps(vault, ensure_ascii=False, indent=2, default=str) + "\n"
    markdown = render_evidence_vault_markdown(vault, config)
    for path in (latest_json, timestamped_json, project_path(cfg["index_path"])):
        atomic_write_text(path, payload)
    for path in (latest_md, timestamped_md):
        atomic_write_text(path, markdown, encoding="utf-8-sig")
    return {
        "evidence_vault_path": str(timestamped_md),
        "evidence_vault_json_path": str(timestamped_json),
        "evidence_vault_latest_path": str(latest_md),
        "evidence_vault_latest_json_path": str(latest_json),
    }


def render_evidence_vault_markdown(vault: Dict[str, Any], config: Dict[str, Any]) -> str:
    verification = verify_evidence_vault(config)
    summary = vault.get("summary") or {}
    control = vault.get("archive_control") or {}
    lines = [
        "# Official Evidence Vault",
        "",
        "- generated_at: {0}".format(vault.get("generated_at", "")),
        "- technical_status: {0}".format(verification.get("technical_status", "UNKNOWN")),
        "- business_status: {0}".format(verification.get("business_status", "UNKNOWN")),
        "- event_chain_integrity: {0}".format(vault.get("integrity_status", "UNKNOWN")),
        "- documents / captured / quarantined: {0} / {1} / {2}".format(
            summary.get("document_count", 0), summary.get("captured_count", 0), summary.get("quarantined_count", 0)
        ),
        "- linked_signals: {0}".format(summary.get("linked_signal_count", 0)),
        "- production_archive_ready: {0}".format(str(bool(control.get("ready"))).lower()),
        "- local_store_is_production_archive: false",
        "",
        "## Source inventory",
        "",
        "| Document | Status | Domain | Published | Size | Signals | Binding |",
        "|---|---|---|---|---:|---:|---|",
    ]
    documents = list((vault.get("documents") or {}).values())
    if not documents:
        lines.append("| none | - | - | - | 0 | 0 | - |")
    for item in documents[:100]:
        lines.append("| {0} | {1} | {2} | {3} | {4} | {5} | {6} |".format(
            item.get("document_id", ""), item.get("status", ""), item.get("source_domain", ""),
            item.get("published_at", ""), item.get("byte_size", 0), len(item.get("signal_ids") or []),
            item.get("source_binding_status", ""),
        ))
    lines.extend(["", "## Production archive control", ""])
    lines.append("- backend: {0}".format(control.get("backend", "")))
    lines.append("- policy_fingerprint: {0}".format(control.get("policy_fingerprint", "")))
    lines.append("- approved_reviews: {0}/{1}".format(
        control.get("approved_review_count", 0), control.get("required_review_count", 0)
    ))
    lines.append("- activated: {0}".format(str(bool((control.get("activation") or {}).get("active"))).lower()))
    lines.append("- ready: {0}".format(str(bool(control.get("ready"))).lower()))
    for blocker in control.get("blockers") or []:
        lines.append("- BLOCKED: {0}".format(blocker))
    if verification.get("errors"):
        lines.extend(["", "## Technical errors", ""])
        lines.extend("- {0}".format(item) for item in verification["errors"])
    lines.extend([
        "",
        "## Boundary",
        "",
        "- {0}".format(SAFETY_BOUNDARY),
        "- Raw document bytes are never sent to the external LLM by this module.",
    ])
    return "\n".join(lines).rstrip() + "\n"


def validate_official_url(
    value: str,
    cfg: Dict[str, Any],
    *,
    resolver: Callable[..., Any] = socket.getaddrinfo,
    resolve_dns: Optional[bool] = None,
) -> str:
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise EvidenceVaultError("invalid official evidence URL") from exc
    if cfg["require_https"] and parsed.scheme.lower() != "https":
        raise EvidenceVaultError("official evidence URL must use HTTPS")
    if parsed.scheme.lower() not in {"https"}:
        raise EvidenceVaultError("unsupported evidence URL scheme")
    if parsed.username or parsed.password:
        raise EvidenceVaultError("official evidence URL must not contain user information")
    if port not in {None, 443}:
        raise EvidenceVaultError("official evidence URL must use the default HTTPS port")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or not _host_allowed(host, cfg["allowed_official_domains"]):
        raise EvidenceVaultError("source domain is not on the reviewed official allowlist")
    should_resolve = cfg["resolve_public_ip_on_fetch"] if resolve_dns is None else resolve_dns
    if should_resolve:
        _require_public_dns(host, resolver)
    path = parsed.path or "/"
    return urlunsplit(("https", host, path, parsed.query, ""))


class _CheckedRedirectHandler(HTTPRedirectHandler):
    def __init__(self, cfg: Dict[str, Any], resolver: Callable[..., Any]) -> None:
        super().__init__()
        self.cfg = cfg
        self.resolver = resolver

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        checked = validate_official_url(newurl, self.cfg, resolver=self.resolver)
        return super().redirect_request(req, fp, code, msg, headers, checked)


def _project_archive_control(
    events: Sequence[Dict[str, Any]],
    config: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    fingerprint = archive_policy_fingerprint(config)
    reviews: Dict[str, Dict[str, Any]] = {}
    activation: Dict[str, Any] = {}
    revoked_at = ""
    for event in events:
        payload = event.get("payload") or {}
        if str(payload.get("archive_policy_fingerprint") or "") != fingerprint:
            continue
        event_type = event.get("event_type")
        if event_type == "archive_review_recorded":
            role = str(event.get("actor_role") or "")
            reviews[role] = {
                "decision": payload.get("decision", ""),
                "actor": event.get("actor", ""),
                "actor_role": role,
                "occurred_at": event.get("occurred_at", ""),
                "notes": payload.get("notes", ""),
                "evidence_refs": payload.get("evidence_refs", []),
                "event_id": event.get("event_id", ""),
            }
            activation = {}
        elif event_type == "archive_activated":
            activation = {
                "active": True,
                "actor": event.get("actor", ""),
                "actor_role": event.get("actor_role", ""),
                "occurred_at": event.get("occurred_at", ""),
                "event_id": event.get("event_id", ""),
                "notes": payload.get("notes", ""),
            }
            revoked_at = ""
        elif event_type == "archive_revoked":
            activation = {
                "active": False,
                "revoked_by": event.get("actor", ""),
                "revoked_role": event.get("actor_role", ""),
                "revoked_at": event.get("occurred_at", ""),
                "notes": payload.get("notes", ""),
            }
            revoked_at = str(event.get("occurred_at") or "")
    required = list(cfg["archive_required_review_roles"])
    missing = [role for role in required if (reviews.get(role) or {}).get("decision") != "approve"]
    approved_actors = [
        str((reviews.get(role) or {}).get("actor") or "") for role in required
        if (reviews.get(role) or {}).get("decision") == "approve"
    ]
    distinct = bool(approved_actors) and len(approved_actors) == len(set(approved_actors))
    blockers: List[str] = []
    if cfg["archive_backend"] in LOCAL_SHADOW_BACKENDS:
        blockers.append("local content-addressed storage is not an approved production archive")
    if not cfg["archive_evidence_ref"]:
        blockers.append("production archive contract/control evidence reference is missing")
    if not cfg["archive_external_immutability_verified"]:
        blockers.append("external archive immutability and retention controls are not verified")
    if missing:
        blockers.append("missing current archive reviews: {0}".format(", ".join(missing)))
    if approved_actors and not distinct:
        blockers.append("archive reviewers must be distinct human actors")
    if not activation.get("active"):
        blockers.append("production archive is not manually activated for the current policy fingerprint")
    ready = not blockers
    return {
        "policy_fingerprint": fingerprint,
        "backend": cfg["archive_backend"],
        "evidence_ref": cfg["archive_evidence_ref"],
        "external_immutability_verified": cfg["archive_external_immutability_verified"],
        "required_review_roles": required,
        "required_review_count": len(required),
        "approved_review_count": len(required) - len(missing),
        "missing_approved_review_roles": missing,
        "review_actors_distinct": distinct,
        "reviews": reviews,
        "activation": activation,
        "revoked_at": revoked_at,
        "ready": ready,
        "blockers": blockers,
        "automatic_approval_executed": False,
        "automatic_activation_executed": False,
    }


def _authorize_capture_actor(config: Dict[str, Any], actor: str, actor_role: str) -> Dict[str, Any]:
    actor = str(actor or "").strip()
    actor_role = str(actor_role or "").strip()
    if not actor or actor_role not in CAPTURE_ROLES:
        raise EvidenceVaultError("capture requires an identified actor with an evidence-collection role")
    operations = load_operations_inputs(config)
    if not operations["config"]["enforce_roster"]:
        return {"actor_id": actor, "principal_type": "unverified", "roles": [actor_role], "active": True}
    principal = (operations.get("principal_map") or {}).get(actor)
    if not principal or not principal.get("active") or actor_role not in set(principal.get("roles") or []):
        raise EvidenceVaultError("capture actor is not active in the research roster for the supplied role")
    if principal.get("information_side") != "public":
        raise EvidenceVaultError("official public evidence capture requires a public-side principal")
    return principal


def _authorize_human_role(config: Dict[str, Any], actor: str, actor_role: str) -> Dict[str, Any]:
    operations = load_operations_inputs(config)
    principal = (operations.get("principal_map") or {}).get(str(actor or "").strip())
    if not principal or not principal.get("active"):
        raise EvidenceVaultError("actor is not active in the research roster")
    if principal.get("principal_type") != "human":
        raise EvidenceVaultError("archive control actions require a real human principal")
    if actor_role not in set(principal.get("roles") or []):
        raise EvidenceVaultError("actor does not hold the supplied archive-control role")
    if actor_role == "compliance_officer" and "compliance_authorized" not in set(principal.get("authorizations") or []):
        raise EvidenceVaultError("compliance actor lacks compliance authorization")
    return principal


def _matching_capture(vault: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
    document = (vault.get("documents") or {}).get(event.get("document_id"), {})
    payload = event.get("payload") or {}
    if not document:
        return {}
    existing_urls = {_normalize_url_for_match(value) for value in document.get("source_urls") or []}
    same_url = _normalize_url_for_match(payload.get("source_url")) in existing_urls
    requested_signals = set(payload.get("signal_ids") or [])
    same_signals = requested_signals.issubset(set(document.get("signal_ids") or []))
    same_status = str(document.get("status") or "") == str(payload.get("status") or "")
    if same_url and same_signals and same_status:
        return {"event_id": document.get("last_event_id", ""), "event_hash": document.get("last_event_hash", "")}
    return {}


def _effective_content_type(data: bytes, declared: str) -> Tuple[str, List[str]]:
    normalized = _normalize_content_type(declared)
    warnings: List[str] = []
    stripped = data[:4096].lstrip()
    if data.startswith(b"%PDF-"):
        detected = "application/pdf"
    elif stripped.startswith((b"{", b"[")):
        try:
            json.loads(data.decode("utf-8-sig"))
            detected = "application/json"
        except (UnicodeDecodeError, json.JSONDecodeError):
            detected = "text/plain"
    elif stripped.lower().startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        detected = "text/html"
    elif stripped.startswith(b"<?xml"):
        detected = "application/xml"
    else:
        try:
            data[:65536].decode("utf-8")
            detected = "text/plain"
        except UnicodeDecodeError:
            detected = normalized or "application/octet-stream"
    if normalized and normalized not in {detected, "application/octet-stream", "binary/octet-stream"}:
        warnings.append("declared content type differs from detected content type")
    return detected, warnings


def _looks_like_challenge_page(data: bytes) -> bool:
    text = data[:1024 * 1024].decode("utf-8", errors="ignore")
    return any(pattern.search(text) for pattern in CHALLENGE_PATTERNS)


def _content_addressed_path(root: str, digest: str) -> Path:
    return project_path(root) / digest[:2] / digest


def _atomic_write_binary_once(path: Path, data: bytes, digest: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file() or file_hash(path) != digest:
            raise EvidenceVaultError("content-addressed object already exists with invalid bytes")
        return
    descriptor, temp_name = tempfile.mkstemp(prefix=".{0}.".format(path.name), suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if file_hash(Path(temp_name)) != digest:
            raise EvidenceVaultError("evidence object hash verification failed before commit")
        os.replace(temp_name, str(path))
    finally:
        try:
            Path(temp_name).unlink()
        except FileNotFoundError:
            pass


def _require_public_dns(host: str, resolver: Callable[..., Any]) -> None:
    try:
        results = resolver(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise EvidenceVaultError("official source DNS resolution failed") from exc
    addresses = {str(item[4][0]).split("%", 1)[0] for item in results if len(item) >= 5 and item[4]}
    if not addresses:
        raise EvidenceVaultError("official source DNS returned no addresses")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise EvidenceVaultError("official source DNS returned an invalid address") from exc
        if not ip.is_global:
            raise EvidenceVaultError("official source DNS resolved to a non-public address")


def _host_allowed(host: str, allowed: Sequence[str]) -> bool:
    return any(host == item or host.endswith("." + item) for item in allowed)


def _normalize_url_for_match(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
    except ValueError:
        return text
    host = (parsed.hostname or "").lower().rstrip(".")
    path = unquote(parsed.path or "/")
    return urlunsplit((parsed.scheme.lower(), host, path, parsed.query, ""))


def _url_domain(value: str) -> str:
    try:
        return (urlsplit(value).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _header_content_type(headers: Any) -> str:
    try:
        return str(headers.get_content_type() or "application/octet-stream").lower()
    except AttributeError:
        return _normalize_content_type(_safe_header(headers, "Content-Type")) or "application/octet-stream"


def _normalize_content_type(value: Any) -> str:
    text = str(value or "").split(";", 1)[0].strip().lower()
    return text


def _integer_header(headers: Any, name: str) -> Optional[int]:
    value = _safe_header(headers, name)
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _safe_header(headers: Any, name: str) -> str:
    try:
        return str(headers.get(name, "") or "").strip()[:1000]
    except Exception:
        return ""


def _response_filename(headers: Any, final_url: str) -> str:
    disposition = _safe_header(headers, "Content-Disposition")
    if disposition:
        message = Message()
        message["content-disposition"] = disposition
        filename = message.get_filename()
        if filename:
            return _safe_filename(filename)
    return _safe_filename(Path(urlsplit(final_url).path).name)


def _safe_filename(value: Any) -> str:
    name = Path(str(value or "")).name
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return cleaned[:180]


def _safe_transport(value: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {"http_status", "requested_url", "final_url", "etag", "last_modified", "content_length"}
    return {key: value.get(key) for key in sorted(allowed) if value.get(key) not in {None, ""}}


def _safe_references(values: Sequence[str]) -> List[str]:
    refs = _clean_list(values)
    if any(SECRET_PATTERN.search(item) for item in refs):
        raise EvidenceVaultError("evidence references must not contain credentials or secrets")
    return refs


def _valid_datetime(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
        return True
    except ValueError:
        try:
            datetime.strptime(text, "%Y-%m-%d")
            return True
        except ValueError:
            return False


def _retain_until(value: str, years: int) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    try:
        retained = parsed.replace(year=parsed.year + years)
    except ValueError:
        retained = parsed.replace(month=2, day=28, year=parsed.year + years)
    return retained.isoformat(timespec="seconds")


def _object_locator(digest: str, *, quarantine: bool) -> str:
    prefix = "evidence-quarantine" if quarantine else "evidence-vault"
    return "{0}:sha256:{1}".format(prefix, digest)


def _relative_project_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(project_path(".").resolve())).replace("\\", "/")
    except ValueError:
        return str(path.resolve())


def _policy_projection(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "enforce": cfg["enforce"],
        "allowed_official_domains": cfg["allowed_official_domains"],
        "require_https": cfg["require_https"],
        "allow_network_fetch": cfg["allow_network_fetch"],
        "auto_capture_official_signals": cfg["auto_capture_official_signals"],
        "max_document_bytes": cfg["max_document_bytes"],
        "retention_years": cfg["retention_years"],
        "require_retained_before_external_llm_for": cfg["require_retained_before_external_llm_for"],
        "raw_document_sent_to_external_llm": False,
        "automatic_approval": cfg["automatic_approval"],
        "automatic_publication": cfg["automatic_publication"],
        "automatic_trading": cfg["automatic_trading"],
    }


def _disabled_vault(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "version": EVIDENCE_VAULT_VERSION,
        "generated_at": now_iso(),
        "enabled": False,
        "integrity_status": "DISABLED",
        "event_count": 0,
        "last_event_hash": "",
        "event_errors": [],
        "projection_errors": [],
        "documents": {},
        "signal_links": {},
        "url_index": {},
        "archive_control": {"ready": False, "blockers": ["official evidence vault is disabled"]},
        "summary": {
            "document_count": 0,
            "captured_count": 0,
            "quarantined_count": 0,
            "linked_signal_count": 0,
            "source_domain_counts": {},
            "production_archive_ready": False,
        },
        "policy": _policy_projection(cfg),
        "safety_boundary": SAFETY_BOUNDARY,
    }


def _require_enabled(cfg: Dict[str, Any]) -> None:
    if not cfg["enabled"]:
        raise EvidenceVaultError("official evidence vault is disabled")


def _clean_list(values: Iterable[Any]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _clean_lower_list(values: Any) -> List[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    return sorted({str(value or "").strip().lower() for value in values if str(value or "").strip()})


def _unique(values: Iterable[Any]) -> List[str]:
    return _clean_list(values)
