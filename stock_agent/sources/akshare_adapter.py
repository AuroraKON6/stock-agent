from __future__ import annotations

from typing import Any, Dict, List

from ..models import RawSignal, now_iso


def collect_akshare(source_config: Dict[str, Any]) -> List[RawSignal]:
    if not source_config.get("enabled", False):
        return []

    try:
        import akshare as ak  # type: ignore
    except ImportError:
        return [
            RawSignal(
                source="akshare",
                source_type="source_error",
                title="AkShare 未安装",
                summary="执行 pip install -r requirements-optional.txt 后可启用行情适配器。",
            )
        ]

    try:
        df = ak.stock_zh_a_spot_em()
    except Exception as exc:
        return [
            RawSignal(
                source="akshare",
                source_type="source_error",
                title="AkShare A股行情抓取失败",
                summary=str(exc),
            )
        ]

    symbols = set(str(item) for item in source_config.get("symbols", []))
    rows = []
    for _, row in df.iterrows():
        code = str(_first(row, ["代码", "code", "symbol"], ""))
        name = str(_first(row, ["名称", "name"], ""))
        if symbols and code not in symbols and name not in symbols:
            continue
        rows.append(row)

    signals: List[RawSignal] = []
    for row in rows:
        code = str(_first(row, ["代码", "code", "symbol"], ""))
        name = str(_first(row, ["名称", "name"], code))
        pct = _first(row, ["涨跌幅", "changepercent", "pct_chg"], None)
        latest = _first(row, ["最新价", "trade", "close"], None)
        amount = _first(row, ["成交额", "amount"], None)
        summary = "最新价={0}; 涨跌幅={1}%; 成交额={2}".format(latest, pct, amount)
        signals.append(
            RawSignal(
                source="akshare",
                source_type=str(source_config.get("source_type") or "price"),
                title="{0}({1}) 行情快照".format(name, code),
                summary=summary,
                published_at=now_iso(),
                payload={
                    "code": code,
                    "name": name,
                    "latest": _safe_value(latest),
                    "pct_change": _safe_value(pct),
                    "amount": _safe_value(amount),
                    # The spot table does not expose an exchange trade time.
                    # Keep observation time separate and let governance decide.
                    "quote_timestamp": "",
                    "observed_at": now_iso(),
                    "raw": {str(key): _safe_value(value) for key, value in row.to_dict().items()},
                },
            )
        )
    return signals


def _first(row: Any, keys: List[str], default: Any) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default


def _safe_value(value: Any) -> Any:
    try:
        if value != value:
            return None
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return str(value)
    return value
