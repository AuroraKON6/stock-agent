"""Kimi-routed data sources: ledger, cache, normalization, and gates (K03).

This adapter is the only project-side consumer of the kimi integration
client. It persists query intent before any gateway send, appends results
and consumption receipts afterwards, caches normalized payloads under
rights-aware keys, and refuses to execute anything unless the source
registry entry is registered, approved, and unexpired (fail closed, plan
sections 7.2/7.3). Data-call budget is counted separately from model calls.

Nothing here performs network I/O by itself: the transport is always
injected, and the default configuration keeps the whole path disabled.
The append-only JSONL ledger is audit history — records are added, never
edited or removed.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..config import project_path
from ..integrations import kimi as kimi_integration
from ..integrations.kimi import client as kimi_client
from ..integrations.kimi import contracts as kimi_contracts
from .registry import load_source_registry, registry_status

LEDGER_VERSION = "1.0"
DEFAULT_LEDGER_PATH = "data/kimi/query_ledger.jsonl"
DEFAULT_CACHE_DIR = "data/kimi/cache"

INTENT_RECORD = "query_intent"
RESULT_RECORD = "query_result"
CONSUMPTION_RECORD = "consumed"

# Gate reasons (distinct from client outcome codes; surfaced in the board).
GATE_SOURCE_NOT_REGISTERED = "source_not_registered"
GATE_SOURCE_NOT_APPROVED = "source_not_approved"
GATE_SOURCE_EXPIRED = "source_expired"
GATE_RETENTION_DENIED = "body_retention_not_allowed"


class KimiAdapterError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ledger_path(config: Optional[Dict[str, Any]]) -> Path:
    raw = ((config or {}).get("kimi_sources") or {}).get("ledger_path") or DEFAULT_LEDGER_PATH
    return project_path(str(raw))


def _cache_dir(config: Optional[Dict[str, Any]]) -> Path:
    raw = ((config or {}).get("kimi_sources") or {}).get("cache_dir") or DEFAULT_CACHE_DIR
    return project_path(str(raw))


def _append_ledger(record: Dict[str, Any], config: Optional[Dict[str, Any]]) -> None:
    path = _ledger_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def record_query_intent(query: kimi_client.DataQuery, config: Optional[Dict[str, Any]] = None,
                        *, now: Optional[str] = None) -> Dict[str, Any]:
    record = {
        "record_type": INTENT_RECORD,
        "ledger_version": LEDGER_VERSION,
        "created_at": now or _now_iso(),
        "request_id": query.request_id,
        "idempotency_key": query.idempotency_key,
        "source_id": query.source_id,
        "api_id": query.api_id,
        "data_kind": query.data_kind,
        "purpose": query.purpose,
        "task_id": query.task_id,
        "contract_hash": query.contract_hash,
        "normalized_params": dict(query.normalized_params),
    }
    _append_ledger(record, config)
    return record


def record_query_result(result: kimi_client.DataResult, config: Optional[Dict[str, Any]] = None,
                        *, now: Optional[str] = None,
                        extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    record = {
        "record_type": RESULT_RECORD,
        "ledger_version": LEDGER_VERSION,
        "created_at": now or _now_iso(),
        "request_id": result.request_id,
        "outcome": result.outcome,
        "source_id": result.source_id,
        "data_kind": result.data_kind,
        "cost_status": result.cost_status,
        "attempts": result.attempts,
        "row_count": result.row_count,
        "http_status": result.http_status,
        "artifact_plans": result.artifact_plans,
        "committed_artifacts": result.committed_artifacts,
    }
    if extra:
        record.update(extra)
    _append_ledger(record, config)
    return record


def record_consumption(artifact_id: str, purpose: str,
                       config: Optional[Dict[str, Any]] = None, *,
                       consumer: str = "research_supply", now: Optional[str] = None) -> Dict[str, Any]:
    record = {
        "record_type": CONSUMPTION_RECORD,
        "ledger_version": LEDGER_VERSION,
        "created_at": now or _now_iso(),
        "artifact_id": artifact_id,
        "purpose": purpose,
        "consumer": consumer,
    }
    _append_ledger(record, config)
    return record


def load_query_ledger(config: Optional[Dict[str, Any]] = None, *,
                      limit: int = 200) -> List[Dict[str, Any]]:
    path = _ledger_path(config)
    if not path.is_file():
        return []
    records: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records[-max(0, limit):]


def find_intent(idempotency_key: str, config: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Idempotent replay check: an existing intent for the same key means the
    gateway was already asked (or its attempt was recorded); callers must not
    send a second billable request for it (T14/T19)."""
    for record in load_query_ledger(config, limit=100000):
        if record.get("record_type") == INTENT_RECORD and record.get("idempotency_key") == idempotency_key:
            return record
    return None


