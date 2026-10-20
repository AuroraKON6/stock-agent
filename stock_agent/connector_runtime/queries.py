"""R04: transactional query intake for outbound data connectors.

One SQLite database per host root owns the query attempt chain:

- ``query_intents``: idempotency-key UNIQUE, the authority for "was this
  query already dispatched" (replaces the JSONL read-then-act race, F09).
- ``query_quota_counters``: per-scope daily counters (scope = upstream quota
  group or per-source), incremented in the SAME transaction as the intent
  insert, so the last unit can never be double-spent (A19).
- ``query_rate_events``: sliding one-minute windows for per-source RPM.

Reserve order inside one BEGIN IMMEDIATE: existing-key replay FIRST (a
retried query is never rate-gated), then minute window, then per-source and
per-upstream daily caps, then insert + counters. Verdicts are plain dicts so
adapters can project them without importing exceptions.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

INTAKE_SCHEMA_VERSION = "1.0"

STATUS_RESERVED = "reserved"
STATUS_DISPATCHED = "dispatched"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED_KNOWN = "failed_known"
STATUS_OUTCOME_UNKNOWN = "outcome_unknown"
STATUS_TERMINALS = {STATUS_SUCCEEDED, STATUS_FAILED_KNOWN, STATUS_OUTCOME_UNKNOWN}

# Transport outcome codes whose cost may still have been consumed: the query
# attempt stays open-ended like R02's model attempts (never auto-retried).
_UNKNOWN_COST_OUTCOMES = {"outcome_unknown", "rate_limited"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS query_intents (
    intent_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    source_id TEXT NOT NULL,
    upstream TEXT NOT NULL DEFAULT '',
    quota_group TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT '',
    request_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'reserved',
    created_at TEXT NOT NULL,
    dispatch_at TEXT NOT NULL DEFAULT '',
    completed_at TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS query_quota_counters (
    scope TEXT NOT NULL,
    usage_date TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, usage_date)
);
CREATE TABLE IF NOT EXISTS query_rate_events (
    source_id TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_query_rate_events_source ON query_rate_events(source_id, at);
"""


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def status_for_outcome(outcome: str) -> str:
    """Map a transport/business outcome code to the attempt terminal status."""
    if outcome == "ok":
        return STATUS_SUCCEEDED
    if str(outcome) in _UNKNOWN_COST_OUTCOMES:
        return STATUS_OUTCOME_UNKNOWN
    return STATUS_FAILED_KNOWN


