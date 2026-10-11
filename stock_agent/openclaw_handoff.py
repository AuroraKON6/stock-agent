from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit_log import atomic_write_text, file_hash, object_hash
from .config import project_path
from .models import now_iso


HANDOFF_VERSION = "24.0"
SECRET_PATTERN = re.compile(r"(?:sk-[A-Za-z0-9_-]{12,}|api[_-]?key\s*[:=]|bearer\s+[A-Za-z0-9._-]{12,})", re.IGNORECASE)
TRADING_PATTERN = re.compile(
    r"(?:自动交易|自动下单|自动调仓|立即买入|立即卖出|place\s+order|execute\s+trade|rebalance\s+position)",
    re.IGNORECASE,
)
LOCAL_PATH_PATTERN = re.compile(
    r"(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/]|\\\\[^\\\s]+\\|file://|/(?:Users|home|var|tmp)/)",
    re.IGNORECASE,
)
ANALYSIS_FIELDS = {
    "impact_direction",
    "confidence",
    "horizon",
    "key_thesis",
    "risk_points",
    "review_questions",
    "human_decision_needed",
    "evidence_refs",
    "valid_evidence_refs",
    "grounding_status",
}


def build_openclaw_handoff(config: Dict[str, Any], report_path: str = "") -> Dict[str, Any]:
    report_file = project_path(report_path) if report_path else _latest_report()
    report = _read_json(report_file)
    signals = report.get("signals", []) if isinstance(report.get("signals"), list) else []
    eligible = []
    excluded = Counter()
    handoff_cfg = config.get("openclaw_handoff", {}) or {}
    enabled = bool(handoff_cfg.get("enabled", True))
    maximum = max(1, int(handoff_cfg.get("max_public_signals", 10) or 10))
    for signal in signals:
        if not enabled:
            excluded["handoff_disabled"] += 1
            continue
        if not isinstance(signal, dict):
            excluded["invalid_signal"] += 1
            continue
        allowed, reason = _handoff_allowed(signal)
        if not allowed:
            excluded[reason] += 1
            continue
        public_signal = _public_signal(signal)
        if _contains_sensitive_or_unsafe_text(public_signal):
            excluded["sensitive_or_unsafe_output"] += 1
            continue
        if len(eligible) >= maximum:
            excluded["max_public_signals_reached"] += 1
            continue
        eligible.append(public_signal)
    call_required = enabled and bool(eligible)
    payload = {
        "schema_version": HANDOFF_VERSION,
        "generated_at": now_iso(),
        "data_classification": "public",
        "purpose": "public_research_summary_only",
        "handoff_enabled": enabled,
        "external_llm_allowed": call_required,
        "external_llm_call_required": call_required,
        "call_decision_reason": (
            "governance_approved_public_signals_available" if call_required
            else "handoff_disabled" if not enabled
            else "no_governance_approved_public_signals"
        ),
        "report_id": report_file.stem if report_file else "",
        "source_report_sha256": file_hash(report_file) if report_file and report_file.exists() else "",
        "public_signal_count": len(eligible),
        "excluded_signal_count": max(0, len(signals) - len(eligible)),
        "excluded_reason_counts": dict(sorted(excluded.items())),
        "signals": eligible,
        "instructions": {
            "output_scope": "summarize public signals and evidence limitations only",
            "human_decision_required": True,
            "prohibited": [
                "filesystem_discovery",
                "internal_operations_summary",
                "assignment",
                "approval",
                "activation",
                "publication",
                "trading",
                "orders",
                "position_changes",
            ],
        },
        "automatic_assignment_executed": False,
        "automatic_approval_executed": False,
        "automatic_publication_executed": False,
        "automatic_trading_executed": False,
    }
    payload["payload_sha256"] = _payload_hash(payload)
    return payload


def write_openclaw_handoff(config: Dict[str, Any], report_path: str = "") -> Dict[str, Any]:
    payload = build_openclaw_handoff(config, report_path)
    errors = validate_openclaw_handoff(payload)
    if errors:
        raise ValueError("unsafe OpenClaw handoff: {0}".format("; ".join(errors)))
    directory = project_path("reports/handoff")
    stamp = re.sub(r"[^0-9A-Za-z_]", "", str(payload["generated_at"]).replace("+", "_"))[:32]
    latest = directory / "latest.json"
    timestamped = directory / "handoff_{0}.json".format(stamp)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    atomic_write_text(latest, text)
    atomic_write_text(timestamped, text)
    atomic_write_text(project_path("openclaw/public_handoff_workspace/handoff.json"), text)
    return payload


