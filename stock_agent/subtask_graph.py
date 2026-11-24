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

import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # POSIX advisory lock for cross-process serialization; threads use the RLock.
    import fcntl
except ImportError:  # pragma: no cover - Windows hosts fall back to the RLock
    fcntl = None  # type: ignore[assignment]

from .task_runtime.store import (
    STATUS_CANCELLED,
    TERMINAL_STATUSES,
    PrincipalVerificationError,
    TaskRuntimeError,
    TaskStore,
    TaskTransitionError,
    TaskVersionConflictError,
)

SUBTASK_GRAPH_SCHEMA = "1.0"
DEFAULT_MAX_DEPTH = 1
DEFAULT_MAX_CHILDREN = 3

_LOCKS_GUARD = threading.Lock()
_LOCKS: Dict[str, "threading.RLock"] = {}
# One advisory lock FILE per DB, shared (refcounted) by nested boundary entries
# in the same process: RLock is reentrant for the thread, flock is not.
_FILE_LOCKS: Dict[str, Dict[str, Any]] = {}

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

    The parent re-read, duplicate resolution, cap/budget gates and the
    creation loop run inside ONE exclusion boundary per task DB (thread lock
    plus a POSIX advisory file lock), shared with cancellation, so two
    orchestrators racing on the same parent cannot both pass the checks and
    overdraw the budget, exceed fan-out, or create the same child twice.
    The second submission converges on the first one's task as a duplicate.

    Duplicates are resolved BEFORE the fan-out and budget gates, but the
    dependency review runs over the WHOLE normalized batch — a duplicate
    proposal that names an unknown dependency, or that claims dependencies
    the existing child does not have, still refuses the batch
    (``dependency_invalid``) instead of slipping under the dedupe.
    """
    if not isinstance(proposals, list) or not proposals:
        raise SubtaskGraphError("malformed_proposal: proposals must be a non-empty list")
    if getattr(store, "read_only", False):
        # Fail before any read sidecar or write: a read-only caller cannot
        # authorize task creation, and must not be silently upgraded.
        raise SubtaskGraphError(
            "read_only_store: subtask creation requires a writable connection")
    parent_task_id = str(parent_task_id)

    # Preliminary snapshot OUTSIDE the boundary. It can only REFUSE (an
    # obviously terminal or already-saturated parent) — it never authorizes
    # a create. The authoritative parent/children read and every gate run
    # again inside the boundary, through a fresh connection, so a snapshot
    # taken before another submission committed cannot be acted on.
    try:
        preliminary = store.get_task(parent_task_id)
    except TaskRuntimeError:
        raise SubtaskGraphError("unknown_parent: {0}".format(parent_task_id))
    if preliminary["status"] in TERMINAL_STATUSES:
        raise SubtaskGraphError(
            "parent_terminal: {0} is {1}".format(parent_task_id, preliminary["status"]))
    requested_in = cancelled_ancestor(store, preliminary)
    if requested_in is not None:
        raise SubtaskGraphError(
            "parent_cancel_requested: {0} (or an ancestor) has a cancellation request".format(
                requested_in))
    if len(_children_of(store, parent_task_id)) > max_children:
        raise SubtaskGraphError(
            "fanout_exceeded: parent {0} already exceeds cap {1}".format(
                parent_task_id, max_children))

    with creation_boundary(store):
        authoritative = _fresh_store(store)
        try:
            return _propose_locked(
                authoritative, parent_task_id=parent_task_id, proposals=proposals,
                budget_limit=budget_limit, max_depth=max_depth,
                max_children=max_children)
        finally:
            # Only close the connection THIS function opened (never the
            # caller's store, and never an in-memory caller's connection).
            if authoritative is not store:
                authoritative.close()


def _fresh_store(store: TaskStore) -> TaskStore:
    """A second connection on the same DB — the authoritative view used once
    the boundary is held. Tests may subclass the caller's store to slow or
    synchronize reads; those hooks must not sit inside the critical section,
    and in production a fresh request-scoped connection is the R03 rule.

    An in-memory store shares no file and has no cross-process peers, so the
    caller's own connection is used (and must stay open for the caller)."""
    if str(store.db_path) == ":memory:":
        return store
    return TaskStore(store.db_path)


