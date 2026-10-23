"""R14: personal research workspace — the full user loop behind one service.

Closes F20/F21/F22 at the service level so both the board pages and any
future façade share one behavior set:

- **create**: a personal research question becomes a queued task (R01 intake:
  same question+scope+mode deduplicates; the session actor is the owner).
- **cancel**: one click on an unfinished task (expected_version optimistic
  lock), for queued/awaiting states it lands immediately, for running ones it
  wins at the next worker checkpoint.
- **follow-up while awaiting**: provide_input bumps input_version (R01
  snapshot chain) — the input is preserved, never overwritten.
- **re-research after completion (A90/A91/A92)**: the model NOT asking a
  question never blocks the user; an explicit rerun creates a NEW business
  intent with as_of recorded and prior_task_id linked — the old terminal
  result stays untouched, while a network retry of the SAME intent still
  deduplicates.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .models import now_iso
from .task_intake import ensure_question_task
from .task_runtime import TaskStore
from .task_runtime.store import (
    STATUS_AWAITING_HUMAN,
    TERMINAL_STATUSES,
    TaskRuntimeError,
    TaskVersionConflictError,
)


class WorkspaceError(ValueError):
    """reason codes: empty_question, no_symbols, unknown_task, task_terminal,
    not_awaiting, cancel_terminal"""


def create_personal_research(store: TaskStore, *, actor: str, question: str,
                             symbols: list, execution_mode: str = "replay",
                             config: Optional[Dict[str, Any]] = None,
                             intent_id: str = "") -> Dict[str, Any]:
    question = str(question or "").strip()
    symbols = [str(s).strip() for s in (symbols or []) if str(s).strip()]
    if not question:
        raise WorkspaceError("empty_question: research question required")
    if not symbols:
        raise WorkspaceError("no_symbols: name at least one symbol")
    result = ensure_question_task(
        store, research_question=question, symbols=symbols,
        config=config, actor=actor, execution_mode=execution_mode,
        intent_id=intent_id)
    return result


def cancel_personal_task(store: TaskStore, *, actor: str, task_id: str,
                         expected_version: int, reason: str = "") -> Dict[str, Any]:
    task = store.get_task(str(task_id or ""))
    if task["status"] in TERMINAL_STATUSES:
        raise WorkspaceError("cancel_terminal: task already finished ({0})".format(task["status"]))
    return store.request_cancel(
        str(task_id), actor=actor,
        reason=str(reason or "用户取消")[:500],
        expected_version=int(expected_version))


def follow_up_awaiting(store: TaskStore, *, actor: str, task_id: str,
                       expected_version: int, note: str = "",
                       command_id: str = "",
                       extra_note: str = "") -> Dict[str, Any]:
    """Provide input while the task awaits the user (A64): the input version
    bumps and the previous input stays in the snapshot chain."""
    task = store.get_task(str(task_id or ""))
    if task["status"] != STATUS_AWAITING_HUMAN:
        raise WorkspaceError(
            "not_awaiting: follow-up targets awaiting_human, got {0}".format(task["status"]))
    patch = {"extra_note": str(extra_note or note or "").strip()[:2000]}
    return store.record_human_response(
        str(task_id), actor=actor, decision="provide_input",
        expected_version=int(expected_version),
        command_id=str(command_id or ""), input_patch=patch,
        notes=str(note or "")[:2000])


def rerun_completed_research(store: TaskStore, *, actor: str, task_id: str,
                             config: Optional[Dict[str, Any]] = None,
                             execution_mode: str = "replay") -> Dict[str, Any]:
    """A90/A91/A92: explicit re-research of a finished (or awaiting) task.

    The ORIGINAL task keeps its terminal state and output; the rerun is a NEW
    business intent (uuid minted by the intake) whose origin links
    prior_task_id + as_of, so the case page can show both versions."""
    task = store.get_task(str(task_id or ""))
    payload = task.get("input_payload") or {}
    question = str(payload.get("research_question") or "").strip()
    symbols = [str(s).strip() for s in (payload.get("symbols") or []) if str(s).strip()]
    if not question or not symbols:
        raise WorkspaceError("unknown_task: original input lacks a restatable question/scope")
    if task["status"] not in TERMINAL_STATUSES and task["status"] != STATUS_AWAITING_HUMAN:
        # A running/queued task needs no rerun — its result is not final.
        raise WorkspaceError(
            "task_terminal: rerun targets finished tasks, got {0}".format(task["status"]))
    return ensure_question_task(
        store, research_question=question, symbols=symbols,
        config=config, actor=actor, execution_mode=execution_mode,
        new_research=True, as_of=now_iso())
