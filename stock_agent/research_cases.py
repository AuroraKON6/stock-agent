from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
import urllib.request
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .analysis import PRIORITY_ORDER, min_priority_passes
from .config import project_path
from .models import AnalyzedSignal, now_iso
from .llm_agent import _api_key_for_provider, _get_llm_config, _parse_llm_json
from .pi_runtime import call_pi_agent, provider_uses_pi


VALID_CASE_DECISIONS = {
    "continue_tracking",
    "needs_evidence",
    "mark_noise",
    "archive",
}

FORBIDDEN_CASE_DECISIONS = {
    "auto_trade",
    "place_order",
    "order_placement",
    "rebalance_position",
    "position_rebalance",
    "buy",
    "sell",
}

CASE_SYSTEM_PROMPT = """You are an A-share research case analyst.
Return JSON only. For each case, produce:
{
  "case_id": "...",
  "hypotheses": [
    {
      "name": "bull|bear|base|risk|custom",
      "thesis": "one concise Chinese thesis",
      "probability": 0.0,
      "evidence_for": ["..."],
      "evidence_against": ["..."]
    }
  ],
  "evidence_gaps": ["missing evidence to verify"],
  "counter_questions": ["questions that could falsify the case"],
  "next_decision_options": ["continue_tracking", "needs_evidence", "mark_noise", "archive"],
  "human_decision_needed": true
}
Never suggest trading, order placement, or position changes."""


