from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

from ..models import RawSignal, now_iso


DEFAULT_TOKEN_ENV = "XUEQIU_TOKEN"
FALLBACK_TOKEN_ENV = "PYSNOWBALL_TOKEN"


def collect_pysnowball(source_config: Dict[str, Any]) -> List[RawSignal]:
    if not source_config.get("enabled", False):
        return []

    symbols = [str(item).strip() for item in source_config.get("symbols", []) if str(item).strip()]
    if not symbols:
        return []

    token_env = str(source_config.get("token_env") or DEFAULT_TOKEN_ENV)
    token = os.environ.get(token_env, "").strip()
    if not token and token_env == DEFAULT_TOKEN_ENV:
        token = os.environ.get(FALLBACK_TOKEN_ENV, "").strip()
    if not token:
        return [_source_error(
            "PySnowball token missing",
            "Set {0} or {1} environment variable.".format(token_env, FALLBACK_TOKEN_ENV),
        )]

    max_symbols = max(1, int(source_config.get("max_symbols_per_run", 10)))
    xq_symbols = _unique_keep_order(_to_xueqiu_symbol(symbol) for symbol in symbols)[:max_symbols]

    try:
        rows = _fetch_quote_rows(
            xq_symbols,
            token=token,
            timeout=int(source_config.get("timeout", 15)),
        )
    except ImportError:
        return [_source_error("PySnowball not installed", "Run pip install pysnowball or install optional requirements.")]
    except Exception as exc:
        return [_source_error("PySnowball quote fetch failed", str(exc))]

    return [_row_to_signal(row, source_config) for row in rows if isinstance(row, dict)]


def _fetch_quote_rows(symbols: Iterable[str], *, token: str, timeout: int) -> List[Dict[str, Any]]:
    del timeout  # pysnowball does not expose a per-request timeout in its public API.
    import pysnowball as ball  # type: ignore

    ball.set_token(token)
    joined = ",".join(symbols)
    if hasattr(ball, "quotec"):
        return _extract_rows(ball.quotec(joined))
    if hasattr(ball, "quote_detail"):
        rows = []
        for symbol in symbols:
            rows.extend(_extract_rows(ball.quote_detail(symbol)))
        return rows
    raise RuntimeError("Installed pysnowball has no quotec or quote_detail API")


def _extract_rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        if isinstance(data.get("items"), list):
            return [row for row in data["items"] if isinstance(row, dict)]
        if isinstance(data.get("list"), list):
            return [row for row in data["list"] if isinstance(row, dict)]
        if data:
            return [data]
    if any(key in payload for key in ("symbol", "current", "percent", "name")):
        return [payload]
    return []


def _row_to_signal(row: Dict[str, Any], source_config: Dict[str, Any]) -> RawSignal:
    code = str(row.get("symbol") or row.get("code") or "").strip()
    name_map = source_config.get("symbol_names", {}) if isinstance(source_config.get("symbol_names"), dict) else {}
    name = str(row.get("name") or row.get("stock_name") or name_map.get(code) or code or "unknown").strip()
    latest = _num(_first(row, ["current", "latest", "last", "price"], None))
    pct = _num(_first(row, ["percent", "pct_change", "change_percent"], None))
    change = _num(_first(row, ["chg", "change", "price_change"], None))
    amount = _num(_first(row, ["amount", "turnover"], None))
    volume = _num(_first(row, ["volume", "vol"], None))
    timestamp = _first(row, ["timestamp", "time", "updated_at"], None)
    quote_timestamp = _timestamp_to_iso(timestamp)
    observed_at = now_iso()
    published_at = quote_timestamp or observed_at

    summary = "最新价={0}; 涨跌幅={1}%; 涨跌额={2}; 成交额={3}; 成交量={4}".format(
        _display(latest),
        _display(pct),
        _display(change),
        _display(amount),
        _display(volume),
    )
    evidence = _build_source_evidence(
        provider="pysnowball",
        symbol=code,
        row=row,
        required_fields=["current", "percent"],
        optional_fields=["chg", "amount", "volume", "timestamp"],
    )
    return RawSignal(
        source="pysnowball",
        source_type=str(source_config.get("source_type") or "price"),
        title="{0}({1}) 雪球行情快照".format(name, code) if code else "雪球行情快照",
        summary=summary,
        published_at=published_at,
        payload={
            "code": code,
            "name": name,
            "latest": latest,
            "pct_change": pct,
            "price_change": change,
            "amount": amount,
            "volume": volume,
            "quote_timestamp": quote_timestamp,
            "observed_at": observed_at,
            "source_evidence": evidence,
            "raw": row,
        },
    )


def _build_source_evidence(
    *,
    provider: str,
    symbol: str,
    row: Dict[str, Any],
    required_fields: List[str],
    optional_fields: List[str],
) -> Dict[str, Any]:
    fields = required_fields + optional_fields
    present = [field for field in fields if row.get(field) not in (None, "")]
    missing_required = [field for field in required_fields if row.get(field) in (None, "")]
    status = "ok" if not missing_required else "partial"
    return {
        "provider": provider,
        "symbol": symbol,
        "status": status,
        "fetched_at": now_iso(),
        "fields_present": present,
        "missing_fields": missing_required,
        "notes": [] if status == "ok" else ["关键行情字段缺失，LLM 需要降低置信度并要求人工复核。"],
    }


def _source_error(title: str, summary: str) -> RawSignal:
    return RawSignal(
        source="pysnowball",
        source_type="source_error",
        title=title,
        summary=summary,
        published_at=now_iso(),
        payload={
            "source_evidence": {
                "provider": "pysnowball",
                "status": "error",
                "fetched_at": now_iso(),
                "error": summary,
            }
        },
    )


def _to_xueqiu_symbol(symbol: str) -> str:
    value = symbol.strip().upper()
    if value.startswith(("SH", "SZ", "BJ")):
        return value
    if "." in value:
        code, exchange = value.split(".", 1)
        exchange = exchange.upper()
        if exchange in ("SH", "SS"):
            return "SH{0}".format(code)
        if exchange == "SZ":
            return "SZ{0}".format(code)
        if exchange == "BJ":
            return "BJ{0}".format(code)
    if value.startswith(("6", "9")):
        return "SH{0}".format(value)
    if value.startswith(("0", "2", "3")):
        return "SZ{0}".format(value)
    if value.startswith(("4", "8")):
        return "BJ{0}".format(value)
    return value


def _first(row: Dict[str, Any], keys: List[str], default: Any) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default


def _num(value: Any) -> Any:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if number.is_integer():
        return int(number)
    return number


def _timestamp_to_iso(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number > 100000000000:
        number = number / 1000.0
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return ""


def _display(value: Any) -> str:
    if value is None:
        return "NA"
    return str(value)


def _unique_keep_order(values: Iterable[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
