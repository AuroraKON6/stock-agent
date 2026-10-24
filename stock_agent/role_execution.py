"""R17 (offline slice): controlled role execution — the API an orchestrator calls.

The taskbook's R17 runs the analysis → counter-evidence → synthesis chain
with **Dify calling a controlled Python role API**. The Dify transport half
is NOT_RUN (depends on the R16 deployment); this module is everything on the
Python side of that boundary, verified in replay mode where the TEST plays
the orchestrator (exactly where Dify sits later):

- **Frozen flow artifacts**: ``define_role_flow`` + ``prompt_artifact`` pin
  every role's prompt text to a sha256 digest and the whole flow to one
  ``flow_sha256``. Production prompts never drift with unpublished canvas
  edits — any text change is a NEW digest, hence a NEW candidate (A77).
- **Approvals bind digests, not names**: a ``FlowRegistry`` approval covers
  exactly one flow digest; a changed artifact under an old approval is
  refused until a human re-approves the new candidate (A77).
- **Mode switch is an audited input version**: ``bind_role_flow`` bumps
  ``input_version`` (immutable snapshot per R01), never overwrites history.
- **Mutual exclusion (A76)**: a bound task is skipped by the full-chain
  claim path (``claim_next(exclude_role_bound=True)``) and refused by the
  worker entry — the Python full chain never advances it.
- **Steps are ordinary bounded subtasks**: ``execute_role_step`` creates the
  step through the R22 graph (scope/depth/fan-out/budget caps), injects the
  frozen prompt, runs it via the standard worker boundary, then re-verifies
  the prompt digest end-to-end. Succeeded steps never re-run and terminally
  failed steps never auto-retry — the root needs a decision instead (A78:
  cycles bounded, no double billing).
- **Root completion carries provenance**: ``finish_role_flow`` completes the
  root only when every flow role has a succeeded step, recording which task
  produced which output.

NOT_RUN here: the Dify transport itself (A71–A73/A75/A79 C-layer), live
cost/quality comparison, and model-boundary enforcement of prompt CONTENT
in live mode (replay verifies the digest chain, not the provider prompt).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from .subtask_graph import ALLOWED_SUBTASK_ROLES, SubtaskGraphError, propose_subtasks
from .task_runtime import TaskStore
from .task_runtime.store import STATUS_SUCCEEDED, TERMINAL_STATUSES, TaskRuntimeError

ROLE_EXECUTION_SCHEMA = "1.0"


class RoleFlowError(ValueError):
    """reasons: malformed_flow, unknown_role, flow_version_conflict,
    flow_not_registered, flow_not_approved, approval_digest_changed,
    unknown_task, task_terminal, task_not_bound, binding_digest_changed,
    role_not_in_flow, group_not_current, step_already_succeeded,
    step_failed_needs_decision, step_in_progress, flow_incomplete,
    claim_failed, step_binding_invalid"""


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def prompt_artifact(role_id: str, version: int, text: str) -> Dict[str, Any]:
    """One frozen prompt: role, artifact version, full text, sha256 digest."""
    role = str(role_id or "").strip()
    if role not in ALLOWED_SUBTASK_ROLES:
        raise RoleFlowError("unknown_role: {0!r}".format(role_id))
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise RoleFlowError("malformed_flow: prompt version must be a positive integer")
    if not str(text or "").strip():
        raise RoleFlowError("malformed_flow: prompt text required")
    return {"role_id": role, "version": int(version), "text": str(text),
            "sha256": _sha256_text(str(text))}


def define_role_flow(*, flow_id: str, version: int, groups: List[Dict[str, Any]],
                     prompts: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Freeze a role flow: ordered groups of roles + per-role prompt artifacts.

    ``groups`` = [{"roles": [role, ...]}, ...] executed group by group; roles
    within a group may run in parallel. The flow digest covers the group
    structure and every prompt digest — any change is a new candidate."""
    flow_id = str(flow_id or "").strip()
    if not flow_id:
        raise RoleFlowError("malformed_flow: flow_id required")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise RoleFlowError("malformed_flow: flow version must be a positive integer")
    if not isinstance(groups, list) or not groups:
        raise RoleFlowError("malformed_flow: groups must be a non-empty list")
    seen_roles: List[str] = []
    normalized_groups: List[Dict[str, Any]] = []
    for group in groups:
        roles = [str(r) for r in (group or {}).get("roles") or []]
        if not roles:
            raise RoleFlowError("malformed_flow: every group needs at least one role")
        for role in roles:
            if role not in ALLOWED_SUBTASK_ROLES:
                raise RoleFlowError("unknown_role: {0!r}".format(role))
            if role in seen_roles:
                raise RoleFlowError(
                    "malformed_flow: role {0} appears twice in the flow".format(role))
            seen_roles.append(role)
        normalized_groups.append({"roles": roles})
    if not isinstance(prompts, dict) or set(prompts) != set(seen_roles):
        raise RoleFlowError(
            "malformed_flow: prompts must cover exactly the flow roles: {0}".format(
                sorted(seen_roles)))
    normalized_prompts: Dict[str, Dict[str, Any]] = {}
    for role in seen_roles:
        artifact = prompts[role]
        if not isinstance(artifact, dict) or artifact.get("role_id") != role:
            raise RoleFlowError("malformed_flow: prompt artifact role mismatch for {0}".format(role))
        # Re-derive the digest from the text so a stale/caller-supplied digest
        # can never survive artifact construction.
        normalized_prompts[role] = prompt_artifact(
            role, artifact.get("version", 1), artifact.get("text", ""))
    digest_payload = {
        "flow_id": flow_id, "version": int(version), "groups": normalized_groups,
        "prompt_sha256": {role: normalized_prompts[role]["sha256"] for role in seen_roles},
    }
    return {
        "schema_version": ROLE_EXECUTION_SCHEMA, "flow_id": flow_id,
        "version": int(version), "groups": normalized_groups,
        "prompts": normalized_prompts,
        "flow_sha256": _sha256_text(_canonical_json(digest_payload)),
    }


