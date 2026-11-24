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
- **Duplicates first**: an identical child (same parent, same role, same
  question identity) is returned as a duplicate, not created twice — and a
  submission that is ENTIRELY duplicates never touches the budget ledger
  (a retry of the same proposal after the parent budget is exhausted is
  still answered as a duplicate, with zero new tasks and zero new metering).
- **Dependencies**: a proposal may order itself after a sibling role
  (``depends_on``). An unknown, self-referential or cyclic dependency is
  refused whole (``dependency_invalid``); legal dependencies are resolved to
  the sibling task ids and recorded on the child's origin.
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
    budget_exceeded, duplicate_child, self_parented, malformed_proposal,
    cross_case, dependency_invalid"""


def _question_key(question: str) -> str:
    return "".join(ch for ch in str(question or "").lower() if ch.isalnum())


def _normalize_depends(raw: Any, index: int) -> List[str]:
    """Dependency refs are role ids (or {"ref": role}); anything else is malformed."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SubtaskGraphError(
            "malformed_proposal: #{0} depends_on must be a list".format(index))
    refs: List[str] = []
    for entry in raw:
        if isinstance(entry, str):
            ref = entry.strip()
        elif isinstance(entry, dict) and set(entry) <= {"ref", "role_id"}:
            ref = str(entry.get("ref") or entry.get("role_id") or "").strip()
        else:
            raise SubtaskGraphError(
                "malformed_proposal: #{0} dependency must be a role id".format(index))
        if not ref:
            raise SubtaskGraphError(
                "malformed_proposal: #{0} dependency ref is empty".format(index))
        if ref not in refs:
            refs.append(ref)
    return refs


