"""Verify locked Python environments and persist redacted test evidence."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .audit_log import atomic_write_text, file_hash, object_hash
from .config import project_path
from .release_controls import build_release_candidate_identity, parse_python_lock


PYTHON_ENVIRONMENT_VERSION = "1.0"
DEFAULT_PYTHONS = (
    ".venv311/Scripts/python.exe",
    ".venv312/Scripts/python.exe",
)
REQUIRED_MINORS = ("3.11", "3.12")

ENVIRONMENT_PROBE_CODE = """import importlib.metadata as m, json, platform, sys
items = sorted((d.metadata.get('Name', ''), d.version) for d in m.distributions() if d.metadata.get('Name'))
print(json.dumps({'version': platform.python_version(), 'implementation': platform.python_implementation(), 'executable': sys.executable, 'packages': items}, sort_keys=True))
"""
SMOKE_PROBE_CODE = """import json, akshare, pysnowball, yaml
print(json.dumps({'akshare': getattr(akshare, '__version__', 'installed'), 'pysnowball': getattr(pysnowball, '__version__', 'installed'), 'pyyaml': getattr(yaml, '__version__', 'installed')}, sort_keys=True))
"""

CommandRunner = Callable[[Sequence[str], Path, Dict[str, str], int], Tuple[int, str, str, float]]


def build_python_environment_audit(
    python_paths: Optional[Sequence[str]] = None,
    *,
    run_tests: bool = False,
    now: Optional[datetime] = None,
    command_runner: Optional[CommandRunner] = None,
) -> Dict[str, Any]:
    generated = _coerce_now(now)
    runner = command_runner or _run_command
    source_before = build_release_candidate_identity()
    lock_path = project_path("requirements.lock.txt")
    expected_components, lock_errors = parse_python_lock(lock_path)
    expected = {item["name"]: item["version"] for item in expected_components}
    environments = []
    for raw_path in list(python_paths or DEFAULT_PYTHONS):
        environments.append(_audit_one_environment(raw_path, expected, run_tests, runner))

    by_minor = {str(item.get("minor") or ""): item for item in environments if item.get("minor")}
    missing_minors = [minor for minor in REQUIRED_MINORS if minor not in by_minor]
    blockers = list(lock_errors)
    blockers.extend("Required Python environment is missing: {0}".format(minor) for minor in missing_minors)
    for item in environments:
        if item["status"] != "PASS":
            blockers.append("Python environment is not PASS: {0}".format(item["path"]))
    source_after = build_release_candidate_identity()
    blockers.extend("Release candidate snapshot failed: {0}".format(item) for item in source_before["errors"])
    blockers.extend("Release candidate snapshot failed: {0}".format(item) for item in source_after["errors"])
    source_stable = bool(source_before["fingerprint"] == source_after["fingerprint"])
    if not source_stable:
        blockers.append("Release candidate changed during Python environment audit")
    status = "PASS" if not blockers else "BLOCKED"
    identity = {
        "release_candidate_fingerprint": source_before["fingerprint"],
        "lock_sha256": file_hash(lock_path) if lock_path.is_file() else "",
        "required_minors": list(REQUIRED_MINORS),
        "environments": [
            {
                "minor": item.get("minor", ""),
                "version": item.get("version", ""),
                "implementation": item.get("implementation", ""),
                "package_fingerprint": item.get("package_fingerprint", ""),
                "lock_match": item.get("lock_match", False),
                "smoke_status": (item.get("smoke_test") or {}).get("status", ""),
                "test_status": (item.get("test_suite") or {}).get("status", ""),
                "test_count": (item.get("test_suite") or {}).get("test_count", 0),
                "skipped_count": (item.get("test_suite") or {}).get("skipped_count", 0),
            }
            for item in sorted(environments, key=lambda value: str(value.get("minor") or value.get("path") or ""))
        ],
    }
    return {
        "schema_version": PYTHON_ENVIRONMENT_VERSION,
        "generated_at": generated.isoformat(timespec="seconds"),
        "status": status,
        "input_fingerprint": object_hash(identity),
        "release_candidate_fingerprint": source_before["fingerprint"],
        "release_candidate_file_count": source_before["file_count"],
        "release_candidate_stable": source_stable,
        "lock_path": _display_path(lock_path),
        "lock_sha256": identity["lock_sha256"],
        "locked_package_count": len(expected),
        "required_minors": list(REQUIRED_MINORS),
        "missing_minors": missing_minors,
        "environments": environments,
        "blockers": blockers,
        "operation_contract": {
            "network_access": False,
            "package_install_or_update": False,
            "llm_call": False,
            "business_state_write": False,
            "approval_or_activation": False,
            "scheduled_task_change": False,
            "trading_or_order_action": False,
            "test_output_recorded": False,
        },
    }


def write_python_environment_audit(
    python_paths: Optional[Sequence[str]] = None,
    *,
    run_tests: bool = False,
    now: Optional[datetime] = None,
    command_runner: Optional[CommandRunner] = None,
) -> Dict[str, Any]:
    audit = build_python_environment_audit(
        python_paths,
        run_tests=run_tests,
        now=now,
        command_runner=command_runner,
    )
    directory = project_path("reports/release")
    json_path = directory / "python_environment_latest.json"
    md_path = directory / "python_environment_latest.md"
    atomic_write_text(json_path, json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    atomic_write_text(md_path, render_python_environment_markdown(audit), encoding="utf-8-sig")
    return {
        "audit": audit,
        "python_environment_json_path": str(json_path),
        "python_environment_path": str(md_path),
    }


def render_python_environment_markdown(audit: Dict[str, Any]) -> str:
    lines = [
        "# Python Environment Audit",
        "",
        "- Generated at: `{0}`".format(audit.get("generated_at", "")),
        "- Status: `{0}`".format(audit.get("status", "BLOCKED")),
        "- Lock SHA-256: `{0}`".format(audit.get("lock_sha256", "")),
        "- Release candidate SHA-256: `{0}`".format(audit.get("release_candidate_fingerprint", "")),
        "- Release candidate stable during audit: `{0}`".format(
            "yes" if audit.get("release_candidate_stable") else "no"
        ),
        "- Locked packages: `{0}`".format(audit.get("locked_package_count", 0)),
        "- Required minors: `{0}`".format(", ".join(audit.get("required_minors", []) or [])),
        "",
        "| Python | Status | Lock match | Smoke | Tests | Skipped | Duration seconds |",
        "|---|---|---:|---|---:|---:|---:|",
    ]
    for item in audit.get("environments", []) or []:
        tests = item.get("test_suite", {}) or {}
        smoke = item.get("smoke_test", {}) or {}
        lines.append("| {0} | {1} | {2} | {3} | {4} | {5} | {6:.2f} |".format(
            item.get("version", "unknown"),
            item.get("status", "BLOCKED"),
            "yes" if item.get("lock_match") else "no",
            smoke.get("status", "NOT_RUN"),
            tests.get("test_count", 0),
            tests.get("skipped_count", 0),
            float(tests.get("duration_seconds", 0) or 0),
        ))
    lines.extend([
        "",
        "Raw test output is not retained; only its SHA-256, parsed counts, exit status, and duration are recorded.",
        "This control installs nothing and does not touch business state, models, schedules, publication, or trading.",
        "",
    ])
    if audit.get("blockers"):
        lines.extend(["## Blockers", ""] + ["- {0}".format(item) for item in audit["blockers"]] + [""])
    return "\n".join(lines)


def _audit_one_environment(
    path_value: str,
    expected: Dict[str, str],
    run_tests: bool,
    runner: CommandRunner,
) -> Dict[str, Any]:
    path = project_path(path_value)
    base = {
        "path": _display_path(path),
        "exists": path.is_file(),
        "status": "BLOCKED",
        "version": "",
        "minor": "",
        "implementation": "",
        "package_count": 0,
        "package_fingerprint": "",
        "lock_match": False,
        "missing_packages": [],
        "version_mismatches": [],
        "extra_packages": [],
        "probe_error": "",
        "smoke_test": {"status": "NOT_RUN", "output_sha256": ""},
        "test_suite": {
            "status": "NOT_RUN",
            "test_count": 0,
            "skipped_count": 0,
            "duration_seconds": 0.0,
            "output_sha256": "",
            "exit_code": None,
            "raw_output_recorded": False,
        },
    }
    if not path.is_file():
        base["probe_error"] = "interpreter not found"
        return base
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONPYCACHEPREFIX"] = str(project_path(".pycache-codex/environment_audit"))
    code, stdout, stderr, _ = runner([str(path), "-c", ENVIRONMENT_PROBE_CODE], project_path("."), env, 60)
    if code != 0:
        base["probe_error"] = _safe_error(stderr or stdout)
        return base
    try:
        payload = json.loads(stdout.strip().splitlines()[-1])
        version = str(payload.get("version") or "")
        installed = {_normalize_name(name): str(value) for name, value in (payload.get("packages") or [])}
    except Exception as exc:
        base["probe_error"] = "probe JSON invalid: {0}".format(str(exc)[:120])
        return base
    minor = ".".join(version.split(".")[:2])
    missing = sorted(name for name in expected if name not in installed)
    mismatches = sorted(
        "{0}: expected {1}, installed {2}".format(name, expected[name], installed[name])
        for name in expected if name in installed and installed[name] != expected[name]
    )
    extras = sorted(name for name in installed if name not in expected)
    package_projection = sorted((name, installed[name]) for name in installed if name in expected)
    base.update({
        "version": version,
        "minor": minor,
        "implementation": str(payload.get("implementation") or ""),
        "package_count": len(installed),
        "package_fingerprint": object_hash(package_projection),
        "lock_match": not missing and not mismatches,
        "missing_packages": missing,
        "version_mismatches": mismatches,
        "extra_packages": extras,
    })

    smoke_code, smoke_out, smoke_err, _ = runner(
        [str(path), "-c", SMOKE_PROBE_CODE], project_path("."), env, 120
    )
    base["smoke_test"] = {
        "status": "PASS" if smoke_code == 0 else "FAIL",
        "output_sha256": _output_hash(smoke_out, smoke_err),
        "error": "" if smoke_code == 0 else _safe_error(smoke_err or smoke_out),
    }

    if run_tests:
        test_code, test_out, test_err, duration = runner(
            [str(path), "-m", "unittest", "discover", "-s", "tests"],
            project_path("."),
            env,
            900,
        )
        combined = (test_out or "") + "\n" + (test_err or "")
        count_match = re.search(r"Ran\s+(\d+)\s+tests?\s+in", combined)
        skipped_match = re.search(r"skipped=(\d+)", combined)
        ok = test_code == 0 and bool(re.search(r"(?m)^OK(?:\s|$|\()", combined)) and bool(count_match)
        base["test_suite"] = {
            "status": "PASS" if ok else "FAIL",
            "test_count": int(count_match.group(1)) if count_match else 0,
            "skipped_count": int(skipped_match.group(1)) if skipped_match else 0,
            "duration_seconds": round(duration, 3),
            "output_sha256": _output_hash(test_out, test_err),
            "exit_code": test_code,
            "raw_output_recorded": False,
        }
    accepted_minor = minor in REQUIRED_MINORS
    base["status"] = "PASS" if bool(
        accepted_minor
        and base["lock_match"]
        and base["smoke_test"]["status"] == "PASS"
        and base["test_suite"]["status"] == "PASS"
    ) else "BLOCKED"
    return base


def _run_command(
    command: Sequence[str],
    cwd: Path,
    env: Dict[str, str],
    timeout: int,
) -> Tuple[int, str, str, float]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            list(command),
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout or "", result.stderr or "", time.monotonic() - started
    except Exception as exc:
        return 127, "", str(exc), time.monotonic() - started


def _normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", str(value or "").strip()).lower()


def _output_hash(stdout: str, stderr: str) -> str:
    return hashlib.sha256(((stdout or "") + "\n" + (stderr or "")).encode("utf-8")).hexdigest()


def _safe_error(value: str) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer [REDACTED]", text)
    return text[:300]


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
