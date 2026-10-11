from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .config import project_path
from .models import RawSignal, now_iso


Collector = Callable[[Dict[str, Any]], List[RawSignal]]
PRICE_PROVIDERS = ["eastmoney", "akshare", "pysnowball", "alltick", "free_quote"]


def collect_market_quotes_with_resilience(
    config: Dict[str, Any],
    collectors: Optional[Dict[str, Collector]] = None,
) -> Tuple[List[RawSignal], List[Dict[str, Any]], Dict[str, Any]]:
    cfg = config.get("market_resilience", {}) or {}
    sources = config.get("sources", {}) or {}
    if not cfg.get("enabled", True):
        return [], [], _empty_diagnostics(enabled=False)

    collectors = collectors or {}
    expected_symbols = _expected_symbols(config)
    priority = [str(item) for item in cfg.get("fallback_priority", ["eastmoney", "akshare", "pysnowball", "alltick", "free_quote", "cache"])]
    allow_disabled = bool(cfg.get("allow_disabled_fallbacks", False))
    covered = set()
    output: List[RawSignal] = []
    events: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    fallback_used = False
    cache_used = False
    observations: List[Dict[str, Any]] = []
    verification_providers = {
        str(item).strip()
        for item in cfg.get("verification_providers", [])
        if str(item).strip()
    }

    for provider_index, provider in enumerate(priority):
        if provider == "cache":
            if _missing_symbols(expected_symbols, covered):
                cached = load_quote_cache(_missing_symbols(expected_symbols, covered), ttl_minutes=float(cfg.get("cache_ttl_minutes", 60)))
                if cached:
                    cache_used = True
                    fallback_used = True
                    output.extend(cached)
                    covered.update(_covered_symbols(cached))
                    events.append(_event("market_cache", "market_cache", True, True, 0, cached, expected_symbols))
                    observations.extend(_compact_observations(cached, "market_cache"))
            continue
        if provider not in PRICE_PROVIDERS:
            continue
        source_cfg = dict(sources.get(provider, {}) or {})
        if not source_cfg:
            source_cfg = {"enabled": allow_disabled, "symbols": expected_symbols, "source_type": "price"}
        if expected_symbols and not source_cfg.get("symbols"):
            source_cfg["symbols"] = expected_symbols
        if not source_cfg.get("enabled", False):
            if not allow_disabled:
                events.append(_event(provider, provider, False, False, 0, [], _symbols(source_cfg) or expected_symbols))
                continue
            source_cfg["enabled"] = True
        collector = collectors.get(provider)
        if collector is None:
            events.append(_event(provider, provider, source_cfg.get("enabled", False), False, 0, [], _symbols(source_cfg) or expected_symbols))
            continue

        started = time.perf_counter()
        try:
            signals = collector(source_cfg)
        except Exception as exc:
            signals = [_source_error(provider, str(exc))]
        latency_ms = int((time.perf_counter() - started) * 1000)
        event = _event(provider, provider, True, True, latency_ms, signals, _symbols(source_cfg) or expected_symbols)
        events.append(event)

        errors = [s for s in signals if s.source_type == "source_error"]
        normal = [s for s in signals if s.source_type != "source_error"]
        observations.extend(_compact_observations(normal, provider))
        if errors:
            failures.extend({
                "provider": provider,
                "error": s.summary or s.title,
                "diagnosis": diagnose_network_error(s.summary or s.title),
            } for s in errors)
        useful = _filter_missing(normal, expected_symbols, covered)
        if useful:
            if output:
                fallback_used = True
            output.extend(_annotate_resilience(useful, provider, fallback_used=fallback_used))
            covered.update(_covered_symbols(useful))
            write_quote_cache(useful)
        remaining_verification = any(
            candidate in verification_providers
            for candidate in priority[provider_index + 1:]
            if candidate != "cache"
        )
        if expected_symbols and not _missing_symbols(expected_symbols, covered) and not remaining_verification:
            break

    missing = _missing_symbols(expected_symbols, covered)
    if not output and missing:
        cached = load_quote_cache(missing, ttl_minutes=float(cfg.get("cache_ttl_minutes", 60)))
        if cached:
            cache_used = True
            fallback_used = True
            output.extend(cached)
            covered.update(_covered_symbols(cached))
            events.append(_event("market_cache", "market_cache", True, True, 0, cached, expected_symbols))
            observations.extend(_compact_observations(cached, "market_cache"))
            missing = _missing_symbols(expected_symbols, covered)

    resolved = not missing and bool(output)
    if resolved:
        for event in events:
            if _has_source_error(event.get("signals", [])):
                event["fallback_resolved"] = True
    diagnostics = {
        "enabled": True,
        "status": "OK" if not failures and not missing else ("WARN" if output else "FAIL"),
        "expected_symbols": expected_symbols,
        "covered_symbols": [symbol for symbol in expected_symbols if symbol in covered] or sorted(covered),
        "missing_symbols": missing,
        "fallback_priority": priority,
        "fallback_used": bool(fallback_used or (failures and output)),
        "cache_used": cache_used,
        "failures": failures,
        "repair_guide": _repair_guide(failures, missing),
        "providers_attempted": [event["source_id"] for event in events if event.get("attempted")],
        "verification_providers": sorted(verification_providers),
        "observations": observations,
    }
    return output, events, diagnostics