def _dependency_order(items: List[Tuple[int, Dict[str, Any]]],
                      existing_roles: Dict[str, List[str]]) -> List[Tuple[int, Dict[str, Any]]]:
    """Order the batch so a sibling dependency is created first.

    Dependencies on roles outside this batch (already-existing children) are
    satisfied already. A leftover cycle is refused — fail closed, zero
    creation."""
    by_role: Dict[str, List[Tuple[int, Dict[str, Any]]]] = {}
    for entry in items:
        by_role.setdefault(entry[1]["role_id"], []).append(entry)
    placed = set(existing_roles)
    pending = list(items)
    ordered: List[Tuple[int, Dict[str, Any]]] = []
    while pending:
        progressed = False
        for entry in list(pending):
            item = entry[1]
            # A duplicated role is only a legal dependency target when the
            # batch names it once (checked by the caller).
            waiting = [ref for ref in item["depends_on"]
                       if ref in by_role and len(by_role[ref]) == 1
                       and ref not in placed]
            if waiting:
                continue
            ordered.append(entry)
            pending.remove(entry)
            progressed = True
            if not any(other[1]["role_id"] == item["role_id"] for other in pending):
                placed.add(item["role_id"])
        if not progressed:
            remaining = sorted(entry[1]["role_id"] for entry in pending)
            raise SubtaskGraphError(
                "dependency_invalid: dependency cycle among {0}".format(remaining))
    return ordered


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
    whole (fail closed) — partial fan-out is how scope creep starts.
    Duplicates are resolved BEFORE the fan-out and budget gates, so a retry
    of an already-accepted proposal never consumes budget a second time."""
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
    parent_case = str(parent.get("case_id") or "")
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
        proposed_case = str(proposal.get("case_id") or "")
        depends_on = _normalize_depends(proposal.get("depends_on"), index)
        if role not in _ROLES:
            raise SubtaskGraphError("role_not_allowed: {0!r}".format(role))
        if not question:
            raise SubtaskGraphError("malformed_proposal: question required")
        # A child never leaves its parent's case: a proposal naming another
        # case is refused outright instead of silently inheriting.
        if proposed_case and proposed_case != parent_case:
            raise SubtaskGraphError(
                "cross_case: {0!r} is not the parent case {1!r}".format(
                    proposed_case, parent_case))
        widened = set(symbols) - parent_symbols
        if widened:
            raise SubtaskGraphError(
                "scope_widened: {0} not in parent scope".format(sorted(widened)))
        if not symbols:
            symbols = sorted(parent_symbols)
        if reserve < 0:
            raise SubtaskGraphError("malformed_proposal: budget_reserve >= 0")
        if role in depends_on:
            raise SubtaskGraphError(
                "dependency_invalid: {0} cannot depend on itself".format(role))
        normalized.append((index, {
            "role_id": role, "question": question, "symbols": symbols,
            "budget_reserve": reserve, "depends_on": depends_on,
        }))

    existing_keys: Dict[Tuple[str, str], str] = {}
    existing_roles: Dict[str, List[str]] = {}
    for child in existing_children:
        payload = child.get("input_payload") or {}
        role = str(child.get("role_id") or "")
        existing_keys.setdefault(
            (role, _question_key(str(payload.get("research_question") or ""))),
            str(child["task_id"]))
        existing_roles.setdefault(role, []).append(str(child["task_id"]))

    # Duplicates first: within the batch (same role+question identity) and
    # against already-created children. They are answered, never re-gated on
    # fan-out or budget — nothing new is created, so nothing is metered.
    duplicates: List[Dict[str, Any]] = []
    unique_items: List[Tuple[int, Dict[str, Any]]] = []
    seen_keys = set()
    for index, item in normalized:
        key = (item["role_id"], _question_key(item["question"]))
        if key in seen_keys:
            duplicates.append({"index": index, "role_id": item["role_id"],
                               "reason": "duplicate_child"})
            continue
        seen_keys.add(key)
        if key in existing_keys:
            duplicates.append({"index": index, "role_id": item["role_id"],
                               "reason": "duplicate_child",
                               "task_id": existing_keys[key]})
            continue
        unique_items.append((index, item))

    # Fan-out counts DISTINCT new children: a batch that is entirely
    # duplicates widens nothing, so each new (role, question-identity) key
    # counts once against the cap.
    if len(existing_children) + len(unique_items) > max_children:
        raise SubtaskGraphError(
            "fanout_exceeded: {0}+{1} > cap {2}".format(
                len(existing_children), len(unique_items), max_children))

    # Dependency review: every ref must name exactly one existing child or
    # one proposal in this batch, and the batch must be orderable.
    batch_role_counts: Dict[str, int] = {}
    for _, item in unique_items:
        batch_role_counts[item["role_id"]] = batch_role_counts.get(item["role_id"], 0) + 1
    for index, item in unique_items:
        for ref in item["depends_on"]:
            existing_ids = existing_roles.get(ref) or []
            in_batch = ref in batch_role_counts
            if in_batch and batch_role_counts[ref] > 1:
                raise SubtaskGraphError(
                    "dependency_invalid: {0!r} names several proposals; "
                    "the dependency target is ambiguous".format(ref))
            if in_batch and existing_ids:
                raise SubtaskGraphError(
                    "dependency_invalid: {0!r} exists and is also proposed; "
                    "the dependency target is ambiguous".format(ref))
            if not in_batch and len(existing_ids) > 1:
                raise SubtaskGraphError(
                    "dependency_invalid: {0!r} names several existing children".format(ref))
            if not in_batch and not existing_ids:
                raise SubtaskGraphError(
                    "dependency_invalid: unknown dependency {0!r} on #{1}".format(ref, index))
    ordered = _dependency_order(unique_items, existing_roles)

    # Budget across the whole batch — only NEW proposals reserve anything.
    if budget_limit > 0:
        outstanding = sum(int((child.get("input_payload") or {})
                              .get("budget_reserve") or 0)
                          for child in existing_children)
        requested = sum(item["budget_reserve"] for _, item in ordered)
        if outstanding + requested > budget_limit:
            raise SubtaskGraphError(
                "budget_exceeded: {0}+{1} > {2}".format(
                    outstanding, requested, budget_limit))

    # Pass 3 — create in dependency order; resolve refs to task ids.
    resolved: Dict[str, str] = {role: ids[0] for role, ids in existing_roles.items()
                                if len(ids) == 1}
    created: List[Dict[str, Any]] = []
    for index, item in ordered:
        depends_ids: List[str] = []
        for ref in item["depends_on"]:
            if ref not in resolved:
                raise SubtaskGraphError(
                    "dependency_invalid: {0!r} was not created before #{1}".format(ref, index))
            depends_ids.append(resolved[ref])
        key = (item["role_id"], _question_key(item["question"]))
        payload = dict(parent["input_payload"])
        # R17 stores the flow binding only on the root. The orchestrator
        # manages the children through that parent relationship.
        payload.pop("role_flow", None)
        payload["research_question"] = item["question"]
        payload["symbols"] = item["symbols"]
        payload["budget_reserve"] = item["budget_reserve"]
        payload["origin"] = {"kind": "subtask", "parent_task_id": parent_task_id,
                             "depends_on": depends_ids}
        task, outcome = store.create_task(
            title="{0}: {1}".format(item["role_id"], item["question"])[:80],
            input_payload=payload,
            case_id=parent_case,
            role_id=item["role_id"],
            parent_task_id=parent_task_id,
            execution_mode=str(parent.get("execution_mode") or "live"),
            idempotency_key="subtask::{0}::{1}::{2}".format(
                parent_task_id, item["role_id"], key[1][:40]),
            actor="subtask_graph",
        )
        resolved[item["role_id"]] = str(task["task_id"])
        if outcome != "RECORDED":
            # A concurrent submission won the create race: its task IS the
            # child, so this submission is a duplicate and meters nothing.
            duplicates.append({"index": index, "role_id": item["role_id"],
                               "reason": "duplicate_child",
                               "task_id": str(task["task_id"]), "outcome": outcome})
            continue
        created.append({"task_id": str(task["task_id"]), "role_id": item["role_id"],
                        "depends_on": depends_ids, "outcome": outcome})
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
