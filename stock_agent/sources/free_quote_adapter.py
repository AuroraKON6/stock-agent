from __future__ import annotations

from typing import Any, Dict, List

from ..models import RawSignal
from .alltick_adapter import collect_alltick


def collect_free_quote(source_config: Dict[str, Any]) -> List[RawSignal]:
    """Compatibility adapter for CTradeExchange/free-quote / AllTick style quote APIs."""
    cfg = dict(source_config)
    cfg.setdefault("token_env", "FREE_QUOTE_TOKEN")
    cfg.setdefault("base_url", source_config.get("base_url") or "https://quote.alltick.io/quote-stock-b-api")
    signals = collect_alltick(cfg)
    for signal in signals:
        signal.source = "free_quote"
        payload = signal.payload or {}
        evidence = payload.get("source_evidence", {}) if isinstance(payload.get("source_evidence"), dict) else {}
        evidence["provider"] = "free_quote"
        payload["source_evidence"] = evidence
        signal.payload = payload
    return signals
