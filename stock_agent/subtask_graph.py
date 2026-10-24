"""R22: bounded subtask graph — governance constraints before any fan-out.

The multi-agent half the taskbook assigns to R22 that does NOT need Dify:
the PERSISTENT TASK-GRAPH rules every orchestrator (Python chain today,
Dify roles in R17) must pass through — so the constraints live in ONE
place instead of per-orchestrator:

- **Proposals, not tasks**: ``propose_subtasks`` validates a batch of
  proposals against the parent and the graph BEFORE creating anything;
  anything questionable stays a rejected proposal with reasons.
- **Scope**: a child may not widen the parent (no new symbols, no new
  case, question must reference the parent's subject).
- **Budget**: children share the parent's remaining model-attempt budget
  (``budget_reserve`` on each child); a fan-out that would overdraw is
  refused whole.
- **Depth & fan-out**: depth ≤ ``max_depth`` (default 1), per-parent
  children ≤ ``max_children`` (default 3); cycles are impossible by
  construction (a child is always younger than its parent, and the
  parent chain is walked to the root) but a self-parenting proposal is
  still rejected explicitly.
- **Duplicates**: an identical child (same parent, same role, same
  question identity) is returned as a duplicate, not created twice.
- **Cancel propagation**: cancelling a parent cancels its non-terminal
  descendants (depth-first), each with its own outbox event.
- **Partial failure**: children are independent tasks; one child failing
  never flips its siblings (asserted).

The role-orchestration half (analysis → counter → synthesis via Dify,
R17) is NOT_RUN by dependency and stays honestly marked.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .task_runtime.store import (
    STATUS_AWAITING_HUMAN,
    TERMINAL_STATUSES,
    TaskRuntimeError,
    TaskStore,
)

SUBTASK_GRAPH_SCHEMA = "1.0"
DEFAULT_MAX_DEPTH = 1
DEFAULT_MAX_CHILDREN = 3

_ROLES = {"event_analyst", "fundamental_analyst", "counter_evidence",
          "synthesis_drafter", "quality_check"}
# Public alias: R17's role flows bind the SAME role set (no second source of truth).
ALLOWED_SUBTASK_ROLES = frozenset(_ROLES)


class SubtaskGraphError(ValueError):
    """reasons: unknown_parent, parent_terminal, role_not_allowed,
    scope_widened, question_drift, depth_exceeded, fanout_exceeded,
    budget_exceeded, duplicate_child, self_parented, malformed_proposal"""


def _question_key(question: str) -> str:
    return "".join(ch for ch in str(question or "").lower() if ch.isalnum())


def propose_subtasks(
    store: TaskStore,
    *,
    parent_task_id: str,
    proposals: List[Dict[str, Any]],
    budget_limit: int = 0,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_children: int = DEFAULT_MAX_CHILDREN,
) -> Dict[str, Any]:
    """Validate a fan-out batch, then create the surviving children.

    Returns {"created": [...], "rejected": [{"index", "reason"}],
    "duplicates": [...]}. A batch with ANY malformed proposal is refused
    whole (fail closed) — partial fan-out is how scope creep starts."""
    if not isinstance(proposals, list) or not proposals:
        raise SubtaskGraphError("malformed_proposal: proposals must be a non-empty list")
    try:
        parent = store.get_task(str(parent_task_id))
    except TaskRuntimeError:
        raise SubtaskGraphError("unknown_parent: {0}".format(parent_task_id))
    if parent["status"] in TERMINAL_STATUSES:
        raise SubtaskGraphError(
            "parent_terminal: {0} is {1}".format(parent_task_id, parent["status"]))
    if str(parent.get("parent_task_id") or "") == parent_task_id:
        raise SubtaskGraphError("self_parented: task cannot be its own parent")

    parent_symbols = {str(s) for s in
                      (parent.get("input_payload") or {}).get("symbols") or []}
    depth = _depth_of(store, parent) + 1
    if depth > max_depth:
        raise SubtaskGraphError(
            "depth_exceeded: depth {0} > cap {1}".format(depth, max_depth))
    existing_children = _children_of(store, parent_task_id)

    # Pass 1 — validate every proposal (fail closed on any malformation).
    normalized: List[Tuple[int, Dict[str, Any]]] = []
    for index, proposal in enumerate(proposals):
        if not isinstance(proposal, dict):
            raise SubtaskGraphError("malformed_proposal: #{0} not an object".format(index))
        role = str(proposal.get("role_id") or "")
        question = str(proposal.get("question") or "").strip()
        symbols = [str(s) for s in (proposal.get("symbols") or []) if str(s).strip()]
        reserve = int(proposal.get("budget_reserve") or 0)
        if role not in _ROLES:
            raise SubtaskGraphError("role_not_allowed: {0!r}".format(role))
        if not question:
            raise SubtaskGraphError("malformed_proposal: question required")
        widened = set(symbols) - parent_symbols
        if widened:
            raise SubtaskGraphError(
                "scope_widened: {0} not in parent scope".format(sorted(widened)))
        if not symbols:
            symbols = sorted(parent_symbols)
        if reserve < 0:
            raise SubtaskGraphError("malformed_proposal: budget_reserve >= 0")
        normalized.append((index, {
            "role_id": role, "question": question, "symbols": symbols,
            "budget_reserve": reserve,
        }))

    # Fan-out counts DISTINCT new children: a batch that is entirely
    # duplicates widens nothing, so each new (role, question-identity) key
    # counts once against the cap.
    distinct_new = {
        (item["role_id"], _question_key(item["question"]))
        for _, item in normalized}
    distinct_new -= {
        (str(c.get("role_id") or ""), _question_key(
            str((c.get("input_payload") or {}).get("research_question") or "")))
        for c in existing_children}
    if len(existing_children) + len(distinct_new) > max_children:
        raise SubtaskGraphError(
            "fanout_exceeded: {0}+{1} > cap {2}".format(
                len(existing_children), len(distinct_new), max_children))

    # Pass 2 — budget across the whole batch.
    if budget_limit > 0:
        outstanding = sum(int((child.get("input_payload") or {})
                              .get("budget_reserve") or 0)
                          for child in existing_children)
        requested = sum(item["budget_reserve"] for _, item in normalized)
        if outstanding + requested > budget_limit:
            raise SubtaskGraphError(
                "budget_exceeded: {0}+{1} > {2}".format(
                    outstanding, requested, budget_limit))

    # Pass 3 — duplicates against existing children (same parent+role+question
    # identity), then create.
    existing_keys = {
        (str(c.get("role_id") or ""), _question_key(
            str((c.get("input_payload") or {}).get("research_question") or "")))
        for c in existing_children}
    created: List[Dict[str, Any]] = []
    duplicates: List[Dict[str, Any]] = []
    for index, item in normalized:
        key = (item["role_id"], _question_key(item["question"]))
        if key in existing_keys:
            duplicates.append({"index": index, "role_id": item["role_id"],
                              "reason": "duplicate_child"})
            continue
        payload = dict(parent["input_payload"])
        # R17: a role-flow binding belongs to the ROOT only — children are
        # ordinary worker-claimable steps (their identity rides in role_step).
        payload.pop("role_flow", None)
        payload["research_question"] = item["question"]
        payload["symbols"] = item["symbols"]
        payload["budget_reserve"] = item["budget_reserve"]
        payload["origin"] = {"kind": "subtask", "parent_task_id": parent_task_id}
        task, outcome = store.create_task(
            title="{0}: {1}".format(item["role_id"], item["question"])[:80],
            input_payload=payload,
            case_id=str(parent.get("case_id") or ""),
            role_id=item["role_id"],
            parent_task_id=parent_task_id,
            execution_mode=str(parent.get("execution_mode") or "live"),
            idempotency_key="subtask::{0}::{1}::{2}".format(
                parent_task_id, item["role_id"], key[1][:40]),
            actor="subtask_graph",
        )
        existing_keys.add(key)
        created.append({"task_id": task["task_id"], "role_id": item["role_id"],
                        "outcome": outcome})
    return {"schema_version": SUBTASK_GRAPH_SCHEMA,
            "parent_task_id": parent_task_id, "depth": depth,
            "created": created, "duplicates": duplicates, "rejected": []}


def cancel_subtree(store: TaskStore, root_task_id: str, *, actor: str,
                   reason: str) -> Dict[str, Any]:
    """Cancel a parent and propagate to every non-terminal descendant.

    Each cancelled task gets its own outbox event; already-terminal
    descendants are left untouched (their history stands)."""
    cancelled: List[Dict[str, Any]] = []
    stack = [str(root_task_id)]
    while stack:
        task_id = stack.pop()
        task = store.get_task(task_id)
        if task["status"] in TERMINAL_STATUSES:
            continue
        current_version = store.get_task(task_id)["version"]
        if task["status"] == STATUS_AWAITING_HUMAN or task["status"] == "queued":
            fresh = store.request_cancel(task_id, actor=actor, reason=reason,
                                         expected_version=current_version)
        else:
            # leased/running: set the flag; the worker's checkpoint lands it.
            fresh = store.request_cancel(task_id, actor=actor, reason=reason,
                                         expected_version=current_version)
        cancelled.append({"task_id": task_id, "status": fresh["status"]})
        stack.extend(child["task_id"] for child in _children_of(store, task_id))
    return {"root_task_id": str(root_task_id), "cancelled": cancelled,
            "count": len(cancelled)}


# ---------------------------------------------------------------------- #

def _children_of(store: TaskStore, parent_task_id: str) -> List[Dict[str, Any]]:
    return [task for task in store.list_tasks()
            if str(task.get("parent_task_id") or "") == str(parent_task_id)]


def _depth_of(store: TaskStore, task: Dict[str, Any]) -> int:
    depth = 0
    seen = {task["task_id"]}
    current = task
    while str(current.get("parent_task_id") or ""):
        parent_id = str(current["parent_task_id"])
        if parent_id in seen:
            break  # defensive: a corrupt cycle cannot loop forever here
        seen.add(parent_id)
        depth += 1
        try:
            current = store.get_task(parent_id)
        except TaskRuntimeError:
            break
    return depth
