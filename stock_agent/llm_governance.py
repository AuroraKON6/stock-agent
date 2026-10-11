from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from .config import project_path
from .evidence_vault import evidence_vault_config, validate_evidence_vault_policy
from .models import AnalyzedSignal, now_iso


POLICY_VERSION = "28.0"
CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
CLASSIFICATION_RANK = {name: index for index, name in enumerate(CLASSIFICATIONS)}
EXTERNAL_LLM_ALLOWED_CLASSIFICATIONS = {"public"}
HUMAN_EXCEPTION_ROLES = {"compliance_officer", "information_security_officer"}
EXCEPTION_DISPOSITIONS = {"deny_and_close", "request_sanitized_reingest", "confirm_false_positive"}

DEFAULT_PUBLIC_SOURCE_TYPES = {"price", "filing", "financial", "announcement", "news"}
DEFAULT_TRUSTED_PUBLIC_SOURCES = {
    "eastmoney",
    "eastmoney_filing",
    "akshare",
    "pysnowball",
    "alltick",
    "free_quote",
    "cninfo",
    "sse",
    "szse",
    "bse",
    "csrc",
    "sac",
}
DEFAULT_TRUSTED_PUBLIC_DOMAINS = {
    "cninfo.com.cn",
    "sse.com.cn",
    "szse.cn",
    "bse.cn",
    "csrc.gov.cn",
    "sac.net.cn",
    "eastmoney.com",
}

SAFE_EVIDENCE_KEYS = {
    "code",
    "name",
    "latest",
    "pct_change",
    "price_change",
    "amount",
    "volume",
    "main_net_inflow",
    "turnover",
    "trade_direction",
    "source_evidence",
    "filing_evidence",
    "financial_evidence",
    "news_evidence",
    "announcement_type",
    "report_period",
    "key_points",
    "risk_points",
    "metrics",
    "filing_importance_score",
    "filing_importance_level",
    "filing_importance_reasons",
    "source_quality",
    "official_evidence_vault",
}

SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|ak)-[A-Za-z0-9_-]{12,}\b", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}\b", re.IGNORECASE),
    re.compile(r"\b(?:api[_ -]?key|password|passwd|client[_ -]?secret|access[_ -]?token)\s*[:=]\s*\S+", re.IGNORECASE),
)
PERSONAL_DATA_PATTERNS = (
    re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
)
PROMPT_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions", re.IGNORECASE),
    re.compile(r"reveal\s+(?:the\s+)?(?:system|developer)\s+prompt", re.IGNORECASE),
    re.compile(r"(?:system|developer)\s+message\s*:", re.IGNORECASE),
    re.compile(r"(?:call|invoke|use)\s+(?:the\s+)?(?:tool|function|shell)", re.IGNORECASE),
    re.compile(r"忽略(?:之前|以上|前面|所有).*指令"),
    re.compile(r"(?:泄露|输出|显示).*(?:系统提示词|开发者指令|密钥|API.?Key)"),
    re.compile(r"(?:调用|执行).*(?:工具|函数|命令|shell)"),
)
NONPUBLIC_TERMS = (
    "内幕信息",
    "内幕消息",
    "重大未公开",
    "尚未公开",
    "未公开重大",
    "保密信息",
    "confidential",
    "material non-public",
    "mnpi",
)
CLIENT_SENSITIVE_TERMS = (
    "客户账户",
    "客户名单",
    "交易账号",
    "资金账号",
    "持仓明细",
    "身份证号",
    "银行卡号",
    "手机号",
    "个人住址",
)
SENSITIVE_KEY_TOKENS = {
    "api_key",
    "apikey",
    "password",
    "passwd",
    "secret",
    "access_token",
    "refresh_token",
    "authorization",
    "cookie",
    "client_account",
    "customer_account",
    "id_number",
    "bank_account",
}


