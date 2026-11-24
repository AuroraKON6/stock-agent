"""R23: layered observability — one read-only status, two separate truths.

Answers "is the machine healthy" and "is the research unblocked" SEPARATELY
(GLM_REFACTOR_TASKBOOK R23): a system can be technically green while business
gates (approvals, evidence, budget policy) keep research BLOCKED — and that
must never be reported as a technical failure (or vice versa).

- **technical**: queue depth + oldest queued age, in-flight leases,
  invocation attempt outcomes (succeeded / failed_known / outcome_unknown /
  outstanding), stale-lease count, budget usage. Pure numbers from the store.
- **business**: blocked-task reasons categorized by gate (budget /
  evidence_quality / invocation_outcome_unknown / governance / other) — each
  a policy state, not an error to page on.
- Zero side effects: reads only; probes never mutate business state; no
  network, no model, no collection.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .runtime_layout import utc_now
from .task_runtime.store import (
    ATTEMPT_DISPATCHED,
    ATTEMPT_FAILED_KNOWN,
    ATTEMPT_OUTCOME_UNKNOWN,
    ATTEMPT_PREPARED,
    ATTEMPT_RECONCILED,
    ATTEMPT_SUCCEEDED,
)

OBSERVABILITY_SCHEMA = "1.0"
MAX_SAMPLE = 5000

GATE_BUDGET = "budget"
GATE_EVIDENCE_QUALITY = "evidence_quality"
GATE_INVOCATION_UNKNOWN = "invocation_outcome_unknown"
GATE_GOVERNANCE = "governance"
GATE_OTHER = "other"


def _parse(stamp: str) -> Optional[datetime]:
    try:
        moment = datetime.fromisoformat(str(stamp or ""))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment


def _age_seconds(stamp: str, now: datetime) -> Optional[int]:
    moment = _parse(stamp)
    if moment is None:
        return None
    return int((now - moment).total_seconds())


def _categorize_gate(error: str) -> str:
    text = str(error or "").lower()
    if "budget" in text:
        return GATE_BUDGET
    if "citation" in text or "evidence gap" in text or "specific findings" in text:
        return GATE_EVIDENCE_QUALITY
    if "outcome unknown" in text or "manual reconciliation" in text:
        return GATE_INVOCATION_UNKNOWN
    if "governance" in text or "egress" in text or "classification" in text:
        return GATE_GOVERNANCE
    return GATE_OTHER


def build_observability_status(store: Any, *, now: Optional[datetime] = None,
                               attempt_ledger: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Read-only layered status. ``now`` defaults to UTC (persistence clock).

    ``attempt_ledger`` lets a caller that already aggregated the attempts
    (the reconciliation) share that one read, so a report never mixes two
    aggregate reads taken at different moments.
    """
    moment = now or utc_now()
    tasks = store.list_tasks()
    queued = [t for t in tasks if t.get("status") == "queued"]
    inflight = [t for t in tasks if t.get("status") in ("leased", "running")]

    queue_ages = [age for age in (_age_seconds(t.get("created_at"), moment)
                                  for t in queued) if age is not None]
    oldest_queued_age_seconds = max(queue_ages) if queue_ages else 0

    stale_leases = 0
    for task in inflight:
        expires = _parse(str(task.get("lease_expires_at") or ""))
        if expires is not None and expires <= moment:
            stale_leases += 1

    # Full ledger, never a sample: an attempt on the 5,001st task counts.
    ledger = attempt_ledger if attempt_ledger is not None else aggregate_attempts(store)
    attempts, reconciliations_waiting = ledger["states"], ledger["reconciliations_waiting"]

    business_gate_counts: Dict[str, int] = {GATE_BUDGET: 0, GATE_EVIDENCE_QUALITY: 0,
                                            GATE_INVOCATION_UNKNOWN: 0,
                                            GATE_GOVERNANCE: 0, GATE_OTHER: 0}
    blocked_sample: List[Dict[str, Any]] = []
    for task in tasks:
        if task.get("status") != "blocked":
            continue
        error = str(task.get("last_error") or "")
        gate = _categorize_gate(error)
        business_gate_counts[gate] += 1
        if len(blocked_sample) < 20:
            blocked_sample.append({"task_id": str(task.get("task_id") or ""),
                                   "gate": gate, "error_code": error_code(error),
                                   "updated_at": str(task.get("updated_at") or "")})

    technical = {
        "queue_depth": len(queued),
        "oldest_queued_age_seconds": oldest_queued_age_seconds,
        "inflight_leases": len(inflight),
        "stale_leases": stale_leases,
        "invocation_attempts": attempts,
        "reconciliations_waiting": reconciliations_waiting,
        "scope": {"tasks": len(tasks), "attempts": ledger["scope"]},
    }
    business = {
        "blocked_tasks": sum(business_gate_counts.values()),
        "gate_counts": business_gate_counts,
        "blocked_sample": blocked_sample,
    }
    return _safe_tree({
        "schema_version": OBSERVABILITY_SCHEMA,
        "generated_at": moment.isoformat(timespec="seconds"),
        "technical": technical,
        "business": business,
        "scope_note": "只读分层状态：technical=机器健康，business=研究门禁；二者独立报告，互不冒充",
        "side_effects": "none",
        "label_note": LABEL_NOTE,
    })




# --------------------------------------------------------------------------- #
# KONM-96 (R23 continuation): trace, reconciliation, metrics, alert
# candidates, structured log. Same rules as above: projections over the
# authoritative ledgers (task store, invocation attempts, platform runs,
# query intake + query ledger). Nothing here writes, calls a model or
# contacts a source.
#
# What a view may carry: identifiers, states, timestamps, counts and STABLE
# ERROR CODES. Error text, prompts, payloads and model output never leave
# the ledger through a view; the structured log additionally admits only
# allow-listed field names with identifier-shaped values.
# --------------------------------------------------------------------------- #
TRACE_SCHEMA = "1.1"
RECONCILIATION_SCHEMA = "1.1"
METRICS_SCHEMA = "1.0"
ALERT_SCHEMA = "1.0"
LOG_SCHEMA = "1.1"

