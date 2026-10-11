"""Build governed public research contexts for tasks from local caches.

W04-lite: replaces the hand-written demo spec. Quote snapshots come from the
market cache with honest source-time handling (a missing quote_timestamp is
never faked from the fetch time); news items come from the signals store,
deduplicated by URL and with example-provenance rows excluded; evidence
summaries come from the vault index when present. Everything stays public and
size-capped for external egress.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import project_path

MAX_NEWS_PER_SYMBOL = 5
MAX_EVIDENCE_SUMMARIES = 5
MAX_SIGNAL_SCAN = 2000
MAX_BOUND_SIGNALS = 200
MAX_LOOKBACK_HOURS = 24 * 366


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _parse_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _quote_snapshots(symbols: List[str]) -> Dict[str, Any]:
    cache = _load_json(project_path("data/cache/market_quotes/latest.json"))
    snapshots: Dict[str, Any] = {}
    for quote in cache.get("quotes") or []:
        payload = quote.get("payload") if isinstance(quote.get("payload"), dict) else {}
        symbol = str(payload.get("code") or payload.get("symbol") or "")
        if symbol not in symbols or symbol in snapshots:
            continue
        resilience = payload.get("market_resilience") if isinstance(payload.get("market_resilience"), dict) else {}
        # source_time is only ever the upstream quote time; a missing value is
        # reported as unverified instead of being faked from the fetch time.
        source_time = payload.get("quote_timestamp") or payload.get("tick_time") or payload.get("trade_timestamp")
        snapshots[symbol] = {
            "price": payload.get("latest"),
            "pct_change": payload.get("pct_change"),
            "provider": str(resilience.get("provider") or quote.get("source") or ""),
            "source_time": source_time,
            "source_time_status": "verified" if source_time else "unverified_missing_upstream_time",
            "observed_at": payload.get("observed_at") or resilience.get("fetched_at") or "",
        }
    return snapshots


def load_research_signals(config: Optional[Dict[str, Any]] = None, *,
                          symbols: Optional[List[str]] = None, signal_ids: Optional[List[str]] = None,
                          lookback_hours: Optional[int] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Read eligible bindings from the configured store, without migration/writes.

    Stored source classification is authoritative here; a task's public label
    cannot promote an internal signal. The worker still applies egress governance.
    """
    from .llm_governance import _explicit_classification, _source_classification, governance_config
    from .models import AnalyzedSignal

    cfg = config or {}
    report: Dict[str, Any] = {"status": "ready", "selected": 0, "rejected": 0, "truncated": False}
    if lookback_hours is not None and (isinstance(lookback_hours, bool) or not isinstance(lookback_hours, int)
                                      or not 1 <= lookback_hours <= MAX_LOOKBACK_HOURS):
        return [], dict(report, status="invalid_lookback")
    if signal_ids is not None and (not isinstance(signal_ids, list) or
            any(not isinstance(item, str) or not item.strip() for item in signal_ids) or
            len(signal_ids) > MAX_BOUND_SIGNALS):
        return [], dict(report, status="invalid_binding")
    if signal_ids == [] or symbols == []:
        return [], report
    path = project_path(str((cfg.get("storage") or {}).get("sqlite_path") or "data/stock_agent.sqlite3"))
    if not path.is_file():
        return [], dict(report, status="missing")
    conditions = "source_type IN ('news', 'filing')"
    params: list = []
    if signal_ids is not None:
        conditions += " AND signal_id IN ({0})".format(",".join("?" for _ in signal_ids))
        params.extend(signal_ids)
    query = ("SELECT signal_id, source, source_type, title, summary, url, published_at, "
             "collected_at, symbols_json, payload_json FROM signals WHERE " + conditions +
             " ORDER BY collected_at DESC, signal_id LIMIT ?")
    params.append(MAX_SIGNAL_SCAN + 1)
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
    except sqlite3.Error:
        return [], dict(report, status="unavailable")
    report["truncated"] = len(rows) > MAX_SIGNAL_SCAN
    now = datetime.now().astimezone()
    cutoff = now - timedelta(hours=max(1, int(lookback_hours))) if lookback_hours is not None else None
    governance = governance_config(cfg)
    selected = []
    for row in rows[:MAX_SIGNAL_SCAN]:
        try:
            payload = json.loads(row["payload_json"] or "{}")
            row_symbols = json.loads(row["symbols_json"] or "[]")
        except (TypeError, ValueError):
            report["rejected"] += 1
            continue
        published = _parse_time(row["published_at"])
        valid = (isinstance(payload, dict) and isinstance(row_symbols, list) and row_symbols
                 and all(isinstance(symbol, str) and symbol for symbol in row_symbols)
                 and (symbols is None or bool(set(symbols) & set(row_symbols)))
                 and published is not None and published <= now + timedelta(seconds=120)
                 and (cutoff is None or published >= cutoff))
        if not valid or str(payload.get("data_provenance") or "").lower() in {"example", "synthetic", "replay", "demo", "test"}:
            report["rejected"] += 1
            continue
        carrier = AnalyzedSignal(signal_id=row["signal_id"], source=row["source"],
                                 source_type=row["source_type"], title=row["title"], summary=row["summary"] or "",
                                 url=row["url"] or "", published_at=row["published_at"],
                                 collected_at=row["collected_at"] or "", payload=payload)
        if (_explicit_classification(payload) or _source_classification(carrier, governance)) != "public":
            report["rejected"] += 1
            continue
        if len(selected) == MAX_BOUND_SIGNALS:
            report["truncated"] = True
            break
        selected.append(dict(row, symbols=row_symbols, payload=payload))
    report["selected"] = len(selected)
    return selected, report


