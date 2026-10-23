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

from datetime import datetime
from typing import Any, Dict, List, Optional

from .runtime_layout import utc_now

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
                                   "gate": gate, "error": error[:200],
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
