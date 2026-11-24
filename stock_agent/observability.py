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

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

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


def build_observability_status(store: Any, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Read-only layered status. ``now`` defaults to UTC (persistence clock)."""
    moment = now or utc_now()
    tasks = store.list_tasks()
    queued = [t for t in tasks if t.get("status") == "queued"]
    inflight = [t for t in tasks if t.get("status") in ("leased", "running")]

    queue_ages = [age for age in (_age_seconds(t.get("created_at"), moment)
                                  for t in queued[:MAX_SAMPLE]) if age is not None]
    oldest_queued_age_seconds = max(queue_ages) if queue_ages else 0

    stale_leases = 0
    for task in inflight[:MAX_SAMPLE]:
        expires = _parse(str(task.get("lease_expires_at") or ""))
        if expires is not None and expires <= moment:
            stale_leases += 1

    attempts = {"succeeded": 0, "failed_known": 0, "outcome_unknown": 0,
                "reconciled": 0, "outstanding": 0}
    reconciliations_waiting = 0
    for task in tasks[:MAX_SAMPLE]:
        for attempt in store.invocation_attempts(str(task.get("task_id") or "")):
            state = str(attempt.get("state") or "")
            if state in attempts:
                attempts[state] += 1
            elif state in ("prepared", "dispatched"):
                attempts["outstanding"] += 1
            if state == "outcome_unknown" and not str(attempt.get("reconciled_at") or ""):
                reconciliations_waiting += 1

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
                                   "gate": gate, "error": redact(error),
                                   "updated_at": str(task.get("updated_at") or "")})

    technical = {
        "queue_depth": len(queued),
        "oldest_queued_age_seconds": oldest_queued_age_seconds,
        "inflight_leases": len(inflight),
        "stale_leases": stale_leases,
        "invocation_attempts": attempts,
        "reconciliations_waiting": reconciliations_waiting,
    }
    business = {
        "blocked_tasks": sum(business_gate_counts.values()),
        "gate_counts": business_gate_counts,
        "blocked_sample": blocked_sample,
    }
    return {
        "schema_version": OBSERVABILITY_SCHEMA,
        "generated_at": moment.isoformat(timespec="seconds"),
        "technical": technical,
        "business": business,
        "scope_note": "只读分层状态：technical=机器健康，business=研究门禁；二者独立报告，互不冒充",
        "side_effects": "none",
    }


# --------------------------------------------------------------------------- #
# KONM-96 (R23 continuation): trace, reconciliation, metrics, alert
# candidates, structured log. Same rules as above: projections over the
# authoritative ledgers (task store, invocation attempts, platform runs,
# query ledger); nothing here writes, calls a model or contacts a source.
# --------------------------------------------------------------------------- #
TRACE_SCHEMA = "1.0"
RECONCILIATION_SCHEMA = "1.0"
METRICS_SCHEMA = "1.0"
ALERT_SCHEMA = "1.0"
LOG_SCHEMA = "1.0"

# Sampling (the only new observation that emits anything) is OFF unless the
# config turns it on; turning it off leaves every ledger and audit row intact.
DEFAULT_ALERT_THRESHOLDS = {
    "oldest_queued_age_seconds": 3600,
    "stale_leases": 0,
    "reconciliations_waiting": 0,
    "query_outcome_unknown": 0,
    "dispatched_age_seconds": 600,
}

# Execution modes that never reach a real supplier.
REPLAY_EXECUTION_MODES = ("replay", "dry_run", "simulation")

_SECRET_PATTERNS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\b(?:sk|pk|api|key|token|secret)[-_][A-Za-z0-9_-]{12,}"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|"
        r"authorization|cookie|credential)\b[\"']?\s*[:=]\s*[\"']?[^\s,\"'}]{4,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
_REDACTED = "[REDACTED]"
_ERROR_EXCERPT = 120
MAX_EVENTS = 100000


def redact(value: Any, limit: int = 200) -> str:
    """Mask credential-shaped fragments. Deliberately narrow: identifiers
    (task/attempt/run ids, hashes) must stay traceable."""
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text[:limit]


def contains_secret_marker(text: str, markers=()) -> bool:
    """True when ``text`` still carries a credential-shaped fragment or any
    caller-supplied forbidden marker (drills inject synthetic ones)."""
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        return True
    return any(str(marker) and str(marker) in text for marker in markers)


def observability_settings(config: Any = None) -> Dict[str, Any]:
    """Read the optional ``observability`` config block defensively: a
    malformed block degrades to OFF, never to a business failure."""
    settings = {"sampling_enabled": False, "alerts_enabled": False,
                "thresholds": dict(DEFAULT_ALERT_THRESHOLDS), "config_valid": True}
    raw = (config or {}).get("observability") if isinstance(config, dict) else None
    if raw is None:
        return settings
    if not isinstance(raw, dict):
        settings["config_valid"] = False
        return settings
    settings["sampling_enabled"] = raw.get("sampling", {}).get("enabled") is True \
        if isinstance(raw.get("sampling", {}), dict) else False
    alerts = raw.get("alerts", {})
    if isinstance(alerts, dict):
        settings["alerts_enabled"] = alerts.get("enabled") is True
        thresholds = alerts.get("thresholds", {})
        if isinstance(thresholds, dict):
            for name, value in thresholds.items():
                if name in settings["thresholds"] and isinstance(value, (int, float)) \
                        and not isinstance(value, bool) and value >= 0:
                    settings["thresholds"][name] = value
                else:
                    settings["config_valid"] = False
        else:
            settings["config_valid"] = False
    return settings


# -------- trace -------- #

def _load_query_ledger(config: Any) -> List[Dict[str, Any]]:
    if config is None:
        return []
    from .sources import kimi_adapter

    try:
        return list(kimi_adapter.load_query_ledger(config, limit=100000))
    except Exception:  # noqa: BLE001 — a trace degrades to "ledger unreadable"
        return []


def build_trace(store: Any, task_id: str, *, config: Any = None,
                ledger: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """One task's stable correlation chain, read from the authoritative rows.

    Business identity (``case_id`` / ``business_intent_id`` — what was asked)
    is reported apart from platform execution ids (``task_platform_runs`` —
    which Dify/n8n runs drove it), so a retried or replayed platform run is
    seen as the same intent with a second execution. Payloads, prompts,
    model output and raw errors are never included: identifiers, states and
    timestamps only.
    """
    task = store.get_task(str(task_id))
    records = _load_query_ledger(config) if ledger is None else list(ledger)
    intents = {str(r.get("request_id") or ""): r for r in records if r.get("record_type") == "query_intent"}
    results = {str(r.get("request_id") or ""): r for r in records if r.get("record_type") == "query_result"}

    attempts = [{
        "attempt_id": str(a.get("attempt_id") or ""),
        "round_index": a.get("round_index"),
        "worker_id": str(a.get("worker_id") or ""),
        "fencing_token": a.get("fencing_token"),
        "provider": redact(a.get("provider"), 60),
        "request_model": redact(a.get("request_model"), 80),
        "state": str(a.get("state") or ""),
        "sent": str(a.get("sent") or ""),
        "reconciled": bool(a.get("reconciled_at")),
        "reconciliation": str(a.get("reconciliation") or ""),
        "started_at": str(a.get("started_at") or ""),
        "ended_at": str(a.get("ended_at") or ""),
        "error_excerpt": redact(a.get("error"), _ERROR_EXCERPT),
    } for a in sorted(store.invocation_attempts(str(task_id)),
                       key=lambda a: (int(a.get("round_index") or 0),
                                      str(a.get("started_at") or ""), str(a.get("attempt_id") or "")))]

    queries = []
    for request_id, intent in intents.items():
        if str(intent.get("task_id") or "") != str(task_id):
            continue
        result = results.get(request_id) or {}
        queries.append({
            "request_id": request_id,
            "source_id": str(intent.get("source_id") or ""),
            "api_id": str(intent.get("api_id") or ""),
            "data_kind": str(intent.get("data_kind") or ""),
            "created_at": str(intent.get("created_at") or ""),
            "result_present": bool(result),
            "outcome": str(result.get("outcome") or "unknown") if result else "unknown",
            "cost_status": str(result.get("cost_status") or "unknown") if result else "unknown",
        })

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
    return {
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
            "last_error_excerpt": redact(task.get("last_error"), _ERROR_EXCERPT),
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
            "query_ledger_available": config is not None or ledger is not None,
        },
        "side_effects": "none",
        "redaction_note": "仅标识/状态/时间；不含输入输出正文、提示词或完整错误文本",
    }


# -------- reconciliation (view vs authoritative ledgers) -------- #

def build_reconciliation(store: Any, *, config: Any = None,
                         now: Optional[datetime] = None,
                         ledger: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Line up the observation view with the ledgers it claims to describe.

    Every number carries its evidence class: ``ledger`` (counted from an
    authoritative row), ``replay`` (synthetic execution that never reached a
    supplier), ``estimated`` (no estimator exists, so the value stays
    ``unknown`` rather than being invented).
    """
    moment = now or utc_now()
    status = build_observability_status(store, now=moment)
    technical, business = status["technical"], status["business"]
    tasks = list(store.list_tasks())
    summary = store.summary() or {}
    records = _load_query_ledger(config) if ledger is None else list(ledger)
    ledger_available = config is not None or ledger is not None

    events = store.events(limit=MAX_EVENTS)
    event_counts: Dict[str, int] = {}
    for event in events:
        name = str(event.get("event_type") or "")
        event_counts[name] = event_counts.get(name, 0) + 1
    outbox_total = int(summary.get("outbox_events") or 0)

    query_results = [r for r in records if r.get("record_type") == "query_result"]
    query_outcomes: Dict[str, int] = {}
    for result in query_results:
        outcome = str(result.get("outcome") or "unknown")
        query_outcomes[outcome] = query_outcomes.get(outcome, 0) + 1
    intents_without_result = sum(
        1 for r in records if r.get("record_type") == "query_intent"
        and not any(x.get("request_id") == r.get("request_id") for x in query_results))

    by_mode: Dict[str, int] = {}
    for task in tasks:
        mode = str(task.get("execution_mode") or "live")
        by_mode[mode] = by_mode.get(mode, 0) + 1

    attempt_states = technical["invocation_attempts"]
    providers: Dict[str, int] = {}
    for task in tasks[:MAX_SAMPLE]:
        for attempt in store.invocation_attempts(str(task.get("task_id") or "")):
            key = redact(attempt.get("provider"), 60) or "(none)"
            providers[key] = providers.get(key, 0) + 1
    replay_tasks = sum(n for mode, n in by_mode.items() if mode in REPLAY_EXECUTION_MODES)

    budget = {"calls_today": "unknown", "limit": "unknown", "allowed": "unknown"}
    try:
        from .budget_guard import may_start_model_call

        decision = may_start_model_call(store, config or {}, now=moment)
        budget = {"calls_today": decision["calls_today"], "limit": decision["limit"],
                  "allowed": bool(decision["allowed"])}
    except Exception:  # noqa: BLE001 — budget unreadable stays visible as unknown
        pass

    reconciliation = {
        "schema_version": RECONCILIATION_SCHEMA,
        "generated_at": moment.isoformat(timespec="seconds"),
        "technical": technical,
        "business": business,
        "authoritative": {
            "task_status_counts": dict(summary.get("task_counts") or {}),
            "outbox_events": outbox_total,
            "consumption": store.consumption_summary(),
            "event_type_counts": event_counts,
            "event_scan": {"scanned": len(events), "total": outbox_total,
                           "complete": len(events) >= outbox_total},
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
            "by_provider": providers,
            "reconciliations_waiting": technical["reconciliations_waiting"],
            "known_failures": attempt_states["failed_known"],
            "unknown_open": attempt_states["outcome_unknown"],
            "unknown_reconciled": attempt_states["reconciled"],
        },
        "rejections": {
            "budget_blocked_tasks": business["gate_counts"][GATE_BUDGET],
            "governance_blocked_tasks": business["gate_counts"][GATE_GOVERNANCE],
            "query_artifact_rejections": sum(
                1 for r in query_results for p in (r.get("artifact_plans") or [])
                if p.get("remote_name_rejected")),
            "rejected_events": {k: v for k, v in sorted(event_counts.items()) if "reject" in k},
        },
        "evidence": {
            "missing_evidence_blocked_tasks": business["gate_counts"][GATE_EVIDENCE_QUALITY],
            "failed_attempts_with_error": sum(
                1 for t in tasks[:MAX_SAMPLE] for a in store.invocation_attempts(str(t.get("task_id") or ""))
                if a.get("error")),
            "quarantined_evidence_events": event_counts.get("evidence_quarantined", 0),
        },
        "usage": {
            "ledger": {
                "model_calls_today": budget["calls_today"],
                "daily_limit": budget["limit"],
                "budget_allows_next_call": budget["allowed"],
                "attempts_by_state": attempt_states,
                "data_query_intents": sum(1 for r in records if r.get("record_type") == "query_intent"),
                "data_query_results": len(query_results),
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
                "reason": "真实供应商用量需供应商回执；本次仅合成/回放，保持 unknown",
            },
        },
        "resources": {
            "task_db_bytes": _db_bytes(store),
            "process_rss_kb": _rss_kb(),
            "store_read_only": bool(getattr(store, "read_only", False)),
        },
        "queries": {
            "ledger_available": ledger_available,
            "outcome_counts": query_outcomes,
            "intents_without_result": intents_without_result,
            "note": "query 账本不可读或未提供时计数为 0 且 ledger_available=false，不代表没有查询",
        },
        "unknown_items": sorted({"estimated_usage", "real_supplier_usage"}
                                | ({"data_query_ledger"} if not ledger_available else set())),
        "side_effects": "none",
    }
    return reconciliation


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
    queries = (recon.get("queries") or {}).get("outcome_counts") or {}

    def candidate(rule: str, layer: str, value: Any, threshold: Any, evidence: str,
                  severity: str = "warning") -> Dict[str, Any]:
        return {"rule": rule, "layer": layer, "severity": severity,
                "observed": value, "threshold": threshold, "evidence": evidence,
                "candidate": value > threshold}

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
                  queries.get("outcome_unknown", 0), limits["query_outcome_unknown"], "ledger"),
        candidate("blocked_tasks", "business", business.get("blocked_tasks", 0), 0,
                  "ledger", "info"),
    ]
    fired = [row for row in rows if row["candidate"]]
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