def write_quote_cache(signals: Iterable[RawSignal]) -> str:
    records = []
    for signal in signals:
        if signal.source_type != "price":
            continue
        code = _symbol_key((signal.payload or {}).get("code") or (signal.payload or {}).get("symbol") or "")
        if not code:
            continue
        if not _quote_complete(signal):
            continue
        record = asdict(signal)
        record["payload"] = dict(record.get("payload") or {})
        record["payload"]["cache_symbol"] = code
        record["payload"]["cached_from"] = signal.source
        records.append(record)
    if not records:
        return ""
    cache_dir = project_path("data/cache/market_quotes")
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "latest.json"
    path.write_text(json.dumps({"generated_at": now_iso(), "quotes": records}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return str(path)


def load_quote_cache(symbols: Iterable[str], ttl_minutes: float = 60) -> List[RawSignal]:
    path = project_path("data/cache/market_quotes/latest.json")
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    generated_at = str(data.get("generated_at") or "")
    age_seconds = _age_seconds(generated_at)
    if age_seconds is None or age_seconds > ttl_minutes * 60:
        return []
    wanted = {_symbol_key(symbol) for symbol in symbols if _symbol_key(symbol)}
    signals = []
    for row in data.get("quotes", []) or []:
        payload = dict(row.get("payload") or {})
        code = _symbol_key(payload.get("cache_symbol") or payload.get("code") or payload.get("symbol") or "")
        if wanted and code not in wanted:
            continue
        payload["code"] = code
        payload["source_evidence"] = {
            "provider": "market_cache",
            "cached_from": payload.get("cached_from", row.get("source", "")),
            "cached_at": generated_at,
            "age_seconds": age_seconds,
            "status": "cached",
        }
        signals.append(RawSignal(
            source="market_cache",
            source_type="price",
            title=row.get("title") or "{0} cached quote".format(code),
            summary=row.get("summary") or "cached quote",
            url=row.get("url") or "",
            published_at=str(payload.get("quote_timestamp") or row.get("published_at") or generated_at),
            payload=payload,
        ))
    return signals


def seed_quote_cache_from_reports(
    config: Dict[str, Any],
    symbols: Iterable[str] | None = None,
    report_dir: str = "reports/json",
) -> Dict[str, Any]:
    wanted = _unique(_symbol_key(item) for item in (symbols or _expected_symbols(config)))
    by_symbol: Dict[str, RawSignal] = {}
    source_reports: Dict[str, str] = {}
    base_dir = project_path(report_dir)
    files = sorted(base_dir.glob("*.json"), reverse=True) if base_dir.exists() else []
    for path in files:
        if wanted and len(by_symbol) >= len(wanted):
            break
        for signal in _price_signals_from_report(path):
            if _is_cache_signal(signal):
                continue
            code = _symbol_key((signal.payload or {}).get("code") or (signal.payload or {}).get("symbol") or "")
            if not code:
                continue
            if wanted and code not in wanted:
                continue
            if code in by_symbol:
                continue
            if not _quote_complete(signal):
                continue
            payload = dict(signal.payload or {})
            payload["cache_seed_report"] = str(path)
            payload["cache_seeded_at"] = now_iso()
            signal.payload = payload
            by_symbol[code] = signal
            source_reports[code] = str(path)
    cache_path = write_quote_cache(by_symbol.values())
    missing = [symbol for symbol in wanted if symbol not in by_symbol]
    return {
        "seeded_count": len(by_symbol),
        "seeded_symbols": [symbol for symbol in wanted if symbol in by_symbol] if wanted else sorted(by_symbol),
        "missing_symbols": missing,
        "cache_path": cache_path,
        "source_reports": source_reports,
    }


def diagnose_network_error(error: Any) -> str:
    text = str(error or "").lower()
    if "10013" in text or "permission" in text or "access denied" in text:
        return "socket_permission_denied"
    if "timed out" in text or "timeout" in text:
        return "network_timeout"
    if "name or service" in text or "dns" in text or "getaddrinfo" in text:
        return "dns_resolution_failed"
    if "ssl" in text or "certificate" in text:
        return "tls_or_certificate_error"
    if "token" in text or "api key" in text or "unauthorized" in text or "401" in text or "403" in text or "forbidden" in text:
        return "credential_or_token_error"
    return "external_dependency_error"


def _price_signals_from_report(path: Path) -> List[RawSignal]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return []
    result: List[RawSignal] = []
    for row in data.get("signals", []) or []:
        if not isinstance(row, dict) or row.get("source_type") != "price":
            continue
        result.append(RawSignal(
            source=str(row.get("source") or "report_seed"),
            source_type="price",
            title=str(row.get("title") or ""),
            summary=str(row.get("summary") or ""),
            url=str(row.get("url") or ""),
            published_at=str(row.get("published_at") or now_iso()),
            payload=dict(row.get("payload") or {}),
        ))
    return result


def _quote_complete(signal: RawSignal) -> bool:
    payload = signal.payload or {}
    code = _symbol_key(payload.get("code") or payload.get("symbol") or "")
    return bool(code and payload.get("latest") not in (None, "") and payload.get("pct_change") not in (None, ""))


def _is_cache_signal(signal: RawSignal) -> bool:
    payload = signal.payload or {}
    evidence = payload.get("source_evidence") if isinstance(payload.get("source_evidence"), dict) else {}
    return signal.source == "market_cache" or evidence.get("provider") == "market_cache"


def network_repair_guide(diagnosis: str) -> List[str]:
    if diagnosis == "socket_permission_denied":
        return [
            "Allow python.exe outbound HTTPS in Windows Firewall or security software.",
            "Confirm the current terminal/session is allowed to open network sockets.",
            "Retry from the scheduled task account after updating firewall rules.",
        ]
    if diagnosis == "network_timeout":
        return [
            "Reduce provider timeout or enable another fallback provider.",
            "Check proxy/VPN stability and provider service status.",
        ]
    if diagnosis == "credential_or_token_error":
        return [
            "Check LLM_API_KEY/DEEPSEEK_API_KEY or provider token environment variables.",
            "Verify the configured base_url and model are accepted by the provider.",
        ]
    return [
        "Check network connectivity, proxy, DNS, and provider status.",
        "Use cached quotes or enable another market data fallback provider.",
    ]


def _empty_diagnostics(enabled: bool) -> Dict[str, Any]:
    return {
        "enabled": enabled,
        "status": "DISABLED" if not enabled else "OK",
        "expected_symbols": [],
        "covered_symbols": [],
        "missing_symbols": [],
        "fallback_used": False,
        "cache_used": False,
        "failures": [],
        "repair_guide": [],
        "providers_attempted": [],
    }


def _expected_symbols(config: Dict[str, Any]) -> List[str]:
    cfg = config.get("market_resilience", {}) or {}
    explicit = [str(item).strip() for item in cfg.get("symbols", []) if str(item).strip()]
    if explicit:
        return _unique(_symbol_key(item) for item in explicit)
    symbols: List[str] = []
    sources = config.get("sources", {}) or {}
    for provider in PRICE_PROVIDERS:
        symbols.extend(_symbols(sources.get(provider, {}) or {}))
    return _unique(_symbol_key(item) for item in symbols)


def _symbols(source_cfg: Dict[str, Any]) -> List[str]:
    return [str(item).strip() for item in source_cfg.get("symbols", []) if str(item).strip()]


def _event(source_id: str, source_kind: str, enabled: bool, attempted: bool, latency_ms: int, signals: List[RawSignal], expected_symbols: List[str]) -> Dict[str, Any]:
    return {
        "source_id": source_id,
        "source_kind": source_kind,
        "source_name": source_id,
        "enabled": bool(enabled),
        "attempted": bool(attempted),
        "latency_ms": int(latency_ms),
        "signals": signals,
        "expected_symbols": list(expected_symbols),
        "exception": "",
        "fallback_resolved": False,
    }


def _source_error(provider: str, message: str) -> RawSignal:
    return RawSignal(
        source=provider,
        source_type="source_error",
        title="{0} quote fetch failed".format(provider),
        summary=str(message),
        published_at=now_iso(),
    )


def _filter_missing(signals: List[RawSignal], expected: List[str], covered: set) -> List[RawSignal]:
    if not expected:
        return signals
    wanted = set(expected) - set(covered)
    result = []
    for signal in signals:
        code = _symbol_key((signal.payload or {}).get("code") or (signal.payload or {}).get("symbol") or "")
        if not code and signal.source_type == "price" and len(wanted) == 1:
            code = next(iter(wanted))
            payload = signal.payload or {}
            payload["code"] = code
            signal.payload = payload
        if code in wanted:
            result.append(signal)
    return result


def _annotate_resilience(signals: List[RawSignal], provider: str, fallback_used: bool) -> List[RawSignal]:
    for signal in signals:
        payload = signal.payload or {}
        fetched_at = now_iso()
        payload["market_resilience"] = {
            "provider": provider,
            "fallback_used": bool(fallback_used),
            "fetched_at": fetched_at,
        }
        payload.setdefault("observed_at", fetched_at)
        signal.payload = payload
    return signals


def _compact_observations(signals: Iterable[RawSignal], provider: str) -> List[Dict[str, Any]]:
    rows = []
    for signal in signals:
        if signal.source_type != "price":
            continue
        payload = signal.payload or {}
        code = _symbol_key(payload.get("code") or payload.get("symbol") or payload.get("cache_symbol") or "")
        latest = payload.get("latest")
        if not code or latest in (None, ""):
            continue
        evidence = payload.get("source_evidence", {}) if isinstance(payload.get("source_evidence"), dict) else {}
        rows.append({
            "provider": provider,
            "origin_provider": evidence.get("cached_from") or payload.get("cached_from") or provider,
            "symbol": code,
            "latest": latest,
            "quote_timestamp": payload.get("quote_timestamp") or payload.get("tick_time") or "",
            "observed_at": payload.get("observed_at") or evidence.get("fetched_at") or "",
        })
    return rows


def _covered_symbols(signals: Iterable[RawSignal]) -> set:
    covered = set()
    for signal in signals:
        if signal.source_type != "price":
            continue
        payload = signal.payload or {}
        code = _symbol_key(payload.get("code") or payload.get("symbol") or payload.get("cache_symbol") or "")
        if code:
            covered.add(code)
    return covered


def _missing_symbols(expected: List[str], covered: set) -> List[str]:
    return [symbol for symbol in expected if symbol not in covered]


def _has_source_error(signals: Iterable[RawSignal]) -> bool:
    return any(signal.source_type == "source_error" for signal in signals)


def _repair_guide(failures: List[Dict[str, Any]], missing: List[str]) -> List[str]:
    guides: List[str] = []
    for failure in failures:
        for item in network_repair_guide(str(failure.get("diagnosis", ""))):
            if item not in guides:
                guides.append(item)
    if missing:
        guides.append("Enable another fallback quote provider or refresh cache for: {0}".format(",".join(missing)))
    return guides


def _age_seconds(value: str) -> Optional[float]:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())


def _symbol_key(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if text.startswith(("SH", "SZ", "BJ")) and len(text) >= 8:
        return text[2:]
    if "." in text:
        parts = text.split(".")
        if len(parts[0]) == 6:
            return parts[0]
        if len(parts[-1]) == 6:
            return parts[-1]
    if text.startswith(("0.", "1.")) and len(text) >= 8:
        return text.split(".", 1)[1]
    return text


def _unique(values: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
