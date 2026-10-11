from __future__ import annotations

import json
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

from ..models import RawSignal, now_iso


EASTMONEY_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
FIELDS = [
    "f12",  # code
    "f14",  # name
    "f2",   # latest price
    "f3",   # pct change
    "f4",   # price change
    "f5",   # volume
    "f6",   # amount
    "f7",   # amplitude
    "f15",  # high
    "f16",  # low
    "f17",  # open
    "f18",  # previous close
    "f62",  # main net inflow
    "f124",  # exchange quote update timestamp (Unix seconds)
]


def collect_eastmoney(source_config: Dict[str, Any]) -> List[RawSignal]:
    if not source_config.get("enabled", False):
        return []

    symbols = [str(item).strip() for item in source_config.get("symbols", []) if str(item).strip()]
    if not symbols:
        return []

    try:
        rows = _fetch_quotes(symbols, timeout=int(source_config.get("timeout", 15)))
    except Exception as exc:
        return [
            RawSignal(
                source="eastmoney",
                source_type="source_error",
                title="东方财富行情抓取失败",
                summary=str(exc),
            )
        ]

    signals: List[RawSignal] = []
    for row in rows:
        code = _clean(row.get("f12"))
        name = _clean(row.get("f14")) or code
        latest = _num(row.get("f2"))
        pct = _num(row.get("f3"))
        change = _num(row.get("f4"))
        amount = _num(row.get("f6"))
        main_net_inflow = _num(row.get("f62"))
        quote_timestamp = _timestamp_to_iso(row.get("f124"))
        observed_at = now_iso()
        summary = (
            "最新价={latest}; 涨跌幅={pct}%; 涨跌额={change}; "
            "成交额={amount}; 主力净流入={main_net_inflow}"
        ).format(
            latest=_display(latest),
            pct=_display(pct),
            change=_display(change),
            amount=_display(amount),
            main_net_inflow=_display(main_net_inflow),
        )
        signals.append(
            RawSignal(
                source="eastmoney",
                source_type=str(source_config.get("source_type") or "price"),
                title="{0}({1}) 东方财富行情快照".format(name, code),
                summary=summary,
                published_at=quote_timestamp or observed_at,
                payload={
                    "code": code,
                    "name": name,
                    "latest": latest,
                    "pct_change": pct,
                    "price_change": change,
                    "amount": amount,
                    "main_net_inflow": main_net_inflow,
                    "quote_timestamp": quote_timestamp,
                    "observed_at": observed_at,
                    "raw": row,
                },
            )
        )
    return signals


def _fetch_quotes(symbols: Iterable[str], timeout: int) -> List[Dict[str, Any]]:
    secids = ",".join(_to_secid(symbol) for symbol in symbols)
    query = urllib.parse.urlencode(
        {
            "fltt": "2",
            "invt": "2",
            "fields": ",".join(FIELDS),
            "secids": secids,
        }
    )
    request = urllib.request.Request(
        EASTMONEY_URL + "?" + query,
        headers={
            "User-Agent": "Mozilla/5.0 stock-agent/0.1",
            "Referer": "https://quote.eastmoney.com/",
        },
    )
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        data = json.loads(response.read().decode("utf-8", errors="replace"))
    diff = data.get("data", {}).get("diff")
    if not isinstance(diff, list):
        return []
    return [row for row in diff if isinstance(row, dict)]


def _to_secid(symbol: str) -> str:
    value = symbol.strip()
    if "." in value:
        return value
    if value.startswith(("6", "9")):
        return "1." + value
    return "0." + value


def _clean(value: Any) -> str:
    if value is None or value == "-":
        return ""
    return str(value).strip()


def _num(value: Any) -> Any:
    if value is None or value == "-":
        return None
    return value


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
        return ""
