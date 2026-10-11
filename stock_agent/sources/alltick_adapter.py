from __future__ import annotations

import json
import os
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

from ..models import RawSignal, now_iso


DEFAULT_BASE_URL = "https://quote.alltick.io/quote-stock-b-api"
DEFAULT_TOKEN_ENV = "ALLTICK_TOKEN"


def collect_alltick(source_config: Dict[str, Any]) -> List[RawSignal]:
    if not source_config.get("enabled", False):
        return []

    symbols = [str(item).strip() for item in source_config.get("symbols", []) if str(item).strip()]
    if not symbols:
        return []

    token_env = str(source_config.get("token_env") or DEFAULT_TOKEN_ENV)
    token = os.environ.get(token_env, "").strip()
    if not token:
        return [_source_error("AllTick token missing", "Set {0} environment variable.".format(token_env))]

    max_symbols = max(1, int(source_config.get("max_symbols_per_run", 5)))
    codes = _unique_keep_order(_to_alltick_code(symbol) for symbol in symbols)[:max_symbols]

    try:
        rows = _fetch_ticks(
            codes,
            token=token,
            base_url=str(source_config.get("base_url") or DEFAULT_BASE_URL),
            timeout=int(source_config.get("timeout", 15)),
        )
    except Exception as exc:
        return [_source_error("AllTick quote fetch failed", str(exc))]

    return [_row_to_signal(row, source_config) for row in rows if isinstance(row, dict)]


def _fetch_ticks(symbols: Iterable[str], *, token: str, base_url: str, timeout: int) -> List[Dict[str, Any]]:
    endpoint = _endpoint(base_url)
    query_payload = {
        "trace": "stock_agent_{0}".format(int(time.time() * 1000)),
        "data": {"symbol_list": [{"code": symbol} for symbol in symbols]},
    }
    query = urllib.parse.urlencode(
        {
            "token": token,
            "query": json.dumps(query_payload, ensure_ascii=False, separators=(",", ":")),
        }
    )
    request = urllib.request.Request(
        endpoint + "?" + query,
        headers={
            "User-Agent": "Mozilla/5.0 stock-agent/0.1",
            "Accept": "application/json",
        },
    )
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        data = json.loads(response.read().decode("utf-8", errors="replace"))

    ret = data.get("ret")
    if ret not in (None, 0, 200):
        raise RuntimeError("ret={0}; msg={1}".format(ret, data.get("msg") or data.get("message") or ""))

    rows = data.get("data", {}).get("tick_list", [])
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _row_to_signal(row: Dict[str, Any], source_config: Dict[str, Any]) -> RawSignal:
    code = str(row.get("code") or row.get("symbol") or "").strip()
    name_map = source_config.get("symbol_names", {}) if isinstance(source_config.get("symbol_names"), dict) else {}
    name = str(name_map.get(code) or code or "unknown").strip()
    latest = _num(row.get("price") or row.get("latest") or row.get("last"))
    volume = _num(row.get("volume") or row.get("vol"))
    turnover = _num(row.get("turnover") or row.get("amount"))
    trade_direction = row.get("trade_direction")
    tick_time = row.get("tick_time") or row.get("time")
    quote_timestamp = _timestamp_to_iso(tick_time)
    observed_at = now_iso()

    summary = "latest={0}; volume={1}; turnover={2}; trade_direction={3}".format(
        _display(latest),
        _display(volume),
        _display(turnover),
        _display(trade_direction),
    )
    return RawSignal(
        source="alltick",
        source_type=str(source_config.get("source_type") or "price"),
        title="{0}({1}) AllTick quote snapshot".format(name, code) if code else "AllTick quote snapshot",
        summary=summary,
        published_at=quote_timestamp or observed_at,
        payload={
            "code": code,
            "name": name,
            "latest": latest,
            "pct_change": None,
            "main_net_inflow": None,
            "volume": volume,
            "turnover": turnover,
            "trade_direction": trade_direction,
            "tick_time": tick_time,
            "quote_timestamp": quote_timestamp,
            "observed_at": observed_at,
            "raw": row,
        },
    )


def _source_error(title: str, summary: str) -> RawSignal:
    return RawSignal(
        source="alltick",
        source_type="source_error",
        title=title,
        summary=summary,
        published_at=now_iso(),
    )


def _to_alltick_code(symbol: str) -> str:
    value = symbol.strip().upper()
    if "." in value:
        code, exchange = value.rsplit(".", 1)
        return "{0}.{1}".format(code, exchange)
    if value.startswith(("6", "9")):
        return "{0}.SH".format(value)
    if value.startswith(("0", "2", "3")):
        return "{0}.SZ".format(value)
    if value.startswith(("4", "8")):
        return "{0}.BJ".format(value)
    return value


def _endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/trade-tick"):
        return base
    return base + "/trade-tick"


def _unique_keep_order(values: Iterable[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


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


def _display(value: Any) -> str:
    if value is None:
        return "NA"
    return str(value)


def _timestamp_to_iso(value: Any) -> str:
    if value in (None, "", "-"):
        return ""
    try:
        number = float(value)
        if number > 100000000000:
            number /= 1000.0
        return datetime.fromtimestamp(number, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.isoformat(timespec="seconds")
