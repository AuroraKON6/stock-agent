"""R22: personal review — a READ-ONLY projection of what a run actually did.

The review answers one question per task, without writing anything and
without trusting labels:

- **Relations**: parent / children / dependency edges, so a fan-out can be
  read as a graph rather than a flat list.
- **Input & rule versions**: ``input_version`` plus the immutable snapshot
  chain, the pinned prompt/model-config versions, and the approved rule
  binding (flow id/version/digest and each role prompt digest).
- **Role & source**: every output names the task and role that produced it;
  citations are checked against the references that were DELIVERED to the
  model, so an uncited or out-of-allowlist reference is visible.
- **State, failures, metering**: status, failure events, attempt ledger,
  replay/live model-call record and the budget reservation.
- **Attribution**: MODEL ROLES, INDEPENDENT EVIDENCE and HUMAN SIGN-OFF are
  three different things. A model role is never counted as a human; the same
  evidence bytes cited by two roles are ONE source, not independent
  corroboration; only a record written by a human-actor identity counts as
  sign-off (an automated actor claiming sign-off is reported as rejected).

Access is the caller's existing perimeter, not a new one: the identity must
carry the ``read_research`` scope, its ``case_scope`` must cover the task's
case, and a personally-created task (server-side owner binding on the root)
is readable ONLY by that owner. Everything else is refused fail-closed.

This module performs no writes: it uses the store's read APIs only. Callers
that need a hard guarantee can hand in a ``TaskStore(..., read_only=True)``.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .dify_access import DifyAccessError, ensure_case_scope, ensure_scope
from .research_runtime import AUTOMATED_ACTOR_PREFIXES
from .role_execution import binding_of
from .subtask_graph import ALLOWED_SUBTASK_ROLES
from .task_runtime.store import TaskStore, TaskRuntimeError

REVIEW_SCHEMA = "1.0"
REVIEW_SCOPE = "read_research"

# Automated actors that predate / sit outside the shared prefix list.  These
# are orchestrator identities, never humans: a review must not be able to
# launder one of them into "human sign-off".
_EXTRA_AUTOMATED_ACTORS = frozenset({
    "subtask_graph", "role-orchestrator", "role_orchestrator",
    "system", "system:recovery", "full-chain-worker",
})

_FAILURE_EVENT = ("failed", "blocked", "cancel", "outcome_unknown", "error")


class ReviewAccessError(ValueError):
    """reason codes: identity_required, scope_not_granted, case_out_of_scope,
    personal_owner_required, personal_owner_mismatch, unknown_task"""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(": ".join(item for item in (reason, detail) if item))
        self.reason = reason
        self.detail = detail


# ---------------------------------------------------------------------- #
# attribution rules                                                      #
# ---------------------------------------------------------------------- #

def _normalize_actor(actor: Any) -> str:
    return str(actor or "").strip().lower().replace("-", "_").replace(" ", "_")


def actor_kind(actor: Any) -> str:
    """``human`` or ``automated`` — the ONE rule the projection classifies by.

    A model role id, an orchestrator, a service prefix and an empty actor are
    all automated. Only a named actor outside those sets counts as human.
    Service identities carry a ``:<credential>`` suffix (the role API writes
    ``role-orchestrator:<credential_id>``), so the BASE identity decides the
    kind — a suffix must not launder an orchestrator into a human reviewer."""
    normalized = _normalize_actor(actor)
    if not normalized:
        return "automated"
    base = normalized.split(":", 1)[0]
    if normalized in ALLOWED_SUBTASK_ROLES or base in ALLOWED_SUBTASK_ROLES:
        return "automated"
    if normalized in _EXTRA_AUTOMATED_ACTORS or base in _EXTRA_AUTOMATED_ACTORS:
        return "automated"
    if any(base.startswith(prefix) for prefix in AUTOMATED_ACTOR_PREFIXES):
        return "automated"
    return "human"


def producer_kind(task: Dict[str, Any]) -> str:
    """How an output row was produced — never inferred from prose."""
    payload = task.get("input_payload") if isinstance(task.get("input_payload"), dict) else {}
    role = str(task.get("role_id") or "")
    origin = payload.get("origin") if isinstance(payload.get("origin"), dict) else {}
    if isinstance(payload.get("role_step"), dict) or role in ALLOWED_SUBTASK_ROLES:
        return "model_role"
    if str(origin.get("kind") or "") == "subtask":
        return "model_role"
    return "model_single_role"


# ---------------------------------------------------------------------- #
# access                                                                 #
# ---------------------------------------------------------------------- #

def _root_task(store: TaskStore, task: Dict[str, Any]) -> Dict[str, Any]:
    """Walk the parent chain (defensively bounded) and return the root."""
    seen = {str(task.get("task_id") or "")}
    current = task
    while str(current.get("parent_task_id") or ""):
        parent_id = str(current["parent_task_id"])
        if parent_id in seen:
            break
        seen.add(parent_id)
        try:
            current = store.get_task(parent_id)
        except TaskRuntimeError:
            break
    return current


def ensure_review_access(store: TaskStore, identity: Dict[str, Any],
                         task: Dict[str, Any]) -> Dict[str, Any]:
    """Refuse a read the calling identity is not entitled to (fail-closed)."""
    if not isinstance(identity, dict):
        raise ReviewAccessError("identity_required")
    try:
        ensure_scope(identity, REVIEW_SCOPE)
    except DifyAccessError as exc:
        raise ReviewAccessError("scope_not_granted", exc.detail) from exc
    case_id = str(task.get("case_id") or "")
    try:
        ensure_case_scope(identity, case_id)
    except DifyAccessError as exc:
        raise ReviewAccessError("case_out_of_scope", exc.detail) from exc
    root = _root_task(store, task)
    binding = store.input_owner_binding(str(root.get("task_id") or ""))
    if binding:
        actor = str(identity.get("actor_id") or "").strip()
        if not actor:
            raise ReviewAccessError(
                "personal_owner_required",
                "task {0} is personally owned; the identity must name its actor".format(
                    root.get("task_id")))
        if actor != str(binding.get("owner") or ""):
            raise ReviewAccessError(
                "personal_owner_mismatch",
                "task {0} belongs to another personal namespace".format(root.get("task_id")))
    return {"actor_id": str(identity.get("actor_id") or ""),
            "credential_id": str(identity.get("credential_id") or ""),
            "case_id": case_id, "personal_owner": (binding or {}).get("owner", "")}


# ---------------------------------------------------------------------- #
# per-task projection                                                    #
# ---------------------------------------------------------------------- #

def _sha256_json(payload: Any) -> str:
    text = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _task(case: TaskStore, task_id: str) -> Optional[Dict[str, Any]]:
    try:
        return case.get_task(str(task_id))
    except TaskRuntimeError:
        return None


def _children(store: TaskStore, parent_id: str) -> List[Dict[str, Any]]:
    return [item for item in store.list_tasks()
            if str(item.get("parent_task_id") or "") == str(parent_id)]


def _round_index(item: Dict[str, Any]) -> int:
    """The round an invocation row belongs to (``0`` when absent/unusable)."""
    try:
        return int((item or {}).get("round_index") or 0)
    except (TypeError, ValueError):
        return 0


def _delivery_rounds(model_call: Dict[str, Any],
                     attempts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per-round delivery credentials: context round ↔ invocation attempt.

    A context is bound to the round it was prepared for (the worker's
    ``model_call.context_rounds``), and that round to the invocation row the
    attempt chain wrote for the same round. ``sent`` is the store's STRUCTURED
    statement (``yes``/``no``/``unknown``) — never the worker's global
    ``called`` flag, which only says that SOME round returned."""
    by_round: Dict[int, List[Dict[str, Any]]] = {}
    by_id: Dict[str, Dict[str, Any]] = {}
    for attempt in attempts:
        by_id[str(attempt.get("attempt_id") or "")] = attempt
        try:
            index = int(attempt.get("round_index") or 0)
        except (TypeError, ValueError):
            index = 0
        by_round.setdefault(index, []).append(attempt)
    records = model_call.get("context_rounds")
    rounds: List[Dict[str, Any]] = []
    if isinstance(records, list) and records:
        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                index = int(record.get("round") or 0)
            except (TypeError, ValueError):
                index = 0
            attempt = by_id.get(str(record.get("attempt_id") or ""))
            if attempt is None:
                # No explicit link: require the SAME LEASE. Round index and
                # second-level time are not identity — a requeued task can
                # start the same round in the same second under a new fencing
                # token. A record without lease binding matches nothing.
                worker_id = str(record.get("worker_id") or "")
                token = record.get("fencing_token")
                if worker_id and token is not None:
                    candidates = [
                        item for item in by_round.get(index, [])
                        if str(item.get("worker_id") or "") == worker_id
                        and int(item.get("fencing_token") or 0) == int(token)]
                    attempt = candidates[-1] if candidates else None
            rounds.append({"round": index, "context": record, "attempt": attempt})
        return rounds
    # Legacy/other writers: no per-round context records — and therefore NO
    # verifiable binding between the context bytes the record carries and any
    # invocation row. Their round records only name the attempt that returned;
    # the top-level ``context_delivery``/``outbound_context`` is overwritten on
    # every round, including a round whose budget was refused before an
    # attempt row existed. Bounding "the latest round's send" to that record
    # would therefore attribute a later, never-sent reference set to an earlier
    # successful send. Nothing is bound here: the ledger stays visible through
    # ``attempts`` and the caller classifies the context from it, never as
    # delivered.
    return rounds