def data_calls_today(config: Optional[Dict[str, Any]] = None, *,
                     now: Optional[datetime] = None) -> int:
    today = (now or datetime.now(timezone.utc)).date()
    count = 0
    for record in load_query_ledger(config, limit=100000):
        if record.get("record_type") != INTENT_RECORD:
            continue
        try:
            stamp = datetime.fromisoformat(str(record.get("created_at") or ""))
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.astimezone()
        if stamp.date() == today:
            count += 1
    return count


def may_start_data_call(config: Optional[Dict[str, Any]] = None, *,
                        now: Optional[datetime] = None) -> Dict[str, Any]:
    limit = int(((config or {}).get("kimi_sources") or {}).get("daily_data_call_limit") or 5)
    used = data_calls_today(config, now=now)
    if limit > 0 and used >= limit:
        return {"allowed": False, "reason": "daily data-call budget exhausted: {0}/{1}".format(used, limit),
                "calls_today": used, "limit": limit}
    return {"allowed": True, "reason": "ok", "calls_today": used, "limit": limit}


def gate_source(source_id: str, registry: Dict[str, Any]) -> Dict[str, Any]:
    """Registry gate executed before any intent is persisted (T03)."""
    status = registry_status(registry, source_id)
    if not status["registered"]:
        return {"allowed": False, "reason": GATE_SOURCE_NOT_REGISTERED, "registry_status": status}
    if status["approval_state"] != "approved":
        return {"allowed": False, "reason": GATE_SOURCE_NOT_APPROVED, "registry_status": status}
    if status["expired"]:
        return {"allowed": False, "reason": GATE_SOURCE_EXPIRED, "registry_status": status}
    return {"allowed": True, "reason": "ok", "registry_status": status}


