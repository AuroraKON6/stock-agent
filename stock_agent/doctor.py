"""System health check for Go-Live readiness.

Checks config, data sources, LLM, the isolated Pi runtime, reports, scheduled tasks,
and review data. Writes a markdown report with OK/WARN/FAIL status per item.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import PROJECT_ROOT, project_path
from .llm_agent import _api_key_for_provider, _get_llm_config, _select_api_key
from .business_onboarding import build_business_onboarding, business_onboarding_config
from .llm_governance import governance_config, validate_governance_policy
from .model_operations import model_operations_config
from .market_resilience import diagnose_network_error, network_repair_guide
from .market_data_governance import load_latest_market_data_governance, market_data_governance_config
from .audit_log import file_hash
from .openclaw_handoff import validate_openclaw_handoff
from .agent_handoff import validate_agent_handoff
from .pi_runtime import pi_runtime_status
from .review_queue import build_review_queue
from .production_pilot import load_latest_production_pilot, production_pilot_config
from .research_workpapers import research_workpaper_config, verify_research_workpapers
from .evidence_vault import evidence_vault_config, verify_evidence_vault


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _check_python_version() -> Tuple[str, str, str]:
    v = sys.version.split()[0]
    major, minor = v.split(".")[:2]
    if int(major) >= 3 and int(minor) >= 8:
        return ("OK", "Python {0}".format(v), "")
    return ("FAIL", "Python {0} (requires >= 3.8)".format(v), "")


def _check_working_dir() -> Tuple[str, str, str]:
    cwd = os.getcwd()
    return ("OK", cwd, "")


def _check_config(config_path: str) -> Tuple[str, str, str, Optional[Dict[str, Any]]]:
    path = project_path(config_path)
    if not path.exists():
        return ("FAIL", "Config not found: {0}".format(path), "", None)
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        return ("OK", "Config valid: {0}".format(path), "", data)
    except json.JSONDecodeError as e:
        return ("FAIL", "Config JSON invalid: {0}".format(e), "", None)


def _check_local_jsonl(config: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    results = []
    sources = config.get("sources", {})
    jsonl_sources = sources.get("local_jsonl", [])
    if not jsonl_sources:
        results.append(("WARN", "No local_jsonl sources configured", ""))
        return results
    for src in jsonl_sources:
        name = src.get("name", "unknown")
        path_val = src.get("path", "")
        enabled = src.get("enabled", True)
        if not enabled:
            results.append(("OK", "[{0}] disabled, skipped".format(name), ""))
            continue
        fpath = project_path(path_val)
        if fpath.exists():
            results.append(("OK", "[{0}] file exists: {1}".format(name, path_val), ""))
        else:
            results.append(("WARN", "[{0}] file missing: {1}".format(name, path_val), ""))
    return results


def _check_eastmoney(config: Dict[str, Any]) -> Tuple[str, str, str]:
    em = config.get("sources", {}).get("eastmoney", {})
    if em.get("enabled"):
        symbols = em.get("symbols", [])
        return ("OK", "eastmoney enabled, {0} symbols".format(len(symbols)), "")
    return ("OK", "eastmoney disabled", "")


def _check_rss(config: Dict[str, Any]) -> Tuple[str, str, str]:
    rss = config.get("sources", {}).get("rss", [])
    enabled = [r for r in rss if r.get("enabled")]
    if enabled:
        return ("OK", "RSS enabled: {0} feeds".format(len(enabled)), "")
    return ("OK", "RSS disabled", "")


def _check_llm(config: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    results = []
    llm_cfg = config.get("llm_agent", {}) or {}
    api_key = _select_api_key(llm_cfg.get("base_url", ""))
    if api_key:
        results.append(("OK", "LLM API key: present (not shown)", ""))
    else:
        results.append(("WARN", "LLM API key: not set (LLM_API_KEY / DEEPSEEK_API_KEY)", ""))

    if llm_cfg.get("enabled"):
        model = llm_cfg.get("model", "")
        base_url = llm_cfg.get("base_url", "")
        results.append(("OK", "LLM enabled, model={0}, base_url={1}".format(model or "(default)", base_url or "(default)"), ""))
    else:
        results.append(("OK", "LLM agent disabled in config", ""))

    return results


def _check_llm_connectivity(config: Dict[str, Any]) -> Tuple[str, str, str]:
    llm_cfg = config.get("llm_agent", {}) or {}
    if not llm_cfg.get("enabled", False):
        return ("OK", "LLM connectivity skipped: agent disabled", "")
    resolved = _get_llm_config(config)
    providers = [
        provider for provider in resolved.get("providers", [])
        if provider.get("base_url") and provider.get("model") and _api_key_for_provider(provider)
    ]
    if not providers:
        return ("WARN", "LLM connectivity skipped: missing LLM_API_KEY/DEEPSEEK_API_KEY", "Set the API key environment variable.")
    failures = []
    for provider in providers:
        result = _probe_llm_connectivity({
            "base_url": provider.get("base_url", "https://api.deepseek.com/v1"),
            "model": provider.get("model", "deepseek-v4-pro"),
            "api_key": _api_key_for_provider(provider),
            "timeout": min(8, int(provider.get("timeout", llm_cfg.get("timeout", 8)))),
        })
        if result.get("ok"):
            return ("OK", "LLM connectivity OK: {0} ({1})".format(provider.get("name", "provider"), result.get("base_url", provider.get("base_url", ""))), "")
        failures.append((provider, result))
    provider, result = failures[-1]
    diagnosis = result.get("diagnosis") or diagnose_network_error(result.get("error", ""))
    guide = result.get("repair_guide") or network_repair_guide(diagnosis)
    detail = "LLM connectivity WARN: {0}; {1}; provider={2}".format(diagnosis, result.get("error", ""), provider.get("name", "provider"))
    return ("WARN", detail, "; ".join(str(item) for item in guide))


def _probe_llm_connectivity(options: Dict[str, Any]) -> Dict[str, Any]:
    base_url = str(options.get("base_url") or "https://api.deepseek.com/v1").rstrip("/")
    api_key = str(options.get("api_key") or "")
    timeout = int(options.get("timeout") or 8)
    request = urllib.request.Request(
        base_url + "/models",
        headers={"Authorization": "Bearer {0}".format(api_key), "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(256)
        return {"ok": True, "base_url": base_url}
    except Exception as exc:
        diagnosis = diagnose_network_error(str(exc))
        return {
            "ok": False,
            "base_url": base_url,
            "error": str(exc),
            "diagnosis": diagnosis,
            "repair_guide": network_repair_guide(diagnosis),
        }


def _check_openclaw() -> List[Tuple[str, str, str]]:
    results = []
    oc_path = shutil.which("openclaw")
    if oc_path:
        results.append(("OK", "openclaw command found: {0}".format(oc_path), ""))
    else:
        results.append(("WARN", "openclaw command not found in PATH", ""))

    # Check registry for stock_agent_ agents
    registry_path = Path(str(Path.home())) / ".openclaw" / "openclaw.json"
    if registry_path.exists():
        try:
            reg = json.loads(registry_path.read_text(encoding="utf-8"))
            agents_section = reg.get("agents", {})
            # Registry uses agents.list[] (array of objects with "id" field)
            agent_list = agents_section.get("list", [])
            agent_ids = [a.get("id", "") for a in agent_list if isinstance(a, dict)]
            sa_agents = [aid for aid in agent_ids if aid.startswith("stock_agent_")]
            if sa_agents:
                results.append(("OK", "stock_agent_ agents in registry: {0}".format(", ".join(sa_agents)), ""))
            else:
                results.append(("WARN", "No stock_agent_ agents found in openclaw registry", ""))
            if "stock_agent_coordinator" in agent_ids:
                results.append(("OK", "stock_agent_coordinator registered", ""))
            else:
                results.append(("WARN", "stock_agent_coordinator not in registry", ""))
            if "stock_agent_public_handoff" in agent_ids:
                results.append(("OK", "isolated stock_agent_public_handoff registered", ""))
            else:
                results.append((
                    "WARN",
                    "isolated stock_agent_public_handoff not in registry",
                    "Do not send governed public handoffs through the full coordinator context.",
                ))
            plugins = reg.get("plugins", {}) if isinstance(reg.get("plugins", {}), dict) else {}
            plugin_allow = plugins.get("allow", [])
            if isinstance(plugin_allow, list) and plugin_allow:
                results.append(("OK", "OpenClaw plugins.allow explicitly lists audited plugins", ""))
            else:
                results.append((
                    "WARN",
                    "OpenClaw plugins.allow is empty; discovered non-bundled plugins may auto-load",
                    "Audit installed plugins and explicitly allow only approved plugin IDs in ~/.openclaw/openclaw.json.",
                ))
        except (json.JSONDecodeError, OSError):
            results.append(("WARN", "openclaw registry unreadable", ""))
    else:
        results.append(("WARN", "openclaw registry not found: {0}".format(registry_path), ""))

    return results


def _check_openclaw_handoff(config: Dict[str, Any]) -> Tuple[str, str, str]:
    cfg = config.get("openclaw_handoff", {}) or {}
    # Keep the standalone legacy auditor fail-closed for historical summaries.
    # The active Doctor flow no longer calls this helper, and an explicit
    # ``enabled: false`` still marks the retired integration as disabled.
    if not bool(cfg.get("enabled", True)):
        return ("OK", "OpenClaw public handoff disabled", "")
    path = project_path("reports/handoff/latest.json")
    if not path.exists():
        return (
            "WARN",
            "No governed OpenClaw public handoff exists",
            "Run openclaw-handoff after agent-run; do not call OpenClaw directly from an unfiltered report.",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return ("FAIL", "OpenClaw public handoff is unreadable", "External OpenClaw invocation must remain blocked.")
    errors = validate_openclaw_handoff(payload)
    if errors:
        return (
            "FAIL",
            "Unsafe OpenClaw public handoff: {0}".format("; ".join(errors)),
            "Regenerate the handoff from the governed Python boundary before any external model call.",
        )
    call_required = bool(payload.get("external_llm_call_required"))
    report_files = sorted(project_path("reports/json").glob("*.json"))
    if report_files and payload.get("source_report_sha256") != file_hash(report_files[-1]):
        status = "FAIL" if call_required else "WARN"
        return (
            status,
            "OpenClaw public handoff is stale relative to the latest JSON report",
            "Regenerate the handoff; never send a stale public payload to the external model.",
        )
    latest_daily = _latest_scheduled_summary("daily")
    if latest_daily and "openclaw_handoff_public_signal_count" in latest_daily:
        scheduled_count = int(latest_daily.get("openclaw_handoff_public_signal_count", 0) or 0)
        if scheduled_count == 0 and bool(latest_daily.get("used_openclaw")):
            return (
                "FAIL",
                "Last daily run invoked OpenClaw with zero governance-approved public signals",
                "Keep the fail-closed skip branch enabled and inspect the scheduled-run logs.",
            )
    return (
        "OK",
        "Governed OpenClaw handoff valid: public_signals={0}, call_required={1}".format(
            payload.get("public_signal_count", 0), call_required,
        ),
        "",
    )


def _check_pi_runtime(config: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    cfg = config.get("pi_agent", {}) or {}
    if not bool(cfg.get("enabled", False)):
        return [("OK", "Pi Agent runtime disabled", "")]
    status = pi_runtime_status(config)
    results: List[Tuple[str, str, str]] = []
    if not status.get("installed"):
        results.append(("FAIL", "Pinned Pi Agent runtime is not installed", "Run npm install --ignore-scripts in pi_runtime."))
    elif not status.get("version_match"):
        results.append((
            "FAIL",
            "Pi version mismatch: expected {0}, installed {1}".format(
                status.get("expected_version", ""), status.get("installed_version", ""),
            ),
            "Restore the package-lock and reinstall without lifecycle scripts.",
        ))
    else:
        results.append(("OK", "Pinned Pi Agent {0} installed".format(status.get("installed_version", "")), ""))
    if status.get("settings_safe"):
        results.append(("OK", "Pi isolation policy valid: no tools, project trust, sessions, extensions, skills, prompts, or context files", ""))
    else:
        results.append((
            "FAIL",
            "Pi isolated settings are missing or unsafe",
            "Keep defaultProjectTrust=never, defaultTools=[], packages=[], extensions=[], and telemetry disabled.",
        ))
    skill_status = status.get("skill_supply_chain", {}) or {}
    if not skill_status.get("enabled"):
        results.append(("OK", "Pi reviewed-method supply chain disabled", ""))
    elif skill_status.get("status") == "READY":
        results.append((
            "OK",
            "Pi reviewed-method supply chain locked: {0} methods, bundle {1}".format(
                skill_status.get("approved_skill_count", 0),
                str(skill_status.get("bundle_sha256") or "")[:12],
            ),
            "",
        ))
    else:
        results.append((
            "FAIL",
            "Pi reviewed-method supply chain is not READY",
            "; ".join(skill_status.get("errors", [])[:5]) or "Review registry and regenerate the lock explicitly.",
        ))
    if status.get("api_key_present"):
        results.append(("OK", "Pi provider credential environment is available", ""))
    else:
        results.append(("WARN", "Pi provider credential environment is missing", "Set the provider key in the service account environment; never write it to the repository."))
    return results


def _check_agent_handoff(config: Dict[str, Any]) -> Tuple[str, str, str]:
    cfg = config.get("agent_handoff", {}) or {}
    if not bool(cfg.get("enabled", False)):
        return ("OK", "Pi public handoff disabled", "")
    path = project_path("reports/agent_handoff/latest.json")
    if not path.exists():
        return (
            "WARN",
            "No governed Pi public handoff exists",
            "Run pi-handoff-run after agent-run; never send an unfiltered report to Pi.",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return ("FAIL", "Pi public handoff is unreadable", "External Pi invocation must remain blocked.")
    errors = validate_agent_handoff(payload)
    if errors:
        return (
            "FAIL",
            "Unsafe Pi public handoff: {0}".format("; ".join(errors)),
            "Regenerate the handoff from the governed Python boundary.",
        )
    call_required = bool(payload.get("external_llm_call_required"))
    report_files = sorted(project_path("reports/json").glob("*.json"))
    if report_files and payload.get("source_report_sha256") != file_hash(report_files[-1]):
        return (
            "FAIL" if call_required else "WARN",
            "Pi public handoff is stale relative to the latest JSON report",
            "Regenerate the handoff; never send a stale payload.",
        )
    latest_daily = _latest_scheduled_summary("daily")
    if latest_daily and latest_daily.get("agent_runtime") == "pi":
        scheduled_count = int(latest_daily.get("agent_handoff_public_signal_count", 0) or 0)
        used_pi = bool(latest_daily.get("used_pi"))
        model_calls = int(latest_daily.get("pi_model_call_count", 0) or 0)
        if scheduled_count == 0 and (used_pi or model_calls):
            return (
                "FAIL",
                "Last daily run called Pi with zero governance-approved public signals",
                "Keep the fail-closed conditional call branch enabled.",
            )
        if bool(latest_daily.get("agent_handoff_call_required")) != used_pi:
            return (
                "FAIL",
                "Last daily Pi invocation does not match its governed handoff decision",
                "Inspect the scheduled summary and Pi audit chain before rerunning.",
            )
    return (
        "OK",
        "Governed Pi handoff valid: public_signals={0}, call_required={1}".format(
            payload.get("public_signal_count", 0), call_required,
        ),
        "",
    )


def _latest_scheduled_summary(mode: str = "") -> Dict[str, Any]:
    files = sorted(project_path("logs/scheduled").glob("run_*.summary.json"))
    for path in reversed(files):
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(value, dict) and (not mode or value.get("mode") == mode):
            return value
    return {}


def _check_dashboard() -> Tuple[str, str, str]:
    path = project_path("reports/dashboard/latest.html")
    if path.exists():
        size = path.stat().st_size
        return ("OK", "Dashboard exists ({0} bytes)".format(size), "")
    return ("WARN", "Dashboard not found: {0}".format(path), "")


def _check_notifications() -> Tuple[str, str, str]:
    path = project_path("reports/notifications/latest.md")
    if path.exists():
        return ("OK", "Notifications exists", "")
    return ("WARN", "Notifications not found: {0}".format(path), "")


def _check_workspace_inbox() -> Tuple[str, str, str]:
    md_path = project_path("reports/workspace/latest.md")
    json_path = project_path("reports/workspace/latest.json")
    if not md_path.exists() or not json_path.exists():
        return ("WARN", "Workspace Inbox not found: {0} / {1}".format(md_path, json_path), "")
    try:
        data = json.loads(json_path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return ("WARN", "Workspace Inbox unreadable: {0}".format(json_path), "")
    counts = data.get("object_counts", {}) or {}
    detail = (
        "Workspace Inbox exists: signals={signals}, evidence={evidence}, "
        "comments={comments}, drafts={drafts}"
    ).format(
        signals=counts.get("signals", 0),
        evidence=counts.get("evidence_items", 0),
        comments=counts.get("comments", 0),
        drafts=counts.get("research_drafts", 0),
    )
    return ("OK", detail, "")


def _check_source_health() -> Tuple[str, str, str]:
    path = project_path("reports/source_health/latest.json")
    if not path.exists():
        return ("WARN", "Source health not found: {0}".format(path), "")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ("WARN", "Source health unreadable: {0}".format(path), "")
    status = str(data.get("overall_status") or "WARN")
    score = data.get("overall_score", 0)
    low_quality = data.get("low_quality_sources", []) or []
    detail = "Source health {0}, score={1}".format(status, score)
    if low_quality:
        detail += ", low_quality={0}".format(",".join(str(item) for item in low_quality))
    if status == "OK":
        return ("OK", detail, "")
    if status == "FAIL":
        return ("FAIL", detail, "")
    return ("WARN", detail, "")


def _check_market_data_governance(config: Dict[str, Any]) -> Tuple[str, str, str]:
    cfg = market_data_governance_config(config)
    if not cfg["enabled"]:
        return ("OK", "Market data governance disabled", "Enable it before using price signals with an external LLM.")
    snapshot = load_latest_market_data_governance(config)
    if not snapshot:
        return ("WARN", "Market data governance snapshot not found", "Run agent-run or run once to generate the control snapshot.")
    detail = "Market data governance {0}: pass={1}, degraded={2}, blocked={3}, llm_eligible={4}".format(
        snapshot.get("status", "UNKNOWN"),
        snapshot.get("pass_count", 0),
        snapshot.get("degraded_count", 0),
        snapshot.get("blocked_count", 0),
        snapshot.get("llm_eligible_count", 0),
    )
    notes = []
    if not cfg["approved_internal_research_providers"]:
        notes.append("No provider has an explicit internal research and external-model-use attestation")
    reasons = snapshot.get("reason_counts", {}) or {}
    if reasons.get("insufficient_cross_source_consensus"):
        notes.append("A second independent quote source is still required")
    if reasons.get("cross_source_price_conflict"):
        notes.append("Conflicting prices require human investigation")
    status = "OK" if snapshot.get("status") == "OK" and not notes else "WARN"
    return (status, detail, "; ".join(notes))


def _check_review_queue(config: Dict[str, Any]) -> Tuple[str, str, str]:
    try:
        queue = build_review_queue(config)
    except Exception as exc:
        return ("WARN", "Review queue cannot be projected: {0}".format(exc), "Repair roster/review JSONL input; do not delete audit files.")
    summary = queue.get("summary", {}) or {}
    detail = "Review queue: outstanding={0}, overdue={1}, at_risk={2}, unassigned={3}, duplicates={4}".format(
        summary.get("outstanding_count", 0),
        summary.get("overdue_count", 0),
        summary.get("at_risk_count", 0),
        summary.get("unassigned_count", 0),
        summary.get("pending_duplicate_rows", 0),
    )
    blockers = []
    if summary.get("invalid_record_count"):
        blockers.append("invalid append-only records require repair")
    if summary.get("overdue_count"):
        blockers.append("overdue reviews require escalation")
    missing = summary.get("missing_human_capabilities", []) or []
    if missing:
        blockers.append("missing verified human capabilities: {0}".format(", ".join(missing)))
    if summary.get("unassigned_count"):
        blockers.append("human assignment is required; automatic assignment remains disabled")
    return (("WARN" if blockers else "OK"), detail, "; ".join(blockers))


def _check_production_pilot(config: Dict[str, Any]) -> Tuple[str, str, str]:
    cfg = production_pilot_config(config)
    if not cfg["enabled"]:
        return (
            "OK",
            "Controlled production pilot disabled",
            "Enable enforced shadow mode before treating technical health as business go-live readiness.",
        )
    snapshot = load_latest_production_pilot(config)
    if not snapshot:
        return (
            "WARN",
            "Controlled production pilot snapshot not found",
            "Run production-pilot-sync; this warning does not authorize production use.",
        )
    integrity = [
        snapshot.get("control_log_integrity", "UNKNOWN"),
        (snapshot.get("provider_registry") or {}).get("integrity_status", "UNKNOWN"),
        (snapshot.get("incidents") or {}).get("integrity_status", "UNKNOWN"),
        (snapshot.get("shadow_evidence") or {}).get("integrity_status", "UNKNOWN"),
    ]
    if any(value == "FAIL" for value in integrity):
        return (
            "FAIL",
            "Production pilot audit integrity failed",
            "Fail closed and repair the append-only hash chain under authorized human control.",
        )
    technical = str(snapshot.get("technical_readiness") or "UNKNOWN")
    decision = str(snapshot.get("go_live_decision") or "NO_GO")
    detail = "Production pilot: technical={0}, candidate={1}, decision={2}, blockers={3}".format(
        technical,
        snapshot.get("candidate_readiness", "UNKNOWN"),
        decision,
        len(snapshot.get("go_live_blockers", []) or []),
    )
    if technical != "READY":
        return ("FAIL", detail, "Technical pilot controls are blocked; keep effective mode shadow.")
    if decision != "GO":
        return (
            "WARN",
            detail,
            "Technical health is not business authorization. Complete real roster, provider, backlog, shadow-run, and human gates.",
        )
    return ("OK", detail, "Internal research production scope only; trading and external publication remain disabled.")


def _check_runtime_ops() -> Tuple[str, str, str]:
    path = project_path("logs/run_ops/latest.json")
    if not path.exists():
        return ("WARN", "Runtime ops summary not found: {0}".format(path), "")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ("WARN", "Runtime ops summary unreadable: {0}".format(path), "")
    status = str(data.get("overall_status") or "WARN")
    elapsed = data.get("elapsed_ms", 0)
    degradations = data.get("degradations", []) or []
    detail = "Runtime ops {0}, elapsed_ms={1}".format(status, elapsed)
    if degradations:
        detail += ", degradations={0}".format(",".join(str(item.get("reason", "")) for item in degradations[:3]))
    if status == "OK":
        return ("OK", detail, "")
    if status == "FAIL":
        return ("FAIL", detail, "")
    return ("WARN", detail, "")


def _check_research_runtime(config: Dict[str, Any]) -> Tuple[str, str, str]:
    runtime_cfg = config.get("research_runtime", {}) or {}
    if not runtime_cfg.get("enabled", False):
        return ("OK", "Institutional research runtime disabled", "")
    path = project_path("data/research_runtime/runtime.json")
    if not path.exists():
        return ("WARN", "Research runtime snapshot not found: {0}".format(path), "Run research-runtime-sync.")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return ("FAIL", "Research runtime snapshot unreadable: {0}".format(path), "")
    policy = data.get("policy", {}) or {}
    unsafe = []
    if policy.get("auto_trade") is not False:
        unsafe.append("auto_trade must be false")
    if policy.get("auto_publish") is not False:
        unsafe.append("auto_publish must be false")
    if policy.get("external_publication_enabled") is not False:
        unsafe.append("external publication must be disabled")
    if policy.get("human_approval_required") is not True:
        unsafe.append("human approval must be required")
    if policy.get("role_separation_required") is not True:
        unsafe.append("role separation must be required")
    if unsafe:
        return ("FAIL", "Unsafe research runtime policy: {0}".format("; ".join(unsafe)), "")
    cases = data.get("cases", []) or []
    human_pending = sum(
        1 for item in cases
        if item.get("stage") in {"quality_review", "compliance_review", "human_approval"}
    )
    compliance_blocked = sum(
        1 for item in cases
        if str(((item.get("approvals", {}) or {}).get("compliance_review", {}) or {}).get("status")) == "blocked"
    )
    detail = "Research runtime safe: cases={0}, human_gate={1}, compliance_blocked={2}".format(
        len(cases), human_pending, compliance_blocked
    )
    if compliance_blocked:
        return ("WARN", detail, "Blocked cases must not be published.")
    return ("OK", detail, "")


def _check_business_onboarding(config: Dict[str, Any]) -> Tuple[str, str, str]:
    cfg = business_onboarding_config(config)
    if not cfg["enabled"]:
        return (
            "WARN",
            "Business onboarding is disabled",
            "Enable it before collecting organization, roster, provider, archive, shadow-run, or approval evidence.",
        )
    try:
        snapshot = build_business_onboarding(config)
    except (OSError, ValueError, TypeError) as exc:
        return (
            "FAIL",
            "Business onboarding cannot be evaluated: {0}".format(type(exc).__name__),
            "Repair technical inputs; do not bypass or fabricate business evidence.",
        )
    detail = "technical={0}; business={1}; phase={2}; completed={3}/{4}; actions={5}".format(
        snapshot.get("technical_status", "UNKNOWN"),
        snapshot.get("business_status", "BLOCKED"),
        snapshot.get("current_phase", ""),
        snapshot.get("completed_phase_count", 0),
        snapshot.get("phase_count", 0),
        snapshot.get("action_count", 0),
    )
    if snapshot.get("technical_status") != "READY":
        return (
            "FAIL",
            detail,
            "; ".join((snapshot.get("technical_errors") or [])[:5]) or "Repair the append-only control evidence chain.",
        )
    if snapshot.get("business_status") != "READY":
        return (
            "WARN",
            detail,
            "Real organization evidence, distinct human actors, provider rights, archives, shadow runs, and approvals remain human-controlled.",
        )
    return ("OK", detail, "Business readiness remains limited to internal research and the current candidate fingerprint.")


def _check_research_workpapers(config: Dict[str, Any]) -> Tuple[str, str, str]:
    cfg = research_workpaper_config(config)
    if not cfg["enabled"]:
        return ("OK", "Structured research workpapers disabled", "")
    path = project_path(cfg["events_path"])
    if not path.exists():
        return (
            "WARN",
            "Research workpaper event ledger not found: {0}".format(path),
            "Run research-workpaper-sync; protected approvals remain blocked while enforcement is enabled.",
        )
    result = verify_research_workpapers(config)
    detail = "Research workpapers: technical={0}, business={1}, integrity={2}, workpapers={3}, events={4}".format(
        result.get("technical_status", "UNKNOWN"),
        result.get("business_status", "BLOCKED"),
        result.get("integrity_status", "UNKNOWN"),
        result.get("workpaper_count", 0),
        result.get("event_count", 0),
    )
    if result.get("technical_status") != "READY":
        return ("FAIL", detail, "; ".join(result.get("errors", [])[:5]))
    if result.get("business_status") != "READY":
        blockers = result.get("approval_blockers", []) or result.get("warnings", [])
        return (
            "WARN",
            detail,
            "; ".join(blockers[:5]) or "Human evidence, review, and approved archive controls remain pending.",
        )
    return ("OK", detail, "Internal research scope only; trading remains disabled.")


def _check_evidence_vault(config: Dict[str, Any]) -> Tuple[str, str, str]:
    cfg = evidence_vault_config(config)
    if not cfg["enabled"]:
        return ("OK", "Official evidence vault disabled", "")
    result = verify_evidence_vault(config)
    detail = "Official evidence vault: technical={0}, business={1}, integrity={2}, documents={3}, events={4}".format(
        result.get("technical_status", "UNKNOWN"),
        result.get("business_status", "BLOCKED"),
        result.get("integrity_status", "UNKNOWN"),
        result.get("document_count", 0),
        result.get("event_count", 0),
    )
    if result.get("technical_status") != "READY":
        return ("FAIL", detail, "; ".join(result.get("errors", [])[:5]))
    if result.get("business_status") != "READY":
        blockers = result.get("business_blockers", []) or result.get("warnings", [])
        return (
            "WARN",
            detail,
            "; ".join(blockers[:5]) or "Retain official originals and complete real production-archive controls.",
        )
    return ("OK", detail, "Raw evidence bytes remain local and are not sent to the external LLM.")


def _check_research_operations(config: Dict[str, Any]) -> Tuple[str, str, str]:
    """Separate technical health from real business-operating readiness."""
    operations_cfg = config.get("research_operations", {}) or {}
    if not operations_cfg.get("enabled", False):
        return ("OK", "Research operations controls disabled", "")

    path = project_path("reports/research_operations/latest.json")
    if not path.exists():
        return (
            "WARN",
            "Business readiness UNKNOWN: research operations snapshot not found: {0}".format(path),
            "Run research-ops-plan before go-live.",
        )
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return ("FAIL", "Research operations snapshot unreadable: {0}".format(path), "")
    if not isinstance(snapshot, dict):
        return ("FAIL", "Research operations snapshot must be a JSON object: {0}".format(path), "")

    roster = snapshot.get("roster", {}) or {}
    summary = snapshot.get("summary", {}) or {}
    roster_errors = [str(item) for item in (roster.get("errors", []) or []) if str(item).strip()]
    unsafe = []
    if operations_cfg.get("enforce_roster", False) and snapshot.get("enforce_roster") is not True:
        unsafe.append("roster enforcement is not active in the latest snapshot")
    if str(snapshot.get("assignment_mode") or "") != "suggest_only":
        unsafe.append("assignment_mode must be suggest_only")
    if roster.get("valid") is not True or roster_errors:
        unsafe.append("roster or restrictions validation failed")
    auto_assigned = sum(
        1 for item in (snapshot.get("cases", []) or [])
        if isinstance(item, dict) and item.get("automatic_assignment_executed") is True
    )
    if auto_assigned:
        unsafe.append("automatic assignment was executed for {0} case(s)".format(auto_assigned))
    if unsafe:
        detail = "Research operations control failure: {0}".format("; ".join(unsafe))
        if roster_errors:
            detail += "; validation_errors={0}".format(" | ".join(roster_errors[:3]))
        return ("FAIL", detail, "Stop unattended operation and repair the control snapshot.")

    missing = sorted({
        str(item) for item in (roster.get("missing_mandatory_human_capabilities", []) or [])
        if str(item).strip()
    })
    case_count = int(summary.get("case_count", 0) or 0)
    unstaffed = int(summary.get("unstaffed_count", 0) or 0)
    unstaffed_high = int(summary.get("unstaffed_high_priority_count", 0) or 0)
    at_risk = int(summary.get("at_risk_count", 0) or 0)
    overdue = int(summary.get("overdue_count", 0) or 0)
    restricted = int(summary.get("restricted_count", 0) or 0)
    detail = (
        "Business readiness {0}: cases={1}, missing_human_roles={2}, unstaffed={3}, "
        "unstaffed_high_priority={4}, at_risk={5}, overdue={6}, restricted={7}"
    ).format(
        "BLOCKED" if missing else "ACTIVE",
        case_count,
        ",".join(missing) or "none",
        unstaffed,
        unstaffed_high,
        at_risk,
        overdue,
        restricted,
    )
    if missing:
        return (
            "WARN",
            detail,
            "Register verified human principals for every mandatory role; service agents cannot approve or publish.",
        )
    if unstaffed or at_risk or overdue or restricted:
        return (
            "WARN",
            detail,
            "A human coordinator must assign and resolve the operating queue before unattended production use.",
        )
    return ("OK", detail, "")


def _check_llm_governance(config: Dict[str, Any]) -> Tuple[str, str, str]:
    cfg = governance_config(config)
    if not cfg["enabled"]:
        return ("OK", "LLM governance disabled", "")
    policy_errors = validate_governance_policy(config)
    if policy_errors:
        return (
            "FAIL",
            "Unsafe LLM governance policy: {0}".format("; ".join(policy_errors)),
            "Stop external LLM operation until the policy is fail-closed.",
        )
    path = project_path(str(Path(cfg["report_path"]) / "latest.json"))
    if not path.exists():
        return (
            "WARN",
            "LLM governance policy is safe but no runtime snapshot exists: {0}".format(path),
            "Run agent-run once before unattended production use.",
        )
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return ("FAIL", "LLM governance snapshot unreadable: {0}".format(path), "")
    if not isinstance(snapshot, dict):
        return ("FAIL", "LLM governance snapshot must be a JSON object", "")
    if snapshot.get("enforced") is not True:
        return ("FAIL", "LLM governance snapshot is not enforced", "External LLM calls must fail closed.")
    if set(snapshot.get("allowed_external_classifications", []) or []) != {"public"}:
        return ("FAIL", "LLM governance snapshot permits non-public egress", "Allow public classification only.")
    summary = snapshot.get("summary", {}) or {}
    decisions = int(summary.get("decision_count", 0) or 0)
    approved = int(summary.get("approved_count", 0) or 0)
    denied = int(summary.get("denied_count", 0) or 0)
    output_blocked = int(summary.get("output_blocked_count", 0) or 0)
    injection = int(summary.get("prompt_injection_blocked_count", 0) or 0)
    pending = int(summary.get("pending_exception_count", 0) or 0)
    if decisions and not snapshot.get("audit_paths"):
        return ("FAIL", "LLM governance decisions exist without an audit path", "Repair audit persistence before the next call.")
    detail = (
        "LLM governance enforced: decisions={0}, approved={1}, denied={2}, "
        "output_blocked={3}, prompt_injection={4}, pending_exceptions={5}"
    ).format(decisions, approved, denied, output_blocked, injection, pending)
    if denied or output_blocked or pending:
        return ("WARN", detail, "Human compliance or information-security review is required; no automatic exception is allowed.")
    return ("OK", detail, "")


def _check_model_operations(config: Dict[str, Any]) -> Tuple[str, str, str]:
    cfg = model_operations_config(config)
    if not cfg["enabled"]:
        return ("OK", "Model operations disabled", "")
    path = project_path(str(Path(cfg["report_dir"]) / "latest.json"))
    if not path.exists():
        return ("WARN", "Model operations configured but no runtime snapshot exists", "Run agent-run once.")
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return ("FAIL", "Model operations snapshot unreadable: {0}".format(path), "Repair audit persistence before the next scheduled run.")
    if not isinstance(snapshot, dict):
        return ("FAIL", "Model operations snapshot must be a JSON object", "")
    serialized_keys = set()

    def collect_keys(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                serialized_keys.add(str(key).lower())
                collect_keys(child)
        elif isinstance(value, list):
            for child in value:
                collect_keys(child)

    collect_keys(snapshot)
    forbidden = sorted(serialized_keys.intersection({"api_key", "authorization", "raw_prompt", "prompt_content", "response_content", "reasoning_content"}))
    if forbidden:
        return ("FAIL", "Sensitive model telemetry fields found: {0}".format(",".join(forbidden)), "Delete the unsafe artifact and repair telemetry serialization.")
    human = snapshot.get("human_control", {}) or {}
    unsafe = [
        key for key in ("automatic_trade", "automatic_assignment", "automatic_approval", "automatic_publication")
        if human.get(key) is not False
    ]
    if unsafe:
        return ("FAIL", "Unsafe model operations controls: {0}".format(",".join(unsafe)), "Model degradation must remain research-only and human-gated.")
    status = str(snapshot.get("status") or "UNKNOWN")
    quality = snapshot.get("quality", {}) or {}
    usage = snapshot.get("usage", {}) or {}
    detail = "Model operations {0}: main={1:.1%}, role={2:.1%}, usage_metadata={3:.1%}, tokens={4}".format(
        status,
        float(quality.get("main_success_rate", 1)),
        float(quality.get("role_success_rate", 1)),
        float(quality.get("usage_metadata_rate", 1)),
        int(usage.get("total_tokens", 0) or 0),
    )
    if status in {"BREACH", "ATTENTION"}:
        return ("WARN", detail, "Review SLO and grounding breaches; deadline degradation must remain in the human queue.")
    return ("OK", detail, "")


def _check_last_scheduled_run() -> Tuple[str, str, str]:
    log_dir = project_path("logs/scheduled")
    if not log_dir.exists():
        return ("WARN", "No scheduled run logs found", "")
    files = sorted(log_dir.glob("run_*.summary.json"))
    if not files:
        return ("WARN", "No scheduled run logs found", "")
    latest = files[-1]
    try:
        data = json.loads(latest.read_text(encoding="utf-8-sig"))
        exit_code = data.get("exit_code", -1)
        started = data.get("started_at", "?")
        mode = data.get("mode", "?")
        if exit_code == 0:
            if str(data.get("scheduled_slo_status") or "").upper() == "BREACH":
                return (
                    "WARN",
                    "Last run: {0} ({1}) exit=0 but delivery SLO breached ({2}s > {3}s)".format(
                        started, mode, data.get("duration_seconds", "?"), data.get("scheduled_delivery_target_seconds", "?"),
                    ),
            "Inspect model deadline decisions and Pi handoff latency.",
                )
            return ("OK", "Last run: {0} ({1}) exit=0".format(started, mode), "")
        return ("WARN", "Last run: {0} ({1}) exit={2}".format(started, mode, exit_code), "")
    except (json.JSONDecodeError, OSError):
        return ("WARN", "Last run summary unreadable: {0}".format(latest), "")


def _check_scheduled_tasks() -> List[Tuple[str, str, str]]:
    results = []
    expected = [
        "StockAgent_DailyResearch",
        "StockAgent_PilotReconcile",
        "StockAgent_DailyDashboard",
        "StockAgent_WeeklyReview",
    ]
    try:
        command = (
            "$tasks=Get-ScheduledTask -TaskName 'StockAgent_*' -ErrorAction SilentlyContinue;"
            "$rows=@($tasks|ForEach-Object{$i=Get-ScheduledTaskInfo -TaskName $_.TaskName "
            "-TaskPath $_.TaskPath -ErrorAction SilentlyContinue;[pscustomobject]@{"
            "TaskName=$_.TaskName;State=[string]$_.State;LastTaskResult=[long]$i.LastTaskResult;"
            "LastRunTime=[string]$i.LastRunTime;NextRunTime=[string]$i.NextRunTime}});"
            "$rows|ConvertTo-Json -Compress"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode not in (None, 0):
            raise OSError("scheduled task query failed")
        raw = proc.stdout.strip()
        parsed = json.loads(raw) if raw else []
        rows = parsed if isinstance(parsed, list) else [parsed]
        by_name = {str(item.get("TaskName")): item for item in rows if isinstance(item, dict)}
        for task_name in expected:
            item = by_name.get(task_name)
            if not item:
                results.append(("WARN", "Scheduled task not installed: {0}".format(task_name), ""))
                continue
            state = str(item.get("State") or "Unknown")
            code = int(item.get("LastTaskResult", 0) or 0)
            code_hex = "0x{0:08X}".format(code & 0xFFFFFFFF)
            detail = "Scheduled task {0}: state={1}, last_result={2} ({3})".format(
                task_name, state, code, code_hex
            )
            if state.lower() == "disabled":
                results.append(("WARN", detail, "Enable the task before go-live."))
            elif code in {0, 267008, 267009}:
                results.append(("OK", detail, ""))
            elif code == 267011:
                results.append(("WARN", detail, "Task is installed but has not run yet."))
            else:
                results.append(("WARN", detail, "Inspect Task Scheduler history and logs/scheduled."))
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        results.append(("WARN", "Cannot check scheduled tasks (PowerShell unavailable)", ""))
    return results


def _check_review_data(config: Optional[Dict[str, Any]] = None) -> List[Tuple[str, str, str]]:
    results = []
    review_cfg = (config or {}).get("review", {}) or {}
    backlog_warn_count = max(1, int(review_cfg.get("backlog_warn_count", 20) or 20))
    pending_file_warn_count = max(1, int(review_cfg.get("pending_file_warn_count", 30) or 30))
    warn_if_no_completed = bool(review_cfg.get("warn_if_no_completed", True))
    review_dir = project_path("data/review")
    if not review_dir.exists():
        results.append(("OK", "pending_review: 0", ""))
        results.append(("OK", "completed_review: 0", ""))
        return results

    pending = list(review_dir.glob("pending_review_*.jsonl"))
    completed = list(review_dir.glob("completed_review_*.jsonl"))
    pending_count, pending_ids, pending_invalid = _review_record_ids(pending)
    completed_count, completed_ids, completed_invalid = _review_record_ids(completed)
    outstanding = pending_ids - completed_ids
    warning_reasons = []
    if len(outstanding) >= backlog_warn_count:
        warning_reasons.append("outstanding unique records reached warning threshold {0}".format(backlog_warn_count))
    if len(pending) >= pending_file_warn_count:
        warning_reasons.append("pending file count reached warning threshold {0}".format(pending_file_warn_count))
    if warn_if_no_completed and outstanding and not completed_ids:
        warning_reasons.append("pending work exists but no completed review has been recorded")
    if pending_invalid or completed_invalid:
        warning_reasons.append("invalid JSONL records require repair")

    pending_detail = (
        "pending_review: rows={0}, unique={1}, outstanding={2}, files={3}, invalid={4}"
    ).format(pending_count, len(pending_ids), len(outstanding), len(pending), pending_invalid)
    if warning_reasons:
        results.append((
            "WARN",
            pending_detail,
            "; ".join(warning_reasons) + ". Assign verified reviewers and close or supersede records; do not delete audit files.",
        ))
    else:
        results.append(("OK", pending_detail, ""))

    completed_status = "WARN" if completed_invalid else "OK"
    results.append((
        completed_status,
        "completed_review: rows={0}, unique={1}, files={2}, invalid={3}".format(
            completed_count, len(completed_ids), len(completed), completed_invalid
        ),
        "Repair invalid completed review records." if completed_invalid else "",
    ))

    return results


def _review_record_ids(files: Iterable[Path]) -> Tuple[int, set, int]:
    rows = 0
    invalid = 0
    signal_ids = set()
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            invalid += 1
            continue
        for line in lines:
            if not line.strip():
                continue
            rows += 1
            try:
                record = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                invalid += 1
                continue
            signal_id = str(record.get("signal_id") or "").strip() if isinstance(record, dict) else ""
            if signal_id:
                signal_ids.add(signal_id)
            else:
                invalid += 1
    return rows, signal_ids, invalid


def _check_recent_errors() -> List[Tuple[str, str, str]]:
    results = []
    # Source errors from latest JSON report
    json_dir = project_path("reports/json")
    if json_dir.exists():
        files = sorted(json_dir.glob("*.json"))
        if files:
            try:
                data = json.loads(files[-1].read_text(encoding="utf-8"))
                signals = data.get("signals", [])
                source_errors = sum(1 for s in signals if s.get("source_type") == "source_error")
                if source_errors > 0:
                    results.append(("WARN", "Recent source_error signals: {0}".format(source_errors), ""))
                else:
                    results.append(("OK", "Recent source_error signals: 0", ""))
                p0p1 = sum(1 for s in signals if s.get("priority") in ("P0", "P1"))
                if p0p1 > 0:
                    results.append(("WARN", "Recent P0/P1 signals: {0}".format(p0p1), ""))
                else:
                    results.append(("OK", "Recent P0/P1 signals: 0", ""))
            except (json.JSONDecodeError, OSError):
                results.append(("WARN", "Latest JSON report unreadable", ""))
        else:
            results.append(("OK", "No JSON reports yet", ""))
    else:
        results.append(("OK", "No JSON reports directory", ""))
    return results


def run_doctor(config_path: str) -> Dict[str, Any]:
    """Run all health checks and return structured results."""
    checks: List[Dict[str, Any]] = []

    def _add(category: str, name: str, status: str, detail: str, note: str = ""):
        checks.append({"category": category, "name": name, "status": status, "detail": detail, "note": note})

    # 1. Python
    status, detail, note = _check_python_version()
    _add("system", "python_version", status, detail, note)

    # 2. Working dir
    status, detail, note = _check_working_dir()
    _add("system", "working_directory", status, detail, note)

    # 3. Config
    status, detail, note, config = _check_config(config_path)
    _add("config", "config_file", status, detail, note)

    if config is None:
        # Can't proceed with most checks without config
        overall = "FAIL"
        return {
            "timestamp": datetime.now().isoformat(),
            "config_path": config_path,
            "overall_status": overall,
            "checks": checks,
        }

    # 4. Local JSONL sources
    for check in _check_local_jsonl(config):
        status, detail, note = check
        _add("sources", "local_jsonl", status, detail, note)

    # 5. Eastmoney
    status, detail, note = _check_eastmoney(config)
    _add("sources", "eastmoney", status, detail, note)

    # 6. RSS
    status, detail, note = _check_rss(config)
    _add("sources", "rss", status, detail, note)

    # 7. LLM
    for check in _check_llm(config):
        status, detail, note = check
        _add("llm", "llm_config", status, detail, note)
    status, detail, note = _check_llm_connectivity(config)
    _add("llm", "llm_connectivity", status, detail, note)
    status, detail, note = _check_llm_governance(config)
    _add("llm", "llm_governance", status, detail, note)
    status, detail, note = _check_model_operations(config)
    _add("llm", "model_operations", status, detail, note)

    # 8. Pi Agent runtime
    for check in _check_pi_runtime(config):
        status, detail, note = check
        _add("pi", "pi_runtime", status, detail, note)
    status, detail, note = _check_agent_handoff(config)
    _add("pi", "public_handoff", status, detail, note)

    # 9. Dashboard
    status, detail, note = _check_dashboard()
    _add("reports", "dashboard", status, detail, note)

    # 10. Notifications
    status, detail, note = _check_notifications()
    _add("reports", "notifications", status, detail, note)

    status, detail, note = _check_workspace_inbox()
    _add("reports", "workspace_inbox", status, detail, note)

    status, detail, note = _check_source_health()
    _add("sources", "source_health", status, detail, note)

    status, detail, note = _check_market_data_governance(config)
    _add("sources", "market_data_governance", status, detail, note)

    status, detail, note = _check_runtime_ops()
    _add("runtime", "runtime_ops", status, detail, note)

    status, detail, note = _check_research_runtime(config)
    _add("runtime", "research_runtime", status, detail, note)

    status, detail, note = _check_evidence_vault(config)
    _add("runtime", "official_evidence_vault", status, detail, note)

    status, detail, note = _check_research_workpapers(config)
    _add("runtime", "research_workpapers", status, detail, note)

    status, detail, note = _check_research_operations(config)
    _add("operations", "research_operations", status, detail, note)

    status, detail, note = _check_business_onboarding(config)
    _add("operations", "business_onboarding", status, detail, note)

    status, detail, note = _check_production_pilot(config)
    _add("operations", "production_pilot", status, detail, note)

    # 11. Last scheduled run
    status, detail, note = _check_last_scheduled_run()
    _add("scheduled", "last_run", status, detail, note)

    # 12. Scheduled tasks
    for check in _check_scheduled_tasks():
        status, detail, note = check
        _add("scheduled", "task", status, detail, note)

    # 13. Review data
    for check in _check_review_data(config):
        status, detail, note = check
        _add("review", "review_data", status, detail, note)

    status, detail, note = _check_review_queue(config)
    _add("review", "review_queue", status, detail, note)

    # 14. Recent errors
    for check in _check_recent_errors():
        status, detail, note = check
        _add("recent", "recent_data", status, detail, note)

    # Compute overall status
    statuses = [c["status"] for c in checks]
    if "FAIL" in statuses:
        overall = "FAIL"
    elif "WARN" in statuses:
        overall = "WARN"
    else:
        overall = "OK"

    return {
        "timestamp": datetime.now().isoformat(),
        "config_path": config_path,
        "overall_status": overall,
        "checks": checks,
    }


def _render_doctor_markdown(result: Dict[str, Any]) -> str:
    lines = []
    lines.append("# Go-Live Health Check")
    lines.append("")
    lines.append("Time: {0}".format(result["timestamp"]))
    lines.append("Config: {0}".format(result["config_path"]))
    lines.append("Overall: **{0}**".format(result["overall_status"]))
    lines.append("")

    status_icon = {"OK": "[OK]", "WARN": "[WARN]", "FAIL": "[FAIL]"}

    # Group by category
    categories: Dict[str, List[Dict]] = {}
    for c in result["checks"]:
        cat = c["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(c)

    category_labels = {
        "system": "System",
        "config": "Configuration",
        "sources": "Data Sources",
        "llm": "LLM",
        "pi": "Pi Agent",
        "openclaw": "OpenClaw Legacy",
        "reports": "Reports",
        "runtime": "Runtime Operations",
        "operations": "Research Operations",
        "scheduled": "Scheduled Tasks",
        "review": "Review Data",
        "recent": "Recent Data",
    }

    for cat, items in categories.items():
        label = category_labels.get(cat, cat)
        lines.append("## {0}".format(label))
        lines.append("")
        for item in items:
            icon = status_icon.get(item["status"], "[?]")
            lines.append("- {0} {1}: {2}".format(icon, item["name"], item["detail"]))
        lines.append("")

    # Summary
    counts = {"OK": 0, "WARN": 0, "FAIL": 0}
    for c in result["checks"]:
        counts[c["status"]] = counts.get(c["status"], 0) + 1

    lines.append("## Summary")
    lines.append("")
    lines.append("- OK: {0}".format(counts["OK"]))
    lines.append("- WARN: {0}".format(counts["WARN"]))
    lines.append("- FAIL: {0}".format(counts["FAIL"]))
    lines.append("")

    overall = result["overall_status"]
    if overall == "OK":
        lines.append("**All checks passed. Production controls are healthy.**")
    elif overall == "WARN":
        lines.append("**Some warnings found. Resolve or formally accept them before unattended production operation.**")
    else:
        lines.append("**FAILURES detected. Unattended production operation is blocked until resolved.**")
    lines.append("")

    return "\n".join(lines)


def write_doctor_report(config_path: str) -> Tuple[str, Dict[str, Any]]:
    """Run doctor checks, write markdown + JSON, return (path, result)."""
    result = run_doctor(config_path)
    stamp = _stamp()
    health_dir = project_path("reports/health")
    health_dir.mkdir(parents=True, exist_ok=True)

    md_content = _render_doctor_markdown(result)
    latest_md = health_dir / "latest.md"
    ts_md = health_dir / "doctor_{0}.md".format(stamp)
    latest_md.write_text(md_content, encoding="utf-8")
    ts_md.write_text(md_content, encoding="utf-8")

    json_content = json.dumps(result, ensure_ascii=False, indent=2)
    latest_json = health_dir / "latest.json"
    ts_json = health_dir / "doctor_{0}.json".format(stamp)
    latest_json.write_text(json_content, encoding="utf-8")
    ts_json.write_text(json_content, encoding="utf-8")

    return str(latest_md), result