def governance_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("llm_governance", {}) if isinstance(config, dict) else {}
    vault = evidence_vault_config(config)
    allowed = {
        str(item).strip().lower()
        for item in raw.get("allowed_external_classifications", ["public"])
        if str(item).strip().lower() in CLASSIFICATION_RANK
    }
    return {
        "enabled": bool(raw.get("enabled", False)),
        "enforce": bool(raw.get("enforce", False)),
        "external_provider": bool(raw.get("external_provider", True)),
        "default_classification": _classification(raw.get("default_classification"), "internal"),
        "allowed_external_classifications": allowed or {"public"},
        "public_source_types": _lower_set(raw.get("public_source_types", DEFAULT_PUBLIC_SOURCE_TYPES)),
        "trusted_public_sources": _lower_set(raw.get("trusted_public_sources", DEFAULT_TRUSTED_PUBLIC_SOURCES)),
        "trusted_public_domains": _lower_set(raw.get("trusted_public_domains", DEFAULT_TRUSTED_PUBLIC_DOMAINS)),
        "block_secrets": bool(raw.get("block_secrets", True)),
        "block_personal_data": bool(raw.get("block_personal_data", True)),
        "block_nonpublic_information": bool(raw.get("block_nonpublic_information", True)),
        "block_prompt_injection": bool(raw.get("block_prompt_injection", True)),
        "require_evidence_refs": bool(raw.get("require_evidence_refs", True)),
        "max_text_chars": max(500, min(20000, int(raw.get("max_text_chars", 6000) or 6000))),
        "max_list_items": max(5, min(200, int(raw.get("max_list_items", 50) or 50))),
        "audit_enabled": bool(raw.get("audit_enabled", True)),
        "audit_path": str(raw.get("audit_path") or "data/llm_governance/audit"),
        "exception_path": str(raw.get("exception_path") or "data/llm_governance/exceptions"),
        "report_path": str(raw.get("report_path") or "reports/llm_governance"),
        "exception_mode": "human_only",
        "exception_approver_roles": sorted(HUMAN_EXCEPTION_ROLES),
        "retention_years": max(5, int(raw.get("retention_years", 5) or 5)),
        "evidence_vault_enabled": vault["enabled"],
        "evidence_vault_enforced": vault["enabled"] and vault["enforce"],
        "require_retained_evidence_for": set(vault["require_retained_before_external_llm_for"]),
    }


def validate_governance_policy(config: Dict[str, Any]) -> List[str]:
    cfg = governance_config(config)
    if not cfg["enabled"]:
        return []
    errors: List[str] = []
    unsafe = cfg["allowed_external_classifications"] - EXTERNAL_LLM_ALLOWED_CLASSIFICATIONS
    if unsafe:
        errors.append("external LLM may only allow public classification")
    if cfg["external_provider"] and not cfg["enforce"]:
        errors.append("external LLM requires enforce=true")
    if not cfg["block_secrets"]:
        errors.append("secret detection must be enabled")
    if not cfg["block_personal_data"]:
        errors.append("personal-data detection must be enabled")
    if not cfg["block_nonpublic_information"]:
        errors.append("nonpublic-information detection must be enabled")
    if not cfg["block_prompt_injection"]:
        errors.append("prompt-injection detection must be enabled")
    if not cfg["require_evidence_refs"]:
        errors.append("evidence references must be required")
    if not cfg["audit_enabled"]:
        errors.append("governance audit must be enabled")
    if cfg["exception_mode"] != "human_only":
        errors.append("LLM governance exceptions must be human_only")
    if cfg["evidence_vault_enforced"]:
        errors.extend("official_evidence_vault: {0}".format(item) for item in validate_evidence_vault_policy(config))
    return errors


def prepare_llm_batch(
    signals: Sequence[AnalyzedSignal],
    config: Dict[str, Any],
    component: str,
) -> Tuple[List[AnalyzedSignal], Dict[str, Dict[str, Any]], Dict[str, Any]]:
    cfg = governance_config(config)
    prefix = _stats_prefix(component)
    stats: Dict[str, Any] = {
        prefix + "_enabled": cfg["enabled"],
        prefix + "_enforced": cfg["enforce"],
        prefix + "_approved": 0,
        prefix + "_denied": 0,
        prefix + "_sensitive_blocked": 0,
        prefix + "_injection_blocked": 0,
        prefix + "_output_blocked": 0,
        prefix + "_audit_paths": [],
        prefix + "_exception_paths": [],
    }
    if not cfg["enabled"]:
        return list(signals), {}, stats

    policy_errors = validate_governance_policy(config)
    approved: List[AnalyzedSignal] = []
    safe_payloads: Dict[str, Dict[str, Any]] = {}
    audit_records: List[Dict[str, Any]] = []
    exception_records: List[Dict[str, Any]] = []
    restrictions, restriction_errors = _load_active_restrictions(config)

    for signal in signals:
        safe_payload = build_governed_prompt_payload(signal, component, cfg)
        decision = _evaluate_preflight(
            signal,
            safe_payload,
            cfg,
            component,
            restrictions,
            policy_errors + restriction_errors,
        )
        signal.llm_governance[component] = decision
        audit_records.append(_audit_record(decision, "preflight"))
        if decision["decision"] == "approved":
            approved.append(signal)
            safe_payloads[signal.signal_id] = safe_payload
            stats[prefix + "_approved"] += 1
        else:
            stats[prefix + "_denied"] += 1
            if decision.get("sensitive_categories"):
                stats[prefix + "_sensitive_blocked"] += 1
            if decision.get("prompt_injection_detected"):
                stats[prefix + "_injection_blocked"] += 1
            exception_records.append(_exception_record(decision))

    audit_path = _append_audit_records(audit_records, cfg)
    if audit_path:
        stats[prefix + "_audit_paths"].append(audit_path)
    exception_path = _write_exception_records(exception_records, cfg, component)
    if exception_path:
        stats[prefix + "_exception_paths"].append(exception_path)
    return approved, safe_payloads, stats