def _sent_state(sent: str) -> Tuple[str, bool]:
    """Map the structured ledger value to (delivery_state, authoritative)."""
    value = str(sent or "")
    if value == "yes":
        return "sent", True
    if value == "unknown":
        return "send_unknown", False
    return "prepared_only", False


def _delivery(payload: Dict[str, Any],
              model_call: Dict[str, Any],
              attempts: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """What the worker ACTUALLY delivered to the model, not what was configured.

    The authoritative record is the worker's own outbox ledger
    (``model_call.context_delivery``): ``delivered_evidence`` lists the refs
    that entered the outbound context, ``dropped_evidence`` records the refs
    refused before egress (``<ref>:<reason>``). But a PREPARED context is not
    a delivery: the ledger exists from the moment egress is built, including
    on attempts that never produced an answer.

    The execution credential is therefore read from the PER-ROUND invocation
    ledger, not from the global ``called`` flag: a round whose attempt is
    recorded ``sent=yes`` delivered its context even when it ended in a
    deterministic provider failure (401), and the LAST round that may have
    reached the provider with no answer stays ``send_unknown`` even though an
    earlier round succeeded. A context prepared for a round with no attempt
    row never left, and is reported ``prepared_only``.

    When the task never executed there is no ledger; then nothing counts as
    delivered and the configured refs are listed as unverified."""
    context = payload.get("public_context") if isinstance(payload.get("public_context"), dict) else {}
    evidence = context.get("evidence_summaries")
    configured = sorted(str(ref) for ref in evidence) if isinstance(evidence, dict) else []
    rounds = _delivery_rounds(model_call, list(attempts or []))
    latest = rounds[-1] if rounds else None
    if latest is not None and isinstance(latest.get("context"), dict):
        delivery = latest["context"].get("delivery")
    else:
        delivery = model_call.get("context_delivery")
    delivery = delivery if isinstance(delivery, dict) else {}
    delivered_refs = delivery.get("delivered_evidence")
    error = str(model_call.get("error") or "").lower()
    if latest is not None and latest.get("attempt") is not None:
        state, authoritative = _sent_state(str(latest["attempt"].get("sent") or ""))
    elif latest is not None:
        # The latest prepared context has no invocation row: nothing was sent.
        state, authoritative = "prepared_only", False
    elif attempts:
        # A ledger exists but nothing binds it to the current context (a
        # legacy record after a requeue, an earlier round's record, or a run
        # that stopped before any round). Do not fall back to ``called``. The
        # LATEST round still says what happened to the latest prepared
        # context: an ``unknown`` send there stays send_unknown (it may have
        # reached the provider), anything else is unverified and counts as not
        # delivered. Round index, not row order — attempt times have second
        # resolution and same-second rows are not orderable.
        latest_round = max(_round_index(item) for item in attempts)
        at_latest = [item for item in attempts if _round_index(item) == latest_round]
        if any(str(item.get("sent") or "") == "unknown" for item in at_latest):
            state, authoritative = "send_unknown", False
        else:
            state, authoritative = "prepared_only", False
    elif str(model_call.get("replay_source") or ""):
        state, authoritative = "replayed", True
    elif bool(model_call.get("called")):
        state, authoritative = "sent", True
    elif "outcome unknown" in error:
        state, authoritative = "send_unknown", False
    elif model_call:
        state, authoritative = "prepared_only", False
    else:
        state, authoritative = "no_execution_record", False
    outbound = model_call.get("outbound_context")
    outbound = outbound if isinstance(outbound, dict) else {}
    outbound_evidence = outbound.get("evidence_summaries")
    outbound_evidence = outbound_evidence if isinstance(outbound_evidence, dict) else {}
    namespaces = delivery.get("evidence_namespaces")
    namespaces = namespaces if isinstance(namespaces, dict) else {}
    dropped: Dict[str, str] = {}
    for raw in delivery.get("dropped_evidence") or []:
        ref, _, reason = str(raw).partition(":")
        dropped.setdefault(ref, reason or "dropped_before_egress")
    delivered: Dict[str, Dict[str, Any]] = {}
    admitted_refs = (delivered_refs or []) if authoritative else []
    for raw_ref in admitted_refs:
        ref = str(raw_ref)
        info = outbound_evidence.get(ref)
        info = info if isinstance(info, dict) else {}
        delivered[ref] = {
            "sha256": str(info.get("sha256") or ""),
            "summary": str(info.get("summary") or "")[:200],
            "vault_verified": bool(info.get("vault_verified")),
            "namespace_authorized": str(info.get("namespace_authorized")
                                        or namespaces.get(ref) or ""),
        }
    undelivered: List[Dict[str, Any]] = []
    for ref in configured:
        if ref in delivered:
            continue
        if ref in dropped:
            reason = dropped[ref]
        elif not authoritative:
            reason = state
        else:
            reason = "not_delivered_by_execution"
        undelivered.append({"ref": ref, "reason": reason})
    per_round = []
    for item in rounds:
        attempt = item.get("attempt") or {}
        round_state, round_authoritative = (
            _sent_state(str(attempt.get("sent") or "")) if attempt
            else ("prepared_only", False))
        record = item.get("context") if isinstance(item.get("context"), dict) else {}
        per_round.append({
            "round": item["round"],
            "attempt_id": str(attempt.get("attempt_id") or ""),
            "sent": str(attempt.get("sent") or ""),
            "state": round_state,
            "authoritative": round_authoritative,
            "outbound_context_sha256": str(record.get("outbound_context_sha256") or ""),
        })
    return {"delivered": delivered, "configured": configured,
            "undelivered": undelivered, "authoritative": authoritative,
            "state": state,
            "rounds": per_round,
            "source": "worker_context_delivery" if authoritative else state}


def _signoffs(store: TaskStore, task: Dict[str, Any]) -> Tuple[
        List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]],
        List[Dict[str, Any]]]:
    """Human sign-off records vs automated claims refused as such.

    ONLY a trusted ``personal_acknowledgement`` EVENT written by the store is
    a sign-off. The output field of the same name is just task data — a
    producer can write any JSON into it — so a field without the matching
    event is reported as an unverified claim, never as human attribution.
    ``human_response_recorded`` events for ``provide_input`` / ``reject`` are
    not acknowledgements of the result; they stay in ``human_decisions``."""
    human: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    decisions: List[Dict[str, Any]] = []
    untrusted: List[Dict[str, Any]] = []
    output = task.get("output") if isinstance(task.get("output"), dict) else {}
    claim = output.get("personal_acknowledgement")
    claimed_actor = str((claim or {}).get("actor") or "") if isinstance(claim, dict) else ""
    for event in store.events(str(task["task_id"]), limit=500):
        event_type = str(event.get("event_type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        actor = event.get("actor")
        if event_type == "personal_acknowledgement":
            record = {"actor": str(actor or ""), "at": str(event.get("created_at") or ""),
                      "source": event_type,
                      "scope": str(payload.get("on") or "personal_acknowledgement")}
            (human if actor_kind(actor) == "human" else rejected).append(record)
        elif event_type == "human_response_recorded":
            decisions.append({
                "actor": str(actor or ""),
                "at": str(event.get("created_at") or ""),
                "decision": str(payload.get("decision") or ""),
                "signs_off_result": False,
            })
    if isinstance(claim, dict):
        trusted = any(item["source"] == "personal_acknowledgement"
                      and (not claimed_actor or item["actor"] == claimed_actor)
                      for item in human + rejected)
        if not trusted:
            untrusted.append({
                "field": "output.personal_acknowledgement",
                "actor": claimed_actor,
                "at": str(claim.get("at") or ""),
                "reason": "no_trusted_signoff_event",
            })
    return human, rejected, decisions, untrusted


def review_task(store: TaskStore, task: Dict[str, Any]) -> Dict[str, Any]:
    """One task's review row. Read-only: no store method here mutates."""
    task_id = str(task["task_id"])
    payload = task.get("input_payload") if isinstance(task.get("input_payload"), dict) else {}
    output = task.get("output") if isinstance(task.get("output"), dict) else {}
    model_call = task.get("model_call") if isinstance(task.get("model_call"), dict) else {}
    binding = binding_of(task)
    root = _root_task(store, task)
    children = _children(store, task_id)
    origin = payload.get("origin") if isinstance(payload.get("origin"), dict) else {}
    attempts = store.invocation_attempts(task_id)
    delivery = _delivery(payload, model_call, attempts)
    delivered = delivery["delivered"]
    citations = sorted({str(item) for item in output.get("evidence_refs") or []})
    allowed = sorted(delivered)
    snapshots = [{
        "input_version": int(item.get("input_version") or 0),
        "payload_sha256": str(item.get("payload_sha256") or ""),
        "created_by": str(item.get("created_by") or ""),
        "reason": str(item.get("reason") or ""),
        "at": str(item.get("created_at") or ""),
    } for item in store.input_snapshots(task_id)]
    failures = []
    for event in store.events(task_id, limit=500):
        event_type = str(event.get("event_type") or "")
        if not any(marker in event_type for marker in _FAILURE_EVENT):
            continue
        failures.append({"event": event_type, "actor": str(event.get("actor") or ""),
                         "at": str(event.get("created_at") or ""),
                         "detail": str((event.get("payload") or {}).get("error")
                                       or (event.get("payload") or {}).get("reason") or "")[:300]})
    human, rejected, decisions, untrusted = _signoffs(store, task)
    kind = producer_kind(task)
    return {
        "schema_version": REVIEW_SCHEMA,
        "task_id": task_id,
        "case_id": str(task.get("case_id") or ""),
        "title": str(task.get("title") or ""),
        "status": str(task.get("status") or ""),
        "role_id": str(task.get("role_id") or ""),
        "producer": {"kind": kind, "role_id": str(task.get("role_id") or ""),
                     "counts_as_human": False if kind.startswith("model") else None},
        "execution_mode": str(task.get("execution_mode") or ""),
        "relations": {
            "parent_task_id": str(task.get("parent_task_id") or ""),
            "root_task_id": str(root.get("task_id") or task_id),
            "children": [str(child["task_id"]) for child in children],
            "depends_on": [str(item) for item in origin.get("depends_on") or []],
            "origin_kind": str(origin.get("kind") or ""),
        },
        "input": {
            "version": int(task.get("input_version") or 1),
            "research_question": str(payload.get("research_question") or ""),
            "symbols": [str(symbol) for symbol in payload.get("symbols") or []],
            "prompt_version": str(task.get("prompt_version") or ""),
            "model_config_hash": str(task.get("model_config_hash") or ""),
            "rule": ({"flow_id": str(binding.get("flow_id") or ""),
                      "flow_version": int(binding.get("flow_version") or 0),
                      "flow_sha256": str(binding.get("flow_sha256") or ""),
                      "role_prompt_sha256": dict(binding.get("role_prompt_sha256") or {}),
                      "bound_by": str(binding.get("bound_by") or "")}
                     if binding else None),
            "step_prompt_sha256": str((payload.get("role_step") or {}).get(
                "role_prompt_sha256") or "") if isinstance(payload.get("role_step"), dict) else "",
            "snapshots": snapshots,
        },
        "sources": {
            "citations": citations,
            "allowed_references": allowed,
            "delivered_evidence": delivered,
            "configured_references": delivery["configured"],
            "undelivered_references": delivery["undelivered"],
            "delivery_source": delivery["source"],
            "delivery_state": delivery["state"],
            "delivery_authoritative": delivery["authoritative"],
            "delivery_rounds": delivery["rounds"],
            "citations_outside_allowlist": sorted(set(citations) - set(allowed)),
        },
        "result": {
            "summary": str(output.get("summary") or "")[:2000],
            "output_keys": sorted(output),
            "output_sha256": _sha256_json(output),
            "role_outputs": dict(output.get("role_outputs") or {}),
            "key_findings": list(output.get("key_findings") or []),
        },
        "failures": failures,
        "metering": {
            "attempts": int(task.get("attempt_count") or 0),
            "max_attempts": int(task.get("max_attempts") or 0),
            "last_error": str(task.get("last_error") or "")[:500],
            "model_called": bool(model_call.get("called")),
            "mode": str(model_call.get("mode") or task.get("execution_mode") or ""),
            "rounds": len(model_call.get("rounds") or []) if isinstance(
                model_call.get("rounds"), list) else 0,
            "usage": dict(model_call.get("usage") or {}),
            "duration_seconds": model_call.get("duration_seconds"),
            "replay_source": str(model_call.get("replay_source") or ""),
            "invocation_attempts": len(attempts),
            "invocation_sent": sorted({str(item.get("sent") or "") for item in attempts}),
            "budget_reserve": int(payload.get("budget_reserve") or 0),
        },
        "signoff": {"human": human, "rejected_automated_claims": rejected,
                    "human_decisions": decisions,
                    "untrusted_output_claims": untrusted,
                    "acknowledged_by_human": bool(human)},
    }


# ---------------------------------------------------------------------- #
# projection                                                             #
# ---------------------------------------------------------------------- #

def _subtree(store: TaskStore, root: Dict[str, Any]) -> List[Dict[str, Any]]:
    collected: List[Dict[str, Any]] = []
    stack = [root]
    seen = set()
    while stack:
        current = stack.pop()
        task_id = str(current["task_id"])
        if task_id in seen:
            continue
        seen.add(task_id)
        collected.append(current)
        stack.extend(_children(store, task_id))
    return collected


def project_review(store: TaskStore, *, identity: Dict[str, Any],
                   task_ids: Optional[Iterable[str]] = None,
                   case_ids: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Project the readable slice of the task graph (never writes).

    ``task_ids`` selects roots (each is expanded to its subtree);
    ``case_ids`` selects every task in those cases. Any selected root the
    identity may not read aborts the whole projection — a partial answer
    would silently look complete."""
    identity = identity if isinstance(identity, dict) else {}
    try:
        ensure_scope(identity, REVIEW_SCOPE)
    except DifyAccessError as exc:
        raise ReviewAccessError("scope_not_granted", exc.detail) from exc
    all_tasks = store.list_tasks()
    roots: List[Dict[str, Any]] = []
    if task_ids is not None:
        for task_id in task_ids:
            task = _task(store, task_id)
            if task is None:
                raise ReviewAccessError("unknown_task", str(task_id))
            roots.append(task)
    if case_ids is not None:
        wanted = {str(item) for item in case_ids}
        for task in all_tasks:
            if str(task.get("case_id") or "") in wanted:
                roots.append(task)
    if not roots and task_ids is None and case_ids is None:
        roots = list(all_tasks)

    inspected: Dict[str, Dict[str, Any]] = {}
    access_rows: List[Dict[str, Any]] = []
    for root in roots:
        access = ensure_review_access(store, identity, root)
        for task in _subtree(store, root):
            task_id = str(task["task_id"])
            if task_id in inspected:
                continue
            ensure_review_access(store, identity, task)
            inspected[task_id] = review_task(store, task)
        access_rows.append({"task_id": str(root["task_id"]),
                            "personal_owner": access["personal_owner"],
                            "case_id": access["case_id"]})
    rows = sorted(inspected.values(), key=lambda item: item["task_id"])

    model_roles: List[Dict[str, Any]] = []
    evidence: Dict[str, Dict[str, Any]] = {}
    human: List[Dict[str, Any]] = []
    for row in rows:
        if row["producer"]["kind"] == "model_role":
            model_roles.append({"task_id": row["task_id"], "role_id": row["role_id"]})
        for ref in row["sources"]["citations"]:
            delivered = row["sources"]["delivered_evidence"].get(ref) or {}
            # Independence is about the SOURCE BYTES, not how many roles cite
            # them: the same sha256 is one evidence source. A source counts as
            # independent only when it actually entered the outbound context
            # (worker ledger) AND was verified against the vault — an input
            # payload entry that never left the process proves nothing.
            independent = bool(
                delivered and delivered.get("vault_verified")
                and row["sources"]["delivery_authoritative"])
            key = str(delivered.get("sha256") or ref)
            entry = evidence.setdefault(key, {
                "evidence_ref": ref, "sha256": str(delivered.get("sha256") or ""),
                "vault_verified": bool(delivered.get("vault_verified")),
                "delivered": bool(delivered),
                "independent": independent,
                "cited_by": []})
            entry["delivered"] = entry["delivered"] or bool(delivered)
            entry["independent"] = entry["independent"] or independent
            if row["task_id"] not in entry["cited_by"]:
                entry["cited_by"].append(row["task_id"])
        for record in row["signoff"]["human"]:
            human.append({"task_id": row["task_id"], **record})
    return {
        "schema_version": REVIEW_SCHEMA,
        "identity": {"actor_id": str(identity.get("actor_id") or ""),
                     "credential_id": str(identity.get("credential_id") or "")},
        "tasks": rows,
        "access": access_rows,
        "attribution": {
            "model_roles": model_roles,
            "evidence_sources": sorted(evidence.values(),
                                       key=lambda item: item["evidence_ref"]),
            "human_signoffs": human,
            "independent_evidence_count": sum(
                1 for item in evidence.values() if item["independent"]),
            "distinct_human_actors": sorted({item["actor"] for item in human}),
            "rules": {
                "model_roles_are_not_human": True,
                "same_evidence_bytes_count_once": True,
                "signoff_requires_human_actor": True,
                "signoff_requires_acknowledgement": True,
                "signoff_requires_trusted_event": True,
                "delivery_ledger_is_authoritative": True,
                "automated_actors": sorted(set(AUTOMATED_ACTOR_PREFIXES)
                                           | _EXTRA_AUTOMATED_ACTORS),
            },
        },
    }