# Sampling (the only new observation that emits anything) is OFF unless the
# config turns it on; turning it off leaves every ledger and audit row intact.
ALERT_THRESHOLD_NAMES = (
    "oldest_queued_age_seconds",
    "stale_leases",
    "reconciliations_waiting",
    "query_outcome_unknown",
    "dispatched_age_seconds",
)
DEFAULT_ALERT_THRESHOLDS = {
    "oldest_queued_age_seconds": 3600,
    "stale_leases": 0,
    "reconciliations_waiting": 0,
    "query_outcome_unknown": 0,
    "dispatched_age_seconds": 600,
}

# Execution modes that never reach a real supplier.
REPLAY_EXECUTION_MODES = ("replay", "dry_run", "simulation")

MAX_EVENTS = 100000

# Query-ledger availability — three different facts, never collapsed.
LEDGER_NOT_CONFIGURED = "not_configured"
LEDGER_MISSING = "missing"
LEDGER_UNREADABLE = "unreadable"
LEDGER_PARTIAL = "partially_unreadable"
LEDGER_READABLE_EMPTY = "readable_empty"
LEDGER_READABLE = "readable"
_LEDGER_READ_STATES = (LEDGER_READABLE, LEDGER_READABLE_EMPTY)

# Stable error codes: ordered, first match wins. A code describes the CLASS of
# a failure; the message itself stays in the ledger.
ERROR_CODES = (
    "budget_exhausted", "evidence_quality", "invocation_outcome_unknown", "governance",
    "lease_expired", "cancelled", "timeout", "auth_rejected", "rate_limited",
    "connection_error", "provider_rejected", "model_output_invalid", "other",
)
_ERROR_CODE_MARKERS = (
    ("budget_exhausted", ("budget",)),
    ("evidence_quality", ("citation", "evidence gap", "specific findings", "evidence")),
    ("invocation_outcome_unknown", ("outcome unknown", "manual reconciliation", "sent=unknown")),
    ("governance", ("governance", "egress", "classification", "not public", "restricted")),
    ("lease_expired", ("lease expired",)),
    ("cancelled", ("cancel",)),
    ("timeout", ("timeout", "timed out")),
    ("auth_rejected", ("auth", "401", "credential", "unauthorized", "api key")),
    ("rate_limited", ("rate limit", "429", "quota")),
    ("connection_error", ("connection", "reset", "unreachable", "name resolution", "network")),
    ("provider_rejected", ("rejected", "provider", "upstream", "http ")),
    ("model_output_invalid", ("json", "parse", "schema", "invalid response")),
)

# Structured log: field names a record may carry. Anything else is dropped
# (counted, never named), so a caller passing ``prompt`` or ``access_token``
# cannot leak them. Values must then also be identifier-shaped or numeric.
LOG_FIELDS = frozenset({
    "task_id", "attempt_id", "case_id", "run_id", "request_id", "intent_id", "event_id",
    "event_type", "state", "status", "gate", "error_code", "layer", "rule", "worker_id",
    "round_index", "platform", "provider", "outcome", "stage", "sent", "supply_status",
    "count", "attempts", "duration_ms", "calls_today", "limit", "allowed", "link",
})
_LOG_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:\-]{1,80}$")
_LOG_NUMBERS = (int, float, bool)


def error_code(text: Any) -> str:
    """Classify an error message into one stable code; never returns text."""
    value = str(text or "")
    if not value.strip():
        return ""
    lowered = value.lower()
    for code, markers in _ERROR_CODE_MARKERS:
        if any(marker in lowered for marker in markers):
            return code
    return "other"


# A value that reached a view from OUTSIDE the ledger's control (a platform
# run id, a provider or model name, a source id) is free text: the Dify entry
# accepted a sentence as a run id, and a provider label can look like a key.
# Such a value is published only as a validated identifier or as a stable
# irreversible digest — never as itself.
LABEL_NOTE = ("外部关联值只以校验通过的标识或 sha256 稳定摘要呈现；明细与聚合同一标签，计数可对账")
_LABEL = re.compile(r"^[A-Za-z0-9_.:+\-/]{1,80}$")
_LABEL_DIGEST_PREFIX = "sha256:"
# Fields whose value is this module's own prose (Chinese notes / details),
# not a value read from a ledger; they are exempt from labelling.
PROSE_KEYS = frozenset({"note", "reason", "scope_note", "projection_note", "detail",
                        "ledger_detail", "intake_detail", "label_note"})


