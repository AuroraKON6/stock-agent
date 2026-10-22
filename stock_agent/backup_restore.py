"""Controlled local backup and isolated restore drills.

This module proves that selected fact sources can be copied, verified, and
restored into an isolated directory.  It deliberately does not claim that a
same-device, unencrypted rehearsal is a production disaster-recovery control.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from .audit_log import (
    append_hash_chained_jsonl,
    atomic_write_text,
    canonical_json,
    file_hash,
    object_hash,
)
from .config import project_path
from .runtime_layout import resolve_layout
from .release_controls import build_release_candidate_identity


BACKUP_RESTORE_VERSION = "1.0"
BACKUP_POLICY_VERSION = "32.0"
# SQLite identifiers cannot be bound as parameters; only catalog names matching
# this whitelist are ever interpolated into verification SQL.
_SAFE_SQL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SAFETY_BOUNDARY = (
    "Local non-production recovery rehearsal only. It never installs a schedule, "
    "changes live business state, approves a provider or archive, publishes research, "
    "or performs any order, trade, position, or portfolio action."
)

STATIC_RECOVERY_PATHS = (
    ".gitignore",
    "AGENTS.md",
    "README.md",
    "pyproject.toml",
    "requirements-optional.txt",
    "requirements.lock.txt",
    "config/watchlist.example.json",
    "config/pi_skills.json",
    "config/pi_skills.lock.json",
    ".pi/SYSTEM.md",
    ".pi/THIRD_PARTY_NOTICES.md",
    ".pi/settings.json",
    "pi_runtime/.npmrc",
    "pi_runtime/SYSTEM.md",
    "pi_runtime/THIRD_PARTY_NOTICES.md",
    "pi_runtime/package.json",
    "pi_runtime/package-lock.json",
    "pi_runtime/agent_home/settings.json",
    "docs/BACKUP_RESTORE_V32.md",
)
STATIC_RECOVERY_ROOTS = (
    ".pi/skills",
    "pi_runtime/roles",
    "pi_runtime/skills",
)
REQUIRED_STATIC_PATHS = (
    "pyproject.toml",
    "requirements.lock.txt",
    "config/watchlist.example.json",
)
EXCLUDED_FACT_MARKERS = (
    "/events/compact/",
    "/backup_restore/bundles/",
    "/backup_restore/drills/",
    "/llm_governance/uat_audit/",
    "/model_operations/uat_usage/",
    "/inbox/examples/",
    "/uat/",
)
EXCLUDED_NAME_MARKERS = (".example.", "_test", "test_", "_verify", "uat_")
TEXT_SUFFIXES = {
    "", ".cfg", ".ini", ".json", ".jsonl", ".md", ".npmrc", ".ps1",
    ".py", ".toml", ".txt", ".yaml", ".yml",
}
SECRET_PATTERNS = (
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), 0),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), 0),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 0),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), 0),
    (
        "assigned_secret",
        re.compile(
            r"(?i)(?:api[_-]?key|client[_-]?secret|access[_-]?token|bearer[_-]?token|password)"
            r"\s*[:=]\s*[\"']([^\"'\r\n]{12,})[\"']"
        ),
        1,
    ),
)
HEX_64 = re.compile(r"^[0-9a-f]{64}$")


class BackupRestoreError(ValueError):
    pass


def backup_restore_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw = (config or {}).get("backup_restore", {}) or {}
    storage = (config or {}).get("storage", {}) or {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "mode": str(raw.get("mode") or "local_rehearsal"),
        "bundle_root": str(raw.get("bundle_root") or "data/backup_restore/bundles"),
        "drill_root": str(raw.get("drill_root") or "data/backup_restore/drills"),
        "event_path": str(raw.get("event_path") or "data/backup_restore/events.jsonl"),
        "report_dir": str(raw.get("report_dir") or "reports/backup_restore"),
        "sqlite_paths": list(raw.get("sqlite_paths") or [storage.get("sqlite_path") or "data/stock_agent.sqlite3"]),
        "evidence_objects_dir": str(
            raw.get("evidence_objects_dir")
            or ((config or {}).get("official_evidence_vault", {}) or {}).get("objects_dir")
            or "data/evidence_vault/objects/sha256"
        ),
        "maximum_backup_age_minutes": max(1, int(raw.get("maximum_backup_age_minutes", 1440) or 1440)),
        "rpo_target_minutes": max(1, int(raw.get("rpo_target_minutes", 1440) or 1440)),
        "rto_target_minutes": max(1, int(raw.get("rto_target_minutes", 30) or 30)),
        "retention_days": max(1, int(raw.get("retention_days", 30) or 30)),
        "encryption_provider": str(raw.get("encryption_provider") or "").strip(),
        "offsite_target_uri": str(raw.get("offsite_target_uri") or "").strip(),
        "production_control_evidence_ref": str(raw.get("production_control_evidence_ref") or "").strip(),
        "independent_restore_signoff_ref": str(raw.get("independent_restore_signoff_ref") or "").strip(),
        "global_lease_evidence_ref": str(raw.get("global_lease_evidence_ref") or "").strip(),
        "automatic_restore": False,
        "automatic_activation": False,
        "automatic_schedule_install": False,
    }


def create_backup(
    config: Dict[str, Any],
    *,
    config_path: str = "config/watchlist.local.json",
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Create and immediately verify one local rehearsal bundle."""
    cfg = backup_restore_config(config)
    generated = _coerce_now(now)
    started = time.monotonic()
    candidate = build_release_candidate_identity()
    common = {
        "schema_version": BACKUP_RESTORE_VERSION,
        "policy_version": BACKUP_POLICY_VERSION,
        "operation": "backup_create",
        "created_at": generated.isoformat(timespec="seconds"),
        "release_candidate_fingerprint": candidate.get("fingerprint", ""),
        "safety_boundary": SAFETY_BOUNDARY,
        "operation_contract": _operation_contract(write_kind="backup_and_report"),
    }
    if not cfg["enabled"]:
        result = dict(common, status="BLOCKED", errors=["backup_restore is disabled"], blockers=["backup_restore is disabled"])
        return _finish_backup_report(config, cfg, result, generated)

    try:
        bundle_root = _controlled_root(cfg["bundle_root"], "bundle_root")
        drill_root = _controlled_root(cfg["drill_root"], "drill_root")
        if bundle_root == drill_root:
            raise BackupRestoreError("bundle_root and drill_root must be different")
    except BackupRestoreError as exc:
        result = dict(common, status="FAIL", errors=[str(exc)], blockers=[str(exc)])
        return _finish_backup_report(config, cfg, result, generated)

    backup_id = "backup_{0}_{1}".format(generated.strftime("%Y%m%dT%H%M%S%z"), uuid.uuid4().hex[:8])
    final_dir = bundle_root / backup_id
    staging_dir = bundle_root / (".{0}.tmp".format(backup_id))
    result = dict(common, backup_id=backup_id, status="FAIL", errors=[], blockers=[])
    try:
        bundle_root.mkdir(parents=True, exist_ok=True)
        if final_dir.exists() or staging_dir.exists():
            raise BackupRestoreError("backup destination already exists")
        payload_root = staging_dir / "payload"
        payload_root.mkdir(parents=True, exist_ok=False)
        sources, missing = _discover_sources(config, cfg)
        if missing:
            raise BackupRestoreError("required recovery inputs are missing: {0}".format(", ".join(missing)))

        entries: List[Dict[str, Any]] = []
        for source, relative, kind in sources:
            destination = payload_root / Path(relative)
            if kind == "sqlite":
                _backup_sqlite(source, destination)
            else:
                _stable_copy(source, destination)
            entries.append(_manifest_entry(destination, relative, kind, source))

        sanitized_path = payload_root / "config/watchlist.restore.json"
        sanitized_path.parent.mkdir(parents=True, exist_ok=True)
        sanitized_payload = _sanitize_value(config)
        atomic_write_text(
            sanitized_path,
            json.dumps(sanitized_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        entries.append(_manifest_entry(
            sanitized_path,
            "config/watchlist.restore.json",
            "sanitized_config",
            project_path(config_path),
        ))
        entries.sort(key=lambda item: item["path"])

        validation = _validate_payload(payload_root, entries)
        if validation["status"] != "PASS":
            raise BackupRestoreError("payload validation failed: {0}".format("; ".join(validation["errors"][:6])))
        secret_scan = _scan_payload(payload_root, entries)
        if secret_scan["blocking_finding_count"]:
            safe_findings = [
                "{0}:{1}".format(item["path"], item["pattern_id"])
                for item in secret_scan["findings"]
                if item["classification"] == "BLOCKING"
            ]
            raise BackupRestoreError("secret scan blocked backup: {0}".format(", ".join(safe_findings[:8])))

        completed = datetime.now().astimezone()
        manifest: Dict[str, Any] = {
            "schema_version": BACKUP_RESTORE_VERSION,
            "policy_version": BACKUP_POLICY_VERSION,
            "backup_id": backup_id,
            "created_at": generated.isoformat(timespec="seconds"),
            "completed_at": completed.isoformat(timespec="seconds"),
            "mode": cfg["mode"],
            "classification": "local_non_production_rehearsal",
            "bundle_format": "directory",
            # R07: the five-root layout that produced this bundle. Recovery
            # compares it against the CURRENT layout so a restore across
            # relocated roots is an explicit, checkable mapping — never a
            # silent rewrite of stored absolute paths.
            "runtime_layout": resolve_layout(config).snapshot(),
            "release_candidate_fingerprint": candidate.get("fingerprint", ""),
            "files": entries,
            "file_count": len(entries),
            "total_bytes": sum(int(item["bytes"]) for item in entries),
            "fact_projection_hash": validation["fact_projection_hash"],
            "fact_projection": validation["fact_projection"],
            "secret_scan": secret_scan,
            "snapshot_control": {
                "sqlite_online_backup": True,
                "non_sqlite_source_rehashed_after_copy": True,
                "cross_file_atomic_snapshot": False,
                "global_application_lease_used": False,
            },
            "rpo_evidence": {
                "target_minutes": cfg["rpo_target_minutes"],
                "capture_elapsed_seconds": round(time.monotonic() - started, 6),
                "recurring_backup_interval_evidenced": False,
                "status": "NOT_MEASURABLE",
                "reason": "one local snapshot does not prove an operational backup interval",
            },
            "encryption": {
                "encrypted": False,
                "status": "NOT_CONFIGURED",
                "provider_recorded": bool(cfg["encryption_provider"]),
                "credential_material_included": False,
            },
            "offsite_copy": {
                "performed": False,
                "target_recorded": bool(cfg["offsite_target_uri"]),
            },
            "excluded_sensitive_state": [
                "raw local configuration files",
                "human roster and restriction lists",
                "Pi/model credentials and environment secrets",
                "value-validation sealed mappings",
            ],
            "source_recovery": {
                "source_code_included": False,
                "requires_reviewed Git baseline or separately controlled source artifact": True,
            },
            "safety_boundary": SAFETY_BOUNDARY,
            "operation_contract": _operation_contract(write_kind="backup_bundle"),
        }
        manifest["manifest_id"] = _manifest_identity(manifest)
        manifest_path = staging_dir / "manifest.json"
        atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        digest = file_hash(manifest_path)
        atomic_write_text(staging_dir / "manifest.sha256", "{0}  manifest.json\n".format(digest))
        os.replace(str(staging_dir), str(final_dir))

        verification = verify_backup(config, str(final_dir))
        result.update({
            "status": verification["status"],
            "bundle_path": _relative_project_path(final_dir),
            "manifest_id": manifest["manifest_id"],
            "manifest_sha256": digest,
            "file_count": manifest["file_count"],
            "total_bytes": manifest["total_bytes"],
            "fact_projection_hash": manifest["fact_projection_hash"],
            "secret_scan": secret_scan,
            "verification": verification,
            "capture_elapsed_seconds": round(time.monotonic() - started, 6),
            "errors": list(verification.get("errors", [])),
            "blockers": list(verification.get("errors", [])),
        })
    except Exception as exc:
        _safe_remove_tree(staging_dir, bundle_root)
        result["status"] = "FAIL"
        result["errors"] = [str(exc)[:500]]
        result["blockers"] = list(result["errors"])

    try:
        event = _record_event(cfg, {
            "event_type": "backup_created" if result["status"] == "PASS" else "backup_failed",
            "occurred_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "backup_id": backup_id,
            "status": result["status"],
            "manifest_id": result.get("manifest_id", ""),
            "bundle_path": result.get("bundle_path", ""),
        })
        result["audit_event_hash"] = event.get("event_hash", "")
    except Exception as exc:
        result["status"] = "FAIL"
        result.setdefault("errors", []).append("backup audit event failed: {0}".format(str(exc)[:240]))
        result["blockers"] = list(result["errors"])
    return _finish_backup_report(config, cfg, result, generated)


def verify_backup(config: Dict[str, Any], bundle_path: str = "") -> Dict[str, Any]:
    """Recompute every bundle hash and validate SQLite and JSONL facts."""
    cfg = backup_restore_config(config)
    errors: List[str] = []
    try:
        bundle = _resolve_bundle(cfg, bundle_path)
    except BackupRestoreError as exc:
        return _verification_failure(str(exc))
    manifest_path = bundle / "manifest.json"
    digest_path = bundle / "manifest.sha256"
    if not manifest_path.is_file() or not digest_path.is_file():
        return _verification_failure("manifest.json or manifest.sha256 is missing")
    if manifest_path.is_symlink() or digest_path.is_symlink():
        return _verification_failure("manifest files must not be symbolic links")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be an object")
    except Exception as exc:
        return _verification_failure("manifest cannot be parsed: {0}".format(str(exc)[:240]))

    supplied_digest = digest_path.read_text(encoding="ascii", errors="replace").strip().split()[0:1]
    actual_digest = file_hash(manifest_path)
    if not supplied_digest or supplied_digest[0].lower() != actual_digest:
        errors.append("manifest SHA-256 does not match manifest.sha256")
    if str(manifest.get("schema_version") or "") != BACKUP_RESTORE_VERSION:
        errors.append("unsupported backup schema version")
    if str(manifest.get("manifest_id") or "") != _manifest_identity(manifest):
        errors.append("manifest identity does not match manifest content")

    entries = manifest.get("files") or []
    if not isinstance(entries, list) or not entries:
        errors.append("manifest files list is empty or invalid")
        entries = []
    path_errors = _manifest_path_errors(entries)
    errors.extend(path_errors)
    validation = _validate_payload(bundle / "payload", entries) if not path_errors else {
        "status": "FAIL", "errors": [], "fact_projection_hash": "", "fact_projection": []
    }
    errors.extend(validation.get("errors", []))
    if validation.get("fact_projection_hash") != str(manifest.get("fact_projection_hash") or ""):
        errors.append("restored fact projection hash does not match manifest")
    scan = _scan_payload(bundle / "payload", entries) if not path_errors else _empty_secret_scan()
    if scan["blocking_finding_count"]:
        errors.append("payload contains blocking secret-like material")

    return {
        "schema_version": BACKUP_RESTORE_VERSION,
        "operation": "backup_verify",
        "status": "PASS" if not errors else "FAIL",
        "verified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "backup_id": str(manifest.get("backup_id") or ""),
        "manifest_id": str(manifest.get("manifest_id") or ""),
        "manifest_sha256": actual_digest,
        "bundle_path": _relative_project_path(bundle),
        "release_candidate_fingerprint": str(manifest.get("release_candidate_fingerprint") or ""),
        "file_count": len(entries),
        "verified_file_count": int(validation.get("verified_file_count", 0) or 0),
        "fact_projection_hash": validation.get("fact_projection_hash", ""),
        "jsonl": validation.get("jsonl", []),
        "sqlite": validation.get("sqlite", []),
        "secret_scan": scan,
        "errors": errors,
        "safety_boundary": SAFETY_BOUNDARY,
        "operation_contract": _operation_contract(write_kind="none"),
    }


def run_restore_drill(
    config: Dict[str, Any],
    bundle_path: str = "",
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Restore a verified bundle below the configured drill root only."""
    cfg = backup_restore_config(config)
    generated = _coerce_now(now)
    started = time.monotonic()
    verification = verify_backup(config, bundle_path)
    common = {
        "schema_version": BACKUP_RESTORE_VERSION,
        "policy_version": BACKUP_POLICY_VERSION,
        "operation": "restore_drill",
        "created_at": generated.isoformat(timespec="seconds"),
        "backup_id": verification.get("backup_id", ""),
        "manifest_id": verification.get("manifest_id", ""),
        "release_candidate_fingerprint": verification.get("release_candidate_fingerprint", ""),
        "safety_boundary": SAFETY_BOUNDARY,
        "operation_contract": _operation_contract(write_kind="isolated_drill_and_report"),
    }
    if verification["status"] != "PASS":
        result = dict(common, status="FAIL", verification=verification, errors=["source backup verification failed"])
        return _finish_drill_report(config, cfg, result, generated)
    try:
        bundle = _resolve_bundle(cfg, verification["bundle_path"])
        drill_root = _controlled_root(cfg["drill_root"], "drill_root")
        drill_root.mkdir(parents=True, exist_ok=True)
        drill_id = "drill_{0}_{1}".format(generated.strftime("%Y%m%dT%H%M%S%z"), uuid.uuid4().hex[:8])
        drill_dir = drill_root / drill_id
        restored_root = drill_dir / "restored"
        if drill_dir.exists():
            raise BackupRestoreError("restore drill destination already exists")
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8-sig"))
        entries = manifest.get("files") or []
        for entry in entries:
            relative = _safe_manifest_path(str(entry.get("path") or ""))
            source = bundle / "payload" / Path(relative)
            destination = restored_root / Path(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source), str(destination))
        restored_validation = _validate_payload(restored_root, entries)
        restored_scan = _scan_payload(restored_root, entries)
        elapsed = time.monotonic() - started
        rto_seconds = float(cfg["rto_target_minutes"]) * 60.0
        errors = list(restored_validation.get("errors", []))
        if restored_validation.get("fact_projection_hash") != manifest.get("fact_projection_hash"):
            errors.append("isolated recovery projection does not match the backup manifest")
        if restored_scan["blocking_finding_count"]:
            errors.append("isolated restore contains blocking secret-like material")
        if elapsed > rto_seconds:
            errors.append("local rehearsal exceeded the configured RTO target")
        projection = {
            "schema_version": BACKUP_RESTORE_VERSION,
            "drill_id": drill_id,
            "backup_id": verification.get("backup_id", ""),
            "manifest_id": verification.get("manifest_id", ""),
            "rebuilt_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "fact_projection_hash": restored_validation.get("fact_projection_hash", ""),
            "facts": restored_validation.get("fact_projection", []),
            "source_overwrite_performed": False,
            "live_project_modified": False,
        }
        atomic_write_text(
            drill_dir / "recovery_projection.json",
            json.dumps(projection, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        result = dict(common, **{
            "drill_id": drill_id,
            "status": "PASS" if not errors else "FAIL",
            "drill_path": _relative_project_path(drill_dir),
            "restored_root": _relative_project_path(restored_root),
            "recovery_projection_path": _relative_project_path(drill_dir / "recovery_projection.json"),
            "verification": verification,
            "restored_validation": restored_validation,
            "rto": {
                "scope": "selected_data_and_configuration_bundle_only",
                "elapsed_seconds": round(elapsed, 6),
                "target_minutes": cfg["rto_target_minutes"],
                "status": "PASS" if elapsed <= rto_seconds else "FAIL",
                "full_service_recovery_proven": False,
            },
            "source_overwrite_performed": False,
            "live_project_modified": False,
            "errors": errors,
        })
    except Exception as exc:
        result = dict(common, status="FAIL", verification=verification, errors=[str(exc)[:500]])

    try:
        event = _record_event(cfg, {
            "event_type": "restore_drill_completed" if result["status"] == "PASS" else "restore_drill_failed",
            "occurred_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "backup_id": result.get("backup_id", ""),
            "drill_id": result.get("drill_id", ""),
            "status": result["status"],
            "manifest_id": result.get("manifest_id", ""),
            "rto_elapsed_seconds": (result.get("rto") or {}).get("elapsed_seconds", 0),
        })
        result["audit_event_hash"] = event.get("event_hash", "")
    except Exception as exc:
        result["status"] = "FAIL"
        result.setdefault("errors", []).append("restore audit event failed: {0}".format(str(exc)[:240]))
    return _finish_drill_report(config, cfg, result, generated)


def build_backup_restore_status(
    config: Dict[str, Any], *, now: Optional[datetime] = None
) -> Dict[str, Any]:
    cfg = backup_restore_config(config)
    generated = _coerce_now(now)
    report_dir = project_path(cfg["report_dir"])
    backup = _read_json_object(report_dir / "backup_latest.json")
    drill = _read_json_object(report_dir / "drill_latest.json")
    candidate = build_release_candidate_identity()
    technical_blockers: List[str] = []
    if not cfg["enabled"]:
        technical_blockers.append("backup_restore is disabled")
    if backup.get("status") != "PASS":
        technical_blockers.append("no current PASS backup exists")
    if drill.get("status") != "PASS":
        technical_blockers.append("no current PASS isolated restore drill exists")
    manifest_id = str(backup.get("manifest_id") or "")
    if manifest_id and str(drill.get("manifest_id") or "") != manifest_id:
        technical_blockers.append("latest restore drill does not reference the latest backup manifest")
    current_fingerprint = str(candidate.get("fingerprint") or "")
    for label, payload in (("backup", backup), ("restore drill", drill)):
        observed = str(payload.get("release_candidate_fingerprint") or "")
        if payload and observed != current_fingerprint:
            technical_blockers.append("latest {0} is stale for the current release candidate".format(label))

    backup_age_minutes: Optional[float] = None
    created_at = _parse_datetime(str(backup.get("created_at") or ""))
    if created_at:
        backup_age_minutes = (generated - created_at.astimezone(generated.tzinfo)).total_seconds() / 60.0
        if backup_age_minutes < -1:
            technical_blockers.append("latest backup timestamp is in the future")
        elif backup_age_minutes > cfg["maximum_backup_age_minutes"]:
            technical_blockers.append("latest backup exceeds the maximum allowed age")
    elif backup:
        technical_blockers.append("latest backup timestamp is invalid")

    git = _git_recovery_snapshot()
    production_blockers = list(technical_blockers)
    if cfg["mode"] != "production":
        production_blockers.append("backup mode is local_rehearsal, not an approved production control")
    if not cfg["encryption_provider"]:
        production_blockers.append("approved encryption/KMS provider is not recorded")
    if not cfg["offsite_target_uri"]:
        production_blockers.append("offsite or immutable backup target is not recorded")
    if not cfg["production_control_evidence_ref"]:
        production_blockers.append("verified production backup control evidence is missing")
    if not cfg["independent_restore_signoff_ref"]:
        production_blockers.append("independent human restore-drill sign-off is missing")
    if not cfg["global_lease_evidence_ref"]:
        production_blockers.append("cross-file snapshot lease/checkpoint evidence is missing")
    if not (git["head_valid"] and git["worktree_clean"] and git["remote_configured"]):
        production_blockers.append("reviewable and remotely recoverable Git source baseline is missing")
    production_blockers.extend([
        "raw local roster, restrictions, credentials, and sealed mappings require an approved encrypted recovery channel",
        "local rehearsal v1 cannot grant production disaster-recovery approval",
    ])
    technical_blockers = _unique(technical_blockers)
    production_blockers = _unique(production_blockers)
    technical_status = "PASS" if not technical_blockers else "BLOCKED"
    rto = drill.get("rto") or {}
    rpo_age_status = (
        "PASS" if backup_age_minutes is not None
        and 0 <= backup_age_minutes <= cfg["maximum_backup_age_minutes"] else "NOT_EVIDENCED"
    )
    snapshot = {
        "schema_version": BACKUP_RESTORE_VERSION,
        "policy_version": BACKUP_POLICY_VERSION,
        "generated_at": generated.isoformat(timespec="seconds"),
        "status": technical_status,
        "technical_status": technical_status,
        "production_readiness": "READY" if not production_blockers else "BLOCKED",
        "production_readiness_asserted_by_software": False,
        "input_fingerprint": object_hash({
            "manifest_id": manifest_id,
            "drill_id": drill.get("drill_id", ""),
            "policy": cfg,
        }),
        "release_candidate_fingerprint": current_fingerprint,
        "backup": _operation_summary(backup),
        "restore_drill": _operation_summary(drill),
        "rpo": {
            "target_minutes": cfg["rpo_target_minutes"],
            "maximum_backup_age_minutes": cfg["maximum_backup_age_minutes"],
            "latest_backup_age_minutes": round(backup_age_minutes, 3) if backup_age_minutes is not None else None,
            "age_status": rpo_age_status,
            "recurring_interval_evidence": "NOT_EVIDENCED",
            "operational_rpo_proven": False,
        },
        "rto": {
            "target_minutes": cfg["rto_target_minutes"],
            "latest_drill_elapsed_seconds": float(rto.get("elapsed_seconds", 0) or 0),
            "local_bundle_status": str(rto.get("status") or "NOT_EVIDENCED"),
            "full_service_recovery_proven": False,
        },
        "git_source_recovery": git,
        "technical_blockers": technical_blockers,
        "production_blockers": production_blockers,
        "next_actions": [
            "Keep the verified bundle immutable and retain the isolated drill evidence.",
            "Establish a reviewed Git baseline and a credential-free source recovery artifact or remote.",
            "Select an approved encrypted offsite/WORM target and record verified control evidence.",
            "Run a timed full-service restore under an application-wide lease with independent human sign-off.",
        ],
        "safety_boundary": SAFETY_BOUNDARY,
        "operation_contract": _operation_contract(write_kind="report_only"),
    }
    return snapshot


def write_backup_restore_status(
    config: Dict[str, Any], *, now: Optional[datetime] = None
) -> Dict[str, Any]:
    generated = _coerce_now(now)
    cfg = backup_restore_config(config)
    snapshot = build_backup_restore_status(config, now=generated)
    report_dir = project_path(cfg["report_dir"])
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = generated.strftime("%Y%m%dT%H%M%S%z")
    json_text = json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    markdown = render_backup_restore_markdown(snapshot)
    json_path = report_dir / "backup_restore_{0}.json".format(stamp)
    md_path = report_dir / "backup_restore_{0}.md".format(stamp)
    atomic_write_text(json_path, json_text)
    atomic_write_text(md_path, markdown, encoding="utf-8-sig")
    atomic_write_text(report_dir / "latest.json", json_text)
    atomic_write_text(report_dir / "latest.md", markdown, encoding="utf-8-sig")
    return {
        "snapshot": snapshot,
        "json_path": str(json_path),
        "report_path": str(md_path),
        "latest_json_path": str(report_dir / "latest.json"),
        "latest_path": str(report_dir / "latest.md"),
    }


def load_latest_backup_restore(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = backup_restore_config(config or {})
    return _read_json_object(project_path(cfg["report_dir"]) / "latest.json")


def render_backup_restore_markdown(snapshot: Dict[str, Any]) -> str:
    backup = snapshot.get("backup") or {}
    drill = snapshot.get("restore_drill") or {}
    rpo = snapshot.get("rpo") or {}
    rto = snapshot.get("rto") or {}
    lines = [
        "# Backup / Restore Drill",
        "",
        "- Generated at: `{0}`".format(_md(snapshot.get("generated_at", ""))),
        "- Local technical status: `{0}`".format(_md(snapshot.get("technical_status", "BLOCKED"))),
        "- Production DR readiness: `{0}`".format(_md(snapshot.get("production_readiness", "BLOCKED"))),
        "- Production approval asserted by software: `false`",
        "",
        "## Recovery Evidence",
        "",
        "| Evidence | Status | Identifier |",
        "|---|---|---|",
        "| Backup | {0} | `{1}` |".format(_md(backup.get("status", "MISSING")), _md(backup.get("manifest_id", ""))),
        "| Isolated restore | {0} | `{1}` |".format(_md(drill.get("status", "MISSING")), _md(drill.get("drill_id", ""))),
        "| RPO age check | {0} | {1} minutes |".format(_md(rpo.get("age_status", "NOT_EVIDENCED")), _md(rpo.get("latest_backup_age_minutes", ""))),
        "| Local bundle RTO | {0} | {1} seconds |".format(_md(rto.get("local_bundle_status", "NOT_EVIDENCED")), _md(rto.get("latest_drill_elapsed_seconds", 0))),
        "",
        "The measured RTO covers the selected data/configuration bundle only. It does not prove full service recovery.",
        "",
        "## Technical Blockers",
        "",
    ]
    lines.extend(["- {0}".format(_md(item)) for item in snapshot.get("technical_blockers", [])] or ["- none"])
    lines.extend(["", "## Production Blockers", ""])
    lines.extend(["- {0}".format(_md(item)) for item in snapshot.get("production_blockers", [])] or ["- none"])
    lines.extend(["", "## Next Actions", ""])
    lines.extend(["- {0}".format(_md(item)) for item in snapshot.get("next_actions", [])] or ["- none"])
    lines.extend([
        "",
        "## Safety Boundary",
        "",
        _md(snapshot.get("safety_boundary", SAFETY_BOUNDARY)),
        "",
    ])
    return "\n".join(lines)


def _discover_sources(
    config: Dict[str, Any], cfg: Dict[str, Any]
) -> Tuple[List[Tuple[Path, str, str]], List[str]]:
    root = project_path(".").resolve()
    found: Dict[str, Tuple[Path, str, str]] = {}
    missing: List[str] = []

    def add_path(path: Path, relative: str, kind: str) -> None:
        resolved = path.resolve()
        if not path.is_file() or path.is_symlink() or not _is_within(resolved, root):
            return
        normalized = PurePosixPath(relative.replace("\\", "/")).as_posix()
        found[normalized] = (path, normalized, kind)

    for relative in STATIC_RECOVERY_PATHS:
        path = project_path(relative)
        if path.is_file():
            add_path(path, relative, "configuration")
        elif relative in REQUIRED_STATIC_PATHS:
            missing.append(relative)
    for relative_root in STATIC_RECOVERY_ROOTS:
        source_root = project_path(relative_root)
        if source_root.is_dir():
            for path in source_root.rglob("*"):
                if path.is_file():
                    add_path(path, path.resolve().relative_to(root).as_posix(), "method_bundle")

    data_root = project_path("data")
    if data_root.is_dir():
        for path in data_root.rglob("*.jsonl"):
            relative = path.resolve().relative_to(root).as_posix()
            lowered = "/{0}".format(relative.lower())
            if any(marker in lowered for marker in EXCLUDED_FACT_MARKERS):
                continue
            if any(marker in path.name.lower() for marker in EXCLUDED_NAME_MARKERS):
                continue
            add_path(path, relative, "fact_jsonl")

    actions_root = project_path("reports/actions")
    if actions_root.is_dir():
        for path in actions_root.rglob("*.jsonl"):
            relative = path.resolve().relative_to(root).as_posix()
            add_path(path, relative, "action_audit_jsonl")

    index_path = project_path("data/evidence_vault/index.json")
    if index_path.is_file():
        add_path(index_path, "data/evidence_vault/index.json", "evidence_index_projection")
    object_root = project_path(cfg["evidence_objects_dir"])
    if object_root.is_dir():
        for path in object_root.rglob("*"):
            if path.is_file():
                add_path(path, path.resolve().relative_to(root).as_posix(), "evidence_object")

    for value in cfg["sqlite_paths"]:
        path = project_path(str(value))
        relative = _relative_project_path(path)
        if not path.is_file():
            missing.append(relative)
        elif path.is_symlink() or not _is_within(path.resolve(), root):
            missing.append("unsafe sqlite path: {0}".format(relative))
        else:
            add_path(path, relative, "sqlite")

    return [found[key] for key in sorted(found)], _unique(missing)


def _backup_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(str(source), timeout=10)
    destination_connection = sqlite3.connect(str(destination), timeout=10)
    try:
        source_connection.execute("PRAGMA query_only=ON")
        source_connection.backup(destination_connection)
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()
    metadata = _sqlite_metadata(destination)
    if metadata["integrity_status"] != "OK":
        raise BackupRestoreError("SQLite backup integrity check failed: {0}".format(_relative_project_path(source)))


def _stable_copy(source: Path, destination: Path) -> None:
    before = source.stat()
    source_hash_before = file_hash(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source), str(destination))
    after = source.stat()
    source_hash_after = file_hash(source)
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or source_hash_before != source_hash_after
        or source_hash_after != file_hash(destination)
    ):
        raise BackupRestoreError("source changed during backup: {0}".format(_relative_project_path(source)))


def _manifest_entry(destination: Path, relative: str, kind: str, source: Path) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "path": PurePosixPath(relative.replace("\\", "/")).as_posix(),
        "kind": kind,
        "bytes": destination.stat().st_size,
        "sha256": file_hash(destination),
        "source_modified_at": (
            datetime.fromtimestamp(source.stat().st_mtime).astimezone().isoformat(timespec="seconds")
            if source.is_file() else ""
        ),
    }
    if kind == "sqlite":
        entry["sqlite"] = _sqlite_metadata(destination)
    if kind.endswith("jsonl") or kind == "fact_jsonl":
        entry["jsonl"] = _jsonl_metadata(destination)
    return entry


def _validate_payload(payload_root: Path, entries: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    errors: List[str] = []
    expected: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        try:
            relative = _safe_manifest_path(str(entry.get("path") or ""))
        except BackupRestoreError as exc:
            errors.append(str(exc))
            continue
        if relative in expected:
            errors.append("duplicate manifest path: {0}".format(relative))
        expected[relative] = entry

    actual: List[str] = []
    if payload_root.is_dir():
        for path in payload_root.rglob("*"):
            if path.is_symlink():
                errors.append("symbolic link found in payload")
            elif path.is_file():
                actual.append(path.relative_to(payload_root).as_posix())
    else:
        errors.append("payload directory is missing")
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    errors.extend("payload file is missing: {0}".format(item) for item in missing)
    errors.extend("unmanifested payload file exists: {0}".format(item) for item in extra)

    facts: List[Dict[str, Any]] = []
    jsonl_results: List[Dict[str, Any]] = []
    sqlite_results: List[Dict[str, Any]] = []
    verified = 0
    for relative in sorted(set(expected) & set(actual)):
        entry = expected[relative]
        path = payload_root / Path(relative)
        size = path.stat().st_size
        digest = file_hash(path)
        if size != int(entry.get("bytes", -1)):
            errors.append("payload size mismatch: {0}".format(relative))
        if digest != str(entry.get("sha256") or ""):
            errors.append("payload SHA-256 mismatch: {0}".format(relative))
        kind = str(entry.get("kind") or "")
        fact: Dict[str, Any] = {"path": relative, "kind": kind, "bytes": size, "sha256": digest}
        if kind == "sqlite":
            metadata = _sqlite_metadata(path)
            sqlite_results.append(dict(metadata, path=relative))
            fact["sqlite"] = metadata
            if metadata["integrity_status"] != "OK":
                errors.append("SQLite integrity check failed: {0}".format(relative))
            if metadata != entry.get("sqlite"):
                errors.append("SQLite metadata does not match manifest: {0}".format(relative))
        elif kind.endswith("jsonl") or kind == "fact_jsonl":
            metadata = _jsonl_metadata(path)
            jsonl_results.append(dict(metadata, path=relative))
            fact["jsonl"] = metadata
            if metadata["integrity_status"] != "OK":
                errors.append("JSONL integrity check failed: {0}".format(relative))
            if metadata != entry.get("jsonl"):
                errors.append("JSONL metadata does not match manifest: {0}".format(relative))
        elif kind == "sanitized_config":
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                if not isinstance(payload, dict):
                    raise ValueError("sanitized config must be an object")
            except Exception:
                errors.append("sanitized recovery config is invalid")
        elif kind == "evidence_object":
            parts = PurePosixPath(relative).parts
            if len(parts) < 2 or parts[-1] != digest or parts[-2] != digest[:2]:
                errors.append("evidence object is not stored under its content hash: {0}".format(relative))
        facts.append(fact)
        verified += 1
    facts.sort(key=lambda item: item["path"])
    return {
        "status": "PASS" if not errors else "FAIL",
        "verified_file_count": verified,
        "fact_projection": facts,
        "fact_projection_hash": object_hash(facts),
        "jsonl": jsonl_results,
        "sqlite": sqlite_results,
        "errors": errors,
    }


def _jsonl_metadata(path: Path) -> Dict[str, Any]:
    count = 0
    invalid = 0
    errors: List[Dict[str, Any]] = []
    expected_previous = ""
    last_hash = ""
    seen_ids = set()
    mode = "empty"
    chain_mode: Optional[bool] = None
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            for line_number, raw in enumerate(handle, 1):
                text = raw.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    invalid += 1
                    errors.append({"line": line_number, "reason": "invalid JSON"})
                    continue
                if not isinstance(record, dict):
                    invalid += 1
                    errors.append({"line": line_number, "reason": "record must be an object"})
                    continue
                record_chain = all(key in record for key in ("event_id", "event_hash", "previous_hash"))
                any_chain = any(key in record for key in ("event_hash", "previous_hash"))
                if any_chain and not record_chain:
                    invalid += 1
                    errors.append({"line": line_number, "reason": "partial hash-chain fields"})
                    continue
                if chain_mode is None:
                    chain_mode = record_chain
                    mode = "hash_chain" if record_chain else "plain_jsonl"
                elif chain_mode != record_chain:
                    invalid += 1
                    errors.append({"line": line_number, "reason": "mixed hash-chain and plain records"})
                    continue
                if record_chain:
                    event_id = str(record.get("event_id") or "")
                    supplied = str(record.get("event_hash") or "")
                    previous = str(record.get("previous_hash") or "")
                    unhashed = dict(record)
                    unhashed.pop("event_hash", None)
                    calculated = object_hash(unhashed)
                    if not event_id or event_id in seen_ids or previous != expected_previous or supplied != calculated:
                        invalid += 1
                        errors.append({"line": line_number, "reason": "hash-chain verification failed"})
                        continue
                    seen_ids.add(event_id)
                    expected_previous = supplied
                    last_hash = supplied
                count += 1
    except Exception as exc:
        invalid += 1
        errors.append({"line": 0, "reason": "unreadable JSONL: {0}".format(type(exc).__name__)})
    return {
        "mode": mode,
        "record_count": count,
        "invalid_record_count": invalid,
        "last_hash": last_hash,
        "integrity_status": "OK" if not errors else "FAIL",
        "errors": errors[:20],
    }


def _sqlite_metadata(path: Path) -> Dict[str, Any]:
    tables: Dict[str, int] = {}
    schema_rows: List[Tuple[str, str]] = []
    status = "FAIL"
    errors: List[str] = []
    try:
        connection = sqlite3.connect(str(path), timeout=10)
        try:
            checks = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
            status = "OK" if checks == ["ok"] else "FAIL"
            if status != "OK":
                errors.extend(checks[:10])
            rows = connection.execute(
                "SELECT name, COALESCE(sql, '') FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            for name, sql in rows:
                name_str = str(name)
                if not _SAFE_SQL_IDENTIFIER.match(name_str):
                    errors.append("unsafe table name skipped: {0!r}".format(name_str[:60]))
                    continue
                tables[name_str] = int(
                    connection.execute('SELECT COUNT(*) FROM "{0}"'.format(name_str)).fetchone()[0]
                )
                schema_rows.append((name_str, str(sql)))
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()
    except Exception as exc:
        errors.append("{0}: {1}".format(type(exc).__name__, str(exc)[:160]))
        user_version = 0
    return {
        "integrity_status": status,
        "table_counts": tables,
        "schema_sha256": object_hash(schema_rows),
        "user_version": user_version,
        "errors": errors,
    }


def _scan_payload(payload_root: Path, entries: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    findings: List[Dict[str, Any]] = []
    scanned = 0
    for entry in entries:
        try:
            relative = _safe_manifest_path(str(entry.get("path") or ""))
        except BackupRestoreError:
            continue
        path = payload_root / Path(relative)
        if not path.is_file() or path.is_symlink():
            continue
        scanned += 1
        if str(entry.get("kind") or "") == "sqlite":
            findings.extend(_scan_sqlite(path, relative))
        elif path.suffix.lower() in TEXT_SUFFIXES:
            try:
                findings.extend(_scan_text(path.read_text(encoding="utf-8-sig", errors="replace"), relative))
            except OSError:
                findings.append(_scan_error(relative))
        else:
            try:
                raw = path.read_bytes()
                findings.extend(_scan_text(raw.decode("latin-1", errors="ignore"), relative, binary=True))
            except OSError:
                findings.append(_scan_error(relative))
    blocking = sum(item.get("classification") == "BLOCKING" for item in findings)
    placeholders = sum(item.get("classification") == "PLACEHOLDER" for item in findings)
    scan_errors = sum(item.get("classification") == "SCAN_ERROR" for item in findings)
    return {
        "status": "FAIL" if blocking or scan_errors else "PASS",
        "scanned_file_count": scanned,
        "blocking_finding_count": blocking,
        "placeholder_finding_count": placeholders,
        "scan_error_count": scan_errors,
        "findings": findings[:100],
        "secret_values_recorded": False,
        "source_snippets_recorded": False,
    }


def _scan_sqlite(path: Path, relative: str) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    try:
        connection = sqlite3.connect(str(path), timeout=10)
        try:
            tables = [
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            for table in tables:
                table_name = str(table)
                if not _SAFE_SQL_IDENTIFIER.match(table_name):
                    continue
                for row_number, row in enumerate(
                    connection.execute('SELECT * FROM "{0}"'.format(table_name)), 1
                ):
                    for value in row:
                        if isinstance(value, bytes):
                            text = value.decode("latin-1", errors="ignore")
                        elif isinstance(value, str):
                            text = value
                        else:
                            continue
                        for finding in _scan_text(text, relative):
                            finding["location"] = "sqlite:{0}:row:{1}".format(table, row_number)
                            findings.append(finding)
                            if len(findings) >= 100:
                                return findings
        finally:
            connection.close()
    except Exception:
        findings.append(_scan_error(relative))
    return findings


def _scan_text(text: str, relative: str, binary: bool = False) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for pattern_id, pattern, group_index in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(group_index)
            placeholder = _is_placeholder_secret(value)
            findings.append({
                "pattern_id": pattern_id,
                "path": relative,
                "location": "binary" if binary else "line:{0}".format(text.count("\n", 0, match.start()) + 1),
                "classification": "PLACEHOLDER" if placeholder else "BLOCKING",
                "match_sha256": hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest(),
                "match_length": len(value),
                "value_recorded": False,
                "snippet_recorded": False,
            })
            if len(findings) >= 100:
                return findings
    return findings


def _scan_error(relative: str) -> Dict[str, Any]:
    return {
        "pattern_id": "scan_error",
        "path": relative,
        "location": "",
        "classification": "SCAN_ERROR",
        "value_recorded": False,
        "snippet_recorded": False,
    }


def _empty_secret_scan() -> Dict[str, Any]:
    return {
        "status": "NOT_RUN",
        "scanned_file_count": 0,
        "blocking_finding_count": 0,
        "placeholder_finding_count": 0,
        "scan_error_count": 0,
        "findings": [],
        "secret_values_recorded": False,
        "source_snippets_recorded": False,
    }


def _sanitize_value(value: Any, key: str = "") -> Any:
    if key and _sensitive_key(key):
        return "${REDACTED}"
    if isinstance(value, dict):
        return {str(item_key): _sanitize_value(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
        parts = urlsplit(value)
        host = parts.hostname or ""
        if parts.port:
            host = "{0}:{1}".format(host, parts.port)
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    return value


def _sensitive_key(key: str) -> bool:
    lowered = key.strip().lower()
    if lowered.endswith("_env"):
        return False
    return bool(re.search(
        r"(^|_)(api_key|secret|password|credential|access_token|bearer_token|private_key)($|_)",
        lowered,
    ))


def _is_placeholder_secret(value: str) -> bool:
    lowered = str(value or "").lower()
    markers = ("test", "fake", "dummy", "example", "sample", "placeholder", "secret", "changeme", "must-not", "redacted")
    if any(marker in lowered for marker in markers):
        return True
    body = lowered[3:] if lowered.startswith("sk-") else lowered
    if "1234567890" in body or "abcdef" in body or "qwerty" in body:
        return True
    compact = re.sub(r"[^a-z0-9]", "", body)
    return bool(compact and len(set(compact)) <= 2)


def _manifest_path_errors(entries: Sequence[Dict[str, Any]]) -> List[str]:
    errors: List[str] = []
    seen = set()
    for entry in entries:
        try:
            relative = _safe_manifest_path(str(entry.get("path") or ""))
        except BackupRestoreError as exc:
            errors.append(str(exc))
            continue
        if relative in seen:
            errors.append("duplicate manifest path: {0}".format(relative))
        seen.add(relative)
    return errors


def _safe_manifest_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise BackupRestoreError("unsafe manifest path")
    if ":" in path.parts[0]:
        raise BackupRestoreError("unsafe manifest path")
    return path.as_posix()


def _manifest_identity(manifest: Dict[str, Any]) -> str:
    value = dict(manifest)
    value.pop("manifest_id", None)
    return object_hash(value)


def _controlled_root(path_value: str, label: str) -> Path:
    root = project_path(".").resolve()
    path = project_path(path_value).resolve()
    if not _is_within(path, root) or path == root:
        raise BackupRestoreError("{0} must stay inside the project workspace".format(label))
    return path


def _resolve_bundle(cfg: Dict[str, Any], value: str) -> Path:
    root = _controlled_root(cfg["bundle_root"], "bundle_root")
    raw = str(value or "").strip()
    if not raw or raw.lower() == "latest":
        latest = _read_json_object(project_path(cfg["report_dir"]) / "backup_latest.json")
        raw = str(latest.get("bundle_path") or "")
        if not raw:
            raise BackupRestoreError("no latest backup bundle is recorded")
    candidate = Path(raw)
    path = candidate.resolve() if candidate.is_absolute() else project_path(raw).resolve()
    if not _is_within(path, root) or path == root:
        raise BackupRestoreError("backup bundle must stay below the configured bundle_root")
    if not path.is_dir() or path.is_symlink():
        raise BackupRestoreError("backup bundle does not exist or is not a regular directory")
    return path


def _is_within(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath([str(path.resolve()), str(parent.resolve())]) == str(parent.resolve())
    except ValueError:
        return False


def _safe_remove_tree(path: Path, allowed_root: Path) -> None:
    try:
        resolved = path.resolve()
        root = allowed_root.resolve()
    except Exception:
        return
    if resolved == root or not _is_within(resolved, root):
        return
    if path.exists():
        shutil.rmtree(str(path))


def _finish_backup_report(
    config: Dict[str, Any], cfg: Dict[str, Any], result: Dict[str, Any], generated: datetime
) -> Dict[str, Any]:
    paths = _write_operation_report(cfg, "backup", result, generated)
    status = write_backup_restore_status(config, now=generated)
    result["report_paths"] = paths
    result["status_snapshot_path"] = status["latest_path"]
    return result


def _finish_drill_report(
    config: Dict[str, Any], cfg: Dict[str, Any], result: Dict[str, Any], generated: datetime
) -> Dict[str, Any]:
    paths = _write_operation_report(cfg, "drill", result, generated)
    status = write_backup_restore_status(config, now=generated)
    result["report_paths"] = paths
    result["status_snapshot_path"] = status["latest_path"]
    return result


def _write_operation_report(
    cfg: Dict[str, Any], kind: str, payload: Dict[str, Any], generated: datetime
) -> Dict[str, str]:
    report_dir = project_path(cfg["report_dir"])
    report_dir.mkdir(parents=True, exist_ok=True)
    prefix = "backup" if kind == "backup" else "restore_drill"
    stamp = generated.strftime("%Y%m%dT%H%M%S%z")
    path = report_dir / "{0}_{1}.json".format(prefix, stamp)
    latest = report_dir / ("backup_latest.json" if kind == "backup" else "drill_latest.json")
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, text)
    atomic_write_text(latest, text)
    return {"operation_report_path": str(path), "operation_latest_path": str(latest)}


def _record_event(cfg: Dict[str, Any], value: Dict[str, Any]) -> Dict[str, Any]:
    event = dict(value)
    event.update({
        "schema_version": BACKUP_RESTORE_VERSION,
        "actor": "system:backup_restore",
        "actor_type": "service",
        "business_state_changed": False,
        "approval_or_activation": False,
        "scheduled_task_changed": False,
        "trading_or_order_action": False,
        "safety_boundary": SAFETY_BOUNDARY,
    })
    return append_hash_chained_jsonl(project_path(cfg["event_path"]), event)


def _verification_failure(error: str) -> Dict[str, Any]:
    return {
        "schema_version": BACKUP_RESTORE_VERSION,
        "operation": "backup_verify",
        "status": "FAIL",
        "verified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "backup_id": "",
        "manifest_id": "",
        "manifest_sha256": "",
        "bundle_path": "",
        "release_candidate_fingerprint": "",
        "file_count": 0,
        "verified_file_count": 0,
        "fact_projection_hash": "",
        "jsonl": [],
        "sqlite": [],
        "secret_scan": _empty_secret_scan(),
        "errors": [error],
        "safety_boundary": SAFETY_BOUNDARY,
        "operation_contract": _operation_contract(write_kind="none"),
    }


def _operation_contract(write_kind: str) -> Dict[str, Any]:
    return {
        "write_scope": write_kind,
        "network_access": False,
        "credential_export": False,
        "raw_local_config_export": False,
        "live_source_overwrite": False,
        "business_state_write": False,
        "approval_or_activation": False,
        "publication": False,
        "scheduled_task_change": False,
        "llm_call": False,
        "trading_or_order_action": False,
    }


def _operation_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "status", "created_at", "backup_id", "drill_id", "bundle_path", "drill_path",
        "restored_root", "manifest_id", "manifest_sha256", "file_count", "total_bytes",
        "fact_projection_hash", "release_candidate_fingerprint", "rto", "errors",
    )
    return {key: payload.get(key) for key in keys if key in payload}


def _git_recovery_snapshot() -> Dict[str, Any]:
    root = project_path(".").resolve()

    def run(*args: str) -> Tuple[int, str]:
        try:
            completed = subprocess.run(
                ["git"] + list(args), cwd=str(root), text=True, capture_output=True, timeout=10, check=False
            )
            return completed.returncode, completed.stdout.strip()
        except Exception:
            return 1, ""

    repo_code, _ = run("rev-parse", "--show-toplevel")
    head_code, head = run("rev-parse", "--verify", "HEAD")
    status_code, status = run("status", "--porcelain=v1")
    remote_code, remote = run("remote")
    return {
        "repository_detected": repo_code == 0,
        "head_valid": head_code == 0 and bool(head),
        "head": head if head_code == 0 else "",
        "worktree_clean": status_code == 0 and not bool(status),
        "remote_configured": remote_code == 0 and bool(remote),
        "remote_url_recorded": False,
    }


def _read_json_object(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _parse_datetime(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone() if parsed.tzinfo else parsed.astimezone()
    except (TypeError, ValueError):
        return None


def _coerce_now(value: Optional[datetime]) -> datetime:
    current = value or datetime.now().astimezone()
    return current.astimezone() if current.tzinfo is None else current


def _relative_project_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(project_path(".").resolve()).as_posix()
    except Exception:
        return str(path)


def _unique(values: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value)))


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("`", "'")
