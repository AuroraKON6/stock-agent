"""Freshness, provenance, and cross-source gates for market-price signals.

The controls here are internal research operating controls, not exchange or
regulatory deadlines. A blocked quote is retained for audit and human review;
it is only excluded from normal external-LLM analysis.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo  # type: ignore
except ImportError:  # Python 3.8 production compatibility
    ZoneInfo = None  # type: ignore

from .config import project_path


MARKET_DATA_GOVERNANCE_VERSION = "23.0"
ACTIVE_SESSIONS = {"opening_auction", "continuous_am", "continuous_pm", "closing_auction"}


def market_data_governance_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("market_data_governance", {}) if isinstance(config, dict) else {}
    approved = raw.get("approved_internal_research_providers", [])
    if not isinstance(approved, list):
        approved = []
    static_approved = sorted({str(item).strip() for item in approved if str(item).strip()})
    pilot = config.get("production_pilot", {}) if isinstance(config, dict) else {}
    use_registry = bool(raw.get("use_production_pilot_provider_registry", True)) and bool(pilot.get("enabled", False))
    registry_approved: List[str] = []
    if use_registry:
        # Import lazily so market-data collection remains usable when the
        # production-pilot layer is disabled or not configured.
        from .provider_registry import load_active_provider_ids

        registry_approved = load_active_provider_ids(config, required_scope="external_llm")
    if use_registry and bool(pilot.get("enforce", False)):
        effective_approved = registry_approved
        authorization_source = "evidence_approved_provider_registry"
    else:
        effective_approved = sorted(set(static_approved).union(registry_approved))
        authorization_source = "static_config_plus_provider_registry" if registry_approved else "static_config"
    calendar = raw.get("calendar", {}) if isinstance(raw.get("calendar"), dict) else {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "timezone": str(raw.get("timezone") or "Asia/Shanghai"),
        "weekdays": _int_list(calendar.get("weekdays", [0, 1, 2, 3, 4]), 0, 6),
        "holidays": _clean_list(calendar.get("holidays", [])),
        "max_live_age_seconds": max(1.0, float(raw.get("max_live_age_seconds", 120) or 120)),
        "max_closed_market_age_hours": max(1.0, float(raw.get("max_closed_market_age_hours", 36) or 36)),
        "require_quote_timestamp_during_session": bool(raw.get("require_quote_timestamp_during_session", True)),
        "block_cache_without_quote_timestamp": bool(raw.get("block_cache_without_quote_timestamp", True)),
        "allow_degraded_offhours_llm": bool(raw.get("allow_degraded_offhours_llm", False)),
        "require_authorized_provider_for_llm": bool(raw.get("require_authorized_provider_for_llm", True)),
        "approved_internal_research_providers": effective_approved,
        "static_approved_internal_research_providers": static_approved,
        "provider_registry_approved_providers": registry_approved,
        "provider_authorization_source": authorization_source,
        "require_consensus_for_llm": bool(raw.get("require_consensus_for_llm", False)),
        "min_consensus_sources": max(2, int(raw.get("min_consensus_sources", 2) or 2)),
        "max_cross_source_deviation_pct": max(0.01, float(raw.get("max_cross_source_deviation_pct", 0.5) or 0.5)),
        "report_dir": str(raw.get("report_dir") or "reports/market_data_governance"),
    }


def assess_market_signals(
    signals: Iterable[Any],
    config: Dict[str, Any],
    diagnostics: Optional[Dict[str, Any]] = None,
    *,
    now: Optional[datetime] = None,
) -> Tuple[List[Any], Dict[str, Any]]:
    cfg = market_data_governance_config(config)
    current = _market_now(now, cfg["timezone"])
    rows = list(signals)
    observations = _collect_observations(diagnostics or {}, rows)
    status_counts: Counter = Counter()
    reason_counts: Counter = Counter()
    price_count = 0
    blocked_symbols = []
    degraded_symbols = []

    for signal in rows:
        if str(getattr(signal, "source_type", "") or "") != "price":
            continue
        price_count += 1
        gate = _assess_one(signal, cfg, observations, current)
        payload = dict(getattr(signal, "payload", {}) or {})
        payload["market_data_governance"] = gate
        signal.payload = payload
        status_counts[gate["status"]] += 1
        for reason in gate["reason_codes"]:
            reason_counts[reason] += 1
        symbol = str(gate.get("symbol") or "")
        if gate["status"] == "BLOCKED" and symbol:
            blocked_symbols.append(symbol)
        elif gate["status"] == "DEGRADED" and symbol:
            degraded_symbols.append(symbol)

    summary = {
        "version": MARKET_DATA_GOVERNANCE_VERSION,
        "generated_at": current.isoformat(timespec="seconds"),
        "enabled": cfg["enabled"],
        "policy_type": "internal_research_operating_control_not_regulatory_deadline",
        "status": "DISABLED" if not cfg["enabled"] else (
            "FAIL" if status_counts["BLOCKED"] else ("WARN" if status_counts["DEGRADED"] else "OK")
        ),
        "price_signal_count": price_count,
        "pass_count": status_counts["PASS"],
        "degraded_count": status_counts["DEGRADED"],
        "blocked_count": status_counts["BLOCKED"],
        "llm_eligible_count": sum(
            bool(((getattr(signal, "payload", {}) or {}).get("market_data_governance", {}) or {}).get("llm_eligible"))
            for signal in rows
            if str(getattr(signal, "source_type", "") or "") == "price"
        ),
        "blocked_symbols": sorted(set(blocked_symbols)),
        "degraded_symbols": sorted(set(degraded_symbols)),
        "reason_counts": dict(sorted(reason_counts.items())),
        "approved_internal_research_providers": cfg["approved_internal_research_providers"],
        "provider_authorization_source": cfg["provider_authorization_source"],
        "provider_authorization_scope": "explicit internal research and external-model-use attestation",
        "consensus_required": cfg["require_consensus_for_llm"],
        "automatic_trade_or_approval_executed": False,
    }
    return rows, summary


def market_signal_llm_allowed(signal: Any) -> Tuple[bool, str]:
    if str(getattr(signal, "source_type", "") or "") != "price":
        return True, ""
    gate = ((getattr(signal, "payload", {}) or {}).get("market_data_governance", {}) or {})
    if not gate or not gate.get("enabled"):
        return True, ""
    if gate.get("llm_eligible"):
        return True, ""
    reasons = ",".join(str(item) for item in gate.get("reason_codes", []) if str(item))
    return False, reasons or "market_data_governance_blocked"


def write_market_data_governance_summary(summary: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, str]:
    cfg = market_data_governance_config(config)
    out_dir = project_path(cfg["report_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _compact_stamp(str(summary.get("generated_at") or datetime.now().astimezone().isoformat()))
    json_path = out_dir / "market_data_governance_{0}.json".format(stamp)
    md_path = out_dir / "market_data_governance_{0}.md".format(stamp)
    latest_json = out_dir / "latest.json"
    latest_md = out_dir / "latest.md"
    json_text = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    markdown = render_market_data_governance_markdown(summary)
    for path in (json_path, latest_json):
        path.write_text(json_text, encoding="utf-8")
    for path in (md_path, latest_md):
        path.write_text(markdown, encoding="utf-8-sig")
    return {
        "market_data_governance_path": str(md_path),
        "market_data_governance_json_path": str(json_path),
        "market_data_governance_latest_path": str(latest_md),
        "market_data_governance_latest_json_path": str(latest_json),
    }


def load_latest_market_data_governance(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = market_data_governance_config(config or {})
    path = project_path(str(Path(cfg["report_dir"]) / "latest.json"))
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def render_market_data_governance_markdown(summary: Dict[str, Any]) -> str:
    lines = [
        "# Market Data Governance",
        "",
        "- generated_at: {0}".format(summary.get("generated_at", "")),
        "- status: {0}".format(summary.get("status", "")),
        "- policy_type: {0}".format(summary.get("policy_type", "")),
        "- price / pass / degraded / blocked: {0} / {1} / {2} / {3}".format(
            summary.get("price_signal_count", 0), summary.get("pass_count", 0),
            summary.get("degraded_count", 0), summary.get("blocked_count", 0),
        ),
        "- llm_eligible: {0}".format(summary.get("llm_eligible_count", 0)),
        "- approved providers: {0}".format(", ".join(summary.get("approved_internal_research_providers", [])) or "none"),
        "- provider authorization source: {0}".format(summary.get("provider_authorization_source", "unknown")),
        "- automatic trade or approval: false",
        "",
        "## Reason Counts",
        "",
    ]
    reasons = summary.get("reason_counts", {}) or {}
    if reasons:
        for reason, count in sorted(reasons.items()):
            lines.append("- {0}: {1}".format(reason, count))
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def _assess_one(
    signal: Any,
    cfg: Dict[str, Any],
    observations: Dict[str, List[Dict[str, Any]]],
    current: datetime,
) -> Dict[str, Any]:
    payload = dict(getattr(signal, "payload", {}) or {})
    source_evidence = payload.get("source_evidence", {}) if isinstance(payload.get("source_evidence"), dict) else {}
    resilience = payload.get("market_resilience", {}) if isinstance(payload.get("market_resilience"), dict) else {}
    symbol = _symbol(payload.get("code") or payload.get("symbol") or payload.get("cache_symbol"))
    provider = str(resilience.get("provider") or source_evidence.get("provider") or getattr(signal, "source", "") or "").strip()
    origin_provider = str(
        source_evidence.get("cached_from") or payload.get("cached_from") or provider
    ).strip()
    is_cache = provider == "market_cache" or str(getattr(signal, "source", "")) == "market_cache"
    quote_value = _first_value(
        payload,
        ("quote_timestamp", "tick_time", "trade_timestamp", "timestamp", "updated_at"),
    )
    raw = payload.get("raw", {}) if isinstance(payload.get("raw"), dict) else {}
    if quote_value in (None, ""):
        quote_value = raw.get("f124")
    quote_at = _parse_datetime(quote_value, cfg["timezone"])
    observed_at = _parse_datetime(
        payload.get("observed_at") or resilience.get("fetched_at") or source_evidence.get("fetched_at"),
        cfg["timezone"],
    )
    cache_at = _parse_datetime(source_evidence.get("cached_at") or payload.get("cached_at"), cfg["timezone"])
    session = _market_session(current, cfg)
    quote_age = _age_seconds(quote_at, current)
    observed_age = _age_seconds(observed_at, current)
    approved = set(cfg["approved_internal_research_providers"])
    provider_authorized = bool(origin_provider and origin_provider in approved)
    consistency = _consistency(symbol, observations, cfg)
    reasons: List[str] = []
    status = "PASS"

    if not cfg["enabled"]:
        return {
            "version": MARKET_DATA_GOVERNANCE_VERSION,
            "enabled": False,
            "status": "PASS",
            "llm_eligible": True,
            "human_review_required": False,
            "symbol": symbol,
            "session": session,
            "reason_codes": [],
            "assessed_at": current.isoformat(timespec="seconds"),
        }

    if cfg["require_authorized_provider_for_llm"] and not provider_authorized:
        status = "BLOCKED"
        reasons.append("provider_not_internally_attested")

    if session in ACTIVE_SESSIONS:
        if quote_at is None and cfg["require_quote_timestamp_during_session"]:
            status = "BLOCKED"
            reasons.append("missing_quote_timestamp_during_session")
        elif _is_future_timestamp(quote_at, current):
            # A quote dated in the future is an untrustworthy upstream time;
            # the clamped non-negative age alone would silently pass it.
            status = "BLOCKED"
            reasons.append("future_quote_timestamp")
        elif quote_age is not None and quote_age > cfg["max_live_age_seconds"]:
            status = "BLOCKED"
            reasons.append("stale_live_quote")
    else:
        if quote_at is None:
            if is_cache and cfg["block_cache_without_quote_timestamp"]:
                status = "BLOCKED"
                reasons.append("cache_missing_quote_timestamp")
            else:
                status = "BLOCKED"
                reasons.append("missing_quote_timestamp")
        elif _is_future_timestamp(quote_at, current):
            status = "BLOCKED"
            reasons.append("future_quote_timestamp")
        elif quote_age is not None and quote_age > cfg["max_closed_market_age_hours"] * 3600:
            status = "BLOCKED"
            reasons.append("quote_older_than_closed_market_limit")
        elif status != "BLOCKED":
            status = "DEGRADED"
            reasons.append("off_hours_snapshot_not_live")

    if is_cache and status != "BLOCKED":
        status = "DEGRADED"
        reasons.append("cached_quote")

    if consistency["status"] == "CONFLICT":
        status = "BLOCKED"
        reasons.append("cross_source_price_conflict")
    elif cfg["require_consensus_for_llm"] and consistency["source_count"] < cfg["min_consensus_sources"]:
        status = "BLOCKED"
        reasons.append("insufficient_cross_source_consensus")

    llm_eligible = status == "PASS" or (
        status == "DEGRADED" and session not in ACTIVE_SESSIONS and cfg["allow_degraded_offhours_llm"]
    )
    return {
        "version": MARKET_DATA_GOVERNANCE_VERSION,
        "enabled": True,
        "policy_type": "internal_research_operating_control_not_regulatory_deadline",
        "status": status,
        "llm_eligible": bool(llm_eligible),
        "human_review_required": not llm_eligible,
        "symbol": symbol,
        "provider": provider,
        "origin_provider": origin_provider,
        "provider_internally_attested": provider_authorized,
        "provider_attestation_scope": "internal research and external-model use",
        "is_cache": is_cache,
        "session": session,
        "quote_timestamp": quote_at.isoformat(timespec="seconds") if quote_at else "",
        "observed_at": observed_at.isoformat(timespec="seconds") if observed_at else "",
        "cache_timestamp": cache_at.isoformat(timespec="seconds") if cache_at else "",
        "quote_age_seconds": round(quote_age, 3) if quote_age is not None else None,
        "observed_age_seconds": round(observed_age, 3) if observed_age is not None else None,
        "consistency": consistency,
        "reason_codes": _unique(reasons),
        "assessed_at": current.isoformat(timespec="seconds"),
        "automatic_trade_or_approval_executed": False,
    }


def _collect_observations(diagnostics: Dict[str, Any], signals: Iterable[Any]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    raw = diagnostics.get("observations", []) if isinstance(diagnostics, dict) else []
    if isinstance(raw, dict):
        flattened = []
        for symbol, items in raw.items():
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict):
                    flattened.append(dict(item, symbol=item.get("symbol") or symbol))
        raw = flattened
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        symbol = _symbol(item.get("symbol") or item.get("code"))
        latest = _number(item.get("latest"))
        provider = str(item.get("provider") or "").strip()
        if symbol and provider and latest is not None:
            grouped[symbol].append({"provider": provider, "latest": latest, "quote_timestamp": item.get("quote_timestamp")})
    for signal in signals:
        if str(getattr(signal, "source_type", "") or "") != "price":
            continue
        payload = getattr(signal, "payload", {}) or {}
        symbol = _symbol(payload.get("code") or payload.get("symbol") or payload.get("cache_symbol"))
        latest = _number(payload.get("latest"))
        provider = str(getattr(signal, "source", "") or "")
        if symbol and provider and latest is not None:
            grouped[symbol].append({"provider": provider, "latest": latest, "quote_timestamp": payload.get("quote_timestamp")})
    deduped: Dict[str, List[Dict[str, Any]]] = {}
    for symbol, items in grouped.items():
        by_provider = {}
        for item in items:
            by_provider[str(item["provider"])] = item
        deduped[symbol] = list(by_provider.values())
    return deduped


def _consistency(symbol: str, observations: Dict[str, List[Dict[str, Any]]], cfg: Dict[str, Any]) -> Dict[str, Any]:
    items = observations.get(symbol, []) if symbol else []
    prices = [float(item["latest"]) for item in items if _number(item.get("latest")) is not None]
    providers = sorted({str(item.get("provider") or "") for item in items if str(item.get("provider") or "")})
    if len(prices) < 2:
        return {
            "status": "UNVERIFIED",
            "source_count": len(providers),
            "providers": providers,
            "max_deviation_pct": None,
        }
    median = statistics.median(prices)
    deviation = ((max(prices) - min(prices)) / abs(median) * 100.0) if median else float("inf")
    return {
        "status": "CONSISTENT" if deviation <= cfg["max_cross_source_deviation_pct"] else "CONFLICT",
        "source_count": len(providers),
        "providers": providers,
        "max_deviation_pct": round(deviation, 6),
        "threshold_pct": cfg["max_cross_source_deviation_pct"],
    }


def _market_session(current: datetime, cfg: Dict[str, Any]) -> str:
    if current.weekday() not in set(cfg["weekdays"]) or current.date().isoformat() in set(cfg["holidays"]):
        return "closed_market"
    value = current.timetz().replace(tzinfo=None)
    if value < time(9, 15):
        return "pre_market"
    if value < time(9, 25):
        return "opening_auction"
    if value < time(9, 30):
        return "pre_open_pause"
    if value < time(11, 30):
        return "continuous_am"
    if value < time(13, 0):
        return "lunch_break"
    if value < time(14, 57):
        return "continuous_pm"
    if value < time(15, 0):
        return "closing_auction"
    return "post_market"


def _market_now(value: Optional[datetime], timezone_name: str) -> datetime:
    zone = _timezone(timezone_name)
    current = value or datetime.now(zone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=zone)
    return current.astimezone(zone)


def _parse_datetime(value: Any, timezone_name: str) -> Optional[datetime]:
    if value in (None, "", "-"):
        return None
    zone = _timezone(timezone_name)
    if isinstance(value, (int, float)) or str(value).strip().replace(".", "", 1).isdigit():
        try:
            number = float(value)
            if number > 100000000000:
                number /= 1000.0
            return datetime.fromtimestamp(number, tz=timezone.utc).astimezone(zone)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def _age_seconds(value: Optional[datetime], current: datetime) -> Optional[float]:
    if value is None:
        return None
    return max(0.0, (current - value).total_seconds())


FUTURE_TIMESTAMP_TOLERANCE_SECONDS = 120


def _is_future_timestamp(value: Optional[datetime], current: datetime) -> bool:
    """True when a parsed quote time lies beyond a small clock-skew tolerance."""

    if value is None:
        return False
    return value > current + timedelta(seconds=FUTURE_TIMESTAMP_TOLERANCE_SECONDS)


def _timezone(timezone_name: str):
    if timezone_name in {"Asia/Shanghai", "PRC", "UTC+08:00", "+08:00"}:
        return timezone(timedelta(hours=8))
    if ZoneInfo is not None:
        try:
            return ZoneInfo(timezone_name)
        except Exception:
            pass
    return timezone.utc


def _first_value(payload: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if payload.get(key) not in (None, ""):
            return payload[key]
    return None


def _number(value: Any) -> Optional[float]:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text.startswith(("SH", "SZ", "BJ")) and len(text) >= 8:
        text = text[2:]
    if "." in text:
        left, right = text.split(".", 1)
        if left.isdigit():
            text = left
        elif right.isdigit():
            text = right
    return text


def _int_list(values: Any, minimum: int, maximum: int) -> List[int]:
    result = []
    for value in values if isinstance(values, list) else []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if minimum <= number <= maximum and number not in result:
            result.append(number)
    return result or list(range(minimum, maximum + 1))


def _clean_list(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    return [str(item).strip() for item in values if str(item).strip()]


def _unique(values: Iterable[str]) -> List[str]:
    result = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _compact_stamp(value: str) -> str:
    return str(value).replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