def safe_label(value: Any) -> str:
    """A validated identifier, or ``sha256:<16 hex>`` of the exact value."""
    text = str(value or "")
    if text == "":
        return ""
    if _LABEL.match(text) and not contains_secret_marker(text):
        return text
    return _LABEL_DIGEST_PREFIX + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _safe_tree(value: Any) -> Any:
    """Label every string value in a view; leaves non-string data alone."""
    if isinstance(value, dict):
        return {str(key): (str(item) if key in PROSE_KEYS and isinstance(item, str)
                           else _safe_tree(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_tree(item) for item in value]
    if isinstance(value, str):
        return safe_label(value)
    return value


@contextlib.contextmanager
def _consistent_read(store: Any):
    """One SQLite read snapshot for a whole view.

    A read-only connection can still ``BEGIN``: every SELECT inside the block
    then sees the same committed state, so an attempt committed by a writer
    midway through a report cannot appear in one section and not another.
    Nothing is written and the transaction is rolled back. Stores without a
    usable connection, or a caller already inside a transaction, are passed
    through unchanged.
    """
    conn = getattr(store, "_conn", None)
    if conn is None:
        yield
        return
    try:
        if conn.in_transaction:
            yield
            return
        conn.execute("BEGIN")
    except Exception:  # noqa: BLE001 — consistency is best effort, never a failure
        yield
        return
    try:
        yield
    finally:
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass


def redact(value: Any, limit: int = 200) -> str:
    """Mask credential-shaped fragments. Defense in depth only — views are
    built from identifiers and codes, not from redacted free text."""
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text[:limit]


_SECRET_PATTERNS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\b(?:sk|pk|api|key|token|secret)[-_][A-Za-z0-9_-]{12,}"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|"
        r"authorization|cookie|credential)\b[\"']?\s*[:=]\s*[\"']?[^\s,\"'}]{4,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
_REDACTED = "[REDACTED]"


def contains_secret_marker(text: str, markers=()) -> bool:
    """True when ``text`` carries a credential-shaped fragment or any
    caller-supplied forbidden marker (drills inject synthetic ones)."""
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        return True
    return any(str(marker) and str(marker) in text for marker in markers)


def observability_settings(config: Any = None) -> Dict[str, Any]:
    """Read the optional ``observability`` config block STRICTLY.

    Each capability is off unless its own block is well formed. A malformed
    block turns that capability off (fail closed) and flags
    ``config_valid=false``; it never becomes a business failure.
    """
    settings = {"sampling_enabled": False, "alerts_enabled": False,
                "thresholds": dict(DEFAULT_ALERT_THRESHOLDS), "config_valid": True,
                "invalid": []}
    block = config.get("observability") if isinstance(config, dict) else None
    if block is None:
        return settings
    if not isinstance(block, dict):
        settings["config_valid"] = False
        settings["invalid"].append("observability")
        return settings

    sampling = block.get("sampling", {})
    if isinstance(sampling, dict) and not (set(sampling) - {"enabled"}) \
            and sampling.get("enabled", False) in (True, False):
        settings["sampling_enabled"] = sampling.get("enabled", False) is True
    else:
        settings["config_valid"] = False
        settings["invalid"].append("sampling")

    alerts = block.get("alerts", {})
    alerts_valid = isinstance(alerts, dict) and not (set(alerts) - {"enabled", "thresholds"}) \
        and alerts.get("enabled", False) in (True, False)
    thresholds = alerts.get("thresholds", {}) if isinstance(alerts, dict) else None
    if alerts_valid:
        if isinstance(thresholds, dict):
            for name, value in thresholds.items():
                # NaN / inf compare ">= 0" as False, so test finiteness first.
                if name not in DEFAULT_ALERT_THRESHOLDS or isinstance(value, bool) \
                        or not isinstance(value, (int, float)) or not math.isfinite(value) \
                        or value < 0:
                    alerts_valid = False
                    break
                settings["thresholds"][name] = value
        else:
            alerts_valid = False
    if alerts_valid:
        settings["alerts_enabled"] = alerts.get("enabled", False) is True
    else:
        settings["config_valid"] = False
        settings["invalid"].append("alerts")
        settings["thresholds"] = dict(DEFAULT_ALERT_THRESHOLDS)
    return settings


# -------- full-ledger aggregates (no sampling) -------- #

def aggregate_attempts(store: Any) -> Dict[str, Any]:
    """Every invocation attempt in the ledger, in one GROUP BY.

    Falls back to per-task reads only for a store that exposes no SQLite
    connection; ``scope.complete`` then says so instead of implying a total.
    """
    states = {"succeeded": 0, "failed_known": 0, "outcome_unknown": 0,
              "reconciled": 0, "outstanding": 0}
    result = {"states": states, "reconciliations_waiting": 0, "providers": {},
              "with_error": 0, "unrecognized": 0,
              "scope": {"complete": False, "source": "per_task_fallback"}}
    conn = getattr(store, "_conn", None)
    rows = None
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT state, provider, (reconciled_at <> '') AS reconciled, COUNT(*) AS n, "
                "SUM(CASE WHEN error <> '' THEN 1 ELSE 0 END) AS with_error "
                "FROM task_invocation_attempts "
                "GROUP BY state, provider, (reconciled_at <> '')").fetchall()
            result["scope"] = {"complete": True, "source": "sqlite_full_ledger"}
        except Exception:  # noqa: BLE001 — fall through to the per-task reads
            rows = None
    if rows is None:
        collected: Dict[Any, List[Any]] = {}
        for task in store.list_tasks():
            for attempt in store.invocation_attempts(str(task.get("task_id") or "")):
                collected.setdefault((str(attempt.get("state") or ""),
                                      str(attempt.get("provider") or ""),
                                      bool(attempt.get("reconciled_at"))), []).append(attempt)
        rows = [(key[0], key[1], key[2], len(items),
                 sum(1 for item in items if item.get("error")))
                for key, items in collected.items()]
    for state, provider, reconciled, count, with_error in rows:
        state = str(state or "")
        provider = str(provider or "")
        if state in (ATTEMPT_PREPARED, ATTEMPT_DISPATCHED):
            states["outstanding"] += count
        elif state in states:
            states[state] += count
        else:
            result["unrecognized"] += count
        if state == ATTEMPT_OUTCOME_UNKNOWN and not reconciled:
            result["reconciliations_waiting"] += count
        if provider:
            label = safe_label(provider)
            result["providers"][label] = result["providers"].get(label, 0) + count
        result["with_error"] += int(with_error or 0)
    result["providers"] = dict(sorted(result["providers"].items()))
    return result


def event_type_counts(store: Any) -> Dict[str, Any]:
    """Outbox event counts by type over the WHOLE outbox."""
    conn = getattr(store, "_conn", None)
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT event_type, COUNT(*) AS n FROM task_outbox GROUP BY event_type").fetchall()
            return {"counts": {safe_label(r[0]): int(r[1]) for r in rows},
                    "scanned": sum(int(r[1]) for r in rows), "complete": True}
        except Exception:  # noqa: BLE001
            pass
    events = store.events(limit=MAX_EVENTS)
    counts: Dict[str, int] = {}
    for event in events:
        name = safe_label(event.get("event_type") or "")
        counts[name] = counts.get(name, 0) + 1
    summary = getattr(store, "summary", None)
    total = int((summary() or {}).get("outbox_events") or 0) if callable(summary) else None
    # Without a total to compare against the scan cannot claim completeness.
    return {"counts": counts, "scanned": len(events),
            "complete": total is not None and len(events) >= total and total < MAX_EVENTS}


# -------- query ledgers (intake binding + JSONL audit projection) -------- #

def read_query_ledgers(config: Any = None) -> Dict[str, Any]:
    """Read both query records ONCE, reporting exactly what was found.

    - ``state``: the JSONL audit ledger — not_configured / missing /
      unreadable / partially_unreadable / readable_empty / readable. A file
      with no parseable record is ``unreadable`` even when it is non-empty,
      and a file with SOME bad lines is ``partially_unreadable``: only the
      last two states mean the query counts below mean anything, so a
      corrupt ledger can never present 0 as ledger evidence.
    - ``intake_state``: the connector intake (reservation -> consumption
      binding), the authority that ties a query to the task that consumed
      it. Reported with the same vocabulary.

    Reading never creates a file. A WAL database normally leaves no
    ``-wal``/``-shm`` behind once the last connection closes, and opening it
    read-only would CREATE those sidecars — so an intake with no pending
    WAL is opened ``immutable`` (works on a read-only mount too, touches
    nothing). An intake that still has committed frames in a live ``-wal``
    needs a real read-only open to see them; if that is impossible (for
    example a read-only mount without the shared-memory file) the immutable
    fallback is used and ``intake_wal_pending`` says the WAL was not seen.
    """
    read: Dict[str, Any] = {"state": LEDGER_NOT_CONFIGURED, "detail": "",
                            "records": [], "unreadable_lines": 0, "line_count": 0,
                            "intake_state": LEDGER_NOT_CONFIGURED, "intake_detail": "",
                            "intake_wal_pending": False, "consumptions": [], "intents": {}}
    if not isinstance(config, dict):
        return read
    import sqlite3

    from .sources import kimi_adapter

    ledger_path = kimi_adapter._ledger_path(config)
    if not ledger_path.is_file():
        read["state"] = LEDGER_MISSING
        read["detail"] = "query ledger file absent"
    else:
        try:
            with open(ledger_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    read["line_count"] += 1
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        record = None
                    if not isinstance(record, dict):
                        read["unreadable_lines"] += 1
                        continue
                    read["records"].append(record)
            if read["line_count"] == 0:
                read["state"] = LEDGER_READABLE_EMPTY
            elif not read["records"]:
                read["state"] = LEDGER_UNREADABLE
                read["detail"] = "query ledger has no parseable record ({0} bad lines)".format(
                    read["unreadable_lines"])
            elif read["unreadable_lines"]:
                read["state"] = LEDGER_PARTIAL
                read["detail"] = ("query ledger partially unparseable ({0} of {1} lines); "
                                  "counts would be incomplete").format(
                                      read["unreadable_lines"], read["line_count"])
            else:
                read["state"] = LEDGER_READABLE
        except (OSError, UnicodeDecodeError) as exc:
            read["state"] = LEDGER_UNREADABLE
            read["detail"] = "query ledger unreadable: {0}".format(type(exc).__name__)

    intake_path = kimi_adapter._query_store_path(config)
    if not intake_path.is_file():
        read["intake_state"] = LEDGER_MISSING
        read["intake_detail"] = "query intake file absent"
        return read
    try:
        rows = _read_intake(intake_path, read)
    except sqlite3.Error as exc:
        read["intake_state"] = LEDGER_UNREADABLE
        read["intake_detail"] = "query intake unreadable: {0}".format(type(exc).__name__)
        return read
    names, intent_rows, consumption_rows = rows
    if not {"query_intents", "query_consumptions"} <= names:
        read["intake_state"] = LEDGER_READABLE_EMPTY
        read["intake_detail"] = "intake has no query tables yet"
        return read
    read["intents"] = {str(r["intent_id"]): dict(r) for r in intent_rows}
    read["consumptions"] = [dict(r) for r in consumption_rows]
    read["intake_state"] = LEDGER_READABLE if read["intents"] else LEDGER_READABLE_EMPTY
    return read


def _read_intake(intake_path: Path, read: Dict[str, Any]):
    """(table names, intent rows, consumption rows) without creating files."""
    import sqlite3

    wal = Path(str(intake_path) + "-wal")
    wal_pending = wal.exists() and wal.stat().st_size > 0
    base = intake_path.resolve().as_uri()

    def load(uri: str) -> tuple:
        conn = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None,
                               check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            names = {str(r[0]) for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            if not {"query_intents", "query_consumptions"} <= names:
                return names, [], []
            intents = conn.execute("SELECT * FROM query_intents").fetchall()
            consumptions = conn.execute(
                "SELECT * FROM query_consumptions ORDER BY consumed_at, intent_id").fetchall()
            return names, intents, consumptions
        finally:
            conn.close()

    if not wal_pending:
        return load(base + "?mode=ro&immutable=1")
    try:
        return load(base + "?mode=ro")
    except sqlite3.Error:
        # The live WAL could not be read here (e.g. a read-only mount without
        # its shared-memory file): fall back to the main file and SAY so.
        read["intake_wal_pending"] = True
        read["intake_detail"] = "query intake WAL not visible from this reader; main file only"
        return load(base + "?mode=ro&immutable=1")


def queries_for_task(task_id: str, read: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The queries a task consumed, by stable correlation.

    ``consumption_binding`` — the intake row that binds intent -> task (the
    authority written by the governed query path); enriched with the JSONL
    audit projection by ``intent_id`` / ``request_id``.
    ``ledger_task_id`` — legacy JSONL records that carry the task id
    directly and predate the intake binding.
    """
    task_id = str(task_id or "")
    records = read.get("records") or []
    intents = {str(r.get("request_id") or ""): r for r in records
               if r.get("record_type") == "query_intent"}
    results: Dict[str, Dict[str, Any]] = {}
    for record in records:
        if record.get("record_type") == "query_result":
            for key in (str(record.get("request_id") or ""), str(record.get("intent_id") or "")):
                if key:
                    results[key] = record
    intake_intents = read.get("intents") or {}
    queries: List[Dict[str, Any]] = []
    seen = set()

    for binding in read.get("consumptions") or []:
        if str(binding.get("task_id") or "") != task_id:
            continue
        intent_id = str(binding.get("intent_id") or "")
        intake = intake_intents.get(intent_id) or {}
        request_id = str(intake.get("request_id") or "")
        audit = intents.get(request_id) or {}
        result = results.get(intent_id) or results.get(request_id) or {}
        seen.add(intent_id)
        seen.add(request_id)
        queries.append({
            "request_id": request_id, "intent_id": intent_id, "link": "consumption_binding",
            "source_id": str(intake.get("source_id") or audit.get("source_id") or ""),
            "api_id": str(audit.get("api_id") or ""),
            "data_kind": str(audit.get("data_kind") or ""),
            "supply_status": str(binding.get("supply_status") or ""),
            "outcome": str(result.get("outcome") or intake.get("outcome") or "unknown"),
            "cost_status": str(result.get("cost_status") or "unknown"),
            "created_at": str(intake.get("created_at") or audit.get("created_at") or ""),
            "consumed_at": str(binding.get("consumed_at") or ""),
        })

    for request_id, audit in intents.items():
        if request_id in seen or str(audit.get("task_id") or "") != task_id:
            continue
        result = results.get(request_id) or {}
        queries.append({
            "request_id": request_id,
            "intent_id": str(result.get("intent_id") or ""),
            "link": "ledger_task_id",
            "source_id": str(audit.get("source_id") or ""),
            "api_id": str(audit.get("api_id") or ""),
            "data_kind": str(audit.get("data_kind") or ""),
            "supply_status": "",
            "outcome": str(result.get("outcome") or "unknown") if result else "unknown",
            "cost_status": str(result.get("cost_status") or "unknown") if result else "unknown",
            "created_at": str(audit.get("created_at") or ""),
            "consumed_at": "",
        })
    return queries


def ledger_unknown_items(read: Dict[str, Any]) -> List[str]:
    unknown = []
    if read.get("intake_wal_pending"):
        unknown.append("query_consumption_binding_wal_pending")
    if read.get("state") not in _LEDGER_READ_STATES:
        unknown.append("data_query_ledger")
    if read.get("intake_state") not in _LEDGER_READ_STATES:
        unknown.append("query_consumption_binding")
    return unknown


# -------- trace -------- #

def _attempt_order(attempt: Dict[str, Any]) -> tuple:
    """Execution order of one task's attempts.

    The store reader orders by ``started_at`` (whole seconds) and then by a
    random id, so two attempts started in the same second come back in
    arbitrary order. The fencing token increases with every claim, so
    (claim, round, start, id) is the real sequence across worker runs.
    """
    return (int(attempt.get("fencing_token") or 0), int(attempt.get("round_index") or 0),
            str(attempt.get("started_at") or ""), str(attempt.get("attempt_id") or ""))


def build_trace(store: Any, task_id: str, *, config: Any = None,
                ledger: Optional[List[Dict[str, Any]]] = None,
                query_read: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One task's stable correlation chain, read from the authoritative rows.

    Business identity (``case_id`` / ``business_intent_id`` — what was asked)
    is reported apart from platform execution ids (``task_platform_runs`` —
    which Dify/n8n runs drove it), so a retried or replayed platform run is
    seen as the same intent with a second execution. Carries identifiers,
    states, timestamps and error codes only — never payloads, prompts, model
    output or error text.

    ``ledger`` (a raw record list) is the pre-intake call form: it supplies
    the JSONL audit projection and leaves the intake binding unread.
    ``query_read`` (from :func:`read_query_ledgers`) is shared with the
    reconciliation so both report the same availability.
    """
    with _consistent_read(store):
        return _build_trace(store, task_id, config, ledger, query_read)


def _build_trace(store: Any, task_id: str, config: Any, ledger, query_read) -> Dict[str, Any]:
    task = store.get_task(str(task_id))
    read = dict(query_read) if query_read is not None else (
        {"state": LEDGER_READABLE if ledger else LEDGER_READABLE_EMPTY, "records": list(ledger or []),
         "intake_state": LEDGER_MISSING, "consumptions": [], "intents": {}}
        if ledger is not None else read_query_ledgers(config))
    queries = queries_for_task(task_id, read)

    attempts = [{
        "attempt_id": str(a.get("attempt_id") or ""),
        "round_index": a.get("round_index"),
        "worker_id": str(a.get("worker_id") or ""),
        "fencing_token": a.get("fencing_token"),
        # Raw on purpose: the labelling pass turns both into the SAME safe
        # label the aggregate uses, so detail and counts reconcile.
        "provider": str(a.get("provider") or ""),
        "request_model": str(a.get("request_model") or ""),
        "state": str(a.get("state") or ""),
        "sent": str(a.get("sent") or ""),
        "reconciled": bool(a.get("reconciled_at")),
        "reconciliation": str(a.get("reconciliation") or ""),
        "started_at": str(a.get("started_at") or ""),
        "ended_at": str(a.get("ended_at") or ""),
        "error_code": error_code(a.get("error")),
    } for a in sorted(store.invocation_attempts(str(task_id)), key=_attempt_order)]


    events = [{"sequence": e.get("sequence"), "event_id": str(e.get("event_id") or ""),
               "event_type": str(e.get("event_type") or ""),
               "created_at": str(e.get("created_at") or "")}
              for e in store.events(task_id=str(task_id), limit=MAX_EVENTS)]

    platform_runs = [{
        "platform": str(r.get("platform") or ""),
        "run_id": str(r.get("run_id") or ""),
        "workflow_version": str(r.get("workflow_version") or ""),
        "actor": str(r.get("actor") or ""),
        "recorded_at": str(r.get("recorded_at") or ""),
    } for r in store.list_platform_runs(str(task_id))]

    # Outcome kinds in round order: a non-final known failure is a retry;
    # reconciliation is its own visible terminal (not folded into unknown).
    kinds = []
    for index, attempt in enumerate(attempts):
        state = attempt["state"]
        last = index == len(attempts) - 1
        if state == ATTEMPT_RECONCILED or attempt["reconciled"]:
            kinds.append("reconciled")
        elif state == ATTEMPT_OUTCOME_UNKNOWN:
            kinds.append("unknown")
        elif state == ATTEMPT_FAILED_KNOWN:
            kinds.append("failed_known" if last else "retry")
        elif state in (ATTEMPT_PREPARED, ATTEMPT_DISPATCHED):
            kinds.append("outstanding")
        else:
            kinds.append("success" if state == ATTEMPT_SUCCEEDED else state)
    return _safe_tree({
        "schema_version": TRACE_SCHEMA,
        "generated_at": utc_now().isoformat(timespec="seconds"),
        "task": {
            "task_id": str(task.get("task_id") or ""),
            "status": str(task.get("status") or ""),
            "version": task.get("version"),
            "attempt_count": task.get("attempt_count"),
            "max_attempts": task.get("max_attempts"),
            "input_version": task.get("input_version"),
            "role_id": str(task.get("role_id") or ""),
            "execution_mode": str(task.get("execution_mode") or ""),
            "worker_id": str(task.get("worker_id") or ""),
            "lease_expires_at": str(task.get("lease_expires_at") or ""),
            "created_at": str(task.get("created_at") or ""),
            "updated_at": str(task.get("updated_at") or ""),
            "last_error_code": error_code(task.get("last_error")),
        },
        # What was asked: stable across retries and platform replays.
        "business_identity": {
            "case_id": str(task.get("case_id") or ""),
            "business_intent_id": str(task.get("business_intent_id") or ""),
            "parent_task_id": str(task.get("parent_task_id") or ""),
        },
        # Which executions answered: volatile, one per platform retry.
        "platform_runs": platform_runs,
        "platform_run_count": len(platform_runs),
        "attempts": attempts,
        "queries": queries,
        "events": events,
        "coverage": {
            "attempts": len(attempts), "queries": len(queries),
            "events": len(events),
            "outcomes": sorted(set(kinds)),
            "query_ledger_state": read.get("state"),
            "query_ledger_available": read.get("state") in _LEDGER_READ_STATES,
            "query_intake_state": read.get("intake_state"),
            "unknown_items": ledger_unknown_items(read),
        },
        "side_effects": "none",
        "projection_note": "仅标识/状态/时间/稳定错误码；不含输入输出正文、提示词或错误原文",
        "label_note": LABEL_NOTE,
    })


# -------- reconciliation (view vs authoritative ledgers) -------- #

def build_reconciliation(store: Any, *, config: Any = None,
                         now: Optional[datetime] = None,
                         ledger: Optional[List[Dict[str, Any]]] = None,
                         query_read: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Line up the observation view with the ledgers it claims to describe.

    Every number carries its evidence class: ``ledger`` (counted from an
    authoritative row), ``replay`` (synthetic execution that never reached a
    supplier), ``estimated`` (no estimator exists, so the value stays
    ``unknown`` rather than being invented).
    """
    with _consistent_read(store):
        return _build_reconciliation(store, config, now, ledger, query_read)


def _build_reconciliation(store: Any, config: Any, now, ledger, query_read) -> Dict[str, Any]:
    moment = now or utc_now()
    # ONE aggregate read, shared by the status and every section below.
    attempt_ledger = aggregate_attempts(store)
    status = build_observability_status(store, now=moment, attempt_ledger=attempt_ledger)
    technical, business = status["technical"], status["business"]
    tasks = list(store.list_tasks())
    summary = store.summary() or {}
    if query_read is None:
        query_read = ({"state": LEDGER_READABLE if ledger else LEDGER_READABLE_EMPTY,
                       "records": list(ledger or []), "intake_state": LEDGER_MISSING,
                       "consumptions": [], "intents": {}} if ledger is not None
                      else read_query_ledgers(config))
    records = query_read.get("records") or []
    ledger_available = query_read.get("state") in _LEDGER_READ_STATES
    intake_available = query_read.get("intake_state") in _LEDGER_READ_STATES

    events = event_type_counts(store)
    event_counts = events["counts"]
    outbox_total = int(summary.get("outbox_events") or 0)

    query_results = [r for r in records if r.get("record_type") == "query_result"]
    query_outcomes: Dict[str, int] = {}
    for result in query_results:
        outcome = str(result.get("outcome") or "unknown")
        query_outcomes[outcome] = query_outcomes.get(outcome, 0) + 1
    result_keys = {str(r.get("request_id") or "") for r in query_results} | \
                  {str(r.get("intent_id") or "") for r in query_results}
    intents_without_result = sum(
        1 for r in records if r.get("record_type") == "query_intent"
        and str(r.get("request_id") or "") not in result_keys)
    consumptions = query_read.get("consumptions") or []

    by_mode: Dict[str, int] = {}
    for task in tasks:
        mode = str(task.get("execution_mode") or "live")
        by_mode[mode] = by_mode.get(mode, 0) + 1

    attempt_states = technical["invocation_attempts"]
    replay_tasks = sum(n for mode, n in by_mode.items() if mode in REPLAY_EXECUTION_MODES)

    budget = {"calls_today": "unknown", "limit": "unknown", "allowed": "unknown"}
    try:
        from .budget_guard import may_start_model_call

        decision = may_start_model_call(store, config or {}, now=moment)
        budget = {"calls_today": decision["calls_today"], "limit": decision["limit"],
                  "allowed": bool(decision["allowed"])}
    except Exception:  # noqa: BLE001 — budget unreadable stays visible as unknown
        pass

    unknown_items = {"estimated_usage", "real_supplier_usage"}
    unknown_items.update(ledger_unknown_items(query_read))
    if not attempt_ledger["scope"]["complete"]:
        unknown_items.add("invocation_attempts_total")
    if not events["complete"]:
        unknown_items.add("outbox_event_type_counts")

    return _safe_tree({
        "schema_version": RECONCILIATION_SCHEMA,
        "generated_at": moment.isoformat(timespec="seconds"),
        "technical": technical,
        "business": business,
        "authoritative": {
            "task_status_counts": {safe_label(k): int(v)
                                    for k, v in dict(summary.get("task_counts") or {}).items()},
            "outbox_events": outbox_total,
            "consumption": store.consumption_summary(),
            "event_type_counts": dict(sorted(event_counts.items())),
            "event_scan": {"scanned": events["scanned"], "total": outbox_total,
                           "complete": events["complete"]},
        },
        "queue": {
            "depth": {"value": technical["queue_depth"], "evidence": "ledger"},
            "oldest_age_seconds": {"value": technical["oldest_queued_age_seconds"],
                                   "evidence": "ledger"},
            "inflight_leases": {"value": technical["inflight_leases"], "evidence": "ledger"},
            "stale_leases": {"value": technical["stale_leases"], "evidence": "ledger"},
        },
        "invocations": {
            "states": attempt_states,
            "by_provider": attempt_ledger["providers"],
            "reconciliations_waiting": technical["reconciliations_waiting"],
            "known_failures": attempt_states["failed_known"],
            "unknown_open": attempt_states["outcome_unknown"],
            "unknown_reconciled": attempt_states["reconciled"],
            "scope": attempt_ledger["scope"],
        },
        "rejections": {
            "budget_blocked_tasks": business["gate_counts"][GATE_BUDGET],
            "governance_blocked_tasks": business["gate_counts"][GATE_GOVERNANCE],
            "query_artifact_rejections": sum(
                1 for r in query_results for p in (r.get("artifact_plans") or [])
                if isinstance(p, dict) and p.get("remote_name_rejected")),
            "rejected_events": {k: v for k, v in sorted(event_counts.items()) if "reject" in k},
        },
        "evidence": {
            "missing_evidence_blocked_tasks": business["gate_counts"][GATE_EVIDENCE_QUALITY],
            "attempts_with_error": attempt_ledger["with_error"],
            "quarantined_evidence_events": event_counts.get("evidence_quarantined", 0),
        },
        "usage": {
            "ledger": {
                "model_calls_today": budget["calls_today"],
                "daily_limit": budget["limit"],
                "budget_allows_next_call": budget["allowed"],
                "attempts_by_state": attempt_states,
                "data_query_intents": sum(1 for r in records if r.get("record_type") == "query_intent"),
                "data_query_results": len(query_results) if ledger_available else "unknown",
                "query_consumptions_bound": len(consumptions) if intake_available else "unknown",
                "cost_unknown_results": sum(
                    1 for r in query_results if str(r.get("cost_status") or "") in ("", "unknown")),
            },
            "replay": {"tasks_in_replay_mode": replay_tasks,
                       "tasks_by_execution_mode": by_mode,
                       "note": "回放任务不出网、不计真实费用；不计入真实供应商用量"},
            "estimated": {"status": "unknown",
                          "reason": "仓库没有估算账本；不编造数字，保持 unknown"},
            "real_supplier": {
                "status": "unknown",
                "reason": "真实供应商用量需供应商回执；本次仅合成/回放，保持 unknown"},
        },
        "resources": {
            "task_db_bytes": _db_bytes(store),
            "process_rss_kb": _rss_kb(),
            "store_read_only": bool(getattr(store, "read_only", False)),
        },
        "queries": {
            "ledger_state": query_read.get("state"),
            "ledger_available": ledger_available,
            "ledger_detail": query_read.get("detail") or "",
            "ledger_unreadable_lines": query_read.get("unreadable_lines", 0),
            "intake_state": query_read.get("intake_state"),
            "intake_available": intake_available,
            "intake_detail": query_read.get("intake_detail") or "",
            "outcome_counts": query_outcomes if ledger_available else {},
            "intents_without_result": intents_without_result if ledger_available else "unknown",
            "consumptions_bound": len(consumptions) if intake_available else "unknown",
            "note": "ledger_available=false 表示账本未配置/缺失/不可读：计数为 0 不代表没有查询",
        },
        "unknown_items": sorted(unknown_items),
        "side_effects": "none",
        "label_note": LABEL_NOTE,
    })


def _db_bytes(store: Any) -> Optional[int]:
    try:
        return int(Path(store.db_path).stat().st_size)
    except Exception:  # noqa: BLE001
        return None


def _rss_kb() -> Optional[int]:
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS reports bytes, Linux kilobytes.
        return int(usage // 1024 if sys.platform == "darwin" else usage)
    except Exception:  # noqa: BLE001
        return None


# -------- metrics + alert candidates -------- #

def build_metrics(status: Dict[str, Any], reconciliation: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Flat counters; each tagged ledger / replay / unknown so a dashboard or
    alert rule can never sum a replay count into real usage."""
    recon = reconciliation or {}
    usage = recon.get("usage") or {}
    technical = status.get("technical") or {}
    business = status.get("business") or {}
    rows: List[Dict[str, Any]] = []

    def add(name: str, value: Any, evidence: str) -> None:
        rows.append({"name": name, "value": value, "evidence": evidence})

    for key in ("queue_depth", "oldest_queued_age_seconds", "inflight_leases",
                "stale_leases", "reconciliations_waiting"):
        add("stock_agent_" + key, technical.get(key), "ledger")
    for state, count in (technical.get("invocation_attempts") or {}).items():
        add("stock_agent_invocation_attempts{state=\"" + state + "\"}", count, "ledger")
    for gate, count in (business.get("gate_counts") or {}).items():
        add("stock_agent_blocked_tasks{gate=\"" + gate + "\"}", count, "ledger")
    add("stock_agent_blocked_tasks", business.get("blocked_tasks"), "ledger")
    if usage:
        ledger = usage.get("ledger") or {}
        add("stock_agent_model_calls_today", ledger.get("model_calls_today"), "ledger")
        add("stock_agent_tasks_replay_mode",
            (usage.get("replay") or {}).get("tasks_in_replay_mode"), "replay")
        add("stock_agent_usage_estimated", "unknown", "unknown")
        add("stock_agent_usage_real_supplier", "unknown", "unknown")
    return rows


def build_alert_candidates(status: Dict[str, Any], reconciliation: Optional[Dict[str, Any]] = None,
                           *, thresholds: Optional[Dict[str, Any]] = None,
                           alerts_enabled: bool = False) -> Dict[str, Any]:
    """Alert CANDIDATES from the view — never a dispatch. Machine anomalies
    land on the technical layer; business gates stay policy states on the
    business layer and are never raised as pages."""
    limits = dict(DEFAULT_ALERT_THRESHOLDS)
    limits.update({k: v for k, v in (thresholds or {}).items() if k in limits})
    technical = status.get("technical") or {}
    business = status.get("business") or {}
    recon = reconciliation or {}
    queries = recon.get("queries") or {}

    def candidate(rule: str, layer: str, value: Any, threshold: Any, evidence: str,
                  severity: str = "warning") -> Dict[str, Any]:
        return {"rule": rule, "layer": layer, "severity": severity,
                "observed": value, "threshold": threshold, "evidence": evidence,
                "candidate": isinstance(value, (int, float)) and value > threshold}

    # A missing ledger is NOT a zero: the rule cannot fire and says so.
    unknown_queries = not queries.get("ledger_available", False)
    rows = [
        candidate("queue_oldest_age", "technical",
                  technical.get("oldest_queued_age_seconds", 0),
                  limits["oldest_queued_age_seconds"], "ledger"),
        candidate("stale_leases", "technical", technical.get("stale_leases", 0),
                  limits["stale_leases"], "ledger"),
        candidate("reconciliations_waiting", "technical",
                  technical.get("reconciliations_waiting", 0),
                  limits["reconciliations_waiting"], "ledger"),
        candidate("query_outcome_unknown", "technical",
                  "unknown" if unknown_queries else (queries.get("outcome_counts") or {}).get(
                      "outcome_unknown", 0),
                  limits["query_outcome_unknown"], "unknown" if unknown_queries else "ledger"),
        candidate("blocked_tasks", "business", business.get("blocked_tasks", 0), 0,
                  "ledger", "info"),
    ]
    return {
        "schema_version": ALERT_SCHEMA,
        "generated_at": utc_now().isoformat(timespec="seconds"),
        "enabled": bool(alerts_enabled),
        "thresholds": limits,
        "candidates": rows,
        "triggered": [row["rule"] for row in rows if row["candidate"]],
        "dispatched": False,
        "dispatch": "NOT_RUN",
        "note": "仅候选，不发送；生产告警订阅与接收均 NOT_RUN",
    }


# -------- structured log -------- #

def project_log_fields(fields: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], int]:
    """Keep only allow-listed fields whose value is a number or an
    identifier-shaped string; everything else is dropped and only COUNTED."""
    kept: Dict[str, Any] = {}
    dropped = 0
    for name, value in (fields or {}).items():
        try:
            name = str(name)
        except Exception:  # noqa: BLE001
            dropped += 1
            continue
        if name not in LOG_FIELDS:
            dropped += 1
            continue
        if isinstance(value, bool) or isinstance(value, int):
            kept[name] = value
        elif isinstance(value, float):
            kept[name] = value
        elif isinstance(value, str) and _LOG_IDENTIFIER.match(value) \
                and not contains_secret_marker(value):
            kept[name] = value
        else:
            dropped += 1
    return kept, dropped


def emit_observability_log(event: str, fields: Optional[Dict[str, Any]] = None,
                           *, config: Any = None, sink: Any = None) -> Optional[Dict[str, Any]]:
    """One structured JSON line, or None when sampling is off.

    Writes only to ``sink`` (default: stderr) and only when
    ``observability.sampling.enabled`` is true and well formed. The record
    carries the event name plus allow-listed, identifier-shaped fields (see
    :func:`project_log_fields`); dropped fields are counted, never named.
    With sampling off nothing is written and no audit row is touched.
    """
    if not observability_settings(config)["sampling_enabled"]:
        return None
    name = event if isinstance(event, str) and _LOG_IDENTIFIER.match(event) else "invalid_event"
    kept, dropped = project_log_fields(fields)
    record = {"schema_version": LOG_SCHEMA, "event": name,
              "generated_at": utc_now().isoformat(timespec="seconds"),
              "fields": kept, "dropped_fields": dropped}
    if sink is None:
        sink = sys.stderr
    sink.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def build_observability_report(store: Any, *, config: Any = None,
                               task_id: str = "", now: Optional[datetime] = None) -> Dict[str, Any]:
    """Status + reconciliation + metrics + alert candidates (+ one trace)
    from a single read of every ledger, so all parts report the same
    availability."""
    settings = observability_settings(config)
    query_read = read_query_ledgers(config)
    with _consistent_read(store):
        reconciliation = build_reconciliation(
            store, config=config, now=now, query_read=query_read)
        trace = build_trace(store, task_id, query_read=query_read) if task_id else None
    report = {
        "schema_version": RECONCILIATION_SCHEMA,
        "generated_at": reconciliation["generated_at"],
        "status": {"technical": reconciliation["technical"],
                   "business": reconciliation["business"]},
        "reconciliation": reconciliation,
        "metrics": build_metrics(reconciliation, reconciliation),
        "alert_candidates": build_alert_candidates(
            reconciliation, reconciliation, thresholds=settings["thresholds"],
            alerts_enabled=settings["alerts_enabled"]),
        "trace": trace,
        "settings": settings,
        "side_effects": "none",
        "label_note": LABEL_NOTE,
    }
    return _safe_tree(report)