def _propose_locked(
    store: TaskStore,
    *,
    parent_task_id: str,
    proposals: List[Dict[str, Any]],
    budget_limit: int,
    max_depth: int,
    max_children: int,
) -> Dict[str, Any]:
    try:
        parent = store.get_task(parent_task_id)
    except TaskRuntimeError:
        raise SubtaskGraphError("unknown_parent: {0}".format(parent_task_id))
    if parent["status"] in TERMINAL_STATUSES:
        raise SubtaskGraphError(
            "parent_terminal: {0} is {1}".format(parent_task_id, parent["status"]))
    # A cancelled parent is terminal, but a LEASED/RUNNING parent with a pending
    # cancellation request is not: the worker only lands the terminal state at
    # its next checkpoint. From the moment the request is recorded, this task
    # (or any descendant asked to fan out) may create NO new work — otherwise
    # children appear after the cancellation and outlive it. The check is made
    # inside the boundary through the authoritative connection, so it cannot
    # race the cancellation's own parent read.
    requested_in = cancelled_ancestor(store, parent)
    if requested_in is not None:
        raise SubtaskGraphError(
            "parent_cancel_requested: {0} (or an ancestor) has a cancellation request".format(
                requested_in))
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

    # A duplicate is an ANSWER about an existing identity, not a new place to
    # record dependencies. If a repeated line claims dependencies that the
    # first occurrence / existing child does not have (or omits ones it does),
    # the batch is internally inconsistent — creating the other lines while
    # silently dropping this one's edge is how a cycle slips through. Refuse
    # the whole batch; an exact retry still matches and stays a duplicate.
    role_by_id = {str(child["task_id"]): str(child.get("role_id") or "")
                  for child in existing_children}
    first_deps: Dict[Tuple[str, str], set] = {}
    for index, item in normalized:
        key = (item["role_id"], _question_key(item["question"]))
        declared = set(item["depends_on"])
        if key in first_deps:
            if declared != first_deps[key]:
                raise SubtaskGraphError(
                    "dependency_invalid: duplicate {0} on #{1} declares conflicting "
                    "dependencies".format(item["role_id"], index))
            continue
        if key in existing_keys:
            existing = next(child for child in existing_children
                            if str(child["task_id"]) == existing_keys[key])
            origin = (existing.get("input_payload") or {}).get("origin") or {}
            recorded = set()
            intact = True
            for dependency_id in origin.get("depends_on") or []:
                role_name = role_by_id.get(str(dependency_id))
                if not role_name:
                    intact = False
                    break
                recorded.add(role_name)
            if not intact or declared != recorded:
                raise SubtaskGraphError(
                    "dependency_invalid: duplicate {0} on #{1} declares dependencies "
                    "the existing child does not have".format(item["role_id"], index))
        first_deps[key] = declared

    # Fan-out counts DISTINCT new children: a batch that is entirely
    # duplicates widens nothing, so each new (role, question-identity) key
    # counts once against the cap.
    if len(existing_children) + len(unique_items) > max_children:
        raise SubtaskGraphError(
            "fanout_exceeded: {0}+{1} > cap {2}".format(
                len(existing_children), len(unique_items), max_children))

    # Dependency review over EVERY normalized proposal, duplicates included:
    # dedupe decides what is created, never what is validated. A ref must
    # name exactly one existing child or one new proposal in this batch, and
    # the new proposals must be orderable.
    new_role_keys: Dict[str, List[str]] = {}
    for _, item in unique_items:
        new_role_keys.setdefault(item["role_id"], []).append(
            _question_key(item["question"]))
    for index, item in normalized:
        for ref in item["depends_on"]:
            new_keys = new_role_keys.get(ref) or []
            existing_ids = existing_roles.get(ref) or []
            if len(new_keys) > 1:
                raise SubtaskGraphError(
                    "dependency_invalid: {0!r} names several proposals; "
                    "the dependency target is ambiguous".format(ref))
            if new_keys and existing_ids:
                raise SubtaskGraphError(
                    "dependency_invalid: {0!r} exists and is also proposed; "
                    "the dependency target is ambiguous".format(ref))
            if not new_keys and len(existing_ids) > 1:
                raise SubtaskGraphError(
                    "dependency_invalid: {0!r} names several existing children".format(ref))
            if not new_keys and not existing_ids:
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
                parent_task_id, item["role_id"], key[1]),
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