def build_governed_prompt_payload(
    signal: AnalyzedSignal,
    component: str,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = cfg or governance_config({})
    payload: Dict[str, Any] = {
        "signal_id": signal.signal_id,
        "title": signal.title,
        "summary": signal.summary,
        "source": signal.source,
        "source_type": signal.source_type,
        "url": _strip_url_query(signal.url),
        "published_at": signal.published_at,
        "topics": signal.topics,
        "symbols": signal.symbols,
        "sentiment_score": signal.sentiment_score,
        "priority": signal.priority,
        "priority_score": signal.priority_score,
        "needs_review": signal.needs_review,
        "reasons": signal.reasons,
        "evidence": {
            key: (signal.payload or {}).get(key)
            for key in SAFE_EVIDENCE_KEYS
            if key in (signal.payload or {})
        },
    }
    if component == "signal_llm":
        payload["research_tracks"] = signal.research_tracks
        payload["research_questions"] = signal.research_questions
    elif component == "role_llm" and signal.llm_analysis:
        if str(signal.llm_analysis.get("grounding_status") or "") != "blocked":
            payload["rule_llm_analysis"] = signal.llm_analysis
    return _bounded_copy(payload, cfg["max_text_chars"], cfg["max_list_items"])


def validate_signal_analysis(
    signal: AnalyzedSignal,
    analysis: Dict[str, Any],
    config: Dict[str, Any],
    component: str = "signal_llm",
) -> Tuple[Dict[str, Any], bool]:
    cfg = governance_config(config)
    if not cfg["enabled"]:
        return analysis, True
    decision = (signal.llm_governance or {}).get(component, {}) or {}
    validated, valid = _validate_grounding(analysis, decision, cfg, thesis_field="key_thesis")
    decision["postflight"] = {
        "status": validated.get("grounding_status", "blocked"),
        "valid_evidence_refs": validated.get("valid_evidence_refs", []),
        "unsupported_evidence_refs": validated.get("unsupported_evidence_refs", []),
        "evaluated_at": now_iso(),
    }
    signal.llm_governance[component] = decision
    _append_audit_records([_audit_record(decision, "postflight")], cfg)
    return validated, valid


def validate_role_analyses(
    signal: AnalyzedSignal,
    role_analyses: List[Dict[str, Any]],
    synthesis: Dict[str, Any],
    expected_role_ids: Iterable[str],
    config: Dict[str, Any],
    component: str = "role_llm",
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], bool]:
    cfg = governance_config(config)
    if not cfg["enabled"]:
        return role_analyses, synthesis, bool(role_analyses)
    decision = (signal.llm_governance or {}).get(component, {}) or {}
    validated_roles: List[Dict[str, Any]] = []
    all_grounded = True
    any_partial = False
    for item in role_analyses:
        validated, valid = _validate_grounding(item, decision, cfg, thesis_field="thesis")
        validated_roles.append(validated)
        all_grounded = all_grounded and valid
        any_partial = any_partial or validated.get("grounding_status") == "partial"
    expected = {str(item) for item in expected_role_ids if str(item)}
    observed = {str(item.get("role_id") or "") for item in validated_roles}
    missing_roles = sorted(expected - observed)
    if missing_roles:
        all_grounded = False
    synthesis = dict(synthesis or {})
    synthesis["governance_status"] = (
        "partial" if all_grounded and any_partial else ("grounded" if all_grounded else "blocked")
    )
    synthesis["missing_role_outputs"] = missing_roles
    if not all_grounded:
        synthesis["decision_gate"] = "human_review_required"
    decision["postflight"] = {
        "status": synthesis["governance_status"],
        "missing_role_outputs": missing_roles,
        "blocked_role_count": sum(item.get("grounding_status") == "blocked" for item in validated_roles),
        "evaluated_at": now_iso(),
    }
    signal.llm_governance[component] = decision
    _append_audit_records([_audit_record(decision, "postflight")], cfg)
    return validated_roles, synthesis, bool(validated_roles) and all_grounded


