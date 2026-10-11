"""Generate local G0 secret, SBOM, and license control artifacts.

The default path is offline and report-only. Optional PyPI metadata refresh is
explicit because it performs bounded reads from the official package registry.
No secret value or source snippet is ever persisted in a finding.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote

from .audit_log import atomic_write_text, file_hash, object_hash
from .config import load_config, project_path


RELEASE_CONTROLS_VERSION = "1.0"
MAX_SCAN_BYTES = 2 * 1024 * 1024
PYTHON_LOCK_PATH = "requirements.lock.txt"
NPM_LOCK_PATH = "pi_runtime/package-lock.json"
PYTHON_METADATA_CACHE_PATH = "reports/release/python_registry_metadata.json"

ROOT_RELEASE_FILES = (
    ".gitignore",
    "AGENTS.md",
    "README.md",
    "pyproject.toml",
    "requirements-optional.txt",
    "requirements.lock.txt",
)
RELEASE_SOURCE_ROOTS = ("stock_agent", "tests", "scripts", "docs")
PI_RELEASE_FILES = (
    ".pi/SYSTEM.md",
    ".pi/THIRD_PARTY_NOTICES.md",
    ".pi/settings.json",
    "pi_runtime/.npmrc",
    "pi_runtime/SYSTEM.md",
    "pi_runtime/THIRD_PARTY_NOTICES.md",
    "pi_runtime/package.json",
    "pi_runtime/package-lock.json",
    "pi_runtime/agent_home/settings.json",
)
PI_RELEASE_ROOTS = (".pi/skills", "pi_runtime/roles", "pi_runtime/skills")
SCANNABLE_SUFFIXES = {
    "", ".cfg", ".ini", ".json", ".jsonl", ".md", ".npmrc", ".ps1",
    ".py", ".toml", ".txt", ".yaml", ".yml",
}
LOCAL_SENSITIVE_PATTERNS = (
    "config/*.local.json",
    ".env",
    ".env.*",
    "pi_runtime/agent_home/auth.json",
)

SECRET_PATTERNS = (
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), 0),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), 0),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 0),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), 0),
    (
        "assigned_secret",
        re.compile(
            r"(?i)(?:api[_-]?key|client[_-]?secret|access[_-]?token|bearer[_-]?token)"
            r"\s*[:=]\s*[\"']([^\"'\r\n]{12,})[\"']"
        ),
        1,
    ),
)

ALLOWED_LICENSE_IDS = {
    "0BSD",
    "Apache-2.0",
    "BlueOak-1.0.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "CC0-1.0",
    "ISC",
    "MIT",
    "MIT-0",
    "Project-Owned",
    "PSF-2.0",
    "Python-2.0",
    "Unlicense",
    "Zlib",
}
REVIEW_LICENSE_PREFIXES = (
    "AGPL", "BUSL", "EPL", "GPL", "LGPL", "MPL", "SSPL", "Elastic"
)
LICENSE_TEXT_MAP = {
    "apache 2": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "bsd": "BSD-3-Clause",
    "bsd license": "BSD-3-Clause",
    "isc": "ISC",
    "isc license": "ISC",
    "mit": "MIT",
    "mit license": "MIT",
    "python software foundation license": "PSF-2.0",
    "psf": "PSF-2.0",
    "public domain": "Unlicense",
}


def build_release_controls(
    config_path: str = "config/watchlist.local.json",
    *,
    fetch_python_metadata: bool = False,
    python_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build all control payloads without writing them."""
    config = load_config(config_path)
    generated = _coerce_now(now)
    release_files = discover_release_candidate_files()
    local_sensitive = discover_local_sensitive_files()
    secret_scan = build_secret_scan(release_files, local_sensitive, generated)

    python_components, python_errors = parse_python_lock(project_path(PYTHON_LOCK_PATH))
    npm_components, npm_errors = parse_npm_lock(project_path(NPM_LOCK_PATH))
    skill_components, skill_errors = parse_pi_skills(config)
    metadata_cache = dict(python_metadata or load_python_metadata_cache())
    metadata_errors: List[str] = []
    if fetch_python_metadata:
        fetched, metadata_errors = fetch_pypi_metadata(python_components)
        metadata_cache.update(fetched)

    sbom = build_sbom(
        python_components,
        npm_components,
        skill_components,
        metadata_cache,
        python_errors + npm_errors + skill_errors,
        generated,
    )
    license_report = build_license_report(
        sbom,
        metadata_cache,
        metadata_errors,
        generated,
    )
    statuses = {
        "secret_scan": secret_scan["status"],
        "sbom": _sbom_status(sbom),
        "license_report": license_report["status"],
    }
    overall = "PASS" if all(value == "PASS" for value in statuses.values()) else "BLOCKED"
    return {
        "schema_version": RELEASE_CONTROLS_VERSION,
        "generated_at": generated.isoformat(timespec="seconds"),
        "status": overall,
        "statuses": statuses,
        "secret_scan": secret_scan,
        "sbom": sbom,
        "license_report": license_report,
        "python_metadata_cache": metadata_cache,
        "metadata_errors": metadata_errors,
        "operation_contract": {
            "report_write_only": True,
            "network_access": bool(fetch_python_metadata),
            "network_scope": "pypi.org exact-version metadata only" if fetch_python_metadata else "none",
            "llm_call": False,
            "business_state_write": False,
            "approval_or_activation": False,
            "scheduled_task_change": False,
            "trading_or_order_action": False,
        },
    }