def emit_observability_log(event: str, fields: Optional[Dict[str, Any]] = None,
                           *, config: Any = None, sink: Any = None) -> Optional[Dict[str, Any]]:
    """One structured JSON line, redacted. Returns the record (or None when
    sampling is off); writes only to ``sink`` (default: stderr) and only
    when ``observability.sampling.enabled`` is true. With sampling off the
    caller's business flow is untouched and no audit row is removed."""
    settings = observability_settings(config)
    if not settings["sampling_enabled"]:
        return None
    record = {"schema_version": LOG_SCHEMA, "event": str(event),
              "generated_at": utc_now().isoformat(timespec="seconds"),
              "fields": _redact_tree(fields or {})}
    if sink is None:
        sink = sys.stderr
    sink.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def _redact_tree(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _redact_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_tree(v) for v in value]
    if isinstance(value, str):
        return redact(value, 4000)
    return value


def build_observability_report(store: Any, *, config: Any = None,
                               task_id: str = "", now: Optional[datetime] = None) -> Dict[str, Any]:
    """Status + reconciliation + metrics + alert candidates in one read."""
    settings = observability_settings(config)
    reconciliation = build_reconciliation(store, config=config, now=now)
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
        "trace": build_trace(store, task_id, config=config) if task_id else None,
        "settings": settings,
        "side_effects": "none",
    }
    return report