def write_llm_governance_summary(
    signals: Sequence[AnalyzedSignal],
    stats: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    cfg = governance_config(config)
    if not cfg["enabled"]:
        return {}
    records: List[Dict[str, Any]] = []
    for signal in signals:
        for component, decision in (signal.llm_governance or {}).items():
            if not isinstance(decision, dict):
                continue
            records.append({
                "signal_id": signal.signal_id,
                "component": component,
                "audit_id": decision.get("audit_id", ""),
                "classification": decision.get("classification", ""),
                "decision": decision.get("decision", ""),
                "reasons": decision.get("reasons", []),
                "sensitive_categories": decision.get("sensitive_categories", []),
                "prompt_injection_detected": bool(decision.get("prompt_injection_detected")),
                "postflight_status": ((decision.get("postflight", {}) or {}).get("status", "")),
            })
    approved = sum(item["decision"] == "approved" for item in records)
    denied = sum(item["decision"] == "denied" for item in records)
    output_blocked = sum(item["postflight_status"] == "blocked" for item in records)
    injection = sum(item["prompt_injection_detected"] for item in records)
    sensitive = sum(bool(item["sensitive_categories"]) for item in records)
    exception_queue = load_governance_exception_queue(config)
    pending_exceptions = int(exception_queue.get("pending_count", 0) or 0)
    status = "ATTENTION" if denied or output_blocked or pending_exceptions else "OK"
    snapshot = {
        "policy_version": POLICY_VERSION,
        "generated_at": now_iso(),
        "status": status,
        "enabled": cfg["enabled"],
        "enforced": cfg["enforce"],
        "external_provider": cfg["external_provider"],
        "allowed_external_classifications": sorted(cfg["allowed_external_classifications"]),
        "exception_mode": cfg["exception_mode"],
        "retention_years": cfg["retention_years"],
        "summary": {
            "decision_count": len(records),
            "approved_count": approved,
            "denied_count": denied,
            "output_blocked_count": output_blocked,
            "sensitive_blocked_count": sensitive,
            "prompt_injection_blocked_count": injection,
            "pending_exception_count": pending_exceptions,
            "historical_exception_count": int(exception_queue.get("exception_count", 0) or 0),
        },
        "records": records,
        "audit_paths": _collect_stats_paths(stats, "_audit_paths"),
        "exception_paths": _collect_stats_paths(stats, "_exception_paths"),
        "exception_queue_path": exception_queue.get("exception_path", ""),
        "exception_decision_path": exception_queue.get("decision_path", ""),
        "safety_boundary": "External LLM receives public data only. Exceptions require human compliance or information-security approval.",
    }
    report_dir = project_path(cfg["report_path"])
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    latest_json = report_dir / "latest.json"
    timestamped_json = report_dir / "llm_governance_{0}.json".format(stamp)
    latest_md = report_dir / "latest.md"
    timestamped_md = report_dir / "llm_governance_{0}.md".format(stamp)
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2)
    markdown = _render_summary_markdown(snapshot)
    for path in (latest_json, timestamped_json):
        path.write_text(payload, encoding="utf-8")
    for path in (latest_md, timestamped_md):
        path.write_text(markdown, encoding="utf-8")
    return {
        "llm_governance_status": status,
        "llm_governance_summary": snapshot["summary"],
        "llm_governance_path": str(timestamped_md),
        "llm_governance_json_path": str(timestamped_json),
        "llm_governance_latest_path": str(latest_md),
        "llm_governance_latest_json_path": str(latest_json),
    }


