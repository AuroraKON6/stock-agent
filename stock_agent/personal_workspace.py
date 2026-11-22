"""R14: personal research workspace — the full user loop behind one service.

Closes F20/F21/F22 at the service level so both the board pages and any
future façade share one behavior set:

- **create**: a personal research question becomes a queued task (R01 intake:
  same question+scope+mode deduplicates; the session actor is the owner).
  Documents selected from the owner's personal namespace (A93) attach as a
  governed new input version — never a raw payload overwrite.
- **cancel**: one click on an unfinished task (expected_version optimistic
  lock), for queued/awaiting states it lands immediately, for running ones it
  wins at the next worker checkpoint.
- **follow-up while awaiting**: provide_input bumps input_version (R01
  snapshot chain) — the input is preserved, never overwritten.
- **follow-up after a no-question completion (A90)**: the model asking
  nothing must not end the conversation — the user's follow-up question
  becomes a NEW linked task (prior_task_id + user_followup occasion) and the
  finished result stays byte-for-byte intact.
- **personal sign-off on a no-question completion (A90/A50)**: a succeeded
  result with no pending question can still be acknowledged personally; the
  acknowledgement is ADDED to the preserved output, never rewriting it.
- **re-research after completion (A90/A91/A92)**: an explicit rerun creates a
  NEW business intent with as_of recorded and prior_task_id linked — the old
  terminal result stays untouched, while a network retry of the SAME intent
  still deduplicates.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .models import now_iso
from .task_intake import ensure_question_task
from .task_runtime import TaskStore
from .task_runtime.store import (
    STATUS_AWAITING_HUMAN,
    STATUS_SUCCEEDED,
    TERMINAL_STATUSES,
    TaskRuntimeError,
    TaskVersionConflictError,
)


class WorkspaceError(ValueError):
    """reason codes: empty_question, no_symbols, unknown_task, task_terminal,
    not_awaiting, not_succeeded, cancel_terminal, unknown_document,
    empty_note, already_acknowledged"""


def _resolve_personal_documents(config: Optional[Dict[str, Any]], actor: str,
                                document_ids: Optional[list]) -> Dict[str, Dict[str, Any]]:
    """Resolve the user's document selection against the OWNER's personal
    namespace BEFORE anything is created, so an invalid selection can never
    leave a half-created (document-less) task behind."""
    wanted = [str(item).strip() for item in (document_ids or []) if str(item).strip()]
    if not wanted:
        return {}
    from .personal_evidence import personal_documents

    available = {}
    for document in personal_documents(config or {}, actor).get("documents") or []:
        available[str(document.get("document_id") or "")] = document
    unknown = [item for item in wanted if item not in available]
    if unknown:
        raise WorkspaceError(
            "unknown_document: not in the personal namespace of {0}: {1}".format(
                actor, ",".join(unknown)))
    return {
        document_id: {
            "summary": str(available[document_id].get("title") or "")[:1000],
            "sha256": str(available[document_id].get("sha256") or ""),
        }
        for document_id in wanted
    }


def create_personal_research(store: TaskStore, *, actor: str, question: str,
                             symbols: list, execution_mode: str = "replay",
                             config: Optional[Dict[str, Any]] = None,
                             intent_id: str = "",
                             document_ids: Optional[list] = None) -> Dict[str, Any]:
    question = str(question or "").strip()
    symbols = [str(s).strip() for s in (symbols or []) if str(s).strip()]
    if not question:
        raise WorkspaceError("empty_question: research question required")
    if not symbols:
        raise WorkspaceError("no_symbols: name at least one symbol")
    # Validate the selection FIRST: a failed create must leave the queue
    # untouched, so retrying with a corrected selection creates exactly one
    # task (a leftover orphan would swallow the retry as DUPLICATE).
    attachments = _resolve_personal_documents(config, actor, document_ids)
    result = ensure_question_task(
        store, research_question=question, symbols=symbols,
        config=config, actor=actor, execution_mode=execution_mode,
        intent_id=intent_id)
    attached = _attach_personal_documents(
        store, result, actor=actor, attachments=attachments)
    if attached is not None:
        result["task"] = attached
        result["documents_attached"] = len(attachments)
    return result


def _attach_personal_documents(store: TaskStore, result: Dict[str, Any], *,
                               actor: str,
                               attachments: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A93 page wiring: documents the user selected at create time ride the
    task payload as an additional immutable input version (R01 snapshot chain
    via ``bump_input_version`` — the governed path, never a raw payload edit).

    Selections are already namespace-verified by the caller; a DUPLICATE
    intake keeps the existing task untouched."""
    if not attachments:
        return None
    if str(result.get("status") or "") != "RECORDED":
        return None  # a retry/dedup hit returns the existing task as-is
    task = result.get("task") or {}
    payload = task.get("input_payload") or {}
    context = payload.get("public_context") if isinstance(payload.get("public_context"), dict) else {}
    summaries = dict(context.get("evidence_summaries") if isinstance(context.get("evidence_summaries"), dict) else {})
    summaries.update(attachments)
    merged_context = dict(context, evidence_summaries=summaries)
    return store.bump_input_version(
        str(task.get("task_id") or ""),
        payload_overrides={"public_context": merged_context},
        actor=actor, reason="attach_personal_documents")


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