def cache_key(query: kimi_client.DataQuery) -> str:
    import hashlib
    material = {
        "source_id": query.source_id,
        "api_id": query.api_id,
        "contract_hash": query.contract_hash,
        "params": {name: query.normalized_params[name] for name in sorted(query.normalized_params)},
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def read_cache(query: kimi_client.DataQuery, registry: Dict[str, Any],
               config: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Read a cached normalization. Rights are re-verified at read time: the
    source must still be approved and unexpired, or the cache is a miss (T03
    permission-revocation path)."""
    gate = gate_source(query.source_id, registry)
    if not gate["allowed"]:
        return None
    path = _cache_dir(config) / "{0}.json".format(cache_key(query))
    if not path.is_file():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if cached.get("contract_hash") != query.contract_hash:
        return None
    return cached


def write_cache(query: kimi_client.DataQuery, normalized: Dict[str, Any], registry: Dict[str, Any],
                config: Optional[Dict[str, Any]] = None) -> bool:
    """Persist a normalization only when body retention is allowed by the
    registry entry; otherwise only the ledger keeps the run metadata (no
    content) — retention is not inferred from a successful query."""
    status = registry_status(registry, query.source_id)
    if not status.get("body_retention_allowed"):
        return False
    path = _cache_dir(config) / "{0}.json".format(cache_key(query))
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "cache_version": LEDGER_VERSION,
        "created_at": _now_iso(),
        "source_id": query.source_id,
        "api_id": query.api_id,
        "data_kind": query.data_kind,
        "contract_hash": query.contract_hash,
        "normalized": normalized,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return True


# ── Normalization ──────────────────────────────────────────────────────────

# Conservative column aliases for iFinD CSV exports; anything unrecognized is
# kept verbatim under unmapped_columns instead of guessed (T13).
_DATE_ALIASES = ("日期", "date", "trade_date", "trading_day", "交易日")
_OPEN_ALIASES = ("开盘价", "open", "开盘")
_HIGH_ALIASES = ("最高价", "high", "最高")
_LOW_ALIASES = ("最低价", "low", "最低")
_CLOSE_ALIASES = ("收盘价", "close", "收盘")
_VOLUME_ALIASES = ("成交量", "volume", "vol")
_AMOUNT_ALIASES = ("成交额", "amount", "turnover")


def _pick(columns: Iterable[str], aliases: Tuple[str, ...]) -> Optional[str]:
    lowered = {str(col).strip().lower(): str(col) for col in columns}
    for alias in aliases:
        if alias in lowered:
            return lowered[alias]
    for key, original in lowered.items():
        for alias in aliases:
            if alias in key:
                return original
    return None


def normalize_daily_quote_csv(content: str, *, ticker: str = "", adjust: str = "",
                              window: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Normalize a daily-bar CSV payload. Units the contract cannot confirm
    stay explicit nulls with reasons; nothing is converted by guessing."""
    reader = csv.DictReader(io.StringIO(content))
    rows = []
    for row in reader:
        rows.append({str(key): value for key, value in row.items()})
    columns = list(rows[0].keys()) if rows else []
    mapping = {
        "date": _pick(columns, _DATE_ALIASES),
        "open": _pick(columns, _OPEN_ALIASES),
        "high": _pick(columns, _HIGH_ALIASES),
        "low": _pick(columns, _LOW_ALIASES),
        "close": _pick(columns, _CLOSE_ALIASES),
        "volume": _pick(columns, _VOLUME_ALIASES),
        "amount": _pick(columns, _AMOUNT_ALIASES),
    }
    mapped = {key: column for key, column in mapping.items() if column}
    unmapped = [column for column in columns if column not in mapped.values()]
    bars = []
    for row in rows:
        bar = {"date": row.get(mapping["date"]) if mapping["date"] else None}
        for field in ("open", "high", "low", "close", "volume", "amount"):
            raw_value = row.get(mapping[field]) if mapping[field] else None
            if raw_value in (None, "", "-"):
                bar[field] = None
            else:
                try:
                    bar[field] = float(raw_value)
                except (TypeError, ValueError):
                    bar[field] = None
                    unmapped.append("{0}({1})".format(mapping[field], "non_numeric"))
        bars.append(bar)
    return {
        "data_kind": "daily_quote",
        "data_scope": "historical_background",
        "frequency": "D",
        "ticker": ticker,
        "adjust": adjust or None,
        "bars": bars,
        "row_count": len(bars),
        "column_mapping": mapped,
        "unmapped_columns": sorted(set(unmapped)),
        "known_unknowns": {
            "volume_unit": {"value": None, "reason": "iFinD contract does not state volume units"},
            "currency": {"value": None, "reason": "not stated; do not assume CNY in outputs"},
            "source_time": {"value": None, "reason": "response carries no source timestamp"},
        },
        "history_window": window or {},
    }


def announcement_rows_from_csv(content: str) -> Dict[str, Any]:
    """Announcement metadata stays leads: URLs are recorded, never fetched,
    and never treated as evidence of an imported original (plan 6.1)."""
    reader = csv.DictReader(io.StringIO(content))
    rows = []
    for row in reader:
        rows.append({str(key): (value or "").strip() for key, value in row.items()})
    columns = list(rows[0].keys()) if rows else []
    return {
        "data_kind": "announcement_metadata",
        "rows": rows,
        "row_count": len(rows),
        "columns": columns,
        "url_fields": [column for column in columns if "url" in column.lower() or "链接" in column],
        "notes": ["URLs are discovery leads only; official originals enter via evidence vault import"],
    }


def normalize_news_text(user_text: str) -> Dict[str, Any]:
    """Dongcai news search: only the user-side text is data; provider
    analysis was already separated by the client (T15)."""
    lines = [line for line in (user_text or "").splitlines() if line.strip()]
    return {
        "data_kind": "news_metadata",
        "items": lines,
        "item_count": len(lines),
        "known_unknowns": {
            "event_id": {"value": None, "reason": "no documented stable event id for dedup"},
            "publish_time": {"value": None, "reason": "provider text timestamps unverified"},
        },
    }


def normalize_financial_statement_csv(content: str, *, ticker: str = "",
                                      statement: str = "",
                                      report_period: str = "") -> Dict[str, Any]:
    """Financial statements: rows are preserved verbatim; period basis,
    consolidation scope and restatement version stay explicit gaps until a
    human verifies them against the filed originals (plan 6.1)."""
    reader = csv.DictReader(io.StringIO(content))
    rows = [{str(key): value for key, value in row.items()} for row in reader]
    columns = list(rows[0].keys()) if rows else []
    return {
        "data_kind": "financial_statement",
        "ticker": ticker,
        "statement": statement,
        "report_period": report_period,
        "rows": rows,
        "row_count": len(rows),
        "columns": columns,
        "known_unknowns": {
            "cumulative_basis": {"value": None, "reason": "A-share statements are typically year-to-date; unverified for these columns"},
            "consolidation_scope": {"value": None, "reason": "consolidated vs parent not stated by the contract"},
            "restatement_version": {"value": None, "reason": "re-requesting an old period may or may not return restated figures"},
            "announcement_time": {"value": None, "reason": "no filing timestamp in the response"},
        },
    }


def normalize_result_payload(query: kimi_client.DataQuery,
                             result: kimi_client.DataResult) -> Optional[Dict[str, Any]]:
    """Dispatch normalization by data kind; returns None when the outcome
    carries no data to normalize."""
    if not result.outcome == "ok":
        return None
    params = query.normalized_params
    if query.data_kind == "daily_quote":
        content = _first_csv_content(result)
        if content is None:
            return None
        return normalize_daily_quote_csv(
            content, ticker=str(params.get("ticker") or ""),
            adjust=str(params.get("adjust") or ""),
            window={"start": params.get("start_date"), "end": params.get("end_date")})
    if query.data_kind == "announcement_metadata":
        content = _first_csv_content(result)
        if content is None:
            return None
        return announcement_rows_from_csv(content)
    if query.data_kind == "financial_statement":
        content = _first_csv_content(result)
        if content is None:
            return None
        return normalize_financial_statement_csv(
            content, ticker=str(params.get("ticker") or ""),
            statement=str(params.get("statement") or ""),
            report_period=str(params.get("financial_parameter") or ""))
    if query.data_kind == "news_metadata":
        if not result.user_text:
            return None
        return normalize_news_text(result.user_text)
    return None


def _first_csv_content(result: kimi_client.DataResult) -> Optional[str]:
    for plan in result.artifact_plans:
        artifact_id = plan.get("artifact_id")
        data = result.artifact_contents.get(artifact_id)
        if plan.get("accepted") and plan.get("extension") == "csv" and data:
            return data.decode("utf-8", "replace")
    return None


def governed_execute(
    snapshot: Dict[str, Any],
    api_id: str,
    raw_params: Dict[str, Any],
    *,
    purpose: str,
    config: Optional[Dict[str, Any]],
    registry: Dict[str, Any],
    transport: Any,
    credential_provider: Any,
    task_id: Optional[str] = None,
    artifact_root: Optional[Path] = None,
    budget: Optional[kimi_client.ExperimentBudget] = None,
    allow_cache: bool = True,
) -> Dict[str, Any]:
    """The full governed path: gate -> budget -> idempotency -> intent ->
    execute -> normalize -> cache -> receipt. Returns a record with either
    ``result`` (client outcome) or ``gate`` (refusal). The ledger records
    both refusals and sends so attempts are always countable."""
    api = kimi_contracts.find_api(snapshot, api_id)
    integration = kimi_client.KimiIntegrationConfig.from_config(config)
    if not integration.enabled:
        # Disabled integration short-circuits before the ledger: no intent is
        # persisted for a request that can never be sent, so the idempotency
        # slot stays free for a later enabled run (T20 degradation path).
        return {"stage": "integration_disabled", "allowed": False,
                "source_id": api["source_id"], "api_id": api_id,
                "detail": "kimi integration disabled; no network attempted"}
    gate = gate_source(api["source_id"], registry)
    if not gate["allowed"]:
        return {"stage": "gate", "allowed": False, "gate": gate,
                "source_id": api["source_id"], "api_id": api_id}
    budget_state = may_start_data_call(config)
    if not budget_state["allowed"]:
        return {"stage": "budget", "allowed": False, "budget": budget_state,
                "source_id": api["source_id"], "api_id": api_id}
    query = kimi_client.build_query(snapshot, api_id, raw_params, purpose=purpose,
                                    task_id=task_id, poc=integration.poc)
    if allow_cache:
        cached = read_cache(query, registry, config)
        if cached is not None:
            record_consumption("cache:{0}".format(cache_key(query)[:16]), purpose, config,
                               consumer="cache_hit")
            return {"stage": "cache", "allowed": True, "cache_hit": True, "cached": cached,
                    "source_id": query.source_id, "api_id": api_id,
                    "idempotency_key": query.idempotency_key}
    replay = find_intent(query.idempotency_key, config)
    if replay is not None:
        # Never re-bill an idempotent replay: return the prior intent
        # reference; the caller can look up its result in the ledger.
        return {"stage": "replay", "allowed": False, "prior_intent": replay,
                "source_id": query.source_id, "api_id": api_id,
                "idempotency_key": query.idempotency_key}
    record_query_intent(query, config)
    result = kimi_client.execute_query(
        query, integration=integration, transport=transport,
        credential_provider=credential_provider,
        budget=budget or kimi_client.ExperimentBudget(),
        artifact_root=artifact_root)
    normalized = normalize_result_payload(query, result)
    if normalized is not None:
        write_cache(query, normalized, registry, config)
    record_query_result(result, config, extra={
        "normalized_row_count": normalized.get("row_count") if normalized else None})
    return {"stage": "executed", "allowed": result.outcome in {"ok"},
            "result": result, "normalized": normalized,
            "source_id": query.source_id, "api_id": api_id,
            "idempotency_key": query.idempotency_key}