class ConnectorQueryStore:
    """Per-request store (open -> one operation -> close), same discipline as
    workflow_service: HTTP/handler threads never share a connection.

    Concurrent FIRST opens of a brand-new database race on the WAL pragma and
    the CREATE TABLE batch; the schema init below is therefore bounded-retry
    (busy timeout set before anything else), matching what years of board
    tests have exercised on TaskStore against initialized databases."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=30,
                                     isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        import time as _time

        last: Exception = RuntimeError("schema init never attempted")
        for attempt in range(10):
            try:
                self._conn.execute("PRAGMA busy_timeout=30000")
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.executescript(_SCHEMA)
                return
            except (sqlite3.OperationalError, sqlite3.InterfaceError) as exc:
                last = exc
                _time.sleep(0.05 * (attempt + 1))
        raise last

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ConnectorQueryStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    # -------- internals --------

    def _begin(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._conn.commit()

    def _rollback(self) -> None:
        try:
            self._conn.rollback()
        except sqlite3.Error:
            pass

    def _usage(self, scope: str, usage_date: str) -> int:
        row = self._conn.execute(
            "SELECT used FROM query_quota_counters WHERE scope = ? AND usage_date = ?",
            (scope, usage_date)).fetchone()
        return int(row["used"]) if row is not None else 0

    def _bump(self, scope: str, usage_date: str) -> None:
        self._conn.execute(
            "INSERT INTO query_quota_counters (scope, usage_date, used) VALUES (?,?,1) "
            "ON CONFLICT(scope, usage_date) DO UPDATE SET used = used + 1",
            (scope, usage_date))

    # -------- public API --------

    def reserve_query(self, idempotency_key: str, *, source_id: str, upstream: str,
                      purpose: str, per_source_rpm: int, per_source_rpd: int,
                      group_daily_limit: int, request_id: str = "",
                      now: Optional[datetime] = None) -> Dict[str, Any]:
        """Atomically reserve one billable query attempt (or return the replay).

        Verdicts:
        - ``{"reserved": True, "replay": True, "intent": {...}}`` — an intent
          with this key already exists; the caller must NOT dispatch again.
        - ``{"reserved": True, "replay": False, "intent": {...}}`` — fresh
          reservation; counters already spent, dispatch may proceed.
        - ``{"reserved": False, "reason": "rate_limited_minute"|"daily_quota_*
          ", ...}`` — refused, nothing recorded, nothing sent.
        """
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key must not be empty")
        moment = _now(now)
        usage_date = moment.date().isoformat()
        window_start = _iso(moment - timedelta(seconds=60))
        self._begin()
        try:
            existing = self._conn.execute(
                "SELECT * FROM query_intents WHERE idempotency_key = ?", (key,)).fetchone()
            if existing is not None:
                self._rollback()
                return {"reserved": True, "replay": True, "intent": dict(existing)}
            # Per-source minute window (A25): refuse before any send.
            self._conn.execute(
                "DELETE FROM query_rate_events WHERE at < ?",
                (_iso(moment - timedelta(seconds=300)),))
            minute_used = int(self._conn.execute(
                "SELECT COUNT(*) FROM query_rate_events WHERE source_id = ? AND at >= ?",
                (str(source_id), window_start)).fetchone()[0])
            if int(per_source_rpm) > 0 and minute_used >= int(per_source_rpm):
                self._rollback()
                return {"reserved": False, "reason": "rate_limited_minute",
                        "minute_used": minute_used, "limit": int(per_source_rpm)}
            source_scope = "source::{0}".format(source_id)
            source_used = self._usage(source_scope, usage_date)
            if int(per_source_rpd) > 0 and source_used >= int(per_source_rpd):
                self._rollback()
                return {"reserved": False, "reason": "daily_quota_source",
                        "calls_today": source_used, "limit": int(per_source_rpd)}
            group = "upstream::{0}".format(str(upstream or "").strip().lower())
            group_used = self._usage(group, usage_date)
            if int(group_daily_limit) > 0 and group_used >= int(group_daily_limit):
                self._rollback()
                return {"reserved": False, "reason": "daily_quota_group",
                        "calls_today": group_used, "limit": int(group_daily_limit),
                        "quota_group": group}
            intent_id = "qi_{0}".format(uuid.uuid4().hex[:16])
            self._conn.execute(
                "INSERT INTO query_intents (intent_id, idempotency_key, source_id, upstream, quota_group, purpose, request_id, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (intent_id, key, str(source_id), str(upstream or ""), group,
                 str(purpose or ""), str(request_id or ""), STATUS_RESERVED, _iso(moment)))
            self._conn.execute(
                "INSERT INTO query_rate_events (source_id, intent_id, at) VALUES (?,?,?)",
                (str(source_id), intent_id, _iso(moment)))
            self._bump(source_scope, usage_date)
            self._bump(group, usage_date)
            self._commit()
        except BaseException:
            self._rollback()
            raise
        row = self._conn.execute(
            "SELECT * FROM query_intents WHERE intent_id = ?", (intent_id,)).fetchone()
        return {"reserved": True, "replay": False, "intent": dict(row)}

    def mark_dispatched(self, intent_id: str, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        self._begin()
        try:
            changed = self._conn.execute(
                "UPDATE query_intents SET status = ?, dispatch_at = ? "
                "WHERE intent_id = ? AND status = ?",
                (STATUS_DISPATCHED, _iso(_now(now)), str(intent_id), STATUS_RESERVED)).rowcount
            if changed != 1:
                row = self._conn.execute(
                    "SELECT status FROM query_intents WHERE intent_id = ?",
                    (str(intent_id),)).fetchone()
                self._rollback()
                state = str(row["status"]) if row is not None else "unknown"
                raise RuntimeError(
                    "query intent {0} is {1}; only reserved intents dispatch".format(intent_id, state))
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return dict(self._conn.execute(
            "SELECT * FROM query_intents WHERE intent_id = ?", (str(intent_id),)).fetchone())

    def complete_query(self, intent_id: str, *, outcome: str,
                       now: Optional[datetime] = None) -> Dict[str, Any]:
        status = status_for_outcome(outcome)
        self._begin()
        try:
            changed = self._conn.execute(
                "UPDATE query_intents SET status = ?, outcome = ?, completed_at = ? "
                "WHERE intent_id = ? AND status = ?",
                (status, str(outcome or ""), _iso(_now(now)), str(intent_id),
                 STATUS_DISPATCHED)).rowcount
            if changed != 1:
                self._rollback()
                raise RuntimeError(
                    "query intent {0} is not dispatched; cannot complete".format(intent_id))
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return dict(self._conn.execute(
            "SELECT * FROM query_intents WHERE intent_id = ?", (str(intent_id),)).fetchone())

    def find_intent(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM query_intents WHERE idempotency_key = ?",
            (str(idempotency_key),)).fetchone()
        return dict(row) if row is not None else None

    def quota_usage(self, scope: str, *, now: Optional[datetime] = None) -> int:
        return self._usage(str(scope), _now(now).date().isoformat())
