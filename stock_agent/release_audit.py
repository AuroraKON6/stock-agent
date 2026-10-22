"""Read-only release identity and freshness audit.

The audit answers which source revision, configuration, policy inputs, model
settings, and dependency locks a run would use. It never collects market data,
calls an LLM, changes a business ledger, grants an approval, or installs a
scheduled task. Writing the report is its only side effect.
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from .audit_log import atomic_write_text, file_hash, object_hash
from .config import load_config, project_path
from .pi_skills import pi_skill_supply_chain_status
from .release_controls import build_release_candidate_identity


RELEASE_AUDIT_VERSION = "1.0"
RELEASE_POLICY_VERSION = "31.0"
PYTHON_LOCK_CANDIDATES = (
    "requirements.lock",
    "requirements.lock.txt",
    "requirements.linux-py312.lock.txt",  # R08: Linux candidate lock, separately reviewed
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
)
G0_CONTROL_ARTIFACTS = (
    ("secret_scan", "reports/release/secret_scan_latest.json"),
    ("sbom", "reports/release/sbom.cdx.json"),
    ("license_report", "reports/release/license_report_latest.json"),
    ("python_environment", "reports/release/python_environment_latest.json"),
    ("backup_restore", "reports/backup_restore/latest.json"),
)
LATEST_ARTIFACTS = (
    ("health", "reports/health/latest.json"),
    ("production_pilot", "reports/production_pilot/latest.json"),
    ("business_onboarding", "reports/business_onboarding/latest.json"),
    ("provider_registry", "reports/production_pilot/provider_registry_latest.json"),
    ("review_queue", "reports/review_queue/latest.json"),
    ("research_runtime", "reports/research_runtime/latest.json"),
    ("research_workpapers", "reports/research_workpapers/latest.json"),
    ("evidence_vault", "reports/evidence_vault/latest.json"),
    ("llm_governance", "reports/llm_governance/latest.json"),
    ("market_data_governance", "reports/market_data_governance/latest.json"),
    ("model_operations", "reports/model_operations/latest.json"),
    ("pi_runtime", "reports/pi_agent/runtime/latest.json"),
    ("pi_skills", "reports/pi_agent/skills/latest.json"),
)
_SAFE_FINGERPRINT = re.compile(r"^[0-9a-fA-F]{32,128}$")
_REPORTED_STATUS_KEYS = (
    "overall_status",
    "go_live_decision",
    "business_status",
    "status",
    "technical_readiness",
    "candidate_readiness",
    "technical_status",
    "integrity_status",
    "runtime_ready",
)
_ADVERSE_SOURCE_STATUSES = {"FAIL", "BLOCKED", "NO_GO", "ATTENTION", "ERROR"}

CommandRunner = Callable[[Sequence[str], Path], Tuple[int, str, str]]


def build_release_audit(
    config_path: str = "config/watchlist.local.json",
    *,
    now: Optional[datetime] = None,
    command_runner: Optional[CommandRunner] = None,
) -> Dict[str, Any]:
    """Build a release manifest without writing project or business state."""
    generated = _coerce_now(now)
    root = project_path(".").resolve()
    config = load_config(config_path)
    runner = command_runner or _run_command

    git = _git_snapshot(root, runner)
    release_candidate = build_release_candidate_identity()
    inputs = [
        _file_entry(name, path_value, required)
        for name, path_value, required in _input_specs(config, config_path)
    ]
    python_lock = _find_python_lock()
    if python_lock:
        inputs.append(_file_entry("python_dependency_lock", python_lock, True))
    else:
        inputs.append({
            "name": "python_dependency_lock",
            "path": " | ".join(PYTHON_LOCK_CANDIDATES),
            "required": True,
            "exists": False,
            "sha256": "",
            "bytes": 0,
            "modified_at": "",
        })

    controls = [_control_entry(name, path_value) for name, path_value in G0_CONTROL_ARTIFACTS]
    latest = [_latest_artifact(label, path_value, generated) for label, path_value in LATEST_ARTIFACTS]
    scheduled = _latest_scheduled_summary(generated)
    if scheduled:
        latest.append(scheduled)

    pi = _pi_snapshot(config)
    models = _model_snapshot(config)
    runtime = _runtime_snapshot()

    blockers: List[str] = []
    warnings: List[str] = []
    if not git["repository_detected"]:
        # R08: a missing .git NEVER passes on its own. The only alternate
        # reviewable identity is the image PROVENANCE chain (shipped record +
        # external digest binding) — when that evidence is present and self-
        # consistent, the audit records it and still keeps an explicit
        # provenance-mode note; without it, the blocker stands.
        provenance_verdict = _image_provenance_snapshot(root)
        if provenance_verdict.get("verified"):
            warnings.append(
                "Git unavailable; release identity carried by the verified image "
                "provenance chain (source_commit={0}, dirty={1})".format(
                    provenance_verdict.get("source_commit"),
                    provenance_verdict.get("source_dirty")))
        else:
            blockers.append("Git repository is not available or cannot be inspected")
    elif not git["head_valid"]:
        blockers.append("Git HEAD is missing; no reviewable source baseline exists")
    if git["repository_detected"] and not git["worktree_clean"]:
        blockers.append("Git worktree is not clean; release identity is not immutable")

    for item in inputs:
        if item["required"] and not item["exists"]:
            blockers.append("Required release input is missing: {0}".format(item["name"]))
    for item in controls:
        if not item["exists"]:
            blockers.append("Required G0 control artifact is missing: {0}".format(item["name"]))
        elif item["control_status"] != "PASS":
            blockers.append("Required G0 control artifact is not PASS: {0}={1}".format(
                item["name"], item["control_status"]
            ))
    backup_control = next((item for item in controls if item["name"] == "backup_restore"), None)
    if backup_control and backup_control["exists"] and backup_control.get("production_readiness") != "READY":
        blockers.append("Backup/restore production readiness is not READY")
    for error in release_candidate["errors"]:
        blockers.append("Release candidate fingerprint failed: {0}".format(error))
    for control_name in ("secret_scan", "python_environment", "backup_restore"):
        item = next((value for value in controls if value["name"] == control_name), None)
        if item and item["exists"] and item["control_status"] == "PASS":
            if item.get("release_candidate_fingerprint") != release_candidate["fingerprint"]:
                blockers.append("Required G0 control artifact is stale for current source: {0}".format(control_name))

    if pi["enabled"]:
        if not pi["runtime_settings_safe"]:
            blockers.append("Pi production settings are missing or do not enforce the isolated no-tool policy")
        if not pi["package_version_matches"]:
            blockers.append("Pi package version does not match the configured pinned version")
        if pi["compiled_skills_enabled"] and pi["skill_supply_chain_status"] != "READY":
            blockers.append("Pi compiled-method supply chain is not READY")

    for item in latest:
        if not item["exists"]:
            warnings.append("Latest projection is missing: {0}".format(item["label"]))
        elif item["freshness"] == "STALE":
            warnings.append("Latest projection is stale: {0}".format(item["label"]))
        elif item["freshness"] == "FUTURE":
            warnings.append("Latest projection timestamp is in the future: {0}".format(item["label"]))
        if item.get("parse_error"):
            warnings.append("Latest projection cannot be parsed: {0}".format(item["label"]))
        if str(item.get("reported_status") or "").upper() in _ADVERSE_SOURCE_STATUSES:
            warnings.append("Latest projection reports an adverse state: {0}={1}".format(
                item["label"], item["reported_status"]
            ))

    if not models["pi_agent"]["credential_present"]:
        warnings.append("Pi model credential environment variable is not present in this process")
    if models["llm_agent"]["enabled"] and not models["llm_agent"]["credential_present"]:
        warnings.append("LLM credential environment variable is not present in this process")

    identity_projection = {
        "schema_version": RELEASE_AUDIT_VERSION,
        "policy_version": RELEASE_POLICY_VERSION,
        "git": {
            "repository_detected": git["repository_detected"],
            "head": git["head"],
            "branch": git["branch"],
            "worktree_clean": git["worktree_clean"],
            "change_counts": git["change_counts"],
        },
        "runtime": {
            "python_version": runtime["python_version"],
            "implementation": runtime["implementation"],
            "platform": runtime["platform"],
            "project_version": runtime["project_version"],
        },
        "release_candidate_fingerprint": release_candidate["fingerprint"],
        "input_hashes": {item["name"]: item["sha256"] for item in inputs},
        "control_fingerprints": {
            item["name"]: item["input_fingerprint"] or item["sha256"] for item in controls
        },
        "pi": {
            "provider": pi["provider"],
            "model": pi["model"],
            "expected_package": pi["expected_package"],
            "expected_version": pi["expected_version"],
            "installed_version": pi["installed_version"],
            "settings_sha256": pi["settings_sha256"],
            "package_lock_sha256": pi["package_lock_sha256"],
            "skill_bundle_sha256": pi["skill_bundle_sha256"],
        },
        "models": {
            "llm_runtime": models["llm_agent"]["runtime"],
            "llm_model": models["llm_agent"]["model"],
            "llm_endpoint": models["llm_agent"]["base_url"],
            "pi_provider": models["pi_agent"]["provider"],
            "pi_model": models["pi_agent"]["model"],
        },
        "latest_projection_hashes": {item["label"]: item["sha256"] for item in latest},
    }
    manifest_id = object_hash(identity_projection)
    technical_ready = not blockers

    return {
        "schema_version": RELEASE_AUDIT_VERSION,
        "policy_version": RELEASE_POLICY_VERSION,
        "generated_at": generated.isoformat(timespec="seconds"),
        "manifest_id": manifest_id,
        "status": "READY" if technical_ready else "BLOCKED",
        "technical_release_ready": technical_ready,
        "business_enablement": _business_enablement(latest),
        "git": git,
        "runtime": runtime,
        "release_candidate": release_candidate,
        "models": models,
        "pi": pi,
        "inputs": inputs,
        "g0_control_artifacts": controls,
        "latest_artifacts": latest,
        "blockers": blockers,
        "warnings": warnings,
        "identity_projection": identity_projection,
        "operation_contract": {
            "report_write_only": True,
            "network_access": False,
            "market_data_collection": False,
            "llm_call": False,
            "business_state_write": False,
            "approval_or_activation": False,
            "scheduled_task_change": False,
            "trading_or_order_action": False,
        },
    }


def write_release_audit(
    config_path: str = "config/watchlist.local.json",
    *,
    now: Optional[datetime] = None,
    command_runner: Optional[CommandRunner] = None,
) -> Dict[str, Any]:
    """Write timestamped and latest report projections only."""
    audit = build_release_audit(config_path, now=now, command_runner=command_runner)
    generated = datetime.fromisoformat(audit["generated_at"])
    stamp = generated.strftime("%Y%m%dT%H%M%S%z") or generated.strftime("%Y%m%dT%H%M%S")
    report_dir = project_path("reports/release_audit")
    json_path = report_dir / "release_audit_{0}.json".format(stamp)
    md_path = report_dir / "release_audit_{0}.md".format(stamp)
    latest_json = report_dir / "latest.json"
    latest_md = report_dir / "latest.md"
    json_text = json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    markdown = render_release_audit_markdown(audit)
    atomic_write_text(json_path, json_text)
    atomic_write_text(md_path, markdown, encoding="utf-8-sig")
    atomic_write_text(latest_json, json_text)
    atomic_write_text(latest_md, markdown, encoding="utf-8-sig")
    return {
        "audit": audit,
        "release_audit_json_path": str(json_path),
        "release_audit_path": str(md_path),
        "latest_json_path": str(latest_json),
        "latest_path": str(latest_md),
    }


def render_release_audit_markdown(audit: Dict[str, Any]) -> str:
    git = audit.get("git", {}) or {}
    runtime = audit.get("runtime", {}) or {}
    pi = audit.get("pi", {}) or {}
    business = audit.get("business_enablement", {}) or {}
    lines = [
        "# Release Audit",
        "",
        "- Generated at: `{0}`".format(_md(audit.get("generated_at", ""))),
        "- Manifest ID: `{0}`".format(_md(audit.get("manifest_id", ""))),
        "- Technical release status: `{0}`".format(_md(audit.get("status", "BLOCKED"))),
        "- Business enablement: `{0}`".format(_md(business.get("status", "NOT_ASSERTED"))),
        "- Side effects: `report files only`",
        "",
        "This report never grants business activation, approves a provider, publishes research, or executes a trade.",
        "",
        "## Technical Blockers",
        "",
    ]
    blockers = audit.get("blockers", []) or []
    lines.extend(["- {0}".format(_md(item)) for item in blockers] or ["- none"])
    lines.extend(["", "## Warnings", ""])
    warnings = audit.get("warnings", []) or []
    lines.extend(["- {0}".format(_md(item)) for item in warnings] or ["- none"])
    lines.extend([
        "",
        "## Git Baseline",
        "",
        "| Field | Value |",
        "|---|---|",
        "| Repository | {0} |".format("yes" if git.get("repository_detected") else "no"),
        "| HEAD | `{0}` |".format(_md(git.get("head", "") or "missing")),
        "| Branch | `{0}` |".format(_md(git.get("branch", "") or "unknown")),
        "| Clean | {0} |".format("yes" if git.get("worktree_clean") else "no"),
        "| Changed entries | {0} |".format((git.get("change_counts") or {}).get("total", 0)),
        "",
        "## Runtime And Pi",
        "",
        "| Field | Value |",
        "|---|---|",
        "| Python | `{0}` |".format(_md(runtime.get("python_version", ""))),
        "| Project version | `{0}` |".format(_md(runtime.get("project_version", ""))),
        "| Release candidate | `{0}` |".format(
            _md((audit.get("release_candidate") or {}).get("fingerprint", "") or "missing")
        ),
        "| Pi provider/model | `{0}` / `{1}` |".format(_md(pi.get("provider", "")), _md(pi.get("model", ""))),
        "| Pi package | `{0}@{1}` |".format(_md(pi.get("expected_package", "")), _md(pi.get("expected_version", ""))),
        "| Pi settings safe | {0} |".format("yes" if pi.get("runtime_settings_safe") else "no"),
        "| Pi skill chain | `{0}` |".format(_md(pi.get("skill_supply_chain_status", ""))),
        "",
        "## Release Inputs",
        "",
        "| Input | Required | Exists | Control status | SHA-256 |",
        "|---|---:|---:|---|---|",
    ])
    for item in audit.get("inputs", []) or []:
        lines.append("| {0} | {1} | {2} |  | `{3}` |".format(
            _md(item.get("name", "")),
            "yes" if item.get("required") else "no",
            "yes" if item.get("exists") else "no",
            _md(item.get("sha256", "") or "missing"),
        ))
    for item in audit.get("g0_control_artifacts", []) or []:
        lines.append("| {0} | yes | {1} | `{2}` | `{3}` |".format(
            _md(item.get("name", "")),
            "yes" if item.get("exists") else "no",
            _md(item.get("control_status", "MISSING")),
            _md(item.get("sha256", "") or "missing"),
        ))
    lines.extend([
        "",
        "## Latest Projection Freshness",
        "",
        "| Projection | Exists | Freshness | Age hours | Source status |",
        "|---|---:|---|---:|---|",
    ])
    for item in audit.get("latest_artifacts", []) or []:
        lines.append("| {0} | {1} | {2} | {3} | `{4}` |".format(
            _md(item.get("label", "")),
            "yes" if item.get("exists") else "no",
            _md(item.get("freshness", "MISSING")),
            "{0:.1f}".format(float(item.get("age_hours", 0) or 0)) if item.get("exists") else "",
            _md(item.get("reported_status", "") or "unknown"),
        ))
    lines.extend([
        "",
        "## Business Boundary",
        "",
        "- Business status is source-reported only and is never approved by this audit.",
        "- Production pilot report: `{0}`.".format(_md(business.get("production_pilot_reported_status", "") or "unknown")),
        "- Business onboarding report: `{0}`.".format(_md(business.get("business_onboarding_reported_status", "") or "unknown")),
        "- Automatic approval, publication, order placement, and portfolio action: `false`.",
        "",
    ])
    return "\n".join(lines)


def _input_specs(config: Dict[str, Any], config_path: str) -> List[Tuple[str, str, bool]]:
    operations = config.get("research_operations", {}) or {}
    pi_agent = config.get("pi_agent", {}) or {}
    pilot = config.get("production_pilot", {}) or {}
    pi_enabled = bool(pi_agent.get("enabled", False))
    compiled_skills = bool(pi_agent.get("compiled_skills_enabled", False))
    agent_home = str(pi_agent.get("agent_home") or "pi_runtime/agent_home")
    return [
        ("project_metadata", "pyproject.toml", True),
        ("runtime_config", config_path, True),
        ("research_roster", str(operations.get("roster_path") or "config/research_roster.local.json"), True),
        ("research_restrictions", str(operations.get("restrictions_path") or "config/research_restrictions.local.json"), True),
        ("optional_requirements", "requirements-optional.txt", False),
        ("pi_package", "pi_runtime/package.json", pi_enabled),
        ("pi_package_lock", "pi_runtime/package-lock.json", pi_enabled),
        ("pi_production_settings", str(Path(agent_home) / "settings.json"), pi_enabled),
        ("pi_skill_registry", str(pi_agent.get("skill_registry") or "config/pi_skills.json"), compiled_skills),
        ("pi_skill_lock", str(pi_agent.get("skill_lock") or "config/pi_skills.lock.json"), compiled_skills),
        ("provider_event_ledger", str(pilot.get("vendor_event_path") or "data/production_pilot/vendor_events.jsonl"), False),
    ]


def _file_entry(name: str, path_value: str, required: bool) -> Dict[str, Any]:
    path = project_path(path_value)
    exists = path.is_file()
    stat = path.stat() if exists else None
    return {
        "name": name,
        "path": _display_path(path),
        "required": bool(required),
        "exists": exists,
        "sha256": file_hash(path) if exists else "",
        "bytes": int(stat.st_size) if stat else 0,
        "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds") if stat else "",
    }


def _control_entry(name: str, path_value: str) -> Dict[str, Any]:
    entry = _file_entry(name, path_value, True)
    entry.update({
        "control_status": "MISSING" if not entry["exists"] else "INVALID",
        "input_fingerprint": "",
        "release_candidate_fingerprint": "",
        "parse_error": "",
        "production_readiness": "",
    })
    if not entry["exists"]:
        return entry
    path = project_path(path_value)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("control artifact must be a JSON object")
        if name == "sbom":
            properties = {
                str(item.get("name") or ""): str(item.get("value") or "")
                for item in (payload.get("properties") or [])
                if isinstance(item, dict)
            }
            valid = bool(
                payload.get("bomFormat") == "CycloneDX"
                and str(payload.get("specVersion") or "") in {"1.4", "1.5", "1.6"}
                and isinstance(payload.get("components"), list)
                and payload.get("components")
            )
            entry["control_status"] = (
                properties.get("stock-agent:control-status", "INVALID") if valid else "INVALID"
            )
            entry["input_fingerprint"] = properties.get("stock-agent:input-fingerprint", "")
        elif name == "backup_restore":
            entry["control_status"] = str(payload.get("technical_status") or payload.get("status") or "INVALID").upper()
            entry["input_fingerprint"] = str(payload.get("input_fingerprint") or "")
            entry["release_candidate_fingerprint"] = str(
                payload.get("release_candidate_fingerprint") or ""
            )
            entry["production_readiness"] = str(payload.get("production_readiness") or "BLOCKED").upper()
        else:
            entry["control_status"] = str(payload.get("status") or "INVALID").upper()
            entry["input_fingerprint"] = str(payload.get("input_fingerprint") or "")
            entry["release_candidate_fingerprint"] = str(
                payload.get("release_candidate_fingerprint") or ""
            )
    except Exception as exc:
        entry["parse_error"] = str(exc)[:240]
        entry["control_status"] = "INVALID"
    if not _SAFE_FINGERPRINT.fullmatch(entry["input_fingerprint"]):
        entry["input_fingerprint"] = ""
    if not _SAFE_FINGERPRINT.fullmatch(entry["release_candidate_fingerprint"]):
        entry["release_candidate_fingerprint"] = ""
    return entry


def _find_python_lock() -> str:
    for candidate in PYTHON_LOCK_CANDIDATES:
        if project_path(candidate).is_file():
            return candidate
    return ""


def _git_snapshot(root: Path, runner: CommandRunner) -> Dict[str, Any]:
    detected_code, top, detect_error = runner(["git", "rev-parse", "--show-toplevel"], root)
    head_code, head, head_error = runner(["git", "rev-parse", "--verify", "HEAD"], root)
    branch_code, branch, branch_error = runner(["git", "branch", "--show-current"], root)
    status_code, status_text, status_error = runner(["git", "status", "--porcelain=v1"], root)
    repository_detected = detected_code == 0 and bool(top.strip())
    commit_time = ""
    if head_code == 0 and head.strip():
        time_code, time_text, _ = runner(["git", "show", "-s", "--format=%cI", "HEAD"], root)
        if time_code == 0:
            commit_time = time_text.strip()
    status_lines = [line for line in status_text.splitlines() if line.strip()] if status_code == 0 else []
    staged = 0
    unstaged = 0
    untracked = 0
    for line in status_lines:
        code = (line + "  ")[:2]
        if code == "??":
            untracked += 1
            continue
        if code[0] not in {" ", "?"}:
            staged += 1
        if code[1] not in {" ", "?"}:
            unstaged += 1
    errors = [value.strip() for value in (detect_error, head_error, branch_error, status_error) if value.strip()]
    return {
        "repository_detected": repository_detected,
        "top_level": _display_path(Path(top.strip())) if repository_detected else "",
        "head_valid": head_code == 0 and bool(head.strip()),
        "head": head.strip() if head_code == 0 else "",
        "branch": branch.strip() if branch_code == 0 else "",
        "commit_time": commit_time,
        "worktree_clean": bool(repository_detected and status_code == 0 and not status_lines),
        "change_counts": {
            "staged": staged,
            "unstaged": unstaged,
            "untracked": untracked,
            "total": len(status_lines),
        },
        "errors": errors[:4],
    }


def _pi_snapshot(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("pi_agent", {}) or {}
    enabled = bool(raw.get("enabled", False))
    expected_package = str(raw.get("package") or "@earendil-works/pi-coding-agent")
    expected_version = str(raw.get("version") or "0.84.2")
    agent_home = str(raw.get("agent_home") or "pi_runtime/agent_home")
    settings_path = project_path(str(Path(agent_home) / "settings.json"))
    settings_safe = False
    settings_sha = ""
    settings_error = ""
    if settings_path.is_file():
        settings_sha = file_hash(settings_path)
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
            settings_safe = bool(
                settings.get("defaultProjectTrust") == "never"
                and settings.get("defaultTools") == []
                and settings.get("packages", []) == []
                and settings.get("extensions", []) == []
                and settings.get("skills", []) == []
                and settings.get("prompts", []) == []
                and settings.get("themes", []) == []
                and settings.get("enableInstallTelemetry") is False
                and settings.get("enableAnalytics") is False
                and (settings.get("compaction", {}) or {}).get("enabled") is False
                and (settings.get("retry", {}) or {}).get("enabled") is False
            )
        except Exception as exc:
            settings_error = str(exc)

    package_path = project_path("pi_runtime/package.json")
    package_lock = project_path("pi_runtime/package-lock.json")
    installed_version = ""
    package_error = ""
    if package_path.is_file():
        try:
            package = json.loads(package_path.read_text(encoding="utf-8-sig"))
            installed_version = str((package.get("dependencies") or {}).get(expected_package) or "")
        except Exception as exc:
            package_error = str(exc)

    try:
        skill_status = pi_skill_supply_chain_status(config)
    except Exception as exc:
        skill_status = {"status": "BLOCKED", "bundle_sha256": "", "errors": [str(exc)]}
    compiled = bool(raw.get("compiled_skills_enabled", False))
    return {
        "enabled": enabled,
        "provider": str(raw.get("provider") or "deepseek"),
        "model": str(raw.get("model") or "deepseek-v4-pro"),
        "expected_package": expected_package,
        "expected_version": expected_version,
        "installed_version": installed_version,
        "package_version_matches": bool(not enabled or installed_version == expected_version),
        "package_lock_sha256": file_hash(package_lock) if package_lock.is_file() else "",
        "runtime_settings_path": _display_path(settings_path),
        "runtime_settings_safe": bool(not enabled or settings_safe),
        "settings_sha256": settings_sha,
        "compiled_skills_enabled": compiled,
        "skill_supply_chain_status": str(skill_status.get("status") or "UNKNOWN"),
        "skill_bundle_sha256": str(skill_status.get("bundle_sha256") or ""),
        "skill_errors": [str(item) for item in (skill_status.get("errors") or [])],
        "errors": [item for item in (settings_error, package_error) if item],
        "production_tools_enabled": False,
        "dynamic_skill_loading": False,
    }


def _model_snapshot(config: Dict[str, Any]) -> Dict[str, Any]:
    llm = config.get("llm_agent", {}) or {}
    pi = config.get("pi_agent", {}) or {}
    base_url = str(os.environ.get("LLM_BASE_URL") or llm.get("base_url") or "https://api.deepseek.com/v1")
    llm_model = str(os.environ.get("LLM_MODEL") or llm.get("model") or "deepseek-v4-pro")
    llm_key_env = str(llm.get("api_key_env") or ("DEEPSEEK_API_KEY" if "deepseek" in base_url.lower() else "LLM_API_KEY"))
    pi_provider = str(pi.get("provider") or "deepseek")
    pi_key_env = str(pi.get("api_key_env") or ("DEEPSEEK_API_KEY" if pi_provider.lower() == "deepseek" else "LLM_API_KEY"))
    return {
        "llm_agent": {
            "enabled": bool(llm.get("enabled", False)),
            "runtime": str(llm.get("runtime") or "http"),
            "base_url": _safe_url(base_url),
            "model": llm_model,
            "credential_env": llm_key_env,
            "credential_present": bool(llm_key_env and os.environ.get(llm_key_env, "")),
            "credential_value_recorded": False,
        },
        "pi_agent": {
            "enabled": bool(pi.get("enabled", False)),
            "provider": pi_provider,
            "model": str(pi.get("model") or "deepseek-v4-pro"),
            "credential_env": pi_key_env,
            "credential_present": bool(pi_key_env and os.environ.get(pi_key_env, "")),
            "credential_value_recorded": False,
        },
    }


def _runtime_snapshot() -> Dict[str, Any]:
    metadata_path = project_path("pyproject.toml")
    project_version = ""
    requires_python = ""
    if metadata_path.is_file():
        text = metadata_path.read_text(encoding="utf-8-sig", errors="replace")
        version_match = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
        python_match = re.search(r'^requires-python\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
        project_version = version_match.group(1) if version_match else ""
        requires_python = python_match.group(1) if python_match else ""
    return {
        "python_version": platform.python_version(),
        "python_full_version": sys.version.splitlines()[0],
        "implementation": platform.python_implementation(),
        "executable": str(Path(sys.executable).resolve()),
        "requires_python": requires_python,
        "project_version": project_version,
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
    }


def _latest_artifact(label: str, path_value: str, now: datetime) -> Dict[str, Any]:
    path = project_path(path_value)
    if not path.is_file():
        return {
            "label": label,
            "path": _display_path(path),
            "exists": False,
            "sha256": "",
            "bytes": 0,
            "modified_at": "",
            "age_hours": 0.0,
            "freshness": "MISSING",
            "reported_status": "",
            "reported_statuses": {},
            "reported_generated_at": "",
            "candidate_fingerprint": "",
            "parse_error": "",
        }
    stat = path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime).astimezone()
    age_hours = (now - modified).total_seconds() / 3600.0
    if age_hours < -0.1:
        freshness = "FUTURE"
    elif age_hours <= 24:
        freshness = "CURRENT"
    elif age_hours <= 72:
        freshness = "AGING"
    else:
        freshness = "STALE"
    reported_status = ""
    reported_statuses: Dict[str, str] = {}
    reported_generated_at = ""
    fingerprint = ""
    parse_error = ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict):
            for key in _REPORTED_STATUS_KEYS:
                value = payload.get(key)
                if isinstance(value, (str, int, float, bool)) and str(value).strip():
                    if key == "runtime_ready" and isinstance(value, bool):
                        normalized = "READY" if value else "BLOCKED"
                    else:
                        normalized = str(value).strip()[:128]
                    reported_statuses[key] = normalized
                    if not reported_status:
                        reported_status = normalized
            value = payload.get("generated_at")
            if isinstance(value, str):
                reported_generated_at = value[:128]
            value = str(payload.get("candidate_fingerprint") or "")
            if _SAFE_FINGERPRINT.fullmatch(value):
                fingerprint = value.lower()
    except Exception as exc:
        parse_error = str(exc)[:240]
    return {
        "label": label,
        "path": _display_path(path),
        "exists": True,
        "sha256": file_hash(path),
        "bytes": int(stat.st_size),
        "modified_at": modified.isoformat(timespec="seconds"),
        "age_hours": round(age_hours, 3),
        "freshness": freshness,
        "reported_status": reported_status,
        "reported_statuses": reported_statuses,
        "reported_generated_at": reported_generated_at,
        "candidate_fingerprint": fingerprint,
        "parse_error": parse_error,
    }


def _latest_scheduled_summary(now: datetime) -> Optional[Dict[str, Any]]:
    directory = project_path("logs/scheduled")
    if not directory.is_dir():
        return None
    paths = sorted(directory.glob("*.summary.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not paths:
        return None
    entry = _latest_artifact("scheduled_run", str(paths[0]), now)
    entry["path"] = _display_path(paths[0])
    return entry


def _business_enablement(latest: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_label = {str(item.get("label")): item for item in latest}
    pilot = by_label.get("production_pilot", {})
    onboarding = by_label.get("business_onboarding", {})
    pilot_statuses = pilot.get("reported_statuses", {}) or {}
    onboarding_statuses = onboarding.get("reported_statuses", {}) or {}
    stale = [
        str(item.get("label")) for item in latest
        if item.get("freshness") in {"STALE", "MISSING", "FUTURE"}
    ]
    return {
        "status": "NOT_ASSERTED",
        "production_pilot_reported_status": str(
            pilot_statuses.get("go_live_decision") or pilot.get("reported_status") or ""
        ),
        "business_onboarding_reported_status": str(
            onboarding_statuses.get("business_status") or onboarding.get("reported_status") or ""
        ),
        "stale_or_missing_projections": stale,
        "automatic_business_activation": False,
        "human_approval_required": True,
        "note": "Release identity is separate from provider, compliance, archive, publication, and business activation gates.",
    }


def _safe_url(value: str) -> str:
    try:
        parts = urlsplit(str(value or ""))
        if not parts.scheme or not parts.hostname:
            return ""
        host = parts.hostname
        if parts.port:
            host = "{0}:{1}".format(host, parts.port)
        return urlunsplit((parts.scheme, host, parts.path.rstrip("/"), "", ""))
    except Exception:
        return ""


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(project_path(".").resolve())).replace("\\", "/")
    except Exception:
        return str(path)


def _coerce_now(value: Optional[datetime]) -> datetime:
    current = value or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    return current


def _run_command(command: Sequence[str], cwd: Path) -> Tuple[int, str, str]:
    try:
        result = subprocess.run(
            list(command),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        return int(result.returncode), result.stdout or "", result.stderr or ""
    except Exception as exc:
        return 127, "", str(exc)


def _md(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _image_provenance_snapshot(root: Path) -> Dict[str, Any]:
    """R08: read-only provenance-chain verdict for the no-Git audit branch.

    Looks for a shipped PROVENANCE.json plus the EXTERNAL digest binding and
    the runtime-injected expectation; verifies the chain through
    ``stock_agent.release_provenance``. Any missing piece leaves the verdict
    unverified — the caller keeps its Git blocker (no auto-pass)."""
    from .release_provenance import (
        BINDING_FILENAME,
        PROVENANCE_FILENAME,
        ProvenanceError,
        provenance_sha256,
        verify_image_provenance,
    )

    shipped = root / PROVENANCE_FILENAME
    binding_path = root / "deploy" / "docker" / BINDING_FILENAME
    if not shipped.is_file() or not binding_path.is_file():
        return {"verified": False, "problems": ["provenance evidence files absent"]}
    try:
        import json as _json

        record = _json.loads(shipped.read_text(encoding="utf-8"))
        binding = _json.loads(binding_path.read_text(encoding="utf-8"))
        expected = str(binding.get("provenance_sha256") or "")
        observed = str(binding.get("image_digest") or "")
        return verify_image_provenance(
            record, expected, observed, binding)
    except (OSError, ValueError, ProvenanceError) as exc:
        return {"verified": False, "problems": ["provenance evidence unreadable: {0}".format(exc)]}
