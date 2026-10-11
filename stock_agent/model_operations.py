"""Production model operations, SLOs, usage accounting, and deadline policy.

The module deliberately stores request metadata only. API keys, prompts, source
payloads, response content, and reasoning text are never written to its audit
artifacts.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import project_path
from .models import AnalyzedSignal, now_iso


MODEL_OPERATIONS_VERSION = "22.0"
PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
SAFE_FINISH_REASONS = {"", "stop"}


class LLMResponse(str):
    """String-compatible response carrying non-content API metadata."""

    def __new__(cls, content: str, metadata: Optional[Dict[str, Any]] = None) -> "LLMResponse":
        value = str.__new__(cls, content or "")
        value.metadata = sanitize_response_metadata(metadata or {})
        return value


def model_operations_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("model_operations", {}) or {}
    service = raw.get("service_levels", {}) or {}
    deadline = raw.get("deadline_policy", {}) or {}
    quality = raw.get("quality_thresholds", {}) or {}
    pricing = raw.get("pricing", {}) or {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "report_dir": str(raw.get("report_dir") or "reports/model_operations"),
        "usage_dir": str(raw.get("usage_dir") or "data/model_operations/usage"),
        "rolling_window_runs": max(1, int(raw.get("rolling_window_runs", 20) or 20)),
        "service_levels": {
            "agent_target_seconds": float(service.get("agent_target_seconds", 180)),
            "scheduled_delivery_target_seconds": float(service.get("scheduled_delivery_target_seconds", 300)),
            "signal_llm_warn_seconds": float(service.get("signal_llm_warn_seconds", 60)),
            "role_llm_warn_seconds": float(service.get("role_llm_warn_seconds", 90)),
        },
        "deadline_policy": {
            "post_llm_reserve_seconds": float(deadline.get("post_llm_reserve_seconds", 45)),
            "main_min_start_remaining_seconds": float(deadline.get("main_min_start_remaining_seconds", 75)),
            "main_reduce_below_seconds": float(deadline.get("main_reduce_below_seconds", 105)),
            "reduced_main_max_signals": max(1, int(deadline.get("reduced_main_max_signals", 1) or 1)),
            "role_min_start_remaining_seconds": float(deadline.get("role_min_start_remaining_seconds", 75)),
            "role_reduce_below_seconds": float(deadline.get("role_reduce_below_seconds", 120)),
            "reduced_role_max_signals": max(1, int(deadline.get("reduced_role_max_signals", 1) or 1)),
            "retry_min_remaining_seconds": float(deadline.get("retry_min_remaining_seconds", 75)),
        },
        "quality_thresholds": {
            "main_success_rate_min": float(quality.get("main_success_rate_min", 0.95)),
            "role_success_rate_min": float(quality.get("role_success_rate_min", 0.90)),
            "usable_grounding_rate_min": float(quality.get("usable_grounding_rate_min", 0.95)),
            "fully_grounded_rate_min": float(quality.get("fully_grounded_rate_min", 0.75)),
            "usage_metadata_rate_min": float(quality.get("usage_metadata_rate_min", 1.0)),
        },
        "pricing": {
            "enabled": bool(pricing.get("enabled", False)),
            "currency": str(pricing.get("currency") or "USD"),
            "effective_date": str(pricing.get("effective_date") or ""),
            "source_url": str(pricing.get("source_url") or ""),
            "models": pricing.get("models", {}) if isinstance(pricing.get("models", {}), dict) else {},
        },
    }


def sanitize_response_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    usage = metadata.get("usage", {}) if isinstance(metadata, dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("completion_tokens_details", {}) or {}
    clean_usage = {
        "prompt_tokens": _nonnegative_int(usage.get("prompt_tokens")),
        "completion_tokens": _nonnegative_int(usage.get("completion_tokens")),
        "total_tokens": _nonnegative_int(usage.get("total_tokens")),
        "prompt_cache_hit_tokens": _nonnegative_int(usage.get("prompt_cache_hit_tokens")),
        "prompt_cache_miss_tokens": _nonnegative_int(usage.get("prompt_cache_miss_tokens")),
        "reasoning_tokens": _nonnegative_int(details.get("reasoning_tokens") if isinstance(details, dict) else 0),
    }
    return {
        "request_id": str(metadata.get("request_id") or "")[:256],
        "model": str(metadata.get("model") or "")[:256],
        "finish_reason": str(metadata.get("finish_reason") or "")[:64],
        "system_fingerprint": str(metadata.get("system_fingerprint") or "")[:256],
        "usage": clean_usage,
    }


def response_metadata(response: Any) -> Dict[str, Any]:
    return sanitize_response_metadata(getattr(response, "metadata", {}) or {})


def select_deadline_candidates(
    candidates: Sequence[AnalyzedSignal],
    config: Dict[str, Any],
    ops: Any,
    component: str,
) -> Tuple[List[AnalyzedSignal], List[AnalyzedSignal], Dict[str, Any]]:
    ordered = sorted(
        list(candidates),
        key=lambda item: (PRIORITY_RANK.get(str(item.priority).upper(), 9), -float(item.priority_score or 0), item.signal_id),
    )
    cfg = model_operations_config(config)
    decision = {
        "component": component,
        "action": "full",
        "candidate_count": len(ordered),
        "selected_count": len(ordered),
        "skipped_count": 0,
        "agent_target_seconds": cfg["service_levels"]["agent_target_seconds"],
        "elapsed_seconds": round(float(ops.elapsed_seconds()), 3) if ops is not None else 0.0,
        "remaining_seconds": None,
        "reason": "",
    }
    if not cfg["enabled"] or ops is None or not ordered:
        return ordered, [], decision

    remaining = operational_remaining_seconds(ops, cfg)
    decision["remaining_seconds"] = round(remaining, 3)
    policy = cfg["deadline_policy"]
    if component == "signal_llm":
        minimum = policy["main_min_start_remaining_seconds"]
        reduce_below = policy["main_reduce_below_seconds"]
        reduced_max = policy["reduced_main_max_signals"]
    else:
        minimum = policy["role_min_start_remaining_seconds"]
        reduce_below = policy["role_reduce_below_seconds"]
        reduced_max = policy["reduced_role_max_signals"]

    if remaining < minimum:
        selected: List[AnalyzedSignal] = []
        skipped = ordered
        decision.update({
            "action": "skip",
            "selected_count": 0,
            "skipped_count": len(skipped),
            "reason": "delivery deadline reserve would be consumed",
        })
        return selected, skipped, decision

    if remaining < reduce_below and len(ordered) > reduced_max:
        selected = ordered[:reduced_max]
        skipped = ordered[reduced_max:]
        decision.update({
            "action": "reduce",
            "selected_count": len(selected),
            "skipped_count": len(skipped),
            "reason": "candidate batch reduced to preserve scheduled delivery",
        })
        return selected, skipped, decision
    return ordered, [], decision


def operational_remaining_seconds(ops: Any, cfg_or_config: Dict[str, Any]) -> float:
    cfg = cfg_or_config if "service_levels" in cfg_or_config else model_operations_config(cfg_or_config)
    target = float(cfg["service_levels"]["agent_target_seconds"])
    return max(0.0, target - float(ops.elapsed_seconds()))


def deadline_timeout(requested_seconds: float, ops: Any, config: Dict[str, Any]) -> int:
    cfg = model_operations_config(config)
    if not cfg["enabled"] or ops is None:
        return max(1, int(requested_seconds))
    remaining = operational_remaining_seconds(ops, cfg)
    available = remaining - float(cfg["deadline_policy"]["post_llm_reserve_seconds"])
    if available <= 0:
        return 0
    return max(1, int(min(float(requested_seconds), available)))


def deadline_retry_allowed(ops: Any, config: Dict[str, Any]) -> bool:
    cfg = model_operations_config(config)
    if not cfg["enabled"] or ops is None:
        return True
    return operational_remaining_seconds(ops, cfg) >= float(cfg["deadline_policy"]["retry_min_remaining_seconds"])


def build_model_operations_snapshot(
    signals: Sequence[AnalyzedSignal],
    stats: Dict[str, Any],
    run_ops: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    cfg = model_operations_config(config)
    calls = [_compact_call(item, index) for index, item in enumerate(run_ops.get("llm_calls", []) or [], 1)]
    successful_calls = [item for item in calls if item["success"]]
    usage_calls = [item for item in successful_calls if item["usage"]["total_tokens"] > 0]
    usage = _sum_usage(item["usage"] for item in calls)
    usage_rate = _rate(len(usage_calls), len(successful_calls))

    main_selected = int(stats.get("llm_selected", stats.get("llm_candidates", 0)) or 0)
    role_selected = int(stats.get("role_llm_selected", stats.get("role_llm_candidates", 0)) or 0)
    main_success = int(stats.get("llm_success", 0) or 0)
    role_success = int(stats.get("role_llm_success", 0) or 0)
    main_rate = _rate(main_success, main_selected)
    role_rate = _rate(role_success, role_selected)

    grounding = _grounding_metrics(signals)
    elapsed_seconds = round(float(run_ops.get("elapsed_ms", 0) or 0) / 1000.0, 3)
    thresholds = cfg["quality_thresholds"]
    service = cfg["service_levels"]
    checks = [
        _max_check("agent_delivery_seconds", elapsed_seconds, service["agent_target_seconds"], "breach"),
        _min_check("main_success_rate", main_rate, thresholds["main_success_rate_min"], "breach", applicable=main_selected > 0),
        _min_check("role_success_rate", role_rate, thresholds["role_success_rate_min"], "breach", applicable=role_selected > 0),
        _min_check("usable_grounding_rate", grounding["usable_rate"], thresholds["usable_grounding_rate_min"], "breach", applicable=grounding["total"] > 0),
        _min_check("fully_grounded_rate", grounding["fully_grounded_rate"], thresholds["fully_grounded_rate_min"], "warn", applicable=grounding["total"] > 0),
        _min_check("usage_metadata_rate", usage_rate, thresholds["usage_metadata_rate_min"], "warn", applicable=len(successful_calls) > 0),
    ]
    nonstandard_finish = [item for item in calls if item["success"] and item["finish_reason"] not in SAFE_FINISH_REASONS]
    if nonstandard_finish:
        checks.append({
            "name": "finish_reason",
            "status": "BREACH",
            "severity": "breach",
            "actual": ",".join(sorted({item["finish_reason"] for item in nonstandard_finish})),
            "target": "stop",
        })

    breach_count = sum(1 for item in checks if item["status"] == "BREACH")
    warn_count = sum(1 for item in checks if item["status"] == "WARN")
    deadline_actions = [
        item for item in (stats.get("llm_deadline", {}), stats.get("role_llm_deadline", {}))
        if isinstance(item, dict) and item.get("action") in {"reduce", "skip"}
    ]
    status = "IDLE" if not calls and not main_selected and not role_selected else "PASS"
    if breach_count:
        status = "BREACH"
    elif warn_count or deadline_actions:
        status = "ATTENTION"

    pricing = _estimate_cost(calls, cfg["pricing"])
    provider_summary = _provider_summary(calls)
    return {
        "version": MODEL_OPERATIONS_VERSION,
        "generated_at": now_iso(),
        "run_id": str(run_ops.get("run_id") or ""),
        "mode": str(run_ops.get("mode") or ""),
        "status": status,
        "measurement_scope": "agent-run through research decisions; scheduled handoff measured separately",
        "service_levels": service,
        "checks": checks,
        "breach_count": breach_count,
        "warning_count": warn_count,
        "quality": {
            "main_selected": main_selected,
            "main_success": main_success,
            "main_success_rate": main_rate,
            "role_selected": role_selected,
            "role_success": role_success,
            "role_success_rate": role_rate,
            "grounding": grounding,
            "usage_metadata_rate": usage_rate,
        },
        "usage": usage,
        "estimated_cost": pricing,
        "providers": provider_summary,
        "calls": calls,
        "deadline_decisions": {
            "signal_llm": stats.get("llm_deadline", {}),
            "role_llm": stats.get("role_llm_deadline", {}),
        },
        "human_control": {
            "deadline_skipped_signals": int(stats.get("llm_skipped_deadline", 0) or 0) + int(stats.get("role_llm_skipped_deadline", 0) or 0),
            "retry_suppressed": int(stats.get("llm_retry_suppressed", 0) or 0) + int(stats.get("role_llm_retry_suppressed", 0) or 0),
            "automatic_trade": False,
            "automatic_assignment": False,
            "automatic_approval": False,
            "automatic_publication": False,
        },
        "safety_boundary": "Research support only. Deadline degradation creates human review work; it never permits trading, approval, or publication.",
    }


def write_model_operations_summary(
    signals: Sequence[AnalyzedSignal],
    stats: Dict[str, Any],
    run_ops: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    cfg = model_operations_config(config)
    if not cfg["enabled"]:
        return {"model_operations_enabled": False}
    snapshot = build_model_operations_snapshot(signals, stats, run_ops, config)
    report_dir = project_path(cfg["report_dir"])
    usage_dir = project_path(cfg["usage_dir"])
    report_dir.mkdir(parents=True, exist_ok=True)
    usage_dir.mkdir(parents=True, exist_ok=True)
    stamp = _compact_stamp(snapshot["generated_at"])
    json_path = report_dir / "model_ops_{0}.json".format(stamp)
    md_path = report_dir / "model_ops_{0}.md".format(stamp)
    latest_json = report_dir / "latest.json"
    latest_md = report_dir / "latest.md"

    snapshot["rolling"] = _rolling_summary(report_dir, snapshot, cfg["rolling_window_runs"])
    json_text = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)
    markdown = render_model_operations_markdown(snapshot)
    json_path.write_text(json_text, encoding="utf-8")
    latest_json.write_text(json_text, encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8-sig")
    latest_md.write_text(markdown, encoding="utf-8-sig")

    usage_path = usage_dir / "usage_{0}.jsonl".format(snapshot["generated_at"][:10].replace("-", ""))
    if snapshot["calls"]:
        with usage_path.open("a", encoding="utf-8") as handle:
            for item in snapshot["calls"]:
                row = dict(item)
                row.update({
                    "record_id": "{0}:{1}".format(snapshot["run_id"], item["sequence"]),
                    "run_id": snapshot["run_id"],
                    "recorded_at": snapshot["generated_at"],
                })
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return {
        "model_operations_enabled": True,
        "model_operations": snapshot,
        "model_operations_status": snapshot["status"],
        "model_operations_path": str(md_path),
        "model_operations_json_path": str(json_path),
        "model_operations_latest_path": str(latest_md),
        "model_operations_latest_json_path": str(latest_json),
        "model_operations_usage_path": str(usage_path) if snapshot["calls"] else "",
    }


def load_latest_model_operations(config: Dict[str, Any]) -> Dict[str, Any]:
    cfg = model_operations_config(config)
    path = project_path(str(Path(cfg["report_dir"]) / "latest.json"))
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def render_model_operations_markdown(snapshot: Dict[str, Any]) -> str:
    quality = snapshot.get("quality", {}) or {}
    grounding = quality.get("grounding", {}) or {}
    usage = snapshot.get("usage", {}) or {}
    cost = snapshot.get("estimated_cost", {}) or {}
    lines = [
        "# Model Operations",
        "",
        "- status: {0}".format(snapshot.get("status", "UNKNOWN")),
        "- run_id: {0}".format(snapshot.get("run_id", "")),
        "- generated_at: {0}".format(snapshot.get("generated_at", "")),
        "- safety: {0}".format(snapshot.get("safety_boundary", "")),
        "",
        "## Service Levels",
        "",
    ]
    for item in snapshot.get("checks", []) or []:
        lines.append("- [{0}] {1}: actual={2}, target={3}".format(
            item.get("status", ""), item.get("name", ""), item.get("actual", ""), item.get("target", "")
        ))
    lines.extend([
        "",
        "## Quality",
        "",
        "- main: {0}/{1} ({2:.1%})".format(quality.get("main_success", 0), quality.get("main_selected", 0), float(quality.get("main_success_rate", 1))),
        "- roles: {0}/{1} ({2:.1%})".format(quality.get("role_success", 0), quality.get("role_selected", 0), float(quality.get("role_success_rate", 1))),
        "- grounding: grounded={0}, partial={1}, blocked={2}, usable={3:.1%}".format(
            grounding.get("grounded", 0), grounding.get("partial", 0), grounding.get("blocked", 0), float(grounding.get("usable_rate", 1))
        ),
        "- usage metadata coverage: {0:.1%}".format(float(quality.get("usage_metadata_rate", 1))),
        "",
        "## Usage And Cost",
        "",
        "- prompt_tokens: {0}".format(usage.get("prompt_tokens", 0)),
        "- completion_tokens: {0}".format(usage.get("completion_tokens", 0)),
        "- reasoning_tokens: {0}".format(usage.get("reasoning_tokens", 0)),
        "- total_tokens: {0}".format(usage.get("total_tokens", 0)),
        "- estimated_cost: {0} {1} (configuration estimate, not provider billing)".format(cost.get("amount", 0), cost.get("currency", "")),
        "",
        "## Calls",
        "",
    ])
    if not snapshot.get("calls"):
        lines.append("- no LLM call in this run")
    for item in snapshot.get("calls", []) or []:
        lines.append("- {0} | {1}/{2} | success={3} | {4}ms | tokens={5} | finish={6}".format(
            item.get("component", ""), item.get("provider", ""), item.get("model", ""), item.get("success", False),
            item.get("duration_ms", 0), (item.get("usage", {}) or {}).get("total_tokens", 0), item.get("finish_reason", "")
        ))
    lines.extend([
        "",
        "No API key, raw prompt, response content, or reasoning text is stored in this report.",
        "",
    ])
    return "\n".join(lines)


def _compact_call(item: Dict[str, Any], sequence: int) -> Dict[str, Any]:
    usage = sanitize_response_metadata({"usage": item.get("usage", {})})["usage"]
    return {
        "sequence": sequence,
        "component": str(item.get("component") or ""),
        "provider": str(item.get("provider") or ""),
        "model": str(item.get("model") or ""),
        "request_id": str(item.get("request_id") or "")[:256],
        "attempt": _nonnegative_int(item.get("attempt")),
        "item_count": _nonnegative_int(item.get("item_count")),
        "success": bool(item.get("success")),
        "duration_ms": _nonnegative_int(item.get("duration_ms")),
        "finish_reason": str(item.get("finish_reason") or "")[:64],
        "system_fingerprint": str(item.get("system_fingerprint") or "")[:256],
        "usage": usage,
        "error_class": str(item.get("error_class") or "")[:128],
        "error": str(item.get("error") or "")[:500],
    }


def _grounding_metrics(signals: Sequence[AnalyzedSignal]) -> Dict[str, Any]:
    statuses: List[str] = []
    for signal in signals:
        if signal.llm_analysis:
            statuses.append(str(signal.llm_analysis.get("grounding_status") or "unknown"))
        for view in signal.role_analyses or []:
            statuses.append(str(view.get("grounding_status") or "unknown"))
    grounded = statuses.count("grounded")
    partial = statuses.count("partial")
    blocked = statuses.count("blocked") + statuses.count("denied")
    total = len(statuses)
    usable = grounded + partial
    return {
        "total": total,
        "grounded": grounded,
        "partial": partial,
        "blocked": blocked,
        "unknown": max(0, total - grounded - partial - blocked),
        "usable_rate": _rate(usable, total),
        "fully_grounded_rate": _rate(grounded, total),
    }


def _estimate_cost(calls: Sequence[Dict[str, Any]], pricing: Dict[str, Any]) -> Dict[str, Any]:
    result = {
        "enabled": bool(pricing.get("enabled")),
        "currency": str(pricing.get("currency") or "USD"),
        "effective_date": str(pricing.get("effective_date") or ""),
        "source_url": str(pricing.get("source_url") or ""),
        "amount": 0.0,
        "unpriced_call_count": 0,
        "assumption": "Prompt tokens without cache split are conservatively priced as cache miss.",
    }
    if not result["enabled"]:
        return result
    models = pricing.get("models", {}) or {}
    total = 0.0
    unpriced = 0
    for item in calls:
        if not item.get("success"):
            continue
        price = models.get(item.get("model"), {}) if isinstance(models, dict) else {}
        if not isinstance(price, dict) or not price:
            unpriced += 1
            continue
        usage = item.get("usage", {}) or {}
        prompt = _nonnegative_int(usage.get("prompt_tokens"))
        hit = _nonnegative_int(usage.get("prompt_cache_hit_tokens"))
        miss = _nonnegative_int(usage.get("prompt_cache_miss_tokens"))
        if hit + miss == 0 and prompt:
            miss = prompt
        completion = _nonnegative_int(usage.get("completion_tokens"))
        total += hit * float(price.get("input_cache_hit_per_1m", 0) or 0) / 1_000_000.0
        total += miss * float(price.get("input_cache_miss_per_1m", 0) or 0) / 1_000_000.0
        total += completion * float(price.get("output_per_1m", 0) or 0) / 1_000_000.0
    result["amount"] = round(total, 8)
    result["unpriced_call_count"] = unpriced
    return result


def _provider_summary(calls: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for item in calls:
        groups.setdefault((item.get("provider", ""), item.get("model", "")), []).append(item)
    result = []
    for (provider, model), rows in sorted(groups.items()):
        durations = sorted(int(item.get("duration_ms", 0) or 0) for item in rows)
        result.append({
            "provider": provider,
            "model": model,
            "call_count": len(rows),
            "success_count": sum(1 for item in rows if item.get("success")),
            "p95_latency_ms": _percentile(durations, 0.95),
            "total_tokens": sum((item.get("usage", {}) or {}).get("total_tokens", 0) for item in rows),
        })
    return result


def _rolling_summary(report_dir: Path, current: Dict[str, Any], limit: int) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for path in sorted(report_dir.glob("model_ops_*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict) or value.get("run_id") == current.get("run_id"):
            continue
        rows.append(value)
        if len(rows) >= max(0, limit - 1):
            break
    rows.insert(0, current)
    statuses: Dict[str, int] = {}
    for item in rows:
        status = str(item.get("status") or "UNKNOWN")
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "window_runs": len(rows),
        "status_counts": statuses,
        "total_tokens": sum(int((item.get("usage", {}) or {}).get("total_tokens", 0) or 0) for item in rows),
        "estimated_cost": round(sum(float((item.get("estimated_cost", {}) or {}).get("amount", 0) or 0) for item in rows), 8),
    }


def _min_check(name: str, actual: float, target: float, severity: str, applicable: bool = True) -> Dict[str, Any]:
    status = "N/A" if not applicable else ("PASS" if actual >= target else ("BREACH" if severity == "breach" else "WARN"))
    return {"name": name, "status": status, "severity": severity, "actual": round(actual, 6), "target": target}


def _max_check(name: str, actual: float, target: float, severity: str) -> Dict[str, Any]:
    status = "PASS" if actual <= target else ("BREACH" if severity == "breach" else "WARN")
    return {"name": name, "status": status, "severity": severity, "actual": round(actual, 6), "target": target}


def _sum_usage(rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    result = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 0,
        "reasoning_tokens": 0,
    }
    for row in rows:
        for key in result:
            result[key] += _nonnegative_int(row.get(key))
    return result


def _rate(numerator: int, denominator: int) -> float:
    return 1.0 if denominator <= 0 else round(float(numerator) / float(denominator), 6)


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _percentile(values: Sequence[int], percentile: float) -> int:
    if not values:
        return 0
    index = max(0, min(len(values) - 1, int(round((len(values) - 1) * percentile))))
    return int(values[index])


def _compact_stamp(value: str) -> str:
    return str(value or datetime.now().astimezone().isoformat(timespec="seconds")).replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
