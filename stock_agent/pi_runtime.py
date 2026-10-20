"""Isolated Pi Agent subprocess runtime.

Pi is used only as the LLM execution harness. Deterministic workflow control,
data-governance gates, protected human decisions, and audit ownership remain in
Python. Production calls run without tools, project trust, sessions, packages,
or extensions and receive only a governance-approved prompt over stdin.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .audit_log import append_hash_chained_jsonl, atomic_write_text, file_hash
from .config import project_path
from .model_operations import LLMResponse, response_metadata
from .models import now_iso
from .pi_skills import (
    PiSkillSupplyChainError,
    compile_role_system_prompt,
    pi_skill_supply_chain_status,
)


PI_RUNTIME_VERSION = "25.0"
PINNED_PI_VERSION = "0.84.2"
PINNED_PI_PACKAGE = "@earendil-works/pi-coding-agent"
SECRET_PATTERN = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{8,}|api[_-]?key\s*[:=]\s*\S+|bearer\s+\S+)",
    re.IGNORECASE,
)
WINDOWS_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\r\n\t ]+")
SAFE_ENV_KEYS = {
    "ALL_PROXY",
    "APPDATA",
    "COMSPEC",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "HTPPS_PROXY",
    "LOCALAPPDATA",
    "NO_PROXY",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERDOMAIN",
    "USERNAME",
    "WINDIR",
}


class PiRuntimeError(RuntimeError):
    # R02 outbound classification tags (read by invocation_runtime.classify_exception):
    # failure_stage = preflight | process | parse | audit; sent_hint = no|yes|unknown.
    # Untagged errors default to the conservative 'unknown' (never auto-retry).
    failure_stage = ""
    sent_hint = ""


def _tagged_error(message: str, *, stage: str, sent_hint: str) -> PiRuntimeError:
    error = PiRuntimeError(message)
    error.failure_stage = stage
    error.sent_hint = sent_hint
    return error


# Message fragments that answer "did the request leave?" for exit-code errors
# whose stderr we cannot otherwise interpret. Kept local so pi_runtime stays
# import-free of the classification layer.
_EXIT_TEXT_NOT_SENT = (
    "connection refused", "getaddrinfo", "name or service not known",
    "failed to resolve", "no route to host", "network is unreachable",
)
_EXIT_TEXT_SENT_DEFINITIVE = (
    "http error 4", "http error 5", "status 4", "status 5", "statuscode=4",
    "statuscode=5", "401", "403", "429", "unauthorized",
)


def _exit_sent_hint(error_text: str) -> str:
    lowered = str(error_text or "").lower()
    for marker in _EXIT_TEXT_NOT_SENT:
        if marker in lowered:
            return "no"
    for marker in _EXIT_TEXT_SENT_DEFINITIVE:
        if marker in lowered:
            return "yes"
    return "unknown"


PI_UAT_SYSTEM_PROMPT = """You are executing a synthetic runtime acceptance test.
Return exactly one JSON object with these keys and values:
{"status":"ok","runtime":"pi","human_decision_required":true}
Do not add markdown, commentary, tools, actions, financial analysis, or extra keys."""

PI_SKILLS_UAT_SYSTEM_PROMPT = """You are executing a synthetic reviewed-method acceptance test.
Apply the reviewed source-provenance and risk-control methods included below.
Return exactly one JSON object with these keys and values:
{"status":"ok","runtime":"pi","skill_bundle":"verified","conflict_detected":true,"human_decision_required":true,"automatic_trading_executed":false}
Do not add markdown, commentary, tools, actions, financial analysis, or extra keys."""


def get_pi_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw = (config or {}).get("pi_agent", {}) or {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "package": str(raw.get("package") or PINNED_PI_PACKAGE),
        "version": str(raw.get("version") or PINNED_PI_VERSION),
        "provider": str(raw.get("provider") or "deepseek"),
        "model": str(raw.get("model") or "deepseek-v4-pro"),
        "thinking": str(raw.get("thinking") or "high"),
        "timeout": max(1, int(raw.get("timeout", 60) or 60)),
        "binary": str(raw.get("binary") or ""),
        "agent_home": str(raw.get("agent_home") or "pi_runtime/agent_home"),
        "workspace": str(raw.get("workspace") or "data/pi_runtime/workspace"),
        "audit_dir": str(raw.get("audit_dir") or "logs/pi_runtime"),
        "max_stdout_bytes": max(65536, int(raw.get("max_stdout_bytes", 4 * 1024 * 1024) or 0)),
        "max_stderr_bytes": max(4096, int(raw.get("max_stderr_bytes", 256 * 1024) or 0)),
        "allow_tools": False,
        "allow_project_trust": False,
        "allow_sessions": False,
        "allow_packages": False,
        "allow_extensions": False,
        "allow_legacy_http_fallback": bool(raw.get("allow_legacy_http_fallback", False)),
    }


def provider_uses_pi(provider: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> bool:
    runtime = str(provider.get("runtime") or (config or {}).get("llm_agent", {}).get("runtime") or "http")
    return runtime.strip().lower() == "pi"


def call_pi_agent(
    *,
    system_prompt: str,
    user_content: str,
    provider: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    role_id: str = "signal_analyst",
    timeout: Optional[int] = None,
) -> LLMResponse:
    """Execute one no-tool, no-session Pi turn and return its final text."""
    provider = dict(provider or {})
    pi_cfg = get_pi_config(config)
    if not pi_cfg["enabled"]:
        raise _tagged_error("Pi Agent runtime is disabled", stage="preflight", sent_hint="no")

    expected_version = str(pi_cfg["version"])
    command, package_json = resolve_pi_command(pi_cfg)
    installed_version = _installed_pi_version(package_json)
    if installed_version and installed_version != expected_version:
        raise _tagged_error(
            "Pi version mismatch: expected {0}, installed {1}".format(expected_version, installed_version),
            stage="preflight", sent_hint="no")

    pi_provider = str(provider.get("pi_provider") or pi_cfg["provider"] or "deepseek")
    model = str(provider.get("model") or pi_cfg["model"])
    thinking = str(provider.get("thinking") or pi_cfg["thinking"] or "high")
    if thinking == "enabled":
        thinking = "high"
    elif thinking == "disabled":
        thinking = "off"
    if thinking not in {"off", "minimal", "low", "medium", "high", "xhigh", "max"}:
        thinking = "high"
    timeout_seconds = max(1, int(timeout or provider.get("timeout") or pi_cfg["timeout"]))
    api_key_env = str(provider.get("api_key_env") or _provider_api_key_env(pi_provider))
    if not api_key_env or not os.environ.get(api_key_env, ""):
        raise _tagged_error(
            "{0} not set, skipped Pi Agent call".format(api_key_env or "provider API key"),
            stage="preflight", sent_hint="no")

    workspace = project_path(str(pi_cfg["workspace"])) / _safe_segment(role_id)
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        compiled_prompt, skill_bundle = compile_role_system_prompt(system_prompt, role_id, config)
    except PiSkillSupplyChainError as exc:
        raise _tagged_error(
            "Pi reviewed-method supply chain blocked: {0}".format(exc),
            stage="preflight", sent_hint="no") from exc
    prompt_path = _materialize_system_prompt(compiled_prompt, role_id)
    agent_home = project_path(str(pi_cfg["agent_home"]))
    settings_path = agent_home / "settings.json"
    if not settings_path.exists():
        raise _tagged_error(
            "Pi isolated settings missing: {0}".format(settings_path),
            stage="preflight", sent_hint="no")

    args = list(command) + [
        "--mode",
        "json",
        "--print",
        "--no-session",
        "--no-tools",
        "--no-approve",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--provider",
        pi_provider,
        "--model",
        model,
        "--thinking",
        thinking,
        "--system-prompt",
        str(prompt_path),
    ]
    env = _isolated_environment(agent_home, workspace, api_key_env)
    started_at = now_iso()
    started = time.perf_counter()
    success = False
    stdout = ""
    stderr = ""
    error_text = ""
    parsed: Dict[str, Any] = {}
    try:
        exit_code, stdout, stderr = _run_process(
            args,
            cwd=workspace,
            env=env,
            stdin_text=str(user_content or ""),
            timeout=timeout_seconds,
        )
        _enforce_output_limit(stdout, int(pi_cfg["max_stdout_bytes"]), "stdout")
        _enforce_output_limit(stderr, int(pi_cfg["max_stderr_bytes"]), "stderr")
        if exit_code != 0:
            raise _tagged_error(
                "Pi exited with code {0}: {1}".format(exit_code, _sanitize_error(stderr or stdout)),
                stage="process", sent_hint=_exit_sent_hint(stderr or stdout))
        parsed = parse_pi_json_stream(stdout)
        if parsed["tool_call_count"]:
            raise _tagged_error("Pi emitted a tool call despite --no-tools",
                                stage="parse", sent_hint="yes")
        if parsed["stop_reason"] not in {"", "stop"}:
            raise _tagged_error(
                "Pi returned unusable stop reason: {0}{1}".format(
                    parsed["stop_reason"],
                    " ({0})".format(parsed["error_message"]) if parsed["error_message"] else "",
                ),
                stage="parse", sent_hint="yes")
        if not str(parsed["content"]).strip():
            raise _tagged_error("Pi response contained no assistant text",
                                stage="parse", sent_hint="yes")
        success = True
    except Exception as exc:
        error_text = _sanitize_error(exc)
        if isinstance(exc, PiRuntimeError):
            raise
        if isinstance(exc, subprocess.TimeoutExpired):
            # The child was running and may already have sent the request:
            # killing it on timeout never proves the provider did not charge.
            raise _tagged_error(
                "Pi wall-clock timeout after {0}s".format(timeout_seconds),
                stage="process", sent_hint="unknown") from exc
        raise _tagged_error(
            "Pi call failed: {0}".format(error_text),
            stage="process", sent_hint="") from exc
    finally:
        duration_ms = int((time.perf_counter() - started) * 1000)
        audit = {
            "event_type": "pi_agent_call",
            "schema_version": PI_RUNTIME_VERSION,
            "started_at": started_at,
            "finished_at": now_iso(),
            "role_id": str(role_id),
            "runtime": "pi",
            "pi_package": str(pi_cfg["package"]),
            "pi_version": installed_version or expected_version,
            "provider": pi_provider,
            "model": str(parsed.get("model") or model),
            "success": success,
            "duration_ms": duration_ms,
            "input_sha256": _text_hash(str(user_content or "")),
            "system_prompt_sha256": file_hash(prompt_path),
            "compiled_skill_bundle_enabled": bool(skill_bundle.get("enabled")),
            "compiled_skill_ids": list(skill_bundle.get("skill_ids", [])),
            "compiled_skill_role_bundle_sha256": str(skill_bundle.get("bundle_sha256") or ""),
            "compiled_skill_supply_chain_sha256": str(skill_bundle.get("supply_chain_sha256") or ""),
            "compiled_skill_registry_sha256": str(skill_bundle.get("registry_sha256") or ""),
            "compiled_skill_lock_sha256": str(skill_bundle.get("lock_sha256") or ""),
            "output_sha256": _text_hash(str(parsed.get("content") or stdout or "")),
            "tool_call_count": int(parsed.get("tool_call_count", 0) or 0),
            "session_persisted": False,
            "project_trust": "denied",
            "tools_enabled": [],
            "request_id": str(parsed.get("request_id") or parsed.get("session_id") or ""),
            "stop_reason": str(parsed.get("stop_reason") or ""),
            "usage": _normalize_usage(parsed.get("usage", {})),
            "error_class": "" if success else "PiRuntimeError",
            "error": error_text,
            "stderr": _sanitize_error(stderr)[:1000],
            "automatic_assignment_executed": False,
            "automatic_approval_executed": False,
            "automatic_publication_executed": False,
            "automatic_trading_executed": False,
        }
        try:
            audit_record = append_hash_chained_jsonl(_audit_path(pi_cfg), audit)
        except Exception as audit_exc:
            if success:
                raise _tagged_error(
                    "Pi output discarded because audit append failed: {0}".format(audit_exc),
                    stage="audit", sent_hint="yes") from audit_exc
            audit_record = {}

    usage = _normalize_usage(parsed.get("usage", {}))
    return LLMResponse(str(parsed["content"]), {
        "request_id": str(parsed.get("request_id") or parsed.get("session_id") or ""),
        "model": str(parsed.get("model") or model),
        "finish_reason": str(parsed.get("stop_reason") or "stop"),
        "system_fingerprint": "pi:{0}:{1}".format(installed_version or expected_version, audit_record.get("event_id", "")),
        "usage": usage,
    })


def resolve_pi_command(pi_cfg: Optional[Dict[str, Any]] = None) -> Tuple[List[str], Optional[Path]]:
    cfg = pi_cfg or get_pi_config({})
    explicit = os.environ.get("PI_AGENT_BINARY", "") or str(cfg.get("binary") or "")
    if explicit:
        path = project_path(explicit) if not Path(explicit).is_absolute() else Path(explicit)
        if not path.exists():
            found = shutil.which(explicit)
            if not found:
                raise _tagged_error("Pi binary not found: {0}".format(explicit),
                            stage="preflight", sent_hint="no")
            path = Path(found)
        return [str(path)], _package_json_near_binary(path)

    package_json = project_path("pi_runtime/node_modules/@earendil-works/pi-coding-agent/package.json")
    cli_js = package_json.parent / "dist" / "cli.js"
    node = shutil.which("node")
    if node and package_json.exists() and cli_js.exists():
        return [node, str(cli_js)], package_json

    found = shutil.which("pi")
    if found:
        path = Path(found)
        return [str(path)], _package_json_near_binary(path)
    raise _tagged_error("Pi runtime not installed; run npm install in pi_runtime",
                        stage="preflight", sent_hint="no")


def parse_pi_json_stream(stdout: str) -> Dict[str, Any]:
    events: List[Dict[str, Any]] = []
    invalid_lines: List[str] = []
    for raw in str(stdout or "").lstrip("\ufeff").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            invalid_lines.append(line[:200])
            continue
        if isinstance(event, dict):
            events.append(event)
    if invalid_lines:
        raise _tagged_error("Pi JSON stream contained non-JSON stdout", stage="parse", sent_hint="yes")
    if not events:
        raise _tagged_error("Pi JSON stream was empty", stage="parse", sent_hint="yes")

    session = next((item for item in events if item.get("type") == "session"), {})
    tool_calls = sum(1 for item in events if item.get("type") == "tool_execution_start")
    messages = []
    for item in events:
        if item.get("type") != "message_end":
            continue
        message = item.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            messages.append(message)
    if not messages:
        raise _tagged_error("Pi JSON stream has no final assistant message", stage="parse", sent_hint="yes")
    message = messages[-1]
    text_parts = []
    for part in message.get("content", []) if isinstance(message.get("content"), list) else []:
        if isinstance(part, dict) and part.get("type") == "text":
            text_parts.append(str(part.get("text") or ""))
    return {
        "content": "".join(text_parts),
        "provider": str(message.get("provider") or ""),
        "model": str(message.get("responseModel") or message.get("model") or ""),
        "request_id": str(message.get("responseId") or ""),
        "session_id": str(session.get("id") or ""),
        "stop_reason": str(message.get("stopReason") or ""),
        "error_message": str(message.get("errorMessage") or ""),
        "usage": message.get("usage", {}) if isinstance(message.get("usage"), dict) else {},
        "tool_call_count": tool_calls,
        "event_count": len(events),
    }


def pi_runtime_status(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = get_pi_config(config)
    skill_supply_chain = pi_skill_supply_chain_status(config)
    status = {
        "schema_version": PI_RUNTIME_VERSION,
        "generated_at": now_iso(),
        "enabled": cfg["enabled"],
        "expected_package": cfg["package"],
        "expected_version": cfg["version"],
        "installed": False,
        "installed_version": "",
        "version_match": False,
        "settings_present": False,
        "settings_safe": False,
        "api_key_present": False,
        "runtime_ready": False,
        "package_lock_sha256": "",
        "skill_supply_chain": skill_supply_chain,
        "security": {
            "tools_enabled": False,
            "project_trust": False,
            "sessions_enabled": False,
            "packages_enabled": False,
            "extensions_enabled": False,
            "skills_enabled": False,
            "compiled_reviewed_methods_enabled": bool(skill_supply_chain.get("enabled")),
            "prompt_templates_enabled": False,
            "themes_enabled": False,
            "context_files_enabled": False,
        },
        "errors": [],
    }
    try:
        command, package_json = resolve_pi_command(cfg)
        status["command"] = command
        status["installed"] = True
        status["installed_version"] = _installed_pi_version(package_json)
        status["version_match"] = status["installed_version"] == cfg["version"]
    except Exception as exc:
        status["errors"].append(str(exc))
    settings_path = project_path(str(cfg["agent_home"])) / "settings.json"
    status["settings_present"] = settings_path.exists()
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
            status["settings_safe"] = (
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
            status["settings_sha256"] = file_hash(settings_path)
        except Exception as exc:
            status["errors"].append("settings unreadable: {0}".format(exc))
    package_lock = project_path("pi_runtime/package-lock.json")
    if package_lock.exists():
        status["package_lock_sha256"] = file_hash(package_lock)
    else:
        status["errors"].append("package lock missing: {0}".format(package_lock))
    status["api_key_present"] = bool(os.environ.get(_provider_api_key_env(str(cfg["provider"])), ""))
    if skill_supply_chain.get("enabled") and skill_supply_chain.get("status") != "READY":
        status["errors"].extend(
            "skill supply chain: {0}".format(item)
            for item in skill_supply_chain.get("errors", [])
        )
    status["runtime_ready"] = bool(
        status["installed"]
        and status["version_match"]
        and status["settings_present"]
        and status["settings_safe"]
        and status["api_key_present"]
        and not status["errors"]
    )
    return status


def write_pi_runtime_status(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    status = pi_runtime_status(config)
    directory = project_path("reports/pi_agent/runtime")
    json_text = json.dumps(status, ensure_ascii=False, indent=2, default=str)
    lines = [
        "# Pi Agent Runtime",
        "",
        "- Status: `{0}`".format("READY" if status.get("runtime_ready") else "BLOCKED"),
        "- Package: `{0}`".format(status.get("expected_package", "")),
        "- Expected version: `{0}`".format(status.get("expected_version", "")),
        "- Installed version: `{0}`".format(status.get("installed_version", "")),
        "- Isolated settings: `{0}`".format("PASS" if status.get("settings_safe") else "FAIL"),
        "- Package lock SHA-256: `{0}`".format(status.get("package_lock_sha256", "")),
        "- API key present: `{0}`".format("yes" if status.get("api_key_present") else "no"),
        "- Tools / project trust / sessions: `disabled / denied / disabled`",
        "- Extensions / skills / prompts / context files: `disabled / disabled / disabled / disabled`",
        "- Reviewed method supply chain: `{0}` ({1} methods)".format(
            (status.get("skill_supply_chain") or {}).get("status", "DISABLED"),
            (status.get("skill_supply_chain") or {}).get("approved_skill_count", 0),
        ),
        "- Reviewed method bundle SHA-256: `{0}`".format(
            (status.get("skill_supply_chain") or {}).get("bundle_sha256", "")
        ),
        "",
        "Pi is an LLM execution harness only. Python retains governance and protected human gates.",
    ]
    if status.get("errors"):
        lines.extend(["", "## Errors"] + ["- {0}".format(item) for item in status["errors"]])
    atomic_write_text(directory / "latest.json", json_text)
    atomic_write_text(directory / "latest.md", "\n".join(lines) + "\n")
    return status


def run_pi_runtime_uat(config: Dict[str, Any]) -> Dict[str, Any]:
    """Make one explicit synthetic live call and persist a non-secret proof."""
    status = pi_runtime_status(config)
    if not status.get("runtime_ready"):
        raise PiRuntimeError("Pi runtime UAT blocked because runtime_ready is false")
    cfg = get_pi_config(config)
    provider = {
        "name": "pi_runtime_uat",
        "runtime": "pi",
        "pi_provider": cfg["provider"],
        "model": cfg["model"],
        "api_key_env": _provider_api_key_env(str(cfg["provider"])),
        "thinking": cfg["thinking"],
        "timeout": cfg["timeout"],
    }
    response = call_pi_agent(
        system_prompt=PI_UAT_SYSTEM_PROMPT,
        user_content="Synthetic public runtime test. No market, client, account, position, or internal data is included.",
        provider=provider,
        config=config,
        role_id="runtime_uat",
        timeout=int(cfg["timeout"]),
    )
    raw = str(response).strip()
    if raw.startswith("```"):
        raw = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("```"))
    try:
        output = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PiRuntimeError("Pi runtime UAT returned invalid JSON") from exc
    expected = {"status": "ok", "runtime": "pi", "human_decision_required": True}
    if output != expected:
        raise PiRuntimeError("Pi runtime UAT output did not match the exact safe contract")
    metadata = response_metadata(response)
    result = {
        "schema_version": PI_RUNTIME_VERSION,
        "generated_at": now_iso(),
        "status": "PASS",
        "live_model_call": True,
        "synthetic_public_input": True,
        "runtime": "pi",
        "pi_package": cfg["package"],
        "pi_version": status.get("installed_version", ""),
        "package_lock_sha256": status.get("package_lock_sha256", ""),
        "skill_supply_chain_sha256": (status.get("skill_supply_chain") or {}).get("bundle_sha256", ""),
        "skill_registry_sha256": (status.get("skill_supply_chain") or {}).get("registry_sha256", ""),
        "skill_lock_sha256": (status.get("skill_supply_chain") or {}).get("lock_sha256", ""),
        "provider": cfg["provider"],
        "model": str(metadata.get("model") or cfg["model"]),
        "request_id": str(metadata.get("request_id") or ""),
        "usage": metadata.get("usage", {}),
        "validated_output": expected,
        "tools_enabled": False,
        "sessions_enabled": False,
        "project_resources_enabled": False,
        "automatic_assignment_executed": False,
        "automatic_approval_executed": False,
        "automatic_publication_executed": False,
        "automatic_trading_executed": False,
    }
    directory = project_path("reports/pi_agent/uat")
    stamp = re.sub(r"[^0-9A-Za-z_]", "", result["generated_at"].replace("+", "_"))[:32]
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    atomic_write_text(directory / "latest.json", text)
    atomic_write_text(directory / "uat_{0}.json".format(stamp), text)
    return result


def run_pi_skills_uat(config: Dict[str, Any]) -> Dict[str, Any]:
    """Exercise the locked signal-analyst method bundle with synthetic public data."""
    status = pi_runtime_status(config)
    skill_status = status.get("skill_supply_chain", {}) or {}
    if not status.get("runtime_ready") or skill_status.get("status") != "READY":
        raise PiRuntimeError("Pi reviewed-method UAT blocked because runtime or skill supply chain is not READY")
    cfg = get_pi_config(config)
    provider = {
        "name": "pi_skills_uat",
        "runtime": "pi",
        "pi_provider": cfg["provider"],
        "model": cfg["model"],
        "api_key_env": _provider_api_key_env(str(cfg["provider"])),
        "thinking": cfg["thinking"],
        "timeout": cfg["timeout"],
    }
    response = call_pi_agent(
        system_prompt=PI_SKILLS_UAT_SYSTEM_PROMPT,
        user_content=(
            "Synthetic public evidence only. Source A reports contract amount CNY 100 million; "
            "Source B reports CNY 120 million for the same date and scope. Neither source is marked "
            "as an approved market-data vendor. Detect the unresolved conflict and retain human control."
        ),
        provider=provider,
        config=config,
        role_id="signal_analyst",
        timeout=int(cfg["timeout"]),
    )
    raw = str(response).strip()
    if raw.startswith("```"):
        raw = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("```"))
    try:
        output = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PiRuntimeError("Pi reviewed-method UAT returned invalid JSON") from exc
    expected = {
        "status": "ok",
        "runtime": "pi",
        "skill_bundle": "verified",
        "conflict_detected": True,
        "human_decision_required": True,
        "automatic_trading_executed": False,
    }
    if output != expected:
        raise PiRuntimeError("Pi reviewed-method UAT output did not match the exact safe contract")
    metadata = response_metadata(response)
    result = {
        "schema_version": PI_RUNTIME_VERSION,
        "generated_at": now_iso(),
        "status": "PASS",
        "live_model_call": True,
        "synthetic_public_input": True,
        "runtime": "pi",
        "pi_package": cfg["package"],
        "pi_version": status.get("installed_version", ""),
        "provider": cfg["provider"],
        "model": str(metadata.get("model") or cfg["model"]),
        "request_id": str(metadata.get("request_id") or ""),
        "usage": metadata.get("usage", {}),
        "validated_output": expected,
        "skill_supply_chain_sha256": skill_status.get("bundle_sha256", ""),
        "signal_analyst_role_bundle_sha256": (
            (skill_status.get("role_bundles", {}) or {}).get("signal_analyst", {}) or {}
        ).get("sha256", ""),
        "compiled_skill_ids": (
            (skill_status.get("role_bundles", {}) or {}).get("signal_analyst", {}) or {}
        ).get("skill_ids", []),
        "dynamic_skills_enabled": False,
        "tools_enabled": False,
        "sessions_enabled": False,
        "automatic_assignment_executed": False,
        "automatic_approval_executed": False,
        "automatic_publication_executed": False,
        "automatic_trading_executed": False,
    }
    directory = project_path("reports/pi_agent/skills_uat")
    stamp = re.sub(r"[^0-9A-Za-z_]", "", result["generated_at"].replace("+", "_"))[:32]
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    atomic_write_text(directory / "latest.json", text)
    atomic_write_text(directory / "skills_uat_{0}.json".format(stamp), text)
    return result


def _run_process(
    args: Sequence[str],
    *,
    cwd: Path,
    env: Dict[str, str],
    stdin_text: str,
    timeout: int,
) -> Tuple[int, str, str]:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    completed = subprocess.run(
        list(args),
        cwd=str(cwd),
        env=env,
        input=stdin_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=max(1, int(timeout)),
        check=False,
        creationflags=creationflags,
    )
    return int(completed.returncode), completed.stdout or "", completed.stderr or ""


def _isolated_environment(agent_home: Path, workspace: Path, api_key_env: str) -> Dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key.upper() in SAFE_ENV_KEYS and value}
    api_key = os.environ.get(api_key_env, "")
    if api_key:
        env[api_key_env] = api_key
    env["HOME"] = str(workspace)
    env["USERPROFILE"] = str(workspace)
    env["PI_CODING_AGENT_DIR"] = str(agent_home)
    env["PI_CODING_AGENT_SESSION_DIR"] = str(workspace / "sessions-disabled")
    env["PI_SKIP_VERSION_CHECK"] = "1"
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    return env


def _materialize_system_prompt(system_prompt: str, role_id: str) -> Path:
    content = str(system_prompt or "").strip()
    if not content:
        raise PiRuntimeError("Pi system prompt is required")
    if "\n" not in content and "\r" not in content and len(content) <= 240:
        source = Path(content)
        try:
            if source.is_absolute() and source.exists():
                return source
            relative = project_path(content)
            if relative.exists() and relative.is_file():
                return relative
        except OSError:
            pass
    digest = _text_hash(content)[:16]
    path = project_path("data/pi_runtime/system_prompts/{0}_{1}.md".format(_safe_segment(role_id), digest))
    if not path.exists():
        atomic_write_text(path, content + "\n")
    return path


def _installed_pi_version(package_json: Optional[Path]) -> str:
    if package_json is None or not package_json.exists():
        return ""
    try:
        value = json.loads(package_json.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(value.get("version") or "") if isinstance(value, dict) else ""


def _package_json_near_binary(path: Path) -> Optional[Path]:
    for parent in [path.parent] + list(path.parents)[:5]:
        candidate = parent / "node_modules" / "@earendil-works" / "pi-coding-agent" / "package.json"
        if candidate.exists():
            return candidate
    default = project_path("pi_runtime/node_modules/@earendil-works/pi-coding-agent/package.json")
    return default if default.exists() else None


def _provider_api_key_env(provider: str) -> str:
    mapping = {
        "deepseek": "DEEPSEEK_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "google": "GEMINI_API_KEY",
    }
    return mapping.get(str(provider or "").lower(), "LLM_API_KEY")


def _normalize_usage(raw: Any) -> Dict[str, int]:
    value = raw if isinstance(raw, dict) else {}
    prompt = _nonnegative_int(value.get("prompt_tokens", value.get("input", 0)))
    completion = _nonnegative_int(value.get("completion_tokens", value.get("output", 0)))
    total = _nonnegative_int(value.get("total_tokens", value.get("totalTokens", prompt + completion)))
    cache_hit = _nonnegative_int(value.get("prompt_cache_hit_tokens", value.get("cacheRead", 0)))
    cache_miss = _nonnegative_int(value.get("prompt_cache_miss_tokens", max(0, prompt - cache_hit)))
    reasoning = _nonnegative_int(
        ((value.get("completion_tokens_details") or {}).get("reasoning_tokens", 0))
        if isinstance(value.get("completion_tokens_details"), dict)
        else value.get("reasoning", 0)
    )
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "prompt_cache_hit_tokens": cache_hit,
        "prompt_cache_miss_tokens": cache_miss,
        "completion_tokens_details": {"reasoning_tokens": reasoning},
    }


def _audit_path(pi_cfg: Dict[str, Any]) -> Path:
    stamp = now_iso()[:10].replace("-", "")
    return project_path(str(pi_cfg["audit_dir"])) / "calls_{0}.jsonl".format(stamp)


def _sanitize_error(value: Any) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = SECRET_PATTERN.sub("<redacted-secret>", text)
    text = WINDOWS_PATH_PATTERN.sub("<redacted-path>", text)
    return text[:2000]


def _enforce_output_limit(value: str, maximum: int, stream_name: str) -> None:
    if len(str(value or "").encode("utf-8", errors="replace")) > maximum:
        raise PiRuntimeError("Pi {0} exceeded {1} bytes".format(stream_name, maximum))


def _text_hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "pi_agent")).strip("._")
    return cleaned[:80] or "pi_agent"


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