def load_latest_governance_summary(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = governance_config(config or {})
    path = project_path(str(Path(cfg["report_path"]) / "latest.json"))
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    queue = load_governance_exception_queue(config or {})
    summary = data.setdefault("summary", {})
    summary["pending_exception_count"] = int(queue.get("pending_count", 0) or 0)
    summary["historical_exception_count"] = int(queue.get("exception_count", 0) or 0)
    data["exception_queue_path"] = queue.get("exception_path", "")
    data["exception_decision_path"] = queue.get("decision_path", "")
    if summary["pending_exception_count"]:
        data["status"] = "ATTENTION"
    elif not int(summary.get("denied_count", 0) or 0) and not int(summary.get("output_blocked_count", 0) or 0):
        data["status"] = "OK"
    return data


def load_governance_exception_queue(config: Dict[str, Any]) -> Dict[str, Any]:
    cfg = governance_config(config)
    folder = project_path(cfg["exception_path"])
    exceptions: Dict[str, Dict[str, Any]] = {}
    if folder.exists():
        for path in sorted(folder.glob("pending_*.jsonl")):
            for item in _read_jsonl(path):
                exception_id = str(item.get("exception_id") or "")
                if exception_id:
                    row = dict(item)
                    row["source_path"] = str(path)
                    exceptions[exception_id] = row
    decision_path = folder / "decisions.jsonl"
    decisions: Dict[str, Dict[str, Any]] = {}
    if decision_path.exists():
        for item in _read_jsonl(decision_path):
            exception_id = str(item.get("exception_id") or "")
            if exception_id:
                decisions[exception_id] = item
    rows: List[Dict[str, Any]] = []
    pending = 0
    for exception_id, item in sorted(exceptions.items()):
        row = dict(item)
        decision = decisions.get(exception_id, {})
        if decision:
            row["decision"] = decision
            row["status"] = str(decision.get("status") or "closed")
        else:
            pending += 1
        rows.append(row)
    return {
        "exception_count": len(rows),
        "pending_count": pending,
        "resolved_count": len(rows) - pending,
        "exceptions": rows,
        "exception_path": str(folder),
        "decision_path": str(decision_path),
    }


def record_governance_exception_decision(
    exception_id: str,
    disposition: str,
    actor: str,
    actor_role: str,
    notes: str,
    evidence_refs: Sequence[str],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    exception_id = str(exception_id or "").strip()
    disposition = str(disposition or "").strip()
    actor = str(actor or "").strip()
    actor_role = str(actor_role or "").strip()
    notes = str(notes or "").strip()
    refs = [str(item).strip() for item in evidence_refs if str(item).strip()]
    if disposition not in EXCEPTION_DISPOSITIONS:
        raise ValueError("unsupported LLM governance disposition")
    if not exception_id or not actor or not actor_role or not notes:
        raise ValueError("exception_id, actor, actor_role, and notes are required")
    if actor_role not in HUMAN_EXCEPTION_ROLES:
        raise PermissionError("LLM governance exception requires compliance or information-security role")
    _authorize_exception_actor(actor, actor_role, config)
    queue = load_governance_exception_queue(config)
    exception = next(
        (item for item in queue.get("exceptions", []) if item.get("exception_id") == exception_id),
        None,
    )
    if exception is None:
        raise ValueError("unknown LLM governance exception")
    if exception.get("decision"):
        raise ValueError("LLM governance exception is already resolved")
    if disposition in {"request_sanitized_reingest", "confirm_false_positive"} and not refs:
        raise ValueError("this disposition requires retained evidence references")
    status_by_disposition = {
        "deny_and_close": "closed_denied",
        "request_sanitized_reingest": "closed_reingest_required",
        "confirm_false_positive": "closed_false_positive",
    }
    record = {
        "decision_id": "llmgov-decision:{0}".format(hashlib.sha256(
            "{0}|{1}|{2}|{3}".format(exception_id, disposition, actor, now_iso()).encode("utf-8")
        ).hexdigest()[:20]),
        "exception_id": exception_id,
        "audit_id": exception.get("audit_id", ""),
        "signal_id": exception.get("signal_id", ""),
        "component": exception.get("component", ""),
        "disposition": disposition,
        "status": status_by_disposition[disposition],
        "actor": actor,
        "actor_role": actor_role,
        "notes": notes,
        "evidence_refs": refs,
        "decided_at": now_iso(),
        "automatic_reingest_executed": False,
        "external_llm_egress_authorized": False,
    }
    cfg = governance_config(config)
    folder = project_path(cfg["exception_path"])
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "decisions.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    record["decision_path"] = str(path)
    _refresh_governance_snapshot_queue(config)
    return record


def _evaluate_preflight(
    signal: AnalyzedSignal,
    safe_payload: Dict[str, Any],
    cfg: Dict[str, Any],
    component: str,
    restrictions: Dict[str, str],
    policy_errors: List[str],
) -> Dict[str, Any]:
    full_payload = signal.payload or {}
    explicit = _explicit_classification(full_payload)
    classification = explicit or _source_classification(signal, cfg)
    sensitive: List[str] = []
    safe_text = json.dumps(safe_payload, ensure_ascii=False, default=str)
    full_text = "{0}\n{1}\n{2}".format(signal.title, signal.summary, json.dumps(full_payload, ensure_ascii=False, default=str))
    if _matches_any(full_text, SECRET_PATTERNS) or _contains_sensitive_key(full_payload):
        sensitive.append("credentials_or_secrets")
        classification = _max_classification(classification, "restricted")
    if _matches_any(full_text, PERSONAL_DATA_PATTERNS) or _contains_term(full_text, CLIENT_SENSITIVE_TERMS):
        sensitive.append("personal_or_client_data")
        classification = _max_classification(classification, "confidential")
    if _contains_term(full_text, NONPUBLIC_TERMS) or bool(full_payload.get("inside_information")):
        sensitive.append("nonpublic_or_inside_information")
        classification = _max_classification(classification, "restricted")
    restriction_match = _restriction_match(signal, restrictions)
    if restriction_match:
        sensitive.append("restricted_list")
        classification = "restricted"
    injection = _matches_any(safe_text, PROMPT_INJECTION_PATTERNS)
    reasons: List[str] = []
    if policy_errors:
        reasons.extend("policy_error: {0}".format(item) for item in policy_errors)
    if classification not in cfg["allowed_external_classifications"]:
        reasons.append("classification {0} is not approved for external LLM egress".format(classification))
    if cfg["block_secrets"] and "credentials_or_secrets" in sensitive:
        reasons.append("credentials or secrets detected")
    if cfg["block_personal_data"] and "personal_or_client_data" in sensitive:
        reasons.append("personal or client-sensitive data detected")
    if cfg["block_nonpublic_information"] and "nonpublic_or_inside_information" in sensitive:
        reasons.append("nonpublic or inside information detected")
    if "restricted_list" in sensitive:
        reasons.append("entity matched the active restricted list: {0}".format(restriction_match))
    if cfg["block_prompt_injection"] and injection:
        reasons.append("prompt injection pattern detected in approved fields")
    vault_status = full_payload.get("official_evidence_vault", {}) if isinstance(
        full_payload.get("official_evidence_vault"), dict
    ) else {}
    evidence_required = str(signal.source_type or "").strip().lower() in cfg["require_retained_evidence_for"]
    if cfg["evidence_vault_enforced"] and evidence_required and vault_status.get("llm_eligible") is not True:
        reasons.append("required official original evidence is not retained and verified before external LLM egress")
    if not safe_payload.get("title") and not safe_payload.get("summary"):
        reasons.append("no research content remains after field minimization")
    denied = bool(reasons) and cfg["enforce"]
    canonical = json.dumps(safe_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    evaluated_at = now_iso()
    audit_id = "llmgov:{0}".format(hashlib.sha256(
        "{0}|{1}|{2}|{3}".format(signal.signal_id, component, evaluated_at, canonical).encode("utf-8")
    ).hexdigest()[:20])
    return {
        "policy_version": POLICY_VERSION,
        "audit_id": audit_id,
        "signal_id": signal.signal_id,
        "component": component,
        "evaluated_at": evaluated_at,
        "classification": classification,
        "classification_source": "explicit" if explicit else "derived",
        "decision": "denied" if denied else "approved",
        "reasons": reasons,
        "sensitive_categories": sorted(set(sensitive)),
        "prompt_injection_detected": injection,
        "payload_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "payload_bytes": len(canonical.encode("utf-8")),
        "sent_fields": sorted(safe_payload.keys()),
        "allowed_evidence_refs": _flatten_evidence_refs(safe_payload),
        "restriction_match": restriction_match,
        "exception_mode": "human_only",
        "automatic_exception_allowed": False,
        "official_evidence_required": evidence_required,
        "official_evidence_status": str(vault_status.get("status") or ("MISSING" if evidence_required else "NOT_REQUIRED")),
        "official_evidence_document_ids": _string_list(vault_status.get("document_ids")),
        "raw_document_sent_to_external_llm": False,
    }


def _validate_grounding(
    output: Dict[str, Any],
    decision: Dict[str, Any],
    cfg: Dict[str, Any],
    thesis_field: str,
) -> Tuple[Dict[str, Any], bool]:
    result = dict(output or {})
    refs = _string_list(result.get("evidence_refs"))
    allowed = set(_string_list(decision.get("allowed_evidence_refs")))
    valid_refs: List[str] = []
    invalid_refs: List[str] = []
    for ref in refs:
        normalized = _normalize_evidence_ref(ref)
        if normalized in allowed:
            valid_refs.append(normalized)
        else:
            invalid_refs.append(ref)
    warnings: List[str] = []
    if cfg["require_evidence_refs"] and not valid_refs:
        warnings.append("no valid evidence reference points to the approved prompt payload")
    if invalid_refs:
        warnings.append("unsupported evidence references were returned")
    if not str(result.get(thesis_field) or "").strip():
        warnings.append("research thesis is empty")
    blocked = bool(warnings and (not valid_refs or not str(result.get(thesis_field) or "").strip()))
    result["evidence_refs"] = refs
    result["valid_evidence_refs"] = sorted(set(valid_refs))
    result["unsupported_evidence_refs"] = sorted(set(invalid_refs))
    result["grounding_status"] = "blocked" if blocked else ("partial" if warnings else "grounded")
    result["governance_warnings"] = warnings
    if warnings:
        result["human_decision_needed"] = True
    if blocked and "confidence" in result:
        try:
            result["confidence"] = min(0.5, float(result.get("confidence", 0.5)))
        except (TypeError, ValueError):
            result["confidence"] = 0.5
    return result, not blocked


def _source_classification(signal: AnalyzedSignal, cfg: Dict[str, Any]) -> str:
    source = str(signal.source or "").strip().lower()
    source_type = str(signal.source_type or "").strip().lower()
    payload = signal.payload or {}
    if source_type == "source_error":
        return "internal"
    if bool(payload.get("publicly_available")) or bool(payload.get("verified_public")):
        return "public"
    domain = _url_domain(signal.url)
    trusted_domain = any(domain == item or domain.endswith("." + item) for item in cfg["trusted_public_domains"])
    trusted_source = source in cfg["trusted_public_sources"]
    if source == "market_cache" and source_type == "price":
        evidence = payload.get("source_evidence", {}) if isinstance(payload.get("source_evidence"), dict) else {}
        resilience = payload.get("market_resilience", {}) if isinstance(payload.get("market_resilience"), dict) else {}
        upstream = str(
            payload.get("cached_from")
            or evidence.get("cached_from")
            or resilience.get("provider")
            or ""
        ).strip().lower()
        provenance_complete = bool(payload.get("code")) and payload.get("latest") is not None
        if upstream in cfg["trusted_public_sources"] and provenance_complete:
            return "public"
    if source_type in cfg["public_source_types"] and (trusted_source or trusted_domain):
        return "public"
    return cfg["default_classification"]


def _explicit_classification(payload: Dict[str, Any]) -> str:
    for key in ("data_classification", "information_classification", "access_classification"):
        value = str(payload.get(key) or "").strip().lower()
        if value in CLASSIFICATION_RANK:
            return value
    return ""


def _load_active_restrictions(config: Dict[str, Any]) -> Tuple[Dict[str, str], List[str]]:
    ops = config.get("research_operations", {}) if isinstance(config, dict) else {}
    path_value = str(ops.get("restrictions_path") or "")
    if not path_value:
        return {}, []
    path = project_path(path_value)
    if not path.exists():
        return {}, ["restrictions file not found"]
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}, ["restrictions file unreadable"]
    rows = raw.get("entries", []) if isinstance(raw, dict) else []
    result: Dict[str, str] = {}
    errors: List[str] = []
    for index, item in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(item, dict):
            errors.append("restriction[{0}] is not an object".format(index))
            continue
        entity = str(item.get("entity") or "").strip().lower()
        list_type = str(item.get("list_type") or "").strip().lower()
        status = str(item.get("status") or "active").strip().lower()
        if status != "active":
            continue
        if not entity or list_type not in {"observation", "restricted"}:
            errors.append("restriction[{0}] is invalid".format(index))
            continue
        if list_type == "restricted":
            result[entity] = list_type
    return result, errors


def _restriction_match(signal: AnalyzedSignal, restrictions: Dict[str, str]) -> str:
    candidates = {str(item).strip().lower() for item in signal.symbols if str(item).strip()}
    payload = signal.payload or {}
    for key in ("code", "name", "symbol", "entity"):
        value = str(payload.get(key) or "").strip().lower()
        if value:
            candidates.add(value)
    for candidate in sorted(candidates):
        if candidate in restrictions:
            return candidate
    return ""


def _audit_record(decision: Dict[str, Any], event: str) -> Dict[str, Any]:
    record = {
        key: decision.get(key)
        for key in (
            "policy_version",
            "audit_id",
            "signal_id",
            "component",
            "classification",
            "classification_source",
            "decision",
            "reasons",
            "sensitive_categories",
            "prompt_injection_detected",
            "payload_sha256",
            "payload_bytes",
            "sent_fields",
            "restriction_match",
            "exception_mode",
            "automatic_exception_allowed",
        )
    }
    record["event"] = event
    record["recorded_at"] = now_iso()
    if event == "postflight":
        record["postflight"] = decision.get("postflight", {})
    return record


def _exception_record(decision: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "exception_id": "exception:{0}".format(decision.get("audit_id", "")),
        "audit_id": decision.get("audit_id", ""),
        "signal_id": decision.get("signal_id", ""),
        "component": decision.get("component", ""),
        "classification": decision.get("classification", ""),
        "reasons": decision.get("reasons", []),
        "sensitive_categories": decision.get("sensitive_categories", []),
        "status": "pending_human_review",
        "required_approver_roles": sorted(HUMAN_EXCEPTION_ROLES),
        "automatic_approval_allowed": False,
        "created_at": now_iso(),
    }


def _append_audit_records(records: Sequence[Dict[str, Any]], cfg: Dict[str, Any]) -> str:
    if not records or not cfg["audit_enabled"]:
        return ""
    folder = project_path(cfg["audit_path"])
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "audit_{0}.jsonl".format(datetime.now().strftime("%Y%m%d"))
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return str(path)


def _write_exception_records(records: Sequence[Dict[str, Any]], cfg: Dict[str, Any], component: str) -> str:
    if not records:
        return ""
    folder = project_path(cfg["exception_path"])
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "pending_{0}_{1}.jsonl".format(component, _stamp())
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, default=str) + "\n" for record in records),
        encoding="utf-8",
    )
    return str(path)