class FlowRegistry:
    """Host-owned registry of frozen flows and their digest-bound approvals.

    In-memory by design for the offline slice; production wiring persists the
    same {registered flows, approvals} pair from config — the validation
    semantics do not change."""

    def __init__(self) -> None:
        self._registered: Dict[str, Dict[Any, Dict[str, Any]]] = {}
        self._approvals: Dict[str, Dict[str, Any]] = {}

    def register(self, flow: Dict[str, Any]) -> None:
        key = (int(flow["version"]), str(flow["flow_sha256"]))
        existing = self._registered.setdefault(str(flow["flow_id"]), {})
        for (version, _digest) in existing:
            if version == key[0]:
                raise RoleFlowError(
                    "flow_version_conflict: version {0} already registered with a different digest".format(version))
        existing[key] = flow

    def approve(self, flow: Dict[str, Any], *, actor: str) -> None:
        self._require_registered(flow)
        if not str(actor or "").strip():
            raise RoleFlowError("malformed_flow: approval actor required")
        self._approvals[str(flow["flow_id"])] = {
            "flow_sha256": str(flow["flow_sha256"]),
            "version": int(flow["version"]), "actor": str(actor)}

    def approval_status(self, flow_id: str) -> Dict[str, Any]:
        approvals = self._approvals.get(str(flow_id))
        return dict(approvals) if approvals else {"approved": False}

    def _require_registered(self, flow: Dict[str, Any]) -> None:
        key = (int(flow["version"]), str(flow["flow_sha256"]))
        if key not in self._registered.get(str(flow["flow_id"]), {}):
            raise RoleFlowError(
                "flow_not_registered: {0}@{1} ({2})".format(
                    flow["flow_id"], flow["version"], str(flow["flow_sha256"])[:12]))

    def validate(self, flow: Dict[str, Any]) -> None:
        """The CURRENT artifact must be registered AND covered by the approval."""
        self._require_registered(flow)
        approval = self._approvals.get(str(flow["flow_id"]))
        if approval is None:
            raise RoleFlowError(
                "flow_not_approved: {0}@{1} has no approval".format(flow["flow_id"], flow["version"]))
        if str(approval.get("flow_sha256")) != str(flow["flow_sha256"]):
            raise RoleFlowError(
                "approval_digest_changed: approval covers a different digest — "
                "re-approve the new candidate before running it")


