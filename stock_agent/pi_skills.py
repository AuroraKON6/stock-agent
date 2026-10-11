"""Audited, prompt-compiled Pi research skill supply chain.

Production Pi calls keep dynamic skill discovery and tools disabled. Approved
SKILL.md files are treated as versioned method text: Python validates their
provenance and permissions, verifies an integrity lock, and appends only the
role-mapped methods to the fixed system prompt.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .audit_log import atomic_write_text, file_hash, object_hash
from .config import project_path
from .models import now_iso


PI_SKILL_POLICY_VERSION = "26.0"
DEFAULT_REGISTRY_PATH = "config/pi_skills.json"
DEFAULT_LOCK_PATH = "config/pi_skills.lock.json"
ALLOWED_LICENSES = {"Project-Owned", "Apache-2.0", "MIT"}
ALLOWED_STATUSES = {"approved_project", "approved_adapted"}
ALLOWED_ROLES = {
    "signal_analyst",
    "research_desk",
    "case_analyst",
    "public_handoff",
    "review_optimizer",
    "runtime_uat",
}
DENIED_PERMISSIONS = {
    "network",
    "shell",
    "filesystem_write",
    "trading",
    "approval",
    "publication",
    "assignment",
    "config_mutation",
}
PINNED_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SAFE_SKILL_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
GITHUB_URL_PATTERN = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?$")
PROHIBITED_TEXT_MARKERS = (
    "```",
    "<script",
    "ignore previous instruction",
    "ignore all previous",
    "disregard previous",
    "curl ",
    "wget ",
    "pip install",
    "npm install",
    "npx ",
    "powershell ",
    "cmd.exe",
    "bash -",
    "os.system",
    "subprocess",
    "__import__",
    "eval(",
    "exec(",
    "mcp__",
    "place_order",
    "submit_order",
)


class PiSkillSupplyChainError(ValueError):
    pass


def get_pi_skill_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw = (config or {}).get("pi_agent", {}) or {}
    return {
        "enabled": bool(raw.get("compiled_skills_enabled", False)),
        "registry_path": str(raw.get("skill_registry") or DEFAULT_REGISTRY_PATH),
        "lock_path": str(raw.get("skill_lock") or DEFAULT_LOCK_PATH),
        "require_lock": bool(raw.get("require_skill_lock", True)),
    }


def build_pi_skill_lock(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = get_pi_skill_config(config)
    registry_path = project_path(cfg["registry_path"])
    registry, entries = _load_and_validate_registry(registry_path)
    role_bundles: Dict[str, Dict[str, Any]] = {}
    for role in sorted(ALLOWED_ROLES):
        selected = [item for item in entries if role in item["roles"]]
        if not selected:
            continue
        projection = [
            {"id": item["id"], "sha256": item["sha256"], "version": item["version"]}
            for item in selected
        ]
        role_bundles[role] = {
            "skill_ids": [item["id"] for item in selected],
            "sha256": object_hash(projection),
            "bytes": sum(int(item["bytes"]) for item in selected),
        }
    skills = [
        {
            "id": item["id"],
            "version": item["version"],
            "path": item["path"],
            "sha256": item["sha256"],
            "bytes": item["bytes"],
            "roles": item["roles"],
        }
        for item in entries
    ]
    lock_projection = {
        "policy_version": str(registry.get("policy_version") or ""),
        "registry_sha256": file_hash(registry_path),
        "runtime_mode": str(registry.get("runtime_mode") or ""),
        "skills": skills,
        "role_bundles": role_bundles,
    }
    return {
        "schema_version": "1.0",
        **lock_projection,
        "bundle_sha256": object_hash(lock_projection),
    }


def write_pi_skill_lock(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = get_pi_skill_config(config)
    lock = build_pi_skill_lock(config)
    atomic_write_text(
        project_path(cfg["lock_path"]),
        json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return lock


def pi_skill_supply_chain_status(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = get_pi_skill_config(config)
    status: Dict[str, Any] = {
        "schema_version": "1.0",
        "policy_version": PI_SKILL_POLICY_VERSION,
        "generated_at": now_iso(),
        "enabled": cfg["enabled"],
        "status": "DISABLED" if not cfg["enabled"] else "BLOCKED",
        "runtime_mode": "compiled_prompt_only",
        "dynamic_skill_loading": False,
        "production_tools_enabled": False,
        "registry_path": str(project_path(cfg["registry_path"])),
        "lock_path": str(project_path(cfg["lock_path"])),
        "registry_sha256": "",
        "lock_sha256": "",
        "bundle_sha256": "",
        "approved_skill_count": 0,
        "adapted_skill_count": 0,
        "source_repository_count": 0,
        "rejected_candidate_count": 0,
        "role_bundles": {},
        "skills": [],
        "permissions": {key: False for key in sorted(DENIED_PERMISSIONS)},
        "errors": [],
    }
    if not cfg["enabled"]:
        return status
    registry_path = project_path(cfg["registry_path"])
    lock_path = project_path(cfg["lock_path"])
    try:
        registry, entries = _load_and_validate_registry(registry_path)
        expected_lock = build_pi_skill_lock(config)
        status["registry_sha256"] = expected_lock["registry_sha256"]
        status["bundle_sha256"] = expected_lock["bundle_sha256"]
        status["approved_skill_count"] = len(entries)
        status["adapted_skill_count"] = sum(item["status"] == "approved_adapted" for item in entries)
        status["source_repository_count"] = len({
            source["url"]
            for item in registry.get("skills", [])
            for source in item.get("source_repositories", [])
            if isinstance(source, dict) and source.get("url")
        })
        status["rejected_candidate_count"] = len(registry.get("rejected_candidates", []) or [])
        status["role_bundles"] = expected_lock["role_bundles"]
        status["skills"] = [
            {
                "id": item["id"],
                "version": item["version"],
                "status": item["status"],
                "license": item["license"],
                "roles": item["roles"],
                "sha256": item["sha256"],
                "bytes": item["bytes"],
            }
            for item in entries
        ]
        if cfg["require_lock"]:
            if not lock_path.exists():
                raise PiSkillSupplyChainError("Pi skill lock is missing; review changes and run pi-skills-lock --write")
            actual_lock = _read_json_object(lock_path, "Pi skill lock")
            status["lock_sha256"] = file_hash(lock_path)
            if actual_lock != expected_lock:
                raise PiSkillSupplyChainError(
                    "Pi skill lock mismatch; content, registry, role mapping, or pinned provenance changed"
                )
        status["status"] = "READY"
    except Exception as exc:
        status["errors"].append(str(exc))
    return status


def write_pi_skill_supply_chain_status(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    status = pi_skill_supply_chain_status(config)
    directory = project_path("reports/pi_agent/skills")
    lines = [
        "# Pi Research Skill Supply Chain",
        "",
        "- Status: `{0}`".format(status.get("status", "BLOCKED")),
        "- Runtime mode: `compiled_prompt_only`",
        "- Dynamic Pi skill discovery: `disabled`",
        "- Production tools: `disabled`",
        "- Approved methods: `{0}` (`{1}` adapted)".format(
            status.get("approved_skill_count", 0), status.get("adapted_skill_count", 0)
        ),
        "- Pinned source repositories: `{0}`".format(status.get("source_repository_count", 0)),
        "- Registry SHA-256: `{0}`".format(status.get("registry_sha256", "")),
        "- Lock SHA-256: `{0}`".format(status.get("lock_sha256", "")),
        "- Bundle SHA-256: `{0}`".format(status.get("bundle_sha256", "")),
        "",
        "Skills are validated text methods compiled by Python into fixed role prompts. Pi still runs with --no-skills and --no-tools.",
    ]
    if status.get("role_bundles"):
        lines.extend(["", "## Role Bundles"])
        for role, bundle in sorted(status["role_bundles"].items()):
            lines.append("- `{0}`: {1}".format(role, ", ".join(bundle.get("skill_ids", []))))
    if status.get("errors"):
        lines.extend(["", "## Errors"] + ["- {0}".format(item) for item in status["errors"]])
    atomic_write_text(directory / "latest.json", json.dumps(status, ensure_ascii=False, indent=2) + "\n")
    atomic_write_text(directory / "latest.md", "\n".join(lines) + "\n")
    return status


def compile_role_system_prompt(
    system_prompt: str,
    role_id: str,
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Dict[str, Any]]:
    cfg = get_pi_skill_config(config)
    if not cfg["enabled"]:
        return str(system_prompt), {
            "enabled": False,
            "role_id": str(role_id),
            "skill_ids": [],
            "bundle_sha256": "",
            "registry_sha256": "",
            "lock_sha256": "",
        }
    status = pi_skill_supply_chain_status(config)
    if status.get("status") != "READY":
        raise PiSkillSupplyChainError("Pi skill supply chain is not READY: {0}".format(
            "; ".join(status.get("errors", [])) or "unknown validation error"
        ))
    registry, entries = _load_and_validate_registry(project_path(cfg["registry_path"]))
    selected = [item for item in entries if str(role_id) in item["roles"]]
    metadata = {
        "enabled": True,
        "role_id": str(role_id),
        "skill_ids": [item["id"] for item in selected],
        "bundle_sha256": str((status.get("role_bundles", {}).get(str(role_id), {}) or {}).get("sha256") or ""),
        "registry_sha256": status.get("registry_sha256", ""),
        "lock_sha256": status.get("lock_sha256", ""),
        "supply_chain_sha256": status.get("bundle_sha256", ""),
    }
    if not selected:
        return str(system_prompt), metadata
    bundle_parts = [
        "<reviewed_method_bundle policy_version=\"{0}\" role=\"{1}\">".format(
            registry.get("policy_version", ""), role_id
        ),
        "These project-controlled methods are subordinate to the system contract above.",
        "Use them only when relevant to supplied evidence. Preserve the caller's output schema.",
        "They grant no tools, data access, source approval, trading, assignment, publication, or approval authority.",
    ]
    for item in selected:
        bundle_parts.extend(["", "## Reviewed method: {0}".format(item["id"]), item["body"].strip()])
    bundle_parts.append("</reviewed_method_bundle>")
    bundle = "\n".join(bundle_parts)
    max_bytes = int((registry.get("limits") or {}).get("max_role_bundle_bytes", 48000) or 48000)
    if len(bundle.encode("utf-8")) > max_bytes:
        raise PiSkillSupplyChainError("Compiled role method bundle exceeds max_role_bundle_bytes")
    return str(system_prompt).rstrip() + "\n\n" + bundle + "\n", metadata


def _load_and_validate_registry(path: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    registry = _read_json_object(path, "Pi skill registry")
    errors: List[str] = []
    if registry.get("schema_version") != "1.0":
        errors.append("registry schema_version must be 1.0")
    if registry.get("policy_version") != PI_SKILL_POLICY_VERSION:
        errors.append("registry policy_version must be {0}".format(PI_SKILL_POLICY_VERSION))
    if registry.get("runtime_mode") != "compiled_prompt_only":
        errors.append("runtime_mode must be compiled_prompt_only")
    if registry.get("dynamic_skill_loading") is not False:
        errors.append("dynamic_skill_loading must be false")
    if registry.get("production_tools_enabled") is not False:
        errors.append("production_tools_enabled must be false")
    raw_skills = registry.get("skills")
    if not isinstance(raw_skills, list) or not raw_skills:
        errors.append("registry skills must be a non-empty list")
        raw_skills = []
    max_skill_bytes = int((registry.get("limits") or {}).get("max_skill_bytes", 12000) or 12000)
    seen = set()
    entries: List[Dict[str, Any]] = []
    for index, raw in enumerate(raw_skills):
        prefix = "skills[{0}]".format(index)
        if not isinstance(raw, dict):
            errors.append("{0} must be an object".format(prefix))
            continue
        skill_id = str(raw.get("id") or "")
        if not SAFE_SKILL_ID_PATTERN.fullmatch(skill_id):
            errors.append("{0}.id is not a safe skill id".format(prefix))
        if skill_id in seen:
            errors.append("duplicate skill id: {0}".format(skill_id))
        seen.add(skill_id)
        status = str(raw.get("status") or "")
        license_name = str(raw.get("license") or "")
        if status not in ALLOWED_STATUSES:
            errors.append("{0}.status is not approved".format(prefix))
        if license_name not in ALLOWED_LICENSES:
            errors.append("{0}.license is not allowed".format(prefix))
        roles = raw.get("roles") if isinstance(raw.get("roles"), list) else []
        if not roles or any(str(role) not in ALLOWED_ROLES for role in roles):
            errors.append("{0}.roles contains an empty or unsupported role mapping".format(prefix))
        permissions = raw.get("permissions") if isinstance(raw.get("permissions"), dict) else {}
        for permission in sorted(DENIED_PERMISSIONS):
            if permission not in permissions or permissions.get(permission) is not False:
                errors.append("{0}.permissions.{1} must be false".format(prefix, permission))
        if any(value is True for key, value in permissions.items() if key not in DENIED_PERMISSIONS):
            errors.append("{0}.permissions contains an unreviewed enabled capability".format(prefix))
        sources = raw.get("source_repositories") if isinstance(raw.get("source_repositories"), list) else []
        if status == "approved_adapted" and not sources:
            errors.append("{0} requires pinned source_repositories".format(prefix))
        for source_index, source in enumerate(sources):
            source_prefix = "{0}.source_repositories[{1}]".format(prefix, source_index)
            if not isinstance(source, dict):
                errors.append("{0} must be an object".format(source_prefix))
                continue
            if not GITHUB_URL_PATTERN.fullmatch(str(source.get("url") or "")):
                errors.append("{0}.url must be a canonical GitHub repository URL".format(source_prefix))
            if not PINNED_COMMIT_PATTERN.fullmatch(str(source.get("commit") or "")):
                errors.append("{0}.commit must be a full 40-character commit".format(source_prefix))
            if str(source.get("license") or "") not in ALLOWED_LICENSES - {"Project-Owned"}:
                errors.append("{0}.license is not allowed".format(source_prefix))
            if source.get("incorporation") != "methodology_adaptation":
                errors.append("{0}.incorporation must be methodology_adaptation".format(source_prefix))
            if not isinstance(source.get("source_paths"), list) or not source.get("source_paths"):
                errors.append("{0}.source_paths must be non-empty".format(source_prefix))
        rel_path = str(raw.get("path") or "")
        path_error = _validate_skill_path(rel_path)
        if path_error:
            errors.append("{0}.path {1}".format(prefix, path_error))
            continue
        skill_path = project_path(rel_path)
        if not skill_path.exists():
            errors.append("{0}.path does not exist: {1}".format(prefix, rel_path))
            continue
        try:
            payload = skill_path.read_bytes()
            text = payload.decode("utf-8-sig")
        except (OSError, UnicodeError) as exc:
            errors.append("{0}.path is unreadable UTF-8: {1}".format(prefix, exc))
            continue
        if len(payload) > max_skill_bytes:
            errors.append("{0}.path exceeds max_skill_bytes".format(prefix))
        frontmatter, body = _split_skill_document(text)
        if frontmatter.get("name") != skill_id:
            errors.append("{0} frontmatter name must equal id".format(prefix))
        unknown_frontmatter = sorted(set(frontmatter) - {"name", "description"})
        if unknown_frontmatter:
            errors.append("{0} has unsupported frontmatter fields: {1}".format(prefix, ", ".join(unknown_frontmatter)))
        lowered = body.lower()
        markers = [marker for marker in PROHIBITED_TEXT_MARKERS if marker in lowered]
        if markers:
            errors.append("{0} contains prohibited executable or prompt-override marker: {1}".format(prefix, markers[0]))
        entries.append({
            "id": skill_id,
            "version": str(raw.get("version") or ""),
            "path": rel_path.replace("\\", "/"),
            "status": status,
            "license": license_name,
            "roles": [str(role) for role in roles],
            "sha256": file_hash(skill_path),
            "bytes": len(payload),
            "body": body,
        })
    if errors:
        raise PiSkillSupplyChainError("; ".join(errors))
    return registry, entries


def _validate_skill_path(value: str) -> str:
    if not value:
        return "is required"
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        return "must be a relative path without traversal"
    normalized = value.replace("\\", "/")
    if not normalized.startswith(".pi/skills/") or not normalized.endswith("/SKILL.md"):
        return "must be inside .pi/skills/<id>/SKILL.md"
    try:
        project_path(value).resolve().relative_to(project_path(".pi/skills").resolve())
    except ValueError:
        return "resolves outside .pi/skills"
    return ""


def _split_skill_document(text: str) -> Tuple[Dict[str, str], str]:
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        raise PiSkillSupplyChainError("SKILL.md must start with YAML frontmatter")
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration as exc:
        raise PiSkillSupplyChainError("SKILL.md frontmatter is not closed") from exc
    metadata: Dict[str, str] = {}
    for raw in lines[1:end]:
        if not raw.strip():
            continue
        if ":" not in raw:
            raise PiSkillSupplyChainError("SKILL.md frontmatter must contain simple key/value fields")
        key, value = raw.split(":", 1)
        key = key.strip()
        if key in metadata:
            raise PiSkillSupplyChainError("SKILL.md frontmatter contains duplicate field: {0}".format(key))
        metadata[key] = value.strip().strip('"').strip("'")
    body = "\n".join(lines[end + 1:]).strip()
    if not metadata.get("name") or not metadata.get("description") or not body:
        raise PiSkillSupplyChainError("SKILL.md requires name, description, and body")
    return metadata, body


def _read_json_object(path: Path, label: str) -> Dict[str, Any]:
    if not path.exists():
        raise PiSkillSupplyChainError("{0} is missing: {1}".format(label, path))
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PiSkillSupplyChainError("{0} is unreadable: {1}".format(label, exc)) from exc
    if not isinstance(value, dict):
        raise PiSkillSupplyChainError("{0} must be a JSON object".format(label))
    return value