def _news_clusters(symbols: List[str], signals: List[Dict[str, Any]]) -> Dict[str, Any]:
    clusters: Dict[str, Any] = {}
    seen_urls = set()
    for row in signals:
        if row["source_type"] != "news":
            continue
        url = str(row["url"] or "")
        for symbol in symbols:
            if symbol not in row["symbols"] or (url and (symbol, url) in seen_urls):
                continue
            cluster = clusters.setdefault("news-{0}".format(symbol), {"items": []})
            if len(cluster["items"]) >= MAX_NEWS_PER_SYMBOL:
                continue
            if url:
                seen_urls.add((symbol, url))
            cluster["items"].append({
                "signal_id": row["signal_id"],
                "title": str(row["title"] or "")[:200],
                "url": url,
                "published_at": str(row["published_at"] or ""),
            })
    return clusters


def _evidence_summaries(config: Optional[Dict[str, Any]], signal_ids: List[str]) -> Dict[str, Any]:
    """Deliverable evidence entries from the authoritative vault verification.

    Uses the vault's own verified projection (real producer schema, chain and
    object-byte checks) via the shared evidence-context helper — never a
    hand-written index reader.
    """

    from .evidence_context import verified_evidence_index

    if not signal_ids:
        return {}
    entries = verified_evidence_index(config or {})
    bound = set(signal_ids)
    matching = ((ref, item) for ref, item in entries.items() if bound.intersection(item.get("signal_ids") or []))
    result = {}
    for ref, item in matching:
        result[ref] = item
        if len(result) == MAX_EVIDENCE_SUMMARIES:
            break
    return result


def build_public_context(symbols: List[str], *, lookback_hours: int = 48, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    symbols = [str(symbol).strip() for symbol in symbols if str(symbol).strip()]
    signals, selection = load_research_signals(config, symbols=symbols, lookback_hours=lookback_hours)
    signal_ids = sorted(item["signal_id"] for item in signals)
    snapshots = _quote_snapshots(symbols)
    context = {
        "quote_snapshots": snapshots,
        "news_clusters": _news_clusters(symbols, signals),
        "evidence_summaries": _evidence_summaries(config, signal_ids),
        "context_notes": {
            "bound_signal_ids": signal_ids,
            "signal_selection": selection,
            "symbols": symbols,
            "quotes_missing": [symbol for symbol in symbols if symbol not in snapshots],
            "source_time_policy": "missing upstream quote time is reported as unverified, never faked",
        },
    }
    return context


def build_task_spec(
    *,
    research_question: str,
    symbols: List[str],
    lookback_hours: int = 48,
    execution_mode: str = "replay",
    replay_response: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not str(research_question or "").strip():
        raise ValueError("research_question must not be empty")
    spec: Dict[str, Any] = {
        "research_question": str(research_question),
        "data_classification": "public",
        "symbols": [str(symbol).strip() for symbol in symbols if str(symbol).strip()],
        "public_context": build_public_context(symbols, lookback_hours=lookback_hours, config=config),
    }
    spec["bound_signal_ids"] = list(spec["public_context"]["context_notes"]["bound_signal_ids"])
    spec["signal_lookback_hours"] = max(1, int(lookback_hours))
    if execution_mode == "replay":
        if not isinstance(replay_response, dict):
            replay_response = {
                "schema_version": "1.0",
                "task_id": "placeholder",
                "summary": "REPLAY 草稿：基于所附公开上下文生成，未调用外部模型。",
                "key_findings": [],
                "unknowns": ["replay 模式未做真实分析"],
                "evidence_refs": [],
                "human_questions": [
                    {"question": "是否切换为 live 模式做真实模型分析？", "why_needed": "replay 仅用于流程演示", "options": ["切 live", "维持 replay"]}
                ],
                "model_self_assessed_confidence": 0.2,
            }
        spec["replay_response"] = replay_response
    return spec