# ---------------------------------------------------------------------- #

def binding_of(task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    binding = (task.get("input_payload") or {}).get("role_flow")
    return binding if isinstance(binding, dict) else None


def bind_role_flow(store: TaskStore, task_id: str, *, flow: Dict[str, Any],
                   registry: FlowRegistry, actor: str) -> Dict[str, Any]:
    """Switch a queued research task to role mode — as a NEW input version.

    Fail-closed: the flow must be registered and approved at bind time, the
    task must not already be bound, and the bump is one audited transaction
    (immutable snapshot per R01; earlier versions stand unchanged)."""
    registry.validate(flow)
    try:
        task = store.get_task(str(task_id))
    except TaskRuntimeError:
        raise RoleFlowError("unknown_task: {0}".format(task_id))
    if task["status"] in TERMINAL_STATUSES:
        raise RoleFlowError("task_terminal: {0} is {1}".format(task_id, task["status"]))
    if binding_of(task) is not None:
        existing = binding_of(task)
        if str(existing.get("flow_sha256")) == str(flow["flow_sha256"]):
            raise RoleFlowError(
                "task_not_bound: {0} already carries this exact candidate".format(task_id))
        if int(existing.get("flow_version") or 0) > int(flow["version"]):
            raise RoleFlowError(
                "task_not_bound: refusing to downgrade {0} from flow version {1} to {2}".format(
                    task_id, existing.get("flow_version"), flow["version"]))
        # A77: re-binding a CHANGED artifact is the explicit upgrade path —
        # each bind is its own audited input version; the old binding stays
        # readable in the immutable snapshot history.
    binding = {
        "schema_version": ROLE_EXECUTION_SCHEMA,
        "flow_id": str(flow["flow_id"]), "flow_version": int(flow["version"]),
        "flow_sha256": str(flow["flow_sha256"]),
        "role_prompt_sha256": {role: artifact["sha256"]
                               for role, artifact in flow["prompts"].items()},
        "mode": "role", "bound_by": str(actor or ""),
    }
    fresh = store.bump_input_version(
        str(task_id), payload_overrides={"role_flow": binding}, actor=str(actor or ""),
        reason="role_flow_bind:{0}@{1}".format(flow["flow_id"], flow["version"]))
    return fresh


def _validate_binding(task: Dict[str, Any], flow: Dict[str, Any],
                      registry: FlowRegistry) -> Dict[str, Any]:
    binding = binding_of(task)
    if binding is None:
        raise RoleFlowError("task_not_bound: {0} has no role_flow binding".format(task.get("task_id")))
    registry.validate(flow)
    if str(binding.get("flow_id")) != str(flow["flow_id"]):
        raise RoleFlowError("malformed_flow: binding names flow {0!r}".format(binding.get("flow_id")))
    if str(binding.get("flow_sha256")) != str(flow["flow_sha256"]):
        # A77: the artifact changed after the task was bound — the old
        # binding/approval does not carry over; re-bind the new candidate.
        raise RoleFlowError(
            "binding_digest_changed: task bound to {0}, current flow is {1}".format(
                str(binding.get("flow_sha256"))[:12], str(flow["flow_sha256"])[:12]))
    return binding


def flow_steps(store: TaskStore, root_task_id: str) -> List[Dict[str, Any]]:
    """The root's subtask children (origin.kind=subtask), oldest first."""
    return [task for task in store.list_tasks()
            if str(task.get("parent_task_id") or "") == str(root_task_id)
            and str(((task.get("input_payload") or {}).get("origin") or {}).get("kind") or "")
            == "subtask"]


def _current_group(flow: Dict[str, Any], succeeded_roles: set) -> int:
    for index, group in enumerate(flow["groups"]):
        if any(role not in succeeded_roles for role in group["roles"]):
            return index
    return len(flow["groups"])


def _claim_specific(store: TaskStore, task_id: str, worker_id: str,
                    *, skip_role_bound: bool) -> Dict[str, Any]:
    """Claim until we hold task_id; side claims are released back to queued."""
    side: List[Dict[str, Any]] = []
    claimed = None
    for _ in range(20):
        claim = (store.claim_next(worker_id, exclude_role_bound=True)
                 if skip_role_bound else store.claim_next(worker_id))
        if claim is None:
            break
        if str(claim["task_id"]) == str(task_id):
            claimed = claim
            break
        side.append(claim)
    for claim in side:
        store.fail(str(claim["task_id"]), worker_id=worker_id,
                   fencing_token=int(claim["fencing_token"]),
                   error="role orchestrator claim probe: not the target task",
                   retryable=True)
    if claimed is None:
        raise RoleFlowError("claim_failed: never reached {0}".format(task_id))
    return claimed


def execute_role_step(store: TaskStore, root_task_id: str, *, role_id: str,
                      flow: Dict[str, Any], registry: FlowRegistry,
                      worker_id: str = "role-orchestrator",
                      config: Optional[Dict[str, Any]] = None,
                      step_overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run ONE role step of a bound flow — the API Dify calls per step.

    Creates the step as a bounded subtask (R22 caps apply), injects the
    frozen prompt, executes it through the standard worker boundary, then
    re-verifies the prompt digest from the persisted payload. Bounded by
    construction: succeeded steps refuse to re-run, terminally failed steps
    refuse without a decision, out-of-group roles wait their turn."""
    try:
        root = store.get_task(str(root_task_id))
    except TaskRuntimeError:
        raise RoleFlowError("unknown_task: {0}".format(root_task_id))
    if root["status"] in TERMINAL_STATUSES:
        raise RoleFlowError("task_terminal: {0} is {1}".format(root_task_id, root["status"]))
    _validate_binding(root, flow, registry)
    role = str(role_id or "")
    flow_roles = [r for group in flow["groups"] for r in group["roles"]]
    if role not in flow_roles:
        raise RoleFlowError("role_not_in_flow: {0!r}".format(role_id))

    steps = flow_steps(store, root_task_id)
    succeeded = {str(t.get("role_id") or "") for t in steps
                 if t["status"] == STATUS_SUCCEEDED}
    if role in succeeded:
        raise RoleFlowError("step_already_succeeded: {0} will not be billed twice".format(role))
    mine = [t for t in steps if str(t.get("role_id") or "") == role]
    if any(t["status"] not in TERMINAL_STATUSES for t in mine):
        return {"status": "step_in_progress", "role_id": role,
                "task_id": str(next(t["task_id"] for t in mine
                                    if t["status"] not in TERMINAL_STATUSES))}
    if mine:
        # A78: terminally failed steps never auto-retry — the root needs a
        # human/orchestrator decision, not a silent new billing cycle.
        raise RoleFlowError(
            "step_failed_needs_decision: {0} already failed terminally ({1})".format(
                role, mine[-1]["status"]))
    current_group_roles = flow["groups"][_current_group(flow, succeeded)]["roles"]
    if role not in current_group_roles:
        raise RoleFlowError(
            "group_not_current: {0} is not in the current group {1} (succeeded so far: {2})".format(
                role, current_group_roles, sorted(succeeded) or "none"))

    question = str((root.get("input_payload") or {}).get("research_question")
                   or root.get("title") or "role step")
    prompt = flow["prompts"][role]
    proposal = propose_subtasks(store, parent_task_id=str(root_task_id),
                                proposals=[{"role_id": role, "question": question}])
    if not proposal.get("created"):
        raise RoleFlowError(
            "step_failed_needs_decision: step proposal rejected ({0})".format(
                proposal.get("duplicates") or "unknown"))
    subtask_id = str(proposal["created"][0]["task_id"])
    overrides: Dict[str, Any] = {
        "role_step": {
            "schema_version": ROLE_EXECUTION_SCHEMA,
            "flow_id": str(flow["flow_id"]), "flow_sha256": str(flow["flow_sha256"]),
            "role_id": role, "prompt_version": int(prompt["version"]),
            "role_prompt_sha256": prompt["sha256"],
        },
        "role_prompt": prompt["text"],
    }
    if step_overrides:
        overrides.update(step_overrides)
    store.bump_input_version(subtask_id, payload_overrides=overrides,
                             actor=worker_id, reason="role_step:{0}:{1}".format(flow["flow_id"], role))

    _claim_specific(store, subtask_id, worker_id, skip_role_bound=True)
    from .research_worker import run_research_task  # local import: no import cycle

    run_research_task(store, subtask_id, worker_id=worker_id, config=config)
    subtask = store.get_task(subtask_id)
    _verify_step_binding(subtask, flow, role)
    return {"status": str(subtask["status"]), "role_id": role,
            "task_id": subtask_id, "output": dict(subtask.get("output") or {})}


def _verify_step_binding(subtask: Dict[str, Any], flow: Dict[str, Any], role: str) -> None:
    """Post-run boundary check: the persisted prompt still hashes to the
    digest the frozen artifact pinned — tampering between injection and
    execution invalidates the step instead of leaking through."""
    payload = subtask.get("input_payload") or {}
    step = payload.get("role_step") or {}
    recorded = str(step.get("role_prompt_sha256") or "")
    expected = str(flow["prompts"][role]["sha256"])
    actual = _sha256_text(str(payload.get("role_prompt") or ""))
    if recorded != expected or actual != expected:
        raise RoleFlowError(
            "step_binding_invalid: prompt digest mismatch for {0} "
            "(recorded={1}, current={2}, artifact={3})".format(
                role, recorded[:12], actual[:12], expected[:12]))


def finish_role_flow(store: TaskStore, root_task_id: str, *, flow: Dict[str, Any],
                     registry: FlowRegistry,
                     worker_id: str = "role-orchestrator") -> Dict[str, Any]:
    """Complete the root when EVERY flow role has a succeeded step.

    The root itself makes no model call; its output carries the per-role
    provenance (which step task produced which output) so the result stays
    traceable to its sources."""
    try:
        root = store.get_task(str(root_task_id))
    except TaskRuntimeError:
        raise RoleFlowError("unknown_task: {0}".format(root_task_id))
    if root["status"] in TERMINAL_STATUSES:
        raise RoleFlowError("task_terminal: {0} is {1}".format(root_task_id, root["status"]))
    _validate_binding(root, flow, registry)
    steps = flow_steps(store, root_task_id)
    by_role: Dict[str, Dict[str, Any]] = {}
    for task in steps:
        role = str(task.get("role_id") or "")
        if task["status"] == STATUS_SUCCEEDED and (role not in by_role
                                                   or str(task.get("created_at") or "")
                                                   > str(by_role[role].get("created_at") or "")):
            by_role[role] = task
    flow_roles = [r for group in flow["groups"] for r in group["roles"]]
    missing = [role for role in flow_roles if role not in by_role]
    if missing:
        raise RoleFlowError("flow_incomplete: missing succeeded steps for {0}".format(missing))

    claim = _claim_specific(store, str(root_task_id), worker_id, skip_role_bound=False)
    store.mark_running(str(root_task_id), worker_id=worker_id,
                       fencing_token=int(claim["fencing_token"]),
                       expected_version=int(claim["version"]))
    role_outputs = {
        role: {
            "task_id": str(task["task_id"]),
            "summary": str((task.get("output") or {}).get("summary") or ""),
            "output_sha256": _sha256_text(_canonical_json(task.get("output") or {})),
        }
        for role, task in by_role.items()
    }
    output = {
        "summary": "；".join(
            "{0}: {1}".format(role, role_outputs[role]["summary"][:80])
            for role in flow_roles if role_outputs[role]["summary"]) or "role flow complete",
        "role_outputs": role_outputs,
        "role_flow": {"flow_id": str(flow["flow_id"]),
                      "flow_version": int(flow["version"]),
                      "flow_sha256": str(flow["flow_sha256"])},
    }
    model_call = {
        "mode": "role_flow", "called": False, "rounds": [],
        "steps": [{"role_id": role, "task_id": role_outputs[role]["task_id"],
                   "output_sha256": role_outputs[role]["output_sha256"]}
                  for role in flow_roles],
    }
    fresh = store.complete(str(root_task_id), worker_id=worker_id,
                           fencing_token=int(claim["fencing_token"]),
                           output=output, model_call=model_call)
    return {"status": str(fresh["status"]), "task_id": str(root_task_id),
            "steps": model_call["steps"]}
