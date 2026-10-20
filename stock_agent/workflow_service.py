"""R03: neutral workflow service layer — per-request store connections.

Platform-agnostic task operations shared by every façade (the Dify gateway
today; the inbound Research MCP later). Two disciplines live HERE so façades
cannot forget them:

1. **One store connection per request.** HTTP handler threads (and, later,
   MCP sessions) never share a mutable SQLite connection: each operation
   opens its own TaskStore, which serializes through BEGIN IMMEDIATE. This
   closes F06 for good — the façade no longer holds a request-crossing
   connection at all.

2. **Protected actions carry their principal verification INTO the store**,
   where the claim is atomic with the task mutation (F07).

The layer owns no authorization decisions: scopes, case scopes and Dify
credential handling stay in the façades; this module only executes task
operations that an authorized caller asked for.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .task_runtime.store import TaskStore


@contextmanager
def request_store(db_path):
    """Fresh TaskStore connection for exactly one request/operation."""
    store = TaskStore(Path(db_path))
    try:
        yield store
    finally:
        store.close()


def create_research_task(db_path, *, title: str, input_payload: Dict[str, Any],
                         case_id: str = "", business_intent_id: str = "",
                         idempotency_key: str = "", prompt_version: str = "research_v1",
                         execution_mode: str = "live", actor: str = "operator",
                         platform_run: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], str]:
    with request_store(db_path) as store:
        return store.create_task(
            title=title, input_payload=input_payload, case_id=case_id,
            business_intent_id=business_intent_id, idempotency_key=idempotency_key,
            prompt_version=prompt_version, execution_mode=execution_mode,
            actor=actor, platform_run=platform_run)


def get_task(db_path, task_id: str) -> Dict[str, Any]:
    with request_store(db_path) as store:
        return store.get_task(task_id)


def budget_view(db_path, config: Dict[str, Any], *, now=None) -> Dict[str, Any]:
    from .budget_guard import may_start_model_call

    with request_store(db_path) as store:
        return may_start_model_call(store, config, now=now)


def submit_feedback_task(db_path, task_id: str, *, actor: str, decision: str,
                         expected_version: int, command_id: str,
                         input_patch: Optional[Dict[str, Any]] = None, notes: str = "",
                         principal_verification: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    with request_store(db_path) as store:
        return store.record_human_response(
            task_id, actor=actor, decision=decision, expected_version=expected_version,
            command_id=command_id, input_patch=input_patch, notes=notes,
            principal_verification=principal_verification)


def cancel_task(db_path, task_id: str, *, actor: str, reason: str, expected_version: int,
                principal_verification: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    with request_store(db_path) as store:
        return store.request_cancel(
            task_id, actor=actor, reason=reason, expected_version=expected_version,
            principal_verification=principal_verification)


def platform_runs(db_path, task_id: str):
    with request_store(db_path) as store:
        return store.list_platform_runs(task_id)


def platform_run(db_path, platform: str, run_id: str):
    with request_store(db_path) as store:
        return store.get_platform_run(platform, run_id)


def issue_workflow_dispatch(db_path, task_id: str, **kwargs) -> Dict[str, Any]:
    with request_store(db_path) as store:
        return store.issue_workflow_dispatch(task_id, **kwargs)


def validate_workflow_dispatch(db_path, dispatch_id: str, **kwargs) -> Dict[str, Any]:
    with request_store(db_path) as store:
        return store.validate_workflow_dispatch(dispatch_id, **kwargs)


def consume_workflow_dispatch(db_path, dispatch_id: str, **kwargs) -> Dict[str, Any]:
    with request_store(db_path) as store:
        return store.consume_workflow_dispatch(dispatch_id, **kwargs)
