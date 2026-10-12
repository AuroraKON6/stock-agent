"""GLM-09: which events may open a research task (trigger contract).

Deterministic, LLM-free gating so the task engine never floods itself with
low-value items: filings are always research-worthy, news must clear a
minimum priority, per-symbol caps bound one cycle, symbol cooldowns space
out repeat triggers, and a real content revision always reopens research.
Every decision carries a machine-readable reason; nothing is silently
dropped — skipped events are counted and reported.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Optional

PRIORITY_RANKS = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
TRIGGER_SOURCE_TYPES = {"filing"}
TRIGGER_CONFIG_FIELDS = {
    "news_min_priority", "max_per_symbol_per_cycle", "symbol_cooldown_minutes",
    "cluster_within_cycle", "cluster_cooldown_minutes",
}

# Same-story clustering: keep CJK letters/digits and ASCII alnum only, so
# punctuation, brackets, spacing and case differences do not split a story.
_CLUSTER_STRIP = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
_CLUSTER_KEY_CHARS = 60
# Leading decoration tags Chinese reposts prepend: 【快讯】、[独家]、（公告）…
_LEADING_TAG = re.compile(r"^[^0-9A-Za-z\u4e00-\u9fff]*[【\[（(][^】\]）)]*[】\]）)]")
_MAX_LEADING_TAGS = 2


def _strip_leading_tags(title: str) -> str:
    for _ in range(_MAX_LEADING_TAGS):
        stripped = _LEADING_TAG.sub("", title, count=1)
        if stripped == title:
            break
        title = stripped
    return title


def cluster_key(row: Dict[str, Any]) -> str:
    """Identity of a STORY (title + symbols) across sources and rewrites."""
    title = _strip_leading_tags(str(row.get("title") or ""))
    symbols = sorted(str(symbol) for symbol in (row.get("symbols") or []) if str(symbol))
    normalized = _CLUSTER_STRIP.sub("", title).lower()[:_CLUSTER_KEY_CHARS]
    return "cluster::{0}::{1}".format(normalized, ",".join(symbols))


def research_trigger_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = ((config or {}).get("task_runtime") or {}).get("research_trigger") or {}
    if not isinstance(raw, dict):
        raise ValueError("task_runtime.research_trigger must be an object")
    unexpected = set(raw) - TRIGGER_CONFIG_FIELDS
    if unexpected:
        raise ValueError("unknown research_trigger fields: {0}".format(sorted(unexpected)))
    news_min = str(raw.get("news_min_priority") or "P2")
    if news_min not in PRIORITY_RANKS:
        raise ValueError("news_min_priority must be one of {0}".format(sorted(PRIORITY_RANKS)))
    cap = raw.get("max_per_symbol_per_cycle") or 0
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
        raise ValueError("max_per_symbol_per_cycle must be a non-negative integer (0 = off)")
    cooldown = raw.get("symbol_cooldown_minutes") or 0
    if isinstance(cooldown, bool) or not isinstance(cooldown, int) or cooldown < 0:
        raise ValueError("symbol_cooldown_minutes must be a non-negative integer (0 = off)")
    cluster_within = raw.get("cluster_within_cycle", True)
    if not isinstance(cluster_within, bool):
        raise ValueError("cluster_within_cycle must be boolean")
    cluster_cooldown = raw.get("cluster_cooldown_minutes") or 0
    if isinstance(cluster_cooldown, bool) or not isinstance(cluster_cooldown, int) or cluster_cooldown < 0:
        raise ValueError("cluster_cooldown_minutes must be a non-negative integer (0 = off)")
    return {
        "news_min_priority": news_min,
        "max_per_symbol_per_cycle": cap,
        "symbol_cooldown_minutes": cooldown,
        "cluster_within_cycle": cluster_within,
        "cluster_cooldown_minutes": cluster_cooldown,
    }


def _content_version(row: Dict[str, Any]) -> str:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    return str(payload.get("content_version") or "1")


def decide_trigger(
    row: Dict[str, Any],
    *,
    config: Optional[Dict[str, Any]] = None,
    state: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Decide whether one eligible signal opens a research task.

    ``state`` carries cross-event counters for the current cycle:
    symbol_cycle_counts {symbol: created_this_cycle} and symbol_last_trigger
    {symbol: iso}. Revisions (content_version > 1) always trigger.
    """
    cfg = research_trigger_config(config or {})
    state = state or {}
    source_type = str(row.get("source_type") or "")
    symbols = [str(symbol) for symbol in (row.get("symbols") or []) if str(symbol)]
    priority = str(row.get("priority") or "P3")

    if _content_version(row) != "1":
        return {"trigger": True, "reason": "content revision", "symbols": symbols}

    if source_type not in TRIGGER_SOURCE_TYPES and source_type != "news":
        return {"trigger": False,
                "reason": "source_type {0} does not open research tasks".format(source_type),
                "symbols": symbols}
    if source_type == "news" and PRIORITY_RANKS.get(priority, 3) > PRIORITY_RANKS[cfg["news_min_priority"]]:
        return {"trigger": False,
                "reason": "news priority {0} below research threshold {1}".format(
                    priority, cfg["news_min_priority"]),
                "symbols": symbols}

    if cfg["max_per_symbol_per_cycle"] > 0 and symbols:
        counts = state.get("symbol_cycle_counts") or {}
        if any(int(counts.get(symbol, 0)) >= cfg["max_per_symbol_per_cycle"] for symbol in symbols):
            return {"trigger": False, "reason": "per-symbol cycle cap reached", "symbols": symbols}

    if cfg["symbol_cooldown_minutes"] > 0 and symbols and now is not None:
        last = state.get("symbol_last_trigger") or {}
        for symbol in symbols:
            stamp = str(last.get(symbol) or "")
            if not stamp:
                continue
            try:
                last_dt = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if last_dt.tzinfo is None:
                last_dt = last_dt.astimezone()
            if (now - last_dt).total_seconds() < cfg["symbol_cooldown_minutes"] * 60:
                return {"trigger": False,
                        "reason": "symbol {0} in cooldown window".format(symbol),
                        "symbols": symbols}
    return {"trigger": True, "reason": "policy pass", "symbols": symbols}
