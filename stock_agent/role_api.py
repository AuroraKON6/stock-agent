"""R17: the controlled role entry Dify calls to drive a bound task.

The Dify canvas only DECIDES what to call next; every authority stays on
this side. One workflow run goes create -> bind -> one call per role step
in the frozen order -> finish, and each call is re-checked here against the
CURRENT task state, so a canvas that skips, repeats or reorders a step is
refused rather than executed:

- **credential**: the ``run_role_flow`` scope plus the case scope;
- **ownership**: the task's case is the caller's case, the task originated
  in Dify, and this ``dify_run_id`` is one of the runs recorded on the task;
- **input version**: the caller's version equals the authoritative column —
  a late call for an older input never acts on the current task;
- **artifact**: the requested flow digest is a registered candidate AND the
  approved digest. A prompt or canvas edit produces a new digest that no
  old approval covers (A77);
- **order / role**: group order and role membership are enforced by
  ``role_execution`` (A76).

Replays are idempotent rather than refused where nothing would be re-billed:
a repeated step returns the succeeded step, a repeated finish the completed
root. Retries are the task's own bounded attempts — one step task per role,
each attempt ledgered — and a terminally failed step needs a decision (A78).
Every call, allowed or denied, lands in the ``role_flow`` audit ledger.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from . import dify_access
from .role_execution import RoleFlowError, bind_role_flow, execute_role_step, finish_role_flow
from .task_runtime.store import STATUS_QUEUED, STATUS_SUCCEEDED, TaskRuntimeError

ROLE_SCOPE = "run_role_flow"
_SAFE_CASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def _reason(exc: RoleFlowError) -> str:
    return str(exc).split(":", 1)[0]


class RoleGatewayMixin:
    """Mixed into ``DifyGateway``; uses its store, gate, settings and ledgers."""

    store: Any
    role_flows: Any
    role_replay_dir: Optional[Any]
    role_execution_mode: str

    # ------------------------------------------------------------------ #
    # shared entry checks                                                 #
    # ------------------------------------------------------------------ #

    def _role_entry(self, operation: str, token: str, *, task_id: str, case_id: str,
                    dify_run_id: str, workflow_version: str, flow_sha256: str,
                    input_version: Any) -> Dict[str, Any]:
        if self.role_flows is None:
            raise RoleFlowError("role_entry_disabled: no role flow registry is configured")
        for name, value in (("task_id", task_id), ("case_id", case_id),
                            ("dify_run_id", dify_run_id), ("workflow_version", workflow_version),
                            ("flow_sha256", flow_sha256)):
            if not str(value or "").strip():
                raise ValueError("{0} must not be empty".format(name))
        if isinstance(input_version, bool) or not isinstance(input_version, int) or input_version < 1:
            raise ValueError("input_version must be a positive integer")
        if not _SAFE_CASE_ID.fullmatch(str(case_id)):
            raise dify_access.DifyAccessError("task_not_found_or_out_of_scope", str(task_id))
        # Scope only: the case is judged below together with task and run, so
        # an outsider learns nothing about which cases or tasks exist.
        identity = self._gate(token, ROLE_SCOPE, operation=operation)
        try:
            task = self.store.get_task(str(task_id))
        except TaskRuntimeError:
            raise dify_access.DifyAccessError("task_not_found_or_out_of_scope", str(task_id))
        # Not-found, other-case and unassociated runs share one reason so
        # task ids and runs cannot be enumerated through the entry.
        hidden = dify_access.DifyAccessError("task_not_found_or_out_of_scope", str(task_id))
        payload = task.get("input_payload") or {}
        run = self.store.get_platform_run("dify", str(dify_run_id))
        if (str(task.get("case_id") or "") != str(case_id)
                or (payload.get("origin") or {}).get("kind") != "dify"
                or run is None or str(run.get("task_id")) != str(task.get("task_id"))):
            self._audit(identity["credential_id"], operation, allowed=False,
                        reason=hidden.reason, task_id=str(task_id))
            raise hidden
        try:
            dify_access.ensure_case_scope(identity, str(case_id))
        except dify_access.DifyAccessError:
            self._audit(identity["credential_id"], operation, allowed=False,
                        reason=hidden.reason, task_id=str(task_id))
            raise hidden
        context: Dict[str, Any] = {
            "task_id": str(task.get("task_id")), "case_id": str(case_id),
            "dify_run_id": str(dify_run_id), "workflow_version": str(workflow_version),
            "flow_sha256": str(flow_sha256), "input_version": int(input_version),
            "credential_id": identity["credential_id"],
        }
        try:
            if (self.role_workflow_version and str(workflow_version) != self.role_workflow_version) \
                    or str(workflow_version) not in set(
                        self.settings.get("approved_workflow_versions") or []):
                raise dify_access.DifyAccessError(
                    "workflow_version_not_approved",
                    "workflow_version is not in the approved list")
            if int(input_version) != int(task.get("input_version") or 1):
                raise RoleFlowError(
                    "input_version_mismatch: call carries version {0}, task is at {1}".format(
                        input_version, task.get("input_version")))
            if str(task.get("execution_mode") or "") != self.role_execution_mode:
                raise RoleFlowError(
                    "execution_mode_mismatch: task is {0}, this entry runs {1}".format(
                        task.get("execution_mode"), self.role_execution_mode))
            if str(run.get("workflow_version") or "") != str(workflow_version):
                raise dify_access.DifyAccessError("workflow_version_not_approved", "workflow does not match the recorded run")
            flow = self.role_flows.by_digest(str(flow_sha256))
            if self.role_flow_sha256 and flow["flow_sha256"] != self.role_flow_sha256:
                raise RoleFlowError("binding_digest_changed: flow does not match the reviewed canvas")
            # The approver of record for THIS digest (never a neighbouring candidate's).
            approval = self.role_flows.approval_status(str(flow["flow_id"]), str(flow["flow_sha256"]))
        except (RoleFlowError, dify_access.DifyAccessError) as exc:
            self._role_audit(operation, context, allowed=False, executed=False,
                             reason=getattr(exc, "reason", None) or _reason(exc))
            raise
        context["flow"] = flow
        context["approval"] = approval
        return context

    def _role_audit(self, operation: str, context: Dict[str, Any], *, allowed: bool,
                    executed: Optional[bool], reason: str = "ok", **extra: Any) -> None:
        flow = context.get("flow") or {}
        approval = context.get("approval") or {}
        self._append_ledger("data/dify/role_flow.jsonl", {
            "record_type": "role_flow_access",
            "ts": self._now_iso(), "operation": operation,
            "allowed": allowed, "executed": executed, "reason": reason,
            "credential_id": context.get("credential_id"),
            "task_id": context.get("task_id"), "case_id": context.get("case_id"),
            "dify_run_id": context.get("dify_run_id"),
            "workflow_version": context.get("workflow_version"),
            "input_version": context.get("input_version"),
            # Which reviewed artifact this call actually ran under.
            "flow_id": flow.get("flow_id"), "flow_version": flow.get("version"),
            "flow_sha256": flow.get("flow_sha256"),
            "approved_by": approval.get("actor"),
            **extra,
        })

    def _now_iso(self) -> str:
        from datetime import datetime

        return datetime.now().astimezone().isoformat()

    # ------------------------------------------------------------------ #
    # replay fixtures (isolated metering only)                            #
    # ------------------------------------------------------------------ #

    def _role_replay_response(self, case_id: str, role_id: str, attempt: int) -> Optional[Dict[str, Any]]:
        """The recorded answer for one step attempt, or None to withhold it.

        ``<replay_dir>/<case>/<role>.json`` is the standing answer;
        ``<role>.attempt-<n>.json`` overrides the n-th attempt of that step.
        With no fixture the step fails retryably — the metered way to drill
        a bounded retry or an exhausted attempt budget."""
        if self.role_replay_dir is None:
            return None
        folder = self.role_replay_dir / str(case_id)
        for candidate in (folder / "{0}.attempt-{1}.json".format(role_id, int(attempt)),
                          folder / "{0}.json".format(role_id)):
            if not candidate.is_file():
                continue
            document = json.loads(candidate.read_text(encoding="utf-8"))
            return dict(document) if isinstance(document, dict) else None
        return None

    # ------------------------------------------------------------------ #
    # contract: bind / step / finish                                      #
    # ------------------------------------------------------------------ #

    def role_bind(self, token: str, *, task_id: str, case_id: str, dify_run_id: str,
                  workflow_version: str, flow_sha256: str, input_version: int) -> Dict[str, Any]:
        """Switch a queued Dify-created task to role mode (audited input version)."""
        context = self._role_entry("role-bind", token, task_id=task_id, case_id=case_id,
                                   dify_run_id=dify_run_id, workflow_version=workflow_version,
                                   flow_sha256=flow_sha256, input_version=input_version)
        flow = context["flow"]
        task = self.store.get_task(str(task_id))
        binding = (task.get("input_payload") or {}).get("role_flow")
        try:
            if isinstance(binding, dict):
                if str(binding.get("flow_sha256")) != str(flow["flow_sha256"]):
                    raise RoleFlowError(
                        "binding_digest_changed: task is bound to {0}, call names {1}".format(
                            str(binding.get("flow_sha256"))[:12], str(flow["flow_sha256"])[:12]))
                result = {"outcome": "bound", "bind_result": "already_bound", "executed": False}
            else:
                if not self.role_bind_enabled:
                    raise RoleFlowError("role_entry_disabled: new role bindings are disabled")
                if task["status"] != STATUS_QUEUED:
                    raise RoleFlowError(
                        "task_not_bindable: {0} is {1}, only a queued task can be bound".format(
                            task_id, task["status"]))
                fresh = bind_role_flow(self.store, str(task_id), flow=flow,
                                       registry=self.role_flows,
                                       actor="dify:{0}".format(context["credential_id"]), queued_only=True)
                result = {"outcome": "bound", "bind_result": "bound", "executed": True}
                task = fresh
        except RoleFlowError as exc:
            self._role_audit("role-bind", context, allowed=True, executed=False, reason=_reason(exc))
            raise
        self._role_audit("role-bind", context, allowed=True, executed=result["executed"],
                         bind_result=result["bind_result"])
        return {
            **result, "task_id": str(task.get("task_id")), "case_id": str(task.get("case_id")),
            "status": str(task.get("status")),
            "input_version": int(task.get("input_version") or 1),
            "flow": {"flow_id": str(flow["flow_id"]), "flow_version": int(flow["version"]),
                     "flow_sha256": str(flow["flow_sha256"])},
        }

    def role_step(self, token: str, *, task_id: str, case_id: str, dify_run_id: str,
                  workflow_version: str, flow_sha256: str, input_version: int,
                  role_id: str) -> Dict[str, Any]:
        """Run ONE role step of the bound flow, exactly once per step task."""
        context = self._role_entry("role-step", token, task_id=task_id, case_id=case_id,
                                   dify_run_id=dify_run_id, workflow_version=workflow_version,
                                   flow_sha256=flow_sha256, input_version=input_version)
        flow = context["flow"]
        try:
            if str(role_id) not in flow["prompts"]:
                raise RoleFlowError("role_not_in_flow: {0!r}".format(role_id))
            existing = [step for step in self._role_steps(str(task_id))
                        if str(step.get("role_id") or "") == str(role_id)]
            attempt = int(existing[0].get("attempt_count") or 0) + 1 if existing else 1
            response = self._role_replay_response(str(case_id), str(role_id), attempt)
            result = execute_role_step(
                self.store, str(task_id), role_id=str(role_id), flow=flow,
                registry=self.role_flows, worker_id="role-orchestrator:{0}".format(
                    context["credential_id"]),
                config=self.role_config,
                step_overrides={"replay_response": response} if self.role_execution_mode == "replay" else None)
        except RoleFlowError as exc:
            succeeded = _reason(exc) == "step_already_succeeded"
            self._role_audit("role-step", context, allowed=True, executed=False,
                             role_id=str(role_id), reason=_reason(exc))
            if not succeeded:
                raise
            # A78: the step already has its succeeded task — hand it back,
            # create nothing, bill nothing.
            step = next(step for step in self._role_steps(str(task_id))
                        if step["role_id"] == role_id and step["status"] == STATUS_SUCCEEDED)
            result = {"status": STATUS_SUCCEEDED, "role_id": str(role_id),
                      "task_id": str(step["task_id"]), "output": dict(step.get("output") or {}),
                      "executed": False}
        step = self.store.get_task(str(result["task_id"]))
        executed = bool(result.get("executed", True))
        self._role_audit("role-step", context, allowed=True, executed=executed, role_id=str(role_id),
                         step_task_id=str(step["task_id"]), step_status=str(step["status"]),
                         attempts=int(step.get("attempt_count") or 0))
        return {
            "outcome": "step", "step_status": str(step["status"]), "executed": executed,
            "role_id": str(role_id), "task_id": str(context["task_id"]),
            "step_task_id": str(step["task_id"]),
            "input_version": int(context["input_version"]),
            "flow_sha256": str(flow["flow_sha256"]),
            "metering": self._step_metering(step),
        }

    def role_finish(self, token: str, *, task_id: str, case_id: str, dify_run_id: str,
                    workflow_version: str, flow_sha256: str, input_version: int) -> Dict[str, Any]:
        """Complete the root from its succeeded steps; the root makes no model call."""
        context = self._role_entry("role-finish", token, task_id=task_id, case_id=case_id,
                                   dify_run_id=dify_run_id, workflow_version=workflow_version,
                                   flow_sha256=flow_sha256, input_version=input_version)
        flow = context["flow"]
        try:
            result = finish_role_flow(
                self.store, str(task_id), flow=flow, registry=self.role_flows,
                worker_id="role-orchestrator:{0}".format(context["credential_id"]))
        except RoleFlowError as exc:
            self._role_audit("role-finish", context, allowed=True, executed=False, reason=_reason(exc))
            raise
        root = self.store.get_task(str(task_id))
        steps = self._role_steps(str(task_id))
        self._role_audit("role-finish", context, allowed=True,
                         executed=bool(result.get("executed", True)),
                         root_status=str(root["status"]))
        return {
            "outcome": "finished", "status": str(root["status"]), "task_id": str(task_id),
            "executed": bool(result.get("executed", True)),
            "already_complete": bool(result.get("already_complete")),
            "input_version": int(root.get("input_version") or 1),
            "flow_sha256": str(flow["flow_sha256"]),
            "role_outputs": dict((root.get("output") or {}).get("role_outputs") or {}),
            "steps": [{"role_id": str(step.get("role_id")), "task_id": str(step["task_id"]),
                       "status": str(step["status"]),
                       **self._step_metering(step)} for step in steps],
        }

    # ------------------------------------------------------------------ #
    # projections                                                         #
    # ------------------------------------------------------------------ #

    @property
    def role_config(self) -> Dict[str, Any]:
        config = dict(self.config or {})
        config.setdefault("task_runtime", {})
        config["task_runtime"] = {**config["task_runtime"], "db_path": str(self._db_path)}
        return config

    def _role_steps(self, root_task_id: str):
        return [task for task in self.store.list_tasks()
                if str(task.get("parent_task_id") or "") == str(root_task_id)
                and ((task.get("input_payload") or {}).get("origin") or {}).get("kind") == "subtask"]

    @staticmethod
    def _step_metering(step: Dict[str, Any]) -> Dict[str, Any]:
        """What this step actually spent: attempts on the task and the
        provider's statement, attributed to the attempt that ran."""
        call = step.get("model_call") or {}
        usage = call.get("usage") if isinstance(call, dict) else None
        rounds = call.get("rounds") if isinstance(call, dict) else None
        return {
            "attempts": int(step.get("attempt_count") or 0),
            "max_attempts": int(step.get("max_attempts") or 0),
            "mode": str(step.get("execution_mode") or call.get("mode") or ""),
            "model_called": bool(call.get("called")),
            "model_rounds": len(rounds) if isinstance(rounds, list) else 0,
            "provider": str(call.get("provider") or ""), "model": str(call.get("request_model") or call.get("model") or ""),
            "usage": dict(usage) if isinstance(usage, dict) else {},
            "last_error": str(step.get("last_error") or "")[:300],
        }