def load_latest_openclaw_handoff() -> Dict[str, Any]:
    return _read_json(project_path("reports/handoff/latest.json"))


def validate_openclaw_handoff(payload: Dict[str, Any]) -> List[str]:
    errors = []
    if not isinstance(payload, dict):
        return ["payload must be a JSON object"]
    if payload.get("data_classification") != "public":
        errors.append("handoff classification must be public")
    signals = payload.get("signals") if isinstance(payload.get("signals"), list) else []
    count = int(payload.get("public_signal_count", -1) or 0)
    call_required = bool(payload.get("external_llm_call_required"))
    if count != len(signals):
        errors.append("public signal count does not match payload")
    if call_required != bool(signals) or bool(payload.get("external_llm_allowed")) != bool(signals):
        errors.append("external LLM decision does not match eligible signals")
    expected_hash = _payload_hash(payload)
    if payload.get("payload_sha256") != expected_hash:
        errors.append("payload hash mismatch")
    if _contains_sensitive_or_unsafe_text(payload):
        errors.append("payload contains a secret, local path, or prohibited trading instruction")
    for signal in signals:
        governance = signal.get("governance", {}) if isinstance(signal, dict) else {}
        if governance.get("classification") != "public" or governance.get("decision") != "approved":
            errors.append("signal is not governance-approved public data")
            break
    return errors


def _handoff_allowed(signal: Dict[str, Any]) -> tuple:
    governance = signal.get("llm_governance") if isinstance(signal.get("llm_governance"), dict) else {}
    decision = governance.get("signal_llm") if isinstance(governance.get("signal_llm"), dict) else {}
    if decision.get("classification") != "public" or decision.get("decision") != "approved":
        return False, "not_governance_approved_public"
    if decision.get("sensitive_categories") or decision.get("prompt_injection_detected"):
        return False, "governance_sensitive_or_injection"
    postflight = decision.get("postflight") if isinstance(decision.get("postflight"), dict) else {}
    if postflight.get("status") not in {"grounded", "partial"}:
        return False, "ungrounded_output"
    if str(signal.get("source_type") or "") == "price":
        gate = ((signal.get("payload") or {}).get("market_data_governance") or {}) if isinstance(signal.get("payload"), dict) else {}
        if gate and gate.get("enabled") and not gate.get("llm_eligible"):
            return False, "market_data_governance_blocked"
    if not isinstance(signal.get("llm_analysis"), dict) or not signal.get("llm_analysis"):
        return False, "missing_llm_analysis"
    return True, ""


def _public_signal(signal: Dict[str, Any]) -> Dict[str, Any]:
    analysis = signal.get("llm_analysis") or {}
    public_analysis = {key: analysis[key] for key in ANALYSIS_FIELDS if key in analysis}
    return {
        "signal_id": str(signal.get("signal_id") or ""),
        "title": str(signal.get("title") or ""),
        "summary": str(signal.get("summary") or ""),
        "source": str(signal.get("source") or ""),
        "source_type": str(signal.get("source_type") or ""),
        "url": str(signal.get("url") or ""),
        "published_at": str(signal.get("published_at") or ""),
        "priority": str(signal.get("priority") or ""),
        "topics": _string_list(signal.get("topics", [])),
        "symbols": _string_list(signal.get("symbols", [])),
        "reasons": _string_list(signal.get("reasons", [])),
        "llm_analysis": public_analysis,
        "governance": {
            "classification": "public",
            "decision": "approved",
            "grounding_status": str((analysis or {}).get("grounding_status") or ""),
        },
    }


def _contains_sensitive_or_unsafe_text(value: Any) -> bool:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return bool(SECRET_PATTERN.search(text) or TRADING_PATTERN.search(text) or LOCAL_PATH_PATTERN.search(text))


def _payload_hash(payload: Dict[str, Any]) -> str:
    ignored = {"payload_sha256"}
    return object_hash({key: value for key, value in payload.items() if key not in ignored})


def _string_list(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    return [str(item) for item in values[:30] if str(item).strip()]


def _latest_report() -> Optional[Path]:
    directory = project_path("reports/json")
    files = list(directory.glob("*.json")) if directory.exists() else []
    return max(files, key=lambda item: item.stat().st_mtime) if files else None


def _read_json(path: Optional[Path]) -> Dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