def follow_up_completed_research(store: TaskStore, *, actor: str, task_id: str,
                                 note: str,
                                 config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A90: proactive follow-up on a result the model delivered WITHOUT a
    question (the task already sits succeeded).

    The follow-up text IS a new research question: it becomes a NEW task
    linked back to the finished one (prior_task_id + user_followup occasion,
    recorded as a governed input version), and the finished result keeps its
    terminal state and output untouched (A91)."""
    task = store.get_task(str(task_id or ""))
    if task["status"] != STATUS_SUCCEEDED:
        raise WorkspaceError(
            "not_succeeded: follow-up on a finished result targets succeeded, got {0}".format(
                task["status"]))
    note = str(note or "").strip()
    if not note:
        raise WorkspaceError("empty_note: follow-up question required")
    payload = task.get("input_payload") or {}
    symbols = [str(s).strip() for s in (payload.get("symbols") or []) if str(s).strip()]
    if not symbols:
        raise WorkspaceError("unknown_task: original input lacks a symbol scope")
    result = ensure_question_task(
        store, research_question=note, symbols=symbols,
        config=config, actor=actor,
        execution_mode=str(task.get("execution_mode") or "replay"),
        new_research=True)
    if str(result.get("status") or "") != "RECORDED":
        # The same follow-up text already exists — return it unchanged.
        return result
    created = result.get("task") or {}
    origin = dict((created.get("input_payload") or {}).get("origin") or {})
    # Tag the link explicitly: a fresh question text carries no prior of its
    # own, so the followed-up task is bound here in one governed version.
    origin["prior_task_id"] = str(task_id)
    origin["research_occasion"] = "user_followup"
    origin["followup_note"] = note[:2000]
    result["task"] = store.bump_input_version(
        str(created.get("task_id") or ""),
        payload_overrides={"origin": origin},
        actor=actor, reason="user_followup")
    return result


def acknowledge_completed_research(store: TaskStore, *, actor: str, task_id: str,
                                   expected_version: int,
                                   note: str = "") -> Dict[str, Any]:
    """A90/A50: personal sign-off on a result that needs no further input.

    The model asked nothing, the result is final — the acknowledgement is
    ADDED to the preserved output (scope: personal, never institutional);
    status and every prior output byte stay as they were."""
    task = store.get_task(str(task_id or ""))
    if task["status"] != STATUS_SUCCEEDED:
        raise WorkspaceError(
            "not_succeeded: sign-off targets succeeded results, got {0}".format(
                task["status"]))
    if isinstance(task.get("output"), dict) and task["output"].get("personal_acknowledgement"):
        raise WorkspaceError("already_acknowledged: this result already carries a personal acknowledgement")
    return store.record_personal_acknowledgement(
        str(task_id), actor=actor, notes=str(note or "").strip()[:2000],
        expected_version=int(expected_version))