def creation_boundary(store: TaskStore,
                      *, require_file_lock: bool = True) -> "_CreationBoundary":
    """Serialize graph MUTATIONS on one task DB, across threads AND processes.

    Creation and cancellation both enter this boundary: a cancellation that
    lands while a batch is between validation and creation either happens
    first (the proposal then sees a terminal parent and creates nothing) or
    waits and then cancels the freshly created children. The in-process
    RLock is keyed by the resolved DB path; the advisory file lock
    (``flock`` on ``<db>.subtask.lock``) extends the same exclusion to other
    processes. If the file lock is unavailable, creation is REFUSED rather
    than run without cross-process mutual exclusion.

    ``require_file_lock=False`` is for CANCELLATION on hosts where the
    ``fcntl`` MODULE does not exist (Windows). There the in-process RLock is
    the only lock available to anyone, and creation itself is refused, so no
    cross-process creator can exist to race. It is deliberately NOT a licence
    to skip a lock that exists but failed: if ``fcntl`` is importable and
    ``flock`` errors, another process may hold the lock right now — the
    caller must fail closed (``graph_lock_unavailable``) rather than proceed
    alone, or a paused creator could commit a child after the cancellation.

    An in-memory store has no file and no cross-process peers, so it uses
    the in-process lock only and never writes a lock sidecar.
    """
    return _CreationBoundary(store, require_file_lock=require_file_lock)


def dispatch_boundary(store: TaskStore,
                      *, require_file_lock: bool = False) -> "_CreationBoundary":
    """Order a model-call's reservation + dispatch against cancellation.

    Cancellation records its request inside ``creation_boundary``, so a worker
    that re-reads the persistent cancellation state and then writes its budget
    reservation / invocation row INSIDE the same boundary gets one committed
    order for the two acts: a cancellation that already committed is seen and
    the call is refused (nothing reserved, nothing sent), while a round that
    reserved and marked its attempt dispatched first was legitimately in
    flight and is stopped at the worker's next checkpoint. Without the shared
    boundary the worker could pass its last cancellation check, prepare a
    prompt, and still reserve budget after the cancellation had committed.

    The file lock is optional in the same narrow sense as cancellation: it
    degrades only where the ``fcntl`` module does not exist, and a lock that
    exists but errors still fails closed (``graph_lock_unavailable``)."""
    return _CreationBoundary(store, require_file_lock=require_file_lock)


class _CreationBoundary:
    def __init__(self, store: TaskStore, *, require_file_lock: bool = True) -> None:
        self._memory = str(store.db_path) == ":memory:"
        self._require_file_lock = bool(require_file_lock)
        if self._memory:
            self._lock_key = "memory::{0}".format(id(store))
            self._lock_path = None
        else:
            resolved = str(Path(store.db_path).resolve())
            self._lock_key = resolved
            self._lock_path = resolved + ".subtask.lock"
        self._thread_lock: Optional[threading.RLock] = None
        self._file_entry: Optional[Dict[str, Any]] = None

    def __enter__(self) -> None:
        with _LOCKS_GUARD:
            lock = _LOCKS.get(self._lock_key)
            if lock is None:
                lock = _LOCKS[self._lock_key] = threading.RLock()
        lock.acquire()
        self._thread_lock = lock
        try:
            if not self._memory:
                if fcntl is None:
                    if self._require_file_lock:
                        raise SubtaskGraphError(
                            "graph_lock_unavailable: this platform has no cross-process "
                            "file locking; refusing unsafe creation")
                    return None
                with _LOCKS_GUARD:
                    entry = _FILE_LOCKS.setdefault(
                        self._lock_path, {"handle": None, "depth": 0})
                # The per-key RLock is ours, so this entry is touched by this
                # thread only. Take the flock OUTSIDE the global guard: a
                # cross-process wait must not stall unrelated DBs here.
                if entry["handle"] is None:
                    handle = None
                    try:
                        handle = open(self._lock_path, "a+")
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    except OSError as exc:
                        if handle is not None:
                            handle.close()
                        # NEVER degrade here: the lock module exists, so a
                        # failure means a real interlock problem (held by a
                        # creator, or the lock file is unusable). Proceeding
                        # would let a paused creator commit after us.
                        raise SubtaskGraphError(
                            "graph_lock_unavailable: cannot take the cross-process "
                            "subtask lock ({0}); refusing the graph mutation".format(exc))
                    entry["handle"] = handle
                entry["depth"] += 1
                self._file_entry = entry
        except BaseException:
            lock.release()
            self._thread_lock = None
            raise
        return None

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._file_entry is not None:
                with _LOCKS_GUARD:
                    entry = self._file_entry
                    entry["depth"] -= 1
                    if entry["depth"] <= 0:
                        handle = entry["handle"]
                        entry["handle"] = None
                        entry["depth"] = 0
                        if handle is not None:
                            try:
                                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                            finally:
                                handle.close()
        finally:
            self._file_entry = None
            self._thread_lock.release()