def _authorize_exception_actor(actor: str, actor_role: str, config: Dict[str, Any]) -> None:
    try:
        from .research_operations import load_operations_inputs

        inputs = load_operations_inputs(config)
    except Exception as exc:
        raise PermissionError("research operations roster could not be validated") from exc
    if inputs.get("errors"):
        raise PermissionError("research operations roster or restrictions are invalid")
    principal = (inputs.get("principal_map", {}) or {}).get(actor)
    if not isinstance(principal, dict):
        raise PermissionError("exception actor is not registered")
    if principal.get("principal_type") != "human" or not bool(principal.get("active")):
        raise PermissionError("exception actor must be an active human principal")
    if actor_role not in set(principal.get("roles", []) or []):
        raise PermissionError("exception actor is not authorized for the declared role")


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    return rows


def _refresh_governance_snapshot_queue(config: Dict[str, Any]) -> None:
    cfg = governance_config(config)
    path = project_path(str(Path(cfg["report_path"]) / "latest.json"))
    if not path.exists():
        return
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        return
    if not isinstance(snapshot, dict):
        return
    queue = load_governance_exception_queue(config)
    summary = snapshot.setdefault("summary", {})
    summary["pending_exception_count"] = int(queue.get("pending_count", 0) or 0)
    summary["historical_exception_count"] = int(queue.get("exception_count", 0) or 0)
    snapshot["exception_queue_path"] = queue.get("exception_path", "")
    snapshot["exception_decision_path"] = queue.get("decision_path", "")
    if summary["pending_exception_count"]:
        snapshot["status"] = "ATTENTION"
    elif not int(summary.get("denied_count", 0) or 0) and not int(summary.get("output_blocked_count", 0) or 0):
        snapshot["status"] = "OK"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _render_summary_markdown(snapshot: Dict[str, Any]) -> str:
    summary = snapshot.get("summary", {}) or {}
    lines = [
        "# LLM Governance & Evidence Safety",
        "",
        "- Policy: {0}".format(snapshot.get("policy_version", "")),
        "- Generated: {0}".format(snapshot.get("generated_at", "")),
        "- Status: **{0}**".format(snapshot.get("status", "")),
        "- External egress: public data only",
        "- Exception mode: human_only",
        "",
        "## Decisions",
        "",
        "- Decisions: {0}".format(summary.get("decision_count", 0)),
        "- Approved / denied: {0} / {1}".format(summary.get("approved_count", 0), summary.get("denied_count", 0)),
        "- Output blocked: {0}".format(summary.get("output_blocked_count", 0)),
        "- Sensitive blocked: {0}".format(summary.get("sensitive_blocked_count", 0)),
        "- Prompt injection blocked: {0}".format(summary.get("prompt_injection_blocked_count", 0)),
        "- Pending human exceptions: {0}".format(summary.get("pending_exception_count", 0)),
        "",
        "## Audit Records",
        "",
    ]
    for item in snapshot.get("records", []) or []:
        lines.append(
            "- {0} {1}: {2} / {3} / postflight={4}".format(
                item.get("component", ""),
                item.get("signal_id", ""),
                item.get("classification", ""),
                item.get("decision", ""),
                item.get("postflight_status", "") or "not_called",
            )
        )
    lines.extend([
        "",
        "No raw prompts, credentials, personal data, or restricted content are retained in this report.",
        "No automatic exception approval, trading, position change, or external publication is allowed.",
        "",
    ])
    return "\n".join(lines)