def build_research_cases(
    signals: Iterable[AnalyzedSignal],
    config: Dict[str, Any],
    existing_cases: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    if config.get("research_cases", {}).get("enabled", True) is False:
        return []

    existing_by_id = {
        str(case.get("case_id")): dict(case)
        for case in (existing_cases or [])
        if case.get("case_id")
    }
    grouped: Dict[str, Dict[str, Any]] = {}

    for signal in signals:
        key = _case_key(signal)
        case_id = _case_id(key)
        case = grouped.get(case_id)
        if case is None:
            previous = existing_by_id.get(case_id, {})
            case = _new_case(case_id, key, previous)
            grouped[case_id] = case
        _merge_signal_into_case(case, signal)

    cases = list(grouped.values())
    for case_id, previous in existing_by_id.items():
        if case_id not in grouped:
            cases.append(previous)

    cases = _apply_case_decisions(cases, _load_case_decisions())
    cases.sort(key=lambda item: (item.get("status") in ("noise", "archived"), item.get("latest_signal_at", "")), reverse=True)
    return cases


def update_research_cases(
    signals: List[AnalyzedSignal],
    config: Dict[str, Any],
    allow_llm: bool = False,
    ops: Any = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if config.get("research_cases", {}).get("enabled", True) is False:
        return [], _case_stats([])

    existing = _load_cases()
    cases = build_research_cases(signals, config, existing_cases=existing)
    llm_stats = _empty_case_llm_stats(config)
    if allow_llm and config.get("research_cases", {}).get("llm_enabled", True):
        cases, llm_stats = enrich_research_cases(cases, config, ops=ops)
    _save_cases(cases)
    summary = compute_case_summary(cases=cases)
    paths = write_case_summary(summary)
    stats = _case_stats(cases)
    stats.update(llm_stats)
    stats.update(paths)
    stats["research_case_summary"] = summary
    return cases, stats


def enrich_research_cases(
    cases: List[Dict[str, Any]],
    config: Dict[str, Any],
    ops: Any = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    cfg = config.get("research_cases", {})
    llm_cfg = _get_llm_config(config)
    stats = _empty_case_llm_stats(config)
    stats["research_case_llm_model"] = llm_cfg.get("model", "")
    stats["research_case_llm_runtime"] = llm_cfg.get("runtime", "http")
    stats["research_case_llm_base_url"] = llm_cfg.get("base_url", "")
    case_timeout = _case_llm_timeout(cfg, llm_cfg)
    max_attempts = _case_llm_max_attempts(cfg)
    parse_retries = _case_llm_parse_retries(cfg)
    stats["research_case_llm_timeout"] = case_timeout
    stats["research_case_llm_max_attempts"] = max_attempts

    if not llm_cfg.get("enabled", False) or cfg.get("llm_enabled", True) is False:
        return cases, stats

    providers = [provider for provider in llm_cfg.get("providers", []) if _api_key_for_provider(provider)]
    provider = providers[0] if providers else {}
    api_key = _api_key_for_provider(provider) if provider else ""
    candidates = _case_llm_candidates(cases, cfg)
    stats["research_case_llm_candidates"] = len(candidates)
    if not api_key:
        stats["research_case_llm_skipped_no_key"] = len(candidates)
        for case in candidates:
            case["case_error"] = "LLM_API_KEY/DEEPSEEK_API_KEY not set, skipped case analysis"
        return cases, stats

    max_cases = int(cfg.get("max_cases_per_run", 5))
    to_enrich = candidates[:max_cases]
    if not to_enrich:
        return cases, stats

    raw_response = ""
    for attempt in range(max_attempts):
        if ops is not None and not ops.reserve_llm_call("research_case_llm", case_timeout, item_count=len(to_enrich)):
            for case in to_enrich:
                case["case_error"] = "Skipped case LLM analysis: runtime budget exhausted"
            stats["research_case_llm_skipped_budget"] = len(to_enrich)
            return cases, stats
        timeout = ops.effective_timeout(case_timeout) if ops is not None else case_timeout
        started = time.perf_counter()
        try:
            raw_response = _call_case_provider(
                provider,
                api_key=api_key,
                timeout=timeout,
                temperature=llm_cfg["temperature"],
                user_content=_build_case_prompt(to_enrich),
                config=config,
            )
            stats["research_case_llm_called"] += 1
            if ops is not None:
                ops.record_llm_call("research_case_llm", True, int((time.perf_counter() - started) * 1000))
            break
        except Exception as exc:
            stats["research_case_llm_called"] += 1
            if attempt + 1 < max_attempts:
                _sleep_retry_backoff(ops)
                continue
            if ops is not None:
                ops.record_llm_call("research_case_llm", False, int((time.perf_counter() - started) * 1000), str(exc))
            for case in to_enrich:
                case["case_error"] = "case LLM call failed after {0} attempts: {1}".format(max_attempts, exc)
            stats["research_case_llm_failed"] = len(to_enrich)
            return cases, stats

    parsed_items = None
    parse_error = None
    for attempt in range(parse_retries + 1):
        try:
            parsed_items = _parse_llm_json(raw_response)
            break
        except Exception as exc:
            parse_error = exc
            if attempt < parse_retries:
                try:
                    if ops is not None and not ops.reserve_llm_call("research_case_llm", case_timeout, item_count=len(to_enrich)):
                        raise RuntimeError("runtime budget exhausted")
                    timeout = ops.effective_timeout(case_timeout) if ops is not None else case_timeout
                    started = time.perf_counter()
                    raw_response = _call_case_provider(
                        provider,
                        api_key=api_key,
                        timeout=timeout,
                        temperature=llm_cfg["temperature"],
                        user_content=_build_case_prompt(to_enrich),
                        config=config,
                    )
                    stats["research_case_llm_called"] += 1
                    if ops is not None:
                        ops.record_llm_call("research_case_llm", True, int((time.perf_counter() - started) * 1000))
                except Exception:
                    pass
                continue
            for case in to_enrich:
                case["case_error"] = "case LLM JSON parse failed after retry: {0}".format(parse_error)
            stats["research_case_llm_failed"] = len(to_enrich)
            return cases, stats

    by_id = {str(item.get("case_id")): item for item in parsed_items or [] if isinstance(item, dict)}
    for case in to_enrich:
        item = by_id.get(case["case_id"])
        if not item:
            case["case_error"] = "case LLM response missing case_id={0}".format(case["case_id"])
            stats["research_case_llm_failed"] += 1
            continue
        case["case_analysis"] = _validate_case_analysis(item)
        case["case_error"] = ""
        stats["research_case_llm_success"] += 1
    return cases, stats


def _sleep_retry_backoff(ops: Any = None) -> None:
    seconds = ops.retry_backoff_seconds() if ops is not None else 0
    if seconds > 0:
        time.sleep(seconds)


def _case_llm_timeout(cfg: Dict[str, Any], llm_cfg: Dict[str, Any]) -> int:
    if "llm_timeout" in cfg:
        return max(1, int(cfg.get("llm_timeout") or 1))
    return max(1, min(int(llm_cfg.get("timeout", 30) or 30), 30))


def _case_llm_max_attempts(cfg: Dict[str, Any]) -> int:
    return max(1, int(cfg.get("llm_max_attempts", 1) or 1))


def _case_llm_parse_retries(cfg: Dict[str, Any]) -> int:
    return max(0, int(cfg.get("llm_parse_retries", 0) or 0))


def record_case_decision(
    case_id: str,
    decision: str,
    notes: str = "",
    actor: str = "user",
) -> Dict[str, Any]:
    decision = str(decision).strip()
    if decision in FORBIDDEN_CASE_DECISIONS or decision not in VALID_CASE_DECISIONS:
        raise ValueError("Invalid or unsafe case decision: {0}".format(decision))
    decided_at = now_iso()
    record = {
        "record_id": "case_decision:{0}:{1}".format(_compact_time(decided_at), uuid.uuid4().hex[:8]),
        "case_id": str(case_id).strip(),
        "decision": decision,
        "status": _status_from_decision(decision),
        "notes": str(notes or ""),
        "actor": str(actor or "user"),
        "decided_at": decided_at,
        "source": "manual_research_case_decision",
        "forbidden_actions_note": "No auto trading, order placement, or position rebalance.",
    }
    if not record["case_id"]:
        raise ValueError("case_id is required")
    path = _case_decision_log_path(record["decided_at"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    _emit_case_event(record)

    cases = _apply_case_decisions(_load_cases(), _load_case_decisions())
    if cases:
        _save_cases(cases)
        write_case_summary(compute_case_summary(cases=cases))
    return record


def list_research_cases(status: str = "") -> List[Dict[str, Any]]:
    cases = _apply_case_decisions(_load_cases(), _load_case_decisions())
    if status:
        return [case for case in cases if case.get("status") == status]
    return cases


def compute_case_summary(cases: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    cases = _apply_case_decisions(cases if cases is not None else _load_cases(), _load_case_decisions())
    status_counts = Counter(str(case.get("status", "active")) for case in cases)
    latest_decisions = {
        str(case["case_id"]): case.get("case_decision")
        for case in cases
        if case.get("case_id") and case.get("case_decision")
    }
    active_cases = [
        case for case in cases
        if case.get("status") not in ("noise", "archived")
    ]
    return {
        "generated_at": now_iso(),
        "total_cases": len(cases),
        "active_cases": len(active_cases),
        "status_counts": dict(status_counts),
        "latest_decisions": latest_decisions,
        "cases": cases,
        "valid_decisions": sorted(VALID_CASE_DECISIONS),
        "status_changes": [
            {
                "case_id": case["case_id"],
                "status": case.get("status", "active"),
                "decision": (case.get("case_decision") or {}).get("decision", ""),
                "decided_at": (case.get("case_decision") or {}).get("decided_at", ""),
            }
            for case in cases
            if case.get("case_decision")
        ],
    }


def write_case_summary(summary: Dict[str, Any]) -> Dict[str, str]:
    out_dir = project_path("reports/cases")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = summary.get("generated_at", now_iso()).replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
    md_path = out_dir / "case_summary_{0}.md".format(stamp)
    json_path = out_dir / "case_summary_{0}.json".format(stamp)
    latest_md = out_dir / "latest.md"
    latest_json = out_dir / "latest.json"
    md = _render_case_summary_markdown(summary)
    js = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    md_path.write_text(md, encoding="utf-8-sig")
    latest_md.write_text(md, encoding="utf-8-sig")
    json_path.write_text(js, encoding="utf-8")
    latest_json.write_text(js, encoding="utf-8")
    return {
        "research_case_summary_path": str(md_path),
        "research_case_summary_json_path": str(json_path),
        "research_case_summary_latest_path": str(latest_md),
        "research_case_summary_latest_json_path": str(latest_json),
    }


def _call_case_provider(
    provider: Dict[str, Any],
    *,
    api_key: str,
    timeout: int,
    temperature: float,
    user_content: str,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    if provider_uses_pi(provider, config):
        return call_pi_agent(
            system_prompt=CASE_SYSTEM_PROMPT,
            user_content=user_content,
            provider=provider,
            config=config,
            role_id="case_analyst",
            timeout=timeout,
        )
    return _call_llm(
        base_url=str(provider.get("base_url") or ""),
        model=str(provider.get("model") or ""),
        api_key=api_key,
        timeout=timeout,
        temperature=temperature,
        user_content=user_content,
    )


def _call_llm(
    base_url: str,
    model: str,
    api_key: str,
    timeout: int,
    temperature: float,
    user_content: str,
) -> str:
    url = "{0}/chat/completions".format(base_url.rstrip("/"))
    payload = json.dumps({
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": CASE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer {0}".format(api_key),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


def _case_key(signal: AnalyzedSignal) -> Dict[str, str]:
    payload = signal.payload or {}
    filing = payload.get("filing_evidence") if isinstance(payload.get("filing_evidence"), dict) else {}
    entity = _first_non_empty(
        signal.symbols,
        [payload.get("symbol"), payload.get("code"), payload.get("name"), payload.get("company")],
        signal.topics,
        [signal.source_type],
    )
    topic = _first_non_empty(signal.topics, [signal.source_type])
    announcement_type = str(payload.get("announcement_type") or filing.get("announcement_type") or "").strip()
    if signal.source_type == "filing" and announcement_type:
        catalyst_path = "filing:{0}".format(announcement_type)
    elif signal.source_type == "price":
        catalyst_path = "market_price_action"
    elif signal.source_type == "source_error":
        catalyst_path = "source_health"
    elif signal.research_tracks:
        catalyst_path = str(signal.research_tracks[0].get("id") or "event_impact")
    else:
        catalyst_path = "{0}:general".format(signal.source_type)
    return {
        "entity": entity,
        "topic": topic,
        "catalyst_path": catalyst_path,
    }


def _case_id(key: Dict[str, str]) -> str:
    raw = "|".join([key.get("entity", ""), key.get("topic", ""), key.get("catalyst_path", "")]).lower()
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _new_case(case_id: str, key: Dict[str, str], previous: Dict[str, Any]) -> Dict[str, Any]:
    created_at = previous.get("created_at") or now_iso()
    return {
        "case_id": case_id,
        "title": previous.get("title") or "{0} / {1} / {2}".format(key["entity"], key["topic"], key["catalyst_path"]),
        "entity": key["entity"],
        "topic": key["topic"],
        "catalyst_path": key["catalyst_path"],
        "status": previous.get("status", "active"),
        "created_at": created_at,
        "updated_at": now_iso(),
        "latest_signal_at": previous.get("latest_signal_at", ""),
        "signal_ids": list(previous.get("signal_ids", [])),
        "signal_count": int(previous.get("signal_count", 0)),
        "priorities": list(previous.get("priorities", [])),
        "top_priority": previous.get("top_priority", "P3"),
        "source_types": list(previous.get("source_types", [])),
        "announcement_types": list(previous.get("announcement_types", [])),
        "recent_signals": list(previous.get("recent_signals", [])),
        "case_analysis": dict(previous.get("case_analysis", {})),
        "case_error": previous.get("case_error", ""),
        "case_decision": dict(previous.get("case_decision", {})),
    }


def _merge_signal_into_case(case: Dict[str, Any], signal: AnalyzedSignal) -> None:
    if signal.signal_id not in case["signal_ids"]:
        case["signal_ids"].append(signal.signal_id)
    case["signal_count"] = len(case["signal_ids"])
    case["latest_signal_at"] = max(str(case.get("latest_signal_at") or ""), signal.published_at or "")
    case["updated_at"] = now_iso()
    case["priorities"] = _unique(case.get("priorities", []) + [signal.priority])
    case["top_priority"] = _top_priority(case["priorities"])
    case["source_types"] = _unique(case.get("source_types", []) + [signal.source_type])
    announcement_type = str((signal.payload or {}).get("announcement_type") or "")
    if announcement_type:
        case["announcement_types"] = _unique(case.get("announcement_types", []) + [announcement_type])
    compact = {
        "signal_id": signal.signal_id,
        "title": signal.title,
        "priority": signal.priority,
        "source": signal.source,
        "source_type": signal.source_type,
        "published_at": signal.published_at,
        "reasons": signal.reasons[:5],
        "llm_key_thesis": (signal.llm_analysis or {}).get("key_thesis", ""),
        "source_quality": (signal.payload or {}).get("source_quality", {}),
    }
    recent = [item for item in case.get("recent_signals", []) if item.get("signal_id") != signal.signal_id]
    recent.append(compact)
    recent.sort(key=lambda item: item.get("published_at", ""), reverse=True)
    case["recent_signals"] = recent[:10]


def _first_non_empty(*groups: Iterable[Any]) -> str:
    for group in groups:
        for item in group:
            value = str(item or "").strip()
            if value:
                return value
    return "unknown"


def _unique(values: Iterable[Any]) -> List[str]:
    result: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _top_priority(priorities: Iterable[str]) -> str:
    values = list(priorities)
    if not values:
        return "P3"
    return sorted(values, key=lambda item: PRIORITY_ORDER.get(item, 99))[0]


def _case_llm_candidates(cases: List[Dict[str, Any]], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    threshold = str(cfg.get("min_priority") or cfg.get("llm_min_priority") or "P2")
    candidates = []
    for case in cases:
        if case.get("status") in ("noise", "archived"):
            continue
        if not min_priority_passes(str(case.get("top_priority", "P3")), threshold):
            continue
        candidates.append(case)
    return candidates


def _build_case_prompt(cases: List[Dict[str, Any]]) -> str:
    payload = []
    for case in cases:
        payload.append({
            "case_id": case.get("case_id"),
            "title": case.get("title"),
            "entity": case.get("entity"),
            "topic": case.get("topic"),
            "catalyst_path": case.get("catalyst_path"),
            "status": case.get("status"),
            "top_priority": case.get("top_priority"),
            "signal_count": case.get("signal_count"),
            "recent_signals": case.get("recent_signals", []),
            "case_decision": case.get("case_decision", {}),
        })
    return "Analyze these research cases and return a JSON array:\n{0}".format(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    )


def _validate_case_analysis(item: Dict[str, Any]) -> Dict[str, Any]:
    hypotheses = []
    for raw in item.get("hypotheses", []) or []:
        if not isinstance(raw, dict):
            continue
        hypotheses.append({
            "name": str(raw.get("name") or "hypothesis"),
            "thesis": str(raw.get("thesis") or ""),
            "probability": _clamp_float(raw.get("probability"), default=0.5),
            "evidence_for": _string_list(raw.get("evidence_for", [])),
            "evidence_against": _string_list(raw.get("evidence_against", [])),
        })
    options = [
        item for item in _string_list(item.get("next_decision_options", []))
        if item in VALID_CASE_DECISIONS
    ]
    if not options:
        options = ["continue_tracking", "needs_evidence"]
    return {
        "hypotheses": hypotheses,
        "evidence_gaps": _string_list(item.get("evidence_gaps", [])),
        "counter_questions": _string_list(item.get("counter_questions", [])),
        "next_decision_options": options,
        "human_decision_needed": bool(item.get("human_decision_needed", True)),
        "analyzed_at": now_iso(),
    }


def _clamp_float(value: Any, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _string_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value in (None, ""):
        return []
    return [str(value)]


def _empty_case_llm_stats(config: Dict[str, Any]) -> Dict[str, Any]:
    llm_cfg = _get_llm_config(config)
    return {
        "research_case_llm_enabled": bool(config.get("research_cases", {}).get("llm_enabled", True) and llm_cfg.get("enabled", False)),
        "research_case_llm_model": llm_cfg.get("model", ""),
        "research_case_llm_base_url": llm_cfg.get("base_url", ""),
        "research_case_llm_candidates": 0,
        "research_case_llm_called": 0,
        "research_case_llm_success": 0,
        "research_case_llm_failed": 0,
        "research_case_llm_skipped_no_key": 0,
        "research_case_llm_skipped_budget": 0,
    }


def _case_stats(cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    status_counts = Counter(str(case.get("status", "active")) for case in cases)
    return {
        "research_cases": cases,
        "research_case_count": len(cases),
        "research_case_status_counts": dict(status_counts),
        "research_case_active_count": sum(
            1 for case in cases if case.get("status") not in ("noise", "archived")
        ),
        "research_cases_path": str(_cases_path()),
    }


def _status_from_decision(decision: str) -> str:
    if decision == "mark_noise":
        return "noise"
    if decision == "archive":
        return "archived"
    return decision


def _apply_case_decisions(cases: List[Dict[str, Any]], decisions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    for decision in decisions:
        case_id = decision.get("case_id")
        if case_id:
            latest[str(case_id)] = decision
    for case in cases:
        decision = latest.get(str(case.get("case_id")))
        if decision:
            case["case_decision"] = decision
            case["status"] = decision.get("status") or _status_from_decision(decision.get("decision", "continue_tracking"))
    return cases


def _cases_path():
    return project_path("data/research_cases/cases.json")


def _case_decision_log_path(decided_at: str):
    date_part = decided_at[:10].replace("-", "")
    return project_path("data/research_cases/case_decisions_{0}.jsonl".format(date_part))


def _compact_time(value: str) -> str:
    return str(value or now_iso()).replace(":", "").replace("-", "").replace("+", "_").split(".")[0]


def _emit_case_event(record: Dict[str, Any]) -> None:
    try:
        from . import brokerage_architecture
        original_project_path = brokerage_architecture.project_path
        brokerage_architecture.project_path = project_path
        try:
            brokerage_architecture.record_research_case_event(record)
        finally:
            brokerage_architecture.project_path = original_project_path
    except Exception:
        return


def _load_cases() -> List[Dict[str, Any]]:
    path = _cases_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    cases = data.get("cases", []) if isinstance(data, dict) else []
    return [case for case in cases if isinstance(case, dict)]


def _save_cases(cases: List[Dict[str, Any]]) -> None:
    path = _cases_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cases": cases}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _load_case_decisions() -> List[Dict[str, Any]]:
    decision_dir = project_path("data/research_cases")
    if not decision_dir.exists():
        return []
    records: List[Dict[str, Any]] = []
    for path in sorted(decision_dir.glob("case_decisions_*.jsonl")):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    record = json.loads(line)
                    if isinstance(record, dict):
                        records.append(record)
        except (OSError, json.JSONDecodeError):
            continue
    return records


def _render_case_summary_markdown(summary: Dict[str, Any]) -> str:
    lines = ["# Research Case Summary", ""]
    lines.append("- generated_at: {0}".format(summary.get("generated_at", "")))
    lines.append("- total_cases: {0}".format(summary.get("total_cases", 0)))
    lines.append("- active_cases: {0}".format(summary.get("active_cases", 0)))
    lines.append("")
    lines.append("## Status Counts")
    counts = summary.get("status_counts", {})
    if counts:
        for status, count in sorted(counts.items()):
            lines.append("- {0}: {1}".format(status, count))
    else:
        lines.append("- no cases")
    lines.append("")
    lines.append("## Recent Cases")
    for case in summary.get("cases", [])[:20]:
        lines.append("- [{0}] {1} | {2} | signals={3}".format(
            case.get("status", "active"),
            case.get("case_id", ""),
            case.get("title", ""),
            case.get("signal_count", 0),
        ))
    if not summary.get("cases"):
        lines.append("- no cases")
    return "\n".join(lines)