def write_release_controls(
    config_path: str = "config/watchlist.local.json",
    *,
    fetch_python_metadata: bool = False,
    python_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    result = build_release_controls(
        config_path,
        fetch_python_metadata=fetch_python_metadata,
        python_metadata=python_metadata,
        now=now,
    )
    report_dir = project_path("reports/release")
    report_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "secret_scan_path": report_dir / "secret_scan_latest.json",
        "sbom_path": report_dir / "sbom.cdx.json",
        "license_report_path": report_dir / "license_report_latest.json",
        "metadata_cache_path": report_dir / "python_registry_metadata.json",
        "summary_json_path": report_dir / "latest.json",
        "summary_path": report_dir / "latest.md",
    }
    atomic_write_text(
        paths["secret_scan_path"],
        json.dumps(result["secret_scan"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    atomic_write_text(
        paths["sbom_path"],
        json.dumps(result["sbom"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    atomic_write_text(
        paths["license_report_path"],
        json.dumps(result["license_report"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    atomic_write_text(
        paths["metadata_cache_path"],
        json.dumps(result["python_metadata_cache"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    summary = {
        "schema_version": result["schema_version"],
        "generated_at": result["generated_at"],
        "status": result["status"],
        "statuses": result["statuses"],
        "secret_scan_path": _display_path(paths["secret_scan_path"]),
        "sbom_path": _display_path(paths["sbom_path"]),
        "license_report_path": _display_path(paths["license_report_path"]),
        "operation_contract": result["operation_contract"],
    }
    atomic_write_text(
        paths["summary_json_path"],
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    atomic_write_text(paths["summary_path"], render_release_controls_markdown(result), encoding="utf-8-sig")
    return {
        "result": result,
        **{key: str(value) for key, value in paths.items()},
    }


def discover_release_candidate_files() -> List[Path]:
    files: Set[Path] = set()
    for relative in ROOT_RELEASE_FILES + PI_RELEASE_FILES:
        path = project_path(relative)
        if path.is_file():
            files.add(path.resolve())
    for root_name in RELEASE_SOURCE_ROOTS + PI_RELEASE_ROOTS:
        root = project_path(root_name)
        if root.is_dir():
            files.update(_iter_scannable_files(root))
    config_dir = project_path("config")
    if config_dir.is_dir():
        for path in config_dir.glob("*.json"):
            if ".local." not in path.name.lower():
                files.add(path.resolve())
    inbox = project_path("data/inbox")
    for name in ("manual_signals.example.jsonl", "full_flow_test_20260429.jsonl"):
        path = inbox / name
        if path.is_file():
            files.add(path.resolve())
    return sorted(files, key=_display_path)


def build_release_candidate_identity(files: Optional[Sequence[Path]] = None) -> Dict[str, Any]:
    """Fingerprint the reviewable source candidate without retaining file contents."""
    entries: List[Dict[str, str]] = []
    errors: List[str] = []
    for path in list(files if files is not None else discover_release_candidate_files()):
        try:
            entries.append({"path": _display_path(path), "sha256": file_hash(path)})
        except Exception as exc:
            errors.append("{0}: {1}".format(_display_path(path), str(exc)[:160]))
    entries.sort(key=lambda item: item["path"])
    return {
        "fingerprint": object_hash(entries),
        "file_count": len(entries),
        "errors": errors,
    }


def discover_local_sensitive_files() -> List[Path]:
    files: Set[Path] = set()
    for pattern in LOCAL_SENSITIVE_PATTERNS:
        if any(marker in pattern for marker in "*?["):
            files.update(path.resolve() for path in project_path(".").glob(pattern) if path.is_file())
        else:
            path = project_path(pattern)
            if path.is_file():
                files.add(path.resolve())
    return sorted(files, key=_display_path)


def build_secret_scan(
    release_files: Sequence[Path],
    local_sensitive_files: Sequence[Path],
    generated: datetime,
) -> Dict[str, Any]:
    findings: List[Dict[str, Any]] = []
    release_identity = build_release_candidate_identity(release_files)
    errors: List[str] = list(release_identity["errors"])
    scanned: List[Dict[str, Any]] = []
    scopes = [
        ("release_candidate", release_files),
        ("local_sensitive", local_sensitive_files),
    ]
    for scope, paths in scopes:
        for path in paths:
            try:
                size = path.stat().st_size
                if size > MAX_SCAN_BYTES:
                    errors.append("file exceeds scan limit: {0}".format(_display_path(path)))
                    continue
                raw = path.read_bytes()
                if b"\x00" in raw:
                    errors.append("binary file skipped: {0}".format(_display_path(path)))
                    continue
                text = raw.decode("utf-8-sig", errors="replace")
                scanned.append({"scope": scope, "path": _display_path(path), "sha256": file_hash(path)})
                for pattern_id, pattern, group_index in SECRET_PATTERNS:
                    for match in pattern.finditer(text):
                        value = match.group(group_index)
                        placeholder = _is_placeholder_secret(value)
                        findings.append({
                            "pattern_id": pattern_id,
                            "scope": scope,
                            "path": _display_path(path),
                            "line": text.count("\n", 0, match.start()) + 1,
                            "classification": "PLACEHOLDER" if placeholder else "BLOCKING",
                            "match_sha256": hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest(),
                            "match_length": len(value),
                            "value_recorded": False,
                            "snippet_recorded": False,
                        })
            except Exception as exc:
                errors.append("{0}: {1}".format(_display_path(path), str(exc)[:160]))
    findings.sort(key=lambda item: (item["classification"], item["path"], item["line"], item["pattern_id"]))
    blocking = sum(item["classification"] == "BLOCKING" for item in findings)
    placeholders = sum(item["classification"] == "PLACEHOLDER" for item in findings)
    status = "FAIL" if blocking else ("REVIEW_REQUIRED" if errors else "PASS")
    input_fingerprint = object_hash(scanned)
    return {
        "schema_version": RELEASE_CONTROLS_VERSION,
        "generated_at": generated.isoformat(timespec="seconds"),
        "status": status,
        "input_fingerprint": input_fingerprint,
        "release_candidate_fingerprint": release_identity["fingerprint"],
        "release_candidate_file_count": release_identity["file_count"],
        "local_sensitive_file_count": len(local_sensitive_files),
        "scanned_file_count": len(scanned),
        "blocking_finding_count": blocking,
        "placeholder_finding_count": placeholders,
        "scan_error_count": len(errors),
        "findings": findings,
        "errors": errors,
        "redaction_policy": {
            "secret_values_recorded": False,
            "source_snippets_recorded": False,
            "match_fingerprint_only": True,
        },
    }


def parse_python_lock(path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    if not path.is_file():
        return [], ["Python lock missing: {0}".format(_display_path(path))]
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    matches = list(re.finditer(r"(?m)^([A-Za-z0-9_.-]+)==([^\s\\]+)\s*\\?", text))
    components: List[Dict[str, Any]] = []
    direct = _direct_requirement_names(project_path("requirements-optional.txt"))
    for index, match in enumerate(matches):
        name = _normalize_package_name(match.group(1))
        version = match.group(2)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.start():end]
        hashes = sorted(set(re.findall(r"--hash=sha256:([0-9a-f]{64})", block)))
        components.append({
            "ecosystem": "pypi",
            "name": name,
            "version": version,
            "bom_ref": "pkg:pypi/{0}@{1}".format(quote(name, safe=".-_"), quote(version, safe=".-_")),
            "purl": "pkg:pypi/{0}@{1}".format(quote(name, safe=".-_"), quote(version, safe=".-_")),
            "hashes": [{"alg": "SHA-256", "content": value} for value in hashes],
            "direct": name in direct,
        })
    errors = [] if components else ["Python lock contains no pinned components"]
    return components, errors


def parse_npm_lock(path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    if not path.is_file():
        return [], ["npm package lock missing: {0}".format(_display_path(path))]
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        packages = payload.get("packages") or {}
        root_dependencies = set((packages.get("") or {}).get("dependencies") or {})
        components: List[Dict[str, Any]] = []
        seen: Set[Tuple[str, str]] = set()
        for package_path, item in packages.items():
            if not package_path or not isinstance(item, dict) or not item.get("version"):
                continue
            name = str(package_path).rsplit("node_modules/", 1)[-1]
            version = str(item.get("version"))
            key = (name, version)
            if key in seen:
                continue
            seen.add(key)
            purl = "pkg:npm/{0}@{1}".format(quote(name, safe="/"), quote(version, safe=".-_"))
            hashes = []
            integrity = str(item.get("integrity") or "")
            if integrity.startswith("sha512-"):
                try:
                    hashes.append({
                        "alg": "SHA-512",
                        "content": base64.b64decode(integrity[7:]).hex(),
                    })
                except Exception:
                    pass
            components.append({
                "ecosystem": "npm",
                "name": name,
                "version": version,
                "bom_ref": purl,
                "purl": purl,
                "hashes": hashes,
                "license": str(item.get("license") or "UNKNOWN"),
                "direct": name in root_dependencies,
            })
        return sorted(components, key=lambda item: (item["name"], item["version"])), []
    except Exception as exc:
        return [], ["npm lock parse failed: {0}".format(str(exc)[:200])]


def parse_pi_skills(config: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    raw = config.get("pi_agent", {}) or {}
    registry_path = project_path(str(raw.get("skill_registry") or "config/pi_skills.json"))
    lock_path = project_path(str(raw.get("skill_lock") or "config/pi_skills.lock.json"))
    if not registry_path.is_file() or not lock_path.is_file():
        return [], ["Pi skill registry or lock missing"]
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8-sig"))
        lock = json.loads(lock_path.read_text(encoding="utf-8-sig"))
        locked = {str(item.get("id")): item for item in (lock.get("skills") or []) if isinstance(item, dict)}
        components = []
        for item in registry.get("skills", []) or []:
            if not isinstance(item, dict):
                continue
            skill_id = str(item.get("id") or "")
            version = str(item.get("version") or "")
            if not skill_id or not version:
                continue
            ref = "pkg:generic/stock-agent-pi-skill/{0}@{1}".format(
                quote(skill_id, safe=".-_"), quote(version, safe=".-_")
            )
            sha = str((locked.get(skill_id) or {}).get("sha256") or "")
            components.append({
                "ecosystem": "pi-skill",
                "name": skill_id,
                "version": version,
                "bom_ref": ref,
                "purl": ref,
                "hashes": [{"alg": "SHA-256", "content": sha}] if re.fullmatch(r"[0-9a-f]{64}", sha) else [],
                "license": str(item.get("license") or "UNKNOWN"),
                "direct": True,
            })
        return components, []
    except Exception as exc:
        return [], ["Pi skill inventory parse failed: {0}".format(str(exc)[:200])]


def build_sbom(
    python_components: Sequence[Dict[str, Any]],
    npm_components: Sequence[Dict[str, Any]],
    skill_components: Sequence[Dict[str, Any]],
    python_metadata: Dict[str, Dict[str, Any]],
    errors: Sequence[str],
    generated: datetime,
) -> Dict[str, Any]:
    project_version = _project_version()
    source_inputs = {
        "python_lock_sha256": _optional_file_hash(project_path(PYTHON_LOCK_PATH)),
        "npm_lock_sha256": _optional_file_hash(project_path(NPM_LOCK_PATH)),
        "pi_registry_sha256": _optional_file_hash(project_path("config/pi_skills.json")),
        "pi_lock_sha256": _optional_file_hash(project_path("config/pi_skills.lock.json")),
        "component_coordinates": [
            item["bom_ref"] for item in list(python_components) + list(npm_components) + list(skill_components)
        ],
        "python_license_metadata": {
            key: {
                "license_expression": str(value.get("license_expression") or ""),
                "license": str(value.get("license") or ""),
                "license_classifiers": list(value.get("license_classifiers") or []),
            }
            for key, value in sorted(python_metadata.items())
            if isinstance(value, dict)
        },
    }
    input_fingerprint = object_hash(source_inputs)
    control_status = "PASS" if not errors and python_components and npm_components else "FAIL"
    components = []
    for item in list(python_components) + list(npm_components) + list(skill_components):
        component = {
            "type": "library",
            "bom-ref": item["bom_ref"],
            "name": item["name"],
            "version": item["version"],
            "purl": item["purl"],
            "hashes": item.get("hashes", []),
            "properties": [
                {"name": "stock-agent:ecosystem", "value": item["ecosystem"]},
                {"name": "stock-agent:direct-dependency", "value": str(bool(item.get("direct"))).lower()},
            ],
        }
        license_id = str(item.get("license") or "")
        if item["ecosystem"] == "pypi":
            license_id = _python_license(item, python_metadata)
        if license_id and license_id != "UNKNOWN":
            component["licenses"] = [_cyclonedx_license(license_id)]
        components.append(component)
    project_ref = "pkg:pypi/stock-agent@{0}".format(quote(project_version, safe=".-_"))
    direct_refs = sorted(item["bom_ref"] for item in list(python_components) + list(npm_components) + list(skill_components) if item.get("direct"))
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": "urn:uuid:{0}".format(uuid.uuid5(uuid.NAMESPACE_URL, "stock-agent:" + input_fingerprint)),
        "version": 1,
        "metadata": {
            "timestamp": generated.isoformat(timespec="seconds"),
            "tools": {
                "components": [{
                    "type": "application",
                    "name": "stock-agent-release-controls",
                    "version": RELEASE_CONTROLS_VERSION,
                }]
            },
            "component": {
                "type": "application",
                "bom-ref": project_ref,
                "name": "stock-agent",
                "version": project_version,
                "purl": project_ref,
            },
        },
        "components": sorted(components, key=lambda item: item["bom-ref"]),
        "dependencies": [{"ref": project_ref, "dependsOn": direct_refs}],
        "properties": [
            {"name": "stock-agent:control-status", "value": control_status},
            {"name": "stock-agent:input-fingerprint", "value": input_fingerprint},
            {"name": "stock-agent:python-lock-target", "value": "CPython 3.11 / Windows"},
            {"name": "stock-agent:error-count", "value": str(len(errors))},
        ],
        "externalReferences": [],
    }


def build_license_report(
    sbom: Dict[str, Any],
    python_metadata: Dict[str, Dict[str, Any]],
    metadata_errors: Sequence[str],
    generated: datetime,
) -> Dict[str, Any]:
    rows = []
    for component in sbom.get("components", []) or []:
        ecosystem = _component_property(component, "stock-agent:ecosystem")
        expression = _component_license(component)
        if ecosystem == "pypi" and expression == "UNKNOWN":
            key = _metadata_key(component.get("name", ""), component.get("version", ""))
            expression = _license_from_metadata(python_metadata.get(key, {}))
        classification = _classify_license(expression)
        rows.append({
            "bom_ref": str(component.get("bom-ref") or ""),
            "ecosystem": ecosystem,
            "name": str(component.get("name") or ""),
            "version": str(component.get("version") or ""),
            "license": expression,
            "classification": classification,
            "legal_approval_granted": False,
        })
    counts = Counter(item["classification"] for item in rows)
    if counts.get("DENIED", 0):
        status = "FAIL"
    elif counts.get("REVIEW_REQUIRED", 0) or metadata_errors:
        status = "REVIEW_REQUIRED"
    else:
        status = "PASS"
    input_fingerprint = object_hash([
        {"bom_ref": item["bom_ref"], "license": item["license"], "classification": item["classification"]}
        for item in rows
    ])
    return {
        "schema_version": RELEASE_CONTROLS_VERSION,
        "generated_at": generated.isoformat(timespec="seconds"),
        "status": status,
        "input_fingerprint": input_fingerprint,
        "component_count": len(rows),
        "allowed_count": counts.get("ALLOWED", 0),
        "review_required_count": counts.get("REVIEW_REQUIRED", 0),
        "denied_count": counts.get("DENIED", 0),
        "metadata_error_count": len(metadata_errors),
        "components": rows,
        "metadata_errors": list(metadata_errors),
        "policy": {
            "allowed_spdx_ids": sorted(ALLOWED_LICENSE_IDS),
            "copyleft_or_unknown_requires_human_review": True,
            "automatic_legal_approval": False,
            "note": "PASS is a mechanical inventory result, not legal approval for distribution or commercial use.",
        },
    }


def fetch_pypi_metadata(
    python_components: Sequence[Dict[str, Any]],
    *,
    max_workers: int = 6,
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    fetched: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    unique = {(item["name"], item["version"]) for item in python_components}
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 8))) as executor:
        futures = {
            executor.submit(_fetch_one_pypi_metadata, name, version): (name, version)
            for name, version in sorted(unique)
        }
        for future in as_completed(futures):
            name, version = futures[future]
            try:
                fetched[_metadata_key(name, version)] = future.result()
            except Exception as exc:
                errors.append("{0}=={1}: {2}".format(name, version, str(exc)[:180]))
    return fetched, sorted(errors)


def load_python_metadata_cache() -> Dict[str, Dict[str, Any]]:
    path = project_path(PYTHON_METADATA_CACHE_PATH)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def render_release_controls_markdown(result: Dict[str, Any]) -> str:
    secret = result["secret_scan"]
    sbom = result["sbom"]
    licenses = result["license_report"]
    return "\n".join([
        "# G0 Release Controls",
        "",
        "- Generated at: `{0}`".format(result["generated_at"]),
        "- Aggregate status: `{0}`".format(result["status"]),
        "- Secret scan: `{0}` ({1} blocking, {2} placeholders)".format(
            secret["status"], secret["blocking_finding_count"], secret["placeholder_finding_count"]
        ),
        "- SBOM: `{0}` ({1} components)".format(_sbom_status(sbom), len(sbom.get("components", []))),
        "- License inventory: `{0}` ({1} allowed, {2} review, {3} denied)".format(
            licenses["status"], licenses["allowed_count"], licenses["review_required_count"], licenses["denied_count"]
        ),
        "- Network used: `{0}`".format("yes" if result["operation_contract"]["network_access"] else "no"),
        "",
        "Secret values and source snippets are never written to these reports. License PASS is not legal approval.",
        "Business state, provider approval, model calls, scheduling, publication, and trading are outside this command.",
        "",
    ])


def _fetch_one_pypi_metadata(name: str, version: str) -> Dict[str, Any]:
    url = "https://pypi.org/pypi/{0}/{1}/json".format(quote(name, safe=".-_"), quote(version, safe=".-_"))
    request = urllib.request.Request(url, headers={"User-Agent": "stock-agent-release-controls/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("metadata response exceeds size limit")
    payload = json.loads(raw.decode("utf-8"))
    info = payload.get("info") or {}
    classifiers = [
        str(item) for item in (info.get("classifiers") or [])
        if str(item).startswith("License ::")
    ]
    return {
        "name": _normalize_package_name(str(info.get("name") or name)),
        "version": str(info.get("version") or version),
        "license_expression": str(info.get("license_expression") or "")[:240],
        "license": str(info.get("license") or "")[:500],
        "license_classifiers": classifiers,
        "source": "https://pypi.org/pypi/{0}/{1}/json".format(name, version),
    }


def _python_license(component: Dict[str, Any], metadata: Dict[str, Dict[str, Any]]) -> str:
    key = _metadata_key(component["name"], component["version"])
    value = _license_from_metadata(metadata.get(key, {}))
    if value != "UNKNOWN":
        return value
    try:
        distribution = importlib_metadata.distribution(component["name"])
        if str(distribution.version) == str(component["version"]):
            local = {
                "license_expression": distribution.metadata.get("License-Expression", ""),
                "license": distribution.metadata.get("License", ""),
                "license_classifiers": distribution.metadata.get_all("Classifier") or [],
            }
            return _license_from_metadata(local)
    except Exception:
        pass
    return "UNKNOWN"


def _license_from_metadata(metadata: Dict[str, Any]) -> str:
    expression = str(metadata.get("license_expression") or "").strip()
    if expression and expression.upper() not in {"UNKNOWN", "NONE", "N/A"}:
        return expression[:240]
    raw = " ".join(str(metadata.get("license") or "").strip().split())
    mapped = LICENSE_TEXT_MAP.get(raw.lower())
    if mapped:
        return mapped
    if raw in ALLOWED_LICENSE_IDS or any(raw.startswith(prefix) for prefix in REVIEW_LICENSE_PREFIXES):
        return raw[:240]
    for classifier in metadata.get("license_classifiers", []) or []:
        text = str(classifier).lower()
        if "mit license" in text:
            return "MIT"
        if "apache software license" in text:
            return "Apache-2.0"
        if "bsd license" in text:
            return "BSD-3-Clause"
        if "isc license" in text:
            return "ISC"
        if "gnu affero" in text:
            return "AGPL"
        if "gnu lesser" in text:
            return "LGPL"
        if "gnu general" in text:
            return "GPL"
        if "mozilla public" in text:
            return "MPL-2.0"
    return "UNKNOWN"


def _classify_license(expression: str) -> str:
    value = str(expression or "UNKNOWN").strip()
    if value in {"", "UNKNOWN", "NOASSERTION"}:
        return "REVIEW_REQUIRED"
    tokens = set(re.findall(r"[A-Za-z0-9.+-]+", value))
    tokens.difference_update({"AND", "OR", "WITH"})
    if tokens and tokens.issubset(ALLOWED_LICENSE_IDS):
        return "ALLOWED"
    if any(any(token.startswith(prefix) for prefix in REVIEW_LICENSE_PREFIXES) for token in tokens):
        return "REVIEW_REQUIRED"
    if value in ALLOWED_LICENSE_IDS:
        return "ALLOWED"
    return "REVIEW_REQUIRED"


def _cyclonedx_license(value: str) -> Dict[str, Dict[str, str]]:
    if value == "Project-Owned":
        return {"license": {"name": value}}
    if value in ALLOWED_LICENSE_IDS or any(value.startswith(prefix) for prefix in REVIEW_LICENSE_PREFIXES):
        return {"license": {"id": value}}
    return {"license": {"name": value[:240]}}


def _component_license(component: Dict[str, Any]) -> str:
    licenses = component.get("licenses") or []
    if not licenses:
        return "UNKNOWN"
    license_value = (licenses[0] or {}).get("license") or {}
    return str(license_value.get("id") or license_value.get("name") or "UNKNOWN")


def _component_property(component: Dict[str, Any], name: str) -> str:
    for item in component.get("properties", []) or []:
        if isinstance(item, dict) and item.get("name") == name:
            return str(item.get("value") or "")
    return ""


def _sbom_status(sbom: Dict[str, Any]) -> str:
    for item in sbom.get("properties", []) or []:
        if isinstance(item, dict) and item.get("name") == "stock-agent:control-status":
            return str(item.get("value") or "INVALID")
    return "INVALID"


def _direct_requirement_names(path: Path) -> Set[str]:
    if not path.is_file():
        return set()
    result = set()
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        value = line.split(";", 1)[0].strip()
        match = re.match(r"([A-Za-z0-9_.-]+)", value)
        if match:
            result.add(_normalize_package_name(match.group(1)))
    return result


def _project_version() -> str:
    path = project_path("pyproject.toml")
    if not path.is_file():
        return "0.0.0"
    match = re.search(
        r'(?m)^version\s*=\s*["\']([^"\']+)["\']',
        path.read_text(encoding="utf-8-sig", errors="replace"),
    )
    return match.group(1) if match else "0.0.0"


def _iter_scannable_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in SCANNABLE_SUFFIXES:
            yield path.resolve()


def _is_placeholder_secret(value: str) -> bool:
    lowered = str(value or "").lower()
    markers = ("test", "fake", "dummy", "example", "sample", "placeholder", "secret", "changeme", "must-not")
    if any(marker in lowered for marker in markers):
        return True
    body = lowered[3:] if lowered.startswith("sk-") else lowered
    if "1234567890" in body or "abcdef" in body or "qwerty" in body:
        return True
    compact = re.sub(r"[^a-z0-9]", "", body)
    if compact and len(set(compact)) <= 2:
        return True
    return False


def _normalize_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", str(value or "").strip()).lower()


def _metadata_key(name: str, version: str) -> str:
    return "{0}=={1}".format(_normalize_package_name(name), version)


def _optional_file_hash(path: Path) -> str:
    return file_hash(path) if path.is_file() else ""


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