def _bounded_copy(value: Any, max_text_chars: int, max_list_items: int, depth: int = 0) -> Any:
    if depth > 6:
        return "[depth_limited]"
    if isinstance(value, str):
        return value[:max_text_chars]
    if isinstance(value, dict):
        return {
            str(key)[:200]: _bounded_copy(item, max_text_chars, max_list_items, depth + 1)
            for key, item in list(value.items())[:max_list_items]
        }
    if isinstance(value, (list, tuple, set)):
        return [_bounded_copy(item, max_text_chars, max_list_items, depth + 1) for item in list(value)[:max_list_items]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:max_text_chars]


def _flatten_evidence_refs(payload: Dict[str, Any]) -> List[str]:
    refs: set = set()

    def walk(value: Any, path: str) -> None:
        if not path:
            return
        if isinstance(value, dict):
            # Presence and emptiness are both auditable facts. An analyst may
            # legitimately cite ``symbols: []`` or an empty evidence object to
            # explain that coverage is absent.
            refs.add(path)
            for key, item in value.items():
                walk(item, "{0}.{1}".format(path, key))
        elif isinstance(value, list):
            refs.add(path)
        elif value not in (None, ""):
            refs.add(path)

    for key, value in payload.items():
        if key != "signal_id":
            walk(value, str(key))
    return sorted(refs)


