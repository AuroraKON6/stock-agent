from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from .config import project_path
from .models import RawSignal, now_iso


DEFAULT_TRUST = {
    "eastmoney": 0.85,
    "eastmoney_filing": 0.85,
    "local_jsonl": 0.70,
    "filing_jsonl": 0.75,
    "rss": 0.65,
    "pysnowball": 0.75,
    "alltick": 0.75,
    "akshare": 0.75,
    "free_quote": 0.70,
    "market_cache": 0.55,
}


def build_source_specs(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    sources = config.get("sources", {}) or {}
    specs: List[Dict[str, Any]] = []
    for kind in ["local_jsonl", "filing_jsonl", "rss"]:
        for item in sources.get(kind, []) or []:
            name = str(item.get("name") or item.get("path") or item.get("url") or kind)
            specs.append({
                "source_id": "{0}:{1}".format(kind, name),
                "source_kind": kind,
                "source_name": name,
                "enabled": bool(item.get("enabled", True)),
                "expected_symbols": [str(x) for x in item.get("symbols", [])],
            })
    for kind in ["eastmoney", "eastmoney_filing", "alltick", "pysnowball", "akshare", "free_quote"]:
        item = sources.get(kind, {}) or {}
        specs.append({
            "source_id": kind,
            "source_kind": kind,
            "source_name": kind,
            "enabled": bool(item.get("enabled", False)),
            "expected_symbols": [str(x) for x in item.get("symbols", [])],
        })
    return specs


def build_source_health_summary(
    events: Iterable[Dict[str, Any]],
    config: Dict[str, Any],
    duplicate_counts: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    events = list(events)
    duplicate_counts = duplicate_counts or {}
    cfg = config.get("source_health", {}) or {}
    warn_below = float(cfg.get("warn_score_below", 80))
    fail_below = float(cfg.get("fail_score_below", 50))

    items = []
    for event in events:
        item = _score_event(event, cfg, duplicate_counts, warn_below, fail_below)
        items.append(item)

    active_items = [item for item in items if item.get("status") != "DISABLED"]
    blocking_items = [item for item in active_items if _blocks_overall_health(item)]
    if any(item.get("status") == "FAIL" for item in blocking_items):
        overall_status = "FAIL"
    elif any(item.get("status") == "WARN" for item in blocking_items):
        overall_status = "WARN"
    else:
        overall_status = "OK"
    score_items = blocking_items or [
        item for item in active_items
        if not (item.get("fallback_resolved") and item.get("status") == "WARN")
    ]
    overall_score = (
        sum(float(item.get("score", 0)) for item in score_items) / len(score_items)
        if score_items else 100.0
    )
    low_quality = [
        item["source_id"] for item in active_items
        if item.get("status") in ("WARN", "FAIL") and _blocks_overall_health(item)
    ]
    coverage_gap_count = sum(len(item.get("coverage_gap_symbols", [])) for item in active_items)
    source_degraded_events = _build_source_degraded_events(events, config)
    summary = {
        "generated_at": now_iso(),
        "overall_status": overall_status,
        "overall_score": round(overall_score, 1),
        "source_count": len(items),
        "enabled_count": sum(1 for item in items if item.get("enabled")),
        "ok_count": sum(1 for item in active_items if item.get("status") == "OK"),
        "warn_count": sum(1 for item in active_items if item.get("status") == "WARN"),
        "fail_count": sum(1 for item in active_items if item.get("status") == "FAIL"),
        "coverage_gap_count": coverage_gap_count,
        "low_quality_sources": low_quality,
        "source_degraded_event_count": len(source_degraded_events),
        "source_degraded_events": source_degraded_events,
        "sources": items,
    }
    return summary


def apply_duplicate_counts(
    summary: Dict[str, Any],
    duplicate_counts: Dict[str, int],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    events = []
    for item in summary.get("sources", []):
        event = dict(item.get("_event", {}))
        if event:
            events.append(event)
    if not events:
        return summary
    return build_source_health_summary(events, config, duplicate_counts=duplicate_counts)


def attach_source_quality(signals: Iterable[RawSignal], summary: Dict[str, Any]) -> None:
    items = summary.get("sources", []) or []
    for signal in signals:
        item = _match_source_item(signal, items)
        if not item:
            continue
        quality = {
            "source_id": item.get("source_id", signal.source),
            "status": item.get("status", "unknown"),
            "score": item.get("score", 0),
            "success_rate": item.get("success_rate", 0),
            "latency_ms": item.get("latency_ms", 0),
            "field_completeness": item.get("field_completeness", 0),
            "duplicate_rate": item.get("duplicate_rate", 0),
            "trust_score": item.get("trust_score", 0),
            "coverage_gap_symbols": item.get("coverage_gap_symbols", []),
            "anomaly_reasons": item.get("anomaly_reasons", []),
            "evidence_gaps": _signal_evidence_gaps(signal, item),
            "needs_human_review": bool(item.get("needs_human_review") or _signal_evidence_gaps(signal, item)),
        }
        signal.payload.setdefault("source_quality", quality)


def write_source_health_summary(summary: Dict[str, Any]) -> Dict[str, str]:
    out_dir = project_path("reports/source_health")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = summary.get("generated_at", now_iso()).replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
    json_path = out_dir / "source_health_{0}.json".format(stamp)
    md_path = out_dir / "source_health_{0}.md".format(stamp)
    latest_json = out_dir / "latest.json"
    latest_md = out_dir / "latest.md"
    payload = _strip_private(summary)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    json_path.write_text(text, encoding="utf-8")
    latest_json.write_text(text, encoding="utf-8")
    md = _render_markdown(payload)
    md_path.write_text(md, encoding="utf-8-sig")
    latest_md.write_text(md, encoding="utf-8-sig")
    return {
        "source_health_path": str(md_path),
        "source_health_json_path": str(json_path),
        "source_health_latest_path": str(latest_md),
        "source_health_latest_json_path": str(latest_json),
    }


def public_source_health_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    return _strip_private(summary)


def _score_event(
    event: Dict[str, Any],
    cfg: Dict[str, Any],
    duplicate_counts: Dict[str, int],
    warn_below: float,
    fail_below: float,
) -> Dict[str, Any]:
    source_id = str(event.get("source_id") or event.get("source_kind") or "unknown")
    source_kind = str(event.get("source_kind") or source_id)
    enabled = bool(event.get("enabled", True))
    attempted = bool(event.get("attempted", False))
    signals = list(event.get("signals", []) or [])
    expected_symbols = [str(x) for x in event.get("expected_symbols", []) if str(x).strip()]
    latency_ms = int(event.get("latency_ms", 0) or 0)
    exception = str(event.get("exception") or "")
    fallback_resolved = bool(event.get("fallback_resolved", False))

    if not enabled:
        return {
            "source_id": source_id,
            "source_kind": source_kind,
            "source_name": str(event.get("source_name") or source_id),
            "enabled": False,
            "attempted": False,
            "status": "DISABLED",
            "score": 100.0,
            "latency_ms": 0,
            "signal_count": 0,
            "source_error_count": 0,
            "success_rate": 0.0,
            "field_completeness": 1.0,
            "duplicate_count": 0,
            "duplicate_rate": 0.0,
            "trust_score": _trust_score(source_id, source_kind, cfg),
            "coverage_expected": len(expected_symbols),
            "coverage_observed": 0,
            "coverage_gap_symbols": expected_symbols,
            "anomaly_reasons": [],
            "evidence_gaps": [],
            "needs_human_review": False,
            "_event": dict(event),
        }

    source_errors = [s for s in signals if getattr(s, "source_type", "") == "source_error"]
    normal_signals = [s for s in signals if getattr(s, "source_type", "") != "source_error"]
    field_completeness = _field_completeness(normal_signals)
    observed_symbols = _observed_symbols(normal_signals)
    coverage_gap = [symbol for symbol in expected_symbols if symbol not in observed_symbols]
    coverage_score = (
        len(observed_symbols.intersection(set(expected_symbols))) / len(expected_symbols)
        if expected_symbols else 1.0
    )
    duplicate_count = int(duplicate_counts.get(source_id, 0))
    signal_count = len(signals)
    duplicate_rate = duplicate_count / signal_count if signal_count else 0.0
    trust = _trust_score(source_id, source_kind, cfg)
    success_rate = 0.0 if exception or source_errors else (1.0 if attempted else 0.0)
    duplicate_score = max(0.0, 1.0 - duplicate_rate)
    score = 100.0 * (
        0.35 * success_rate +
        0.25 * field_completeness +
        0.15 * coverage_score +
        0.15 * trust +
        0.10 * duplicate_score
    )

    anomalies: List[str] = []
    evidence_gaps: List[str] = []
    if exception:
        anomalies.append("collector_exception")
        evidence_gaps.append("collector_exception")
    if source_errors:
        anomalies.append("source_error")
        evidence_gaps.append("source_error")
    if fallback_resolved:
        anomalies.append("fallback_resolved")
    if attempted and signal_count == 0:
        anomalies.append("empty_enabled_source")
        evidence_gaps.append("no_records")
    if field_completeness < 0.95:
        anomalies.append("field_missing")
        evidence_gaps.append("missing_fields")
    if coverage_gap:
        anomalies.append("coverage_gap")
        evidence_gaps.extend(["coverage:{0}".format(symbol) for symbol in coverage_gap])
    if duplicate_count:
        anomalies.append("duplicates")
    if trust < 0.6:
        anomalies.append("low_trust")

    blocking_anomalies = [
        item for item in anomalies
        if item not in ("low_trust",)
    ]

    if (exception or source_errors) and fallback_resolved:
        status = "WARN"
    elif source_errors and _warn_only_source_error(source_id, source_kind, cfg):
        status = "WARN"
    elif exception or source_errors:
        status = "FAIL"
    elif score < fail_below:
        status = "FAIL"
    elif score < warn_below or blocking_anomalies:
        status = "WARN"
    else:
        status = "OK"

    return {
        "source_id": source_id,
        "source_kind": source_kind,
        "source_name": str(event.get("source_name") or source_id),
        "enabled": enabled,
        "attempted": attempted,
        "status": status,
        "score": round(score, 1),
        "latency_ms": latency_ms,
        "signal_count": signal_count,
        "source_error_count": len(source_errors),
        "success_rate": round(success_rate, 3),
        "field_completeness": round(field_completeness, 3),
        "duplicate_count": duplicate_count,
        "duplicate_rate": round(duplicate_rate, 3),
        "trust_score": round(trust, 3),
        "coverage_expected": len(expected_symbols),
        "coverage_observed": len(observed_symbols.intersection(set(expected_symbols))) if expected_symbols else len(observed_symbols),
        "coverage_gap_symbols": coverage_gap,
        "anomaly_reasons": sorted(set(anomalies)),
        "evidence_gaps": sorted(set(evidence_gaps)),
        "needs_human_review": status in ("WARN", "FAIL"),
        "fallback_resolved": fallback_resolved,
        "_event": dict(event),
    }


def _blocks_overall_health(item: Dict[str, Any]) -> bool:
    status = str(item.get("status") or "")
    if status not in ("WARN", "FAIL"):
        return False
    if item.get("fallback_resolved") and status == "WARN":
        return False
    return True


def _warn_only_source_error(source_id: str, source_kind: str, cfg: Dict[str, Any]) -> bool:
    values = {str(item) for item in cfg.get("warn_only_source_errors", []) or []}
    return source_id in values or source_kind in values


def _field_completeness(signals: List[RawSignal]) -> float:
    if not signals:
        return 0.0
    scores = []
    for signal in signals:
        required = _required_fields(signal)
        if not required:
            scores.append(1.0)
            continue
        present = sum(1 for field in required if _field_present(signal, field))
        base = present / len(required)
        filing = signal.payload.get("filing_evidence") if isinstance(signal.payload, dict) else None
        if isinstance(filing, dict) and filing.get("missing_fields"):
            base = min(base, max(0.0, 1.0 - len(filing.get("missing_fields", [])) * 0.15))
        scores.append(base)
    return sum(scores) / len(scores)


def _required_fields(signal: RawSignal) -> List[str]:
    if signal.source_type == "price":
        return ["title", "published_at", "payload.code", "payload.name", "payload.latest", "payload.pct_change"]
    if signal.source_type == "filing":
        return ["title", "published_at", "url", "payload.announcement_type", "payload.filing_evidence"]
    if signal.source_type in ("news", "manual", "social"):
        return ["title", "summary", "published_at", "url"]
    return ["title", "published_at"]


def _field_present(signal: RawSignal, field: str) -> bool:
    if field.startswith("payload."):
        value: Any = signal.payload
        for part in field.split(".")[1:]:
            if not isinstance(value, dict):
                return False
            value = value.get(part)
        return value not in (None, "")
    value = getattr(signal, field, None)
    return value not in (None, "")


def _observed_symbols(signals: Iterable[RawSignal]) -> set:
    observed = set()
    for signal in signals:
        payload = signal.payload or {}
        for key in ["code", "symbol"]:
            value = str(payload.get(key) or "").strip()
            if value:
                observed.add(value)
                observed.update(_symbol_aliases(value))
        filing = payload.get("filing_evidence")
        if isinstance(filing, dict):
            value = str(filing.get("symbol") or "").strip()
            if value:
                observed.add(value)
                observed.update(_symbol_aliases(value))
    return observed


def _symbol_aliases(value: str) -> set:
    text = str(value or "").strip().upper()
    aliases = {text} if text else set()
    if text.startswith(("SH", "SZ", "BJ")) and len(text) >= 8:
        aliases.add(text[2:])
    if "." in text:
        parts = text.split(".")
        if len(parts[0]) == 6:
            aliases.add(parts[0])
        if len(parts[-1]) == 6:
            aliases.add(parts[-1])
    if text.startswith(("0.", "1.")):
        aliases.add(text.split(".", 1)[1])
    return aliases


def _trust_score(source_id: str, source_kind: str, cfg: Dict[str, Any]) -> float:
    trust_scores = cfg.get("trust_scores", {}) or {}
    if source_id in trust_scores:
        return float(trust_scores[source_id])
    if source_kind in trust_scores:
        return float(trust_scores[source_kind])
    return float(DEFAULT_TRUST.get(source_kind, 0.60))


def _match_source_item(signal: RawSignal, items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    source = str(signal.source)
    for item in items:
        if item.get("source_id") == source or item.get("source_name") == source:
            return item
    for item in items:
        source_id = str(item.get("source_id", ""))
        if source_id.endswith(":{0}".format(source)):
            return item
    for item in items:
        if item.get("source_kind") == source:
            return item
    return None


def _signal_evidence_gaps(signal: RawSignal, item: Dict[str, Any]) -> List[str]:
    gaps = list(item.get("evidence_gaps", []) or [])
    filing = signal.payload.get("filing_evidence") if isinstance(signal.payload, dict) else None
    if isinstance(filing, dict):
        gaps.extend(str(field) for field in filing.get("missing_fields", []) or [])
        if filing.get("status") not in (None, "", "ok"):
            gaps.append("filing_status:{0}".format(filing.get("status")))
    if not signal.url and signal.source_type in ("filing", "news", "manual", "social"):
        gaps.append("url")
    return sorted(set(str(gap) for gap in gaps if str(gap).strip()))


def _strip_private(summary: Dict[str, Any]) -> Dict[str, Any]:
    clean = dict(summary)
    clean["sources"] = []
    for item in summary.get("sources", []):
        row = {k: v for k, v in item.items() if not k.startswith("_")}
        clean["sources"].append(row)
    return clean


def _render_markdown(summary: Dict[str, Any]) -> str:
    lines = ["# Data Source Health", ""]
    lines.append("- generated_at: {0}".format(summary.get("generated_at", "")))
    lines.append("- overall_status: {0}".format(summary.get("overall_status", "")))
    lines.append("- overall_score: {0}".format(summary.get("overall_score", 0)))
    lines.append("- coverage_gap_count: {0}".format(summary.get("coverage_gap_count", 0)))
    lines.append("- source_degraded_event_count: {0}".format(summary.get("source_degraded_event_count", 0)))
    lines.append("")
    degraded = summary.get("source_degraded_events", []) or []
    if degraded:
        lines.append("## Source Degraded Events")
        lines.append("")
        for event in degraded[:20]:
            lines.append("- [{0}] {1}: {2}; fallback={3}".format(
                event.get("severity", ""),
                event.get("source_id", ""),
                event.get("detail", ""),
                ",".join(event.get("fallback_candidates", []) or []) or "-",
            ))
        lines.append("")
    lines.append("## Sources")
    lines.append("")
    for item in summary.get("sources", []):
        lines.append("- [{0}] {1}: score={2}, latency_ms={3}, fields={4}, duplicates={5}, gaps={6}, reasons={7}".format(
            item.get("status", ""),
            item.get("source_id", ""),
            item.get("score", 0),
            item.get("latency_ms", 0),
            item.get("field_completeness", 0),
            item.get("duplicate_rate", 0),
            ",".join(item.get("coverage_gap_symbols", []) or []) or "-",
            ",".join(item.get("anomaly_reasons", []) or []) or "-",
        ))
    if not summary.get("sources"):
        lines.append("- no sources")
    return "\n".join(lines)


def _build_source_degraded_events(events: List[Dict[str, Any]], config: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        from .source_remediation import build_source_degraded_events
        return build_source_degraded_events(events, config)
    except Exception:
        return []