def _cancel_node(store: TaskStore, task_id: str, *, actor: str, reason: str,
                 expected_version: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Cancel one node, absorbing the version race with a live worker.

    A worker may claim/lease the task between our read and the write (that
    bump is exactly what the store's optimistic check reports). Re-read and
    retry with the fresh version: the eventual write sets ``cancel_requested``
    while the task is leased, so the worker's checkpoint lands the terminal
    state. An already-requested task is not written twice (no duplicate
    event), and a terminal one is left untouched."""
    version = expected_version
    for _ in range(8):
        task = store.get_task(task_id)
        if task["status"] in TERMINAL_STATUSES:
            return None
        if task.get("cancel_requested"):
            return task
        if version is None:
            version = int(task["version"])
        try:
            return store.request_cancel(task_id, actor=actor, reason=reason,
                                        expected_version=version)
        except TaskVersionConflictError:
            version = None
    raise SubtaskGraphError(
        "cancel_conflict: {0} kept changing during cancellation; retry".format(task_id))


def cancel_subtree(store: TaskStore, root_task_id: str, *, actor: str,
                   reason: str, skip_root: bool = False,
                   require_file_lock: bool = True) -> Dict[str, Any]:
    """Cancel a subtree, propagating through TERMINAL intermediates.

    Every non-terminal task in the subtree gets its own outbox event.
    Terminal tasks keep their history untouched, but traversal continues
    INTO their children: a finished middle node must not shield a queued
    grandchild from the parent's cancellation.

    Runs inside the same persistent boundary as creation, so a child cannot
    be created behind the cancellation's back.

    ``skip_root`` is for callers that already cancelled the root through
    their own governed entry (``cancel_task_tree`` keeps the caller's
    expected_version and principal verification there); the root is then
    traversed but not cancelled again. ``require_file_lock=False`` lets the
    descent proceed on a host without a usable ``fcntl`` (see
    ``creation_boundary``)."""
    with creation_boundary(store, require_file_lock=require_file_lock):
        return _cancel_subtree_locked(store, root_task_id, actor=actor,
                                      reason=reason, skip_root=skip_root)


def cancel_task_tree(store: TaskStore, task_id: str, *, actor: str, reason: str,
                     expected_version: int,
                     principal_verification: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The ONE governed cancellation entry: root first, then the subtree.

    Callers that hold a governed root-cancel contract (workflow service,
    personal workspace, CLI) route here instead of calling
    ``store.request_cancel`` directly, so no normal entry can cancel a root
    and leave its children queued.

    **Root authorization stays atomic with the store's own transition.**
    The root request is performed by the store inside the graph boundary,
    exactly as before: non-empty actor, optional one-command principal
    receipt, optimistic ``expected_version``, terminal refusal. Descendant
    propagation happens only after that request committed, so an
    unauthorized or stale call mutates nothing anywhere.

    **Partial propagation is resumable and idempotent.** A cancellation
    that commits the root but fails on a descendant (a live worker kept
    bumping the child's version, a lock deadline, a crash) leaves the root
    ``cancelled`` with the descendant's ``cancel_requested`` unset. A repeat
    call with the root's CURRENT version resumes the descent instead of
    raising ``TaskTransitionError``: the caller still presents the
    optimistic token, and only the idempotent descendant pass runs (the
    root is not written or re-emitted). ``_cancel_node`` skips terminal and
    already-requested nodes, so a resumed pass emits no duplicate events.

    On the resume path a protected call must still present a one-command
    receipt, and that receipt is CONSUMED AND COMMITTED by this call
    (``claim_principal_verification``) before any descendant is touched.
    The store's own claim cannot be used here: it lives inside the root
    transaction that raises on a terminal row, so its claim would roll back
    — exactly the "recovery mutated children but recorded no consumption"
    gap. Reusing the already-used FIRST receipt is also accepted: it was
    committed with the original authorized root cancellation, so the resume
    is the continuation of that command rather than a new one.
    """
    task_id = str(task_id)
    with creation_boundary(store, require_file_lock=False):
        try:
            root = store.request_cancel(
                task_id, actor=actor, reason=reason,
                expected_version=expected_version,
                principal_verification=principal_verification)
        except TaskTransitionError:
            root = store.get_task(task_id)
            if root["status"] != STATUS_CANCELLED:
                # A finished (succeeded/failed/blocked/expired) root keeps
                # the store's refusal: cancellation never rewrites history.
                raise
            if int(root["version"]) != int(expected_version):
                raise TaskVersionConflictError(
                    "expected_version {0} does not match current {1}".format(
                        expected_version, root["version"]))
            # Already cancelled, caller proves it holds the current row: resume.
            if principal_verification is not None:
                _authorize_resume(store, principal_verification, task_id=task_id,
                                  actor=actor)
        # ``cancel_subtree`` re-enters the (reentrant, refcounted) boundary, so
        # the descent itself stays inside the same exclusion.
        cancel_subtree(store, task_id, actor=actor, reason=reason,
                       skip_root=True, require_file_lock=False)
    return store.get_task(task_id)


def _authorize_resume(store: TaskStore, principal_verification: Dict[str, Any],
                      *, task_id: str, actor: str) -> None:
    """Consume (or accept the committed first use of) the resume receipt.

    A fresh receipt is claimed in its OWN transaction, so the consumption is
    durable even though the task row is already terminal. A receipt already
    consumed for THIS task's cancel command is accepted as the original
    authorization being continued; anything else stays fail-closed."""
    if not isinstance(principal_verification, dict):
        raise TaskRuntimeError("principal_verification must be an object")
    verdict = store.claim_principal_verification(
        str(principal_verification.get("verification_id") or ""),
        actor=str(principal_verification.get("actor") or actor),
        command_id=str(principal_verification.get("command_id") or "cancel:{0}".format(task_id)),
        task_id=task_id, action="cancel",
        issued_fallback=principal_verification.get("issued_fallback"))
    if verdict.get("claimed"):
        # ``ok`` = fresh receipt consumed in its own committed transaction;
        # ``already_claimed_same_command`` = the original cancellation's
        # receipt, already committed — accepted as the same command.
        return
    raise PrincipalVerificationError(
        str(verdict.get("reason") or "principal_verification_required"))


def _cancel_subtree_locked(store: TaskStore, root_task_id: str, *, actor: str,
                           reason: str, skip_root: bool = False) -> Dict[str, Any]:
    cancelled: List[Dict[str, Any]] = []
    stack = [str(root_task_id)]
    seen = set()
    while stack:
        task_id = stack.pop()
        if task_id in seen:
            continue
        seen.add(task_id)
        already_root = skip_root and task_id == str(root_task_id)
        if not already_root:
            fresh = _cancel_node(store, task_id, actor=actor, reason=reason)
            if fresh is not None:
                cancelled.append({"task_id": task_id, "status": fresh["status"]})
        stack.extend(child["task_id"] for child in _children_of(store, task_id))
    return {"root_task_id": str(root_task_id), "cancelled": cancelled,
            "count": len(cancelled)}


# ---------------------------------------------------------------------- #

def _children_of(store: TaskStore, parent_task_id: str) -> List[Dict[str, Any]]:
    return [task for task in store.list_tasks()
            if str(task.get("parent_task_id") or "") == str(parent_task_id)]


def cancelled_ancestor(store: TaskStore, task: Dict[str, Any]) -> Optional[str]:
    """The nearest task on ``task``'s ancestor chain (itself included) that is
    cancelled or has a cancellation request.

    A cancellation request is recorded on the task itself (``cancel_requested``
    is set on a leased/running row too, before the worker checkpoints). It
    therefore has to be honoured wherever work would be created for that task,
    for any of its descendants, and by a worker about to spend a model call on
    the task: a grandchild must not fan out or dispatch under a cancelled root
    whose middle node is merely queued. Walks the parent chain and stops on a
    cycle or a missing parent (defensive only)."""
    seen = set()
    current: Optional[Dict[str, Any]] = task
    while current is not None:
        task_id = str(current.get("task_id") or "")
        if task_id in seen:
            return None
        seen.add(task_id)
        if current.get("cancel_requested") or current.get("status") == STATUS_CANCELLED:
            return task_id
        parent_id = str(current.get("parent_task_id") or "")
        if not parent_id:
            return None
        try:
            current = store.get_task(parent_id)
        except TaskRuntimeError:
            return None
    return None


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