def _contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_").replace(" ", "_")
            if normalized in SENSITIVE_KEY_TOKENS or any(token in normalized for token in SENSITIVE_KEY_TOKENS):
                return True
            if _contains_sensitive_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def _matches_any(text: str, patterns: Sequence[re.Pattern]) -> bool:
    return any(pattern.search(text or "") for pattern in patterns)


def _contains_term(text: str, terms: Sequence[str]) -> bool:
    lowered = str(text or "").lower()
    return any(str(term).lower() in lowered for term in terms)


def _strip_url_query(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        if parts.scheme not in {"http", "https"}:
            return ""
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return ""


def _url_domain(value: Any) -> str:
    try:
        return (urlsplit(str(value or "")).hostname or "").lower()
    except ValueError:
        return ""


def _classification(value: Any, default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in CLASSIFICATION_RANK else default


def _max_classification(left: str, right: str) -> str:
    return max((left, right), key=lambda item: CLASSIFICATION_RANK.get(item, 1))


def _normalize_evidence_ref(value: Any) -> str:
    text = str(value or "").strip()
    for prefix in ("signal.", "input.", "signals[0]."):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _lower_set(value: Any) -> set:
    if isinstance(value, (set, tuple, list)):
        return {str(item).strip().lower() for item in value if str(item).strip()}
    return set()


def _stats_prefix(component: str) -> str:
    return "llm_governance" if component == "signal_llm" else "role_llm_governance"


def _collect_stats_paths(stats: Dict[str, Any], suffix: str) -> List[str]:
    rows: List[str] = []
    for key, value in stats.items():
        if str(key).endswith(suffix) and isinstance(value, list):
            for item in value:
                text = str(item or "")
                if text and text not in rows:
                    rows.append(text)
    return rows


def _stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
