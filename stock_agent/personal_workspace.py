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
from .personal_evidence import PERSONAL_ORIGIN_VIA
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
    invalid_fact_selector, empty_note, already_acknowledged"""


class WorkspaceForbidden(WorkspaceError):
    """The session principal is not the owner of the targeted personal task.

    Raised BEFORE any create/mutate work so a denied request leaves the task
    store, input bindings, queue and model dispatch byte-for-byte unchanged.
    Shared research (no trusted personal binding) stays available to every
    signed-in principal — the documented legacy compatibility path."""


# Same cap the R13 fact bundle enforces; the entry rejects instead of letting
# the bundle silently truncate a user's selection.
MAX_CREATE_FACT_SELECTORS = 50


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


def _resolve_personal_fact_selectors(config: Optional[Dict[str, Any]], actor: str,
                                     fact_selectors: Optional[list]) -> List[Dict[str, Any]]:
    """Validate the user's excerpt-selector submission against the OWNER's
    personal namespace BEFORE anything is created (same fail-closed contract
    as the document selection): a selector that does not verify against the
    owner's own vault view — foreign namespace, stale hash, shifted range —
    never rides a task input, and a rejected create leaves the queue
    untouched. This grants no egress right: the worker re-verifies every
    selector against the CURRENT vault and host egress policy when it freezes
    the attempt's fact bundle (R13/A60–A63)."""
    wanted = list(fact_selectors or [])
    if not wanted:
        return []
    if len(wanted) > MAX_CREATE_FACT_SELECTORS:
        raise WorkspaceError(
            "invalid_fact_selector: at most {0} selectors per research".format(
                MAX_CREATE_FACT_SELECTORS))
    from .evidence_excerpts import SELECTOR_FIELDS, EvidenceExcerptError, verify_excerpt_selection
    from .personal_evidence import personal_vault_view

    view = personal_vault_view(config or {}, actor)
    resolved: List[Dict[str, Any]] = []
    for item in wanted:
        if not isinstance(item, dict) or set(item) != set(SELECTOR_FIELDS):
            raise WorkspaceError(
                "invalid_fact_selector: expected exactly the excerpt selector fields")
        try:
            verify_excerpt_selection(view, item)
        except EvidenceExcerptError as exc:
            raise WorkspaceError(
                "invalid_fact_selector: not verifiable in the personal namespace of {0}: {1}".format(
                    actor, exc))
        resolved.append({field: item[field] for field in SELECTOR_FIELDS})
    return resolved


def create_personal_research(store: TaskStore, *, actor: str, question: str,
                             symbols: list, execution_mode: str = "replay",
                             config: Optional[Dict[str, Any]] = None,
                             intent_id: str = "",
                             document_ids: Optional[list] = None,
                             fact_selectors: Optional[list] = None) -> Dict[str, Any]:
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
    selectors = _resolve_personal_fact_selectors(config, actor, fact_selectors)
    if selectors:
        # Every accepted fact rides WITH its source: a selector's document
        # joins the delivered evidence summaries (vault projection) even
        # when the user did not tick it separately, so each delivered fact
        # has a citable original. Worker-side gates still apply unchanged.
        from .personal_evidence import personal_documents

        projection = {str(item.get("document_id") or ""): item
                      for item in personal_documents(config or {}, actor).get("documents") or []}
        for selector in selectors:
            document_id = str(selector.get("document_id") or "")
            if not document_id or document_id in attachments:
                continue
            document = projection.get(document_id) or {}
            attachments[document_id] = {
                "summary": str(document.get("title") or "")[:1000] or "vault document",
                "sha256": str(document.get("sha256") or ""),
            }
    # The selected documents AND facts ride the task's FIRST input version,
    # inside the same create transaction: the task becomes claimable only
    # WITH its complete input, so any worker's first claim already sees the
    # sources. The R01 snapshot chain stays fully intact (v1 snapshot written
    # at creation; every later follow-up keeps bumping immutable versions).
    overrides: Dict[str, Any] = {}
    if attachments:
        overrides["public_context"] = {"evidence_summaries": attachments}
    if selectors:
        # R15: the user's approved announcement facts enter as excerpt
        # SELECTORS — content stays in the vault; the worker rebuilds and
        # re-verifies everything at attempt time (A62/A63).
        overrides["approved_facts"] = selectors
    result = ensure_question_task(
        store, research_question=question, symbols=symbols,
        config=config, actor=actor, execution_mode=execution_mode,
        intent_id=intent_id,
        spec_overrides=(overrides or None),
        # R15 ownership: the session-verified principal is bound to the task
        # in a SERVER-SIDE record written inside the create transaction —
        # business input can never write it, and a deduplicated retry keeps
        # the original task's binding.
        input_owner_binding={"owner": actor, "via": PERSONAL_ORIGIN_VIA})
    if str(result.get("status") or "") == "DUPLICATE":
        # Question identity is global (question + scope + mode), so a matching
        # case may already belong to another personal owner. Never hand that
        # task back: the duplicate submission is a read of someone else's case.
        # The DUPLICATE branch writes no new task/binding/queue row, so the
        # refusal leaves the store exactly as it was.
        from .personal_evidence import task_personal_owner

        existing = result.get("task") or {}
        existing_owner = task_personal_owner(store, str(existing.get("task_id") or ""))
        if existing_owner and existing_owner != str(actor or "").strip():
            raise WorkspaceForbidden(
                "not_owner: the matching case belongs to another principal")
    if str(result.get("status") or "") == "RECORDED":
        if attachments:
            result["documents_attached"] = len(attachments)
        if selectors:
            result["facts_attached"] = len(selectors)
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
    prior_task_id + as_of, so the case page can show both versions.

    Write ownership: only the session principal the original task's trusted
    input binding names may re-research it. The check runs before the intake,
    so a denied rerun creates no task, no binding, no queued row and triggers
    no model dispatch. Shared research (no personal binding) remains
    rerunnable — the compatibility path for generic/pipeline/historical
    tasks, whose content is already visible to signed-in principals."""
    task = store.get_task(str(task_id or ""))
    from .personal_evidence import task_personal_owner

    owner = task_personal_owner(store, str(task["task_id"]))
    if owner and owner != str(actor or "").strip():
        raise WorkspaceForbidden("not_owner: the original case belongs to another principal")
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
        new_research=True, as_of=now_iso(),
        input_owner_binding={"owner": actor, "via": PERSONAL_ORIGIN_VIA})


def follow_up_completed_research(store: TaskStore, *, actor: str, task_id: str,
                                 note: str,
                                 config: Optional[Dict[str, Any]] = None,
                                 intent_id: str = "") -> Dict[str, Any]:
    """A90: proactive follow-up on a result the model delivered WITHOUT a
    question (the task already sits succeeded).

    The follow-up text IS a new research question: it becomes a NEW task
    linked back to the finished one, and the finished result keeps its
    terminal state and output untouched (A91). The origin (prior_task_id +
    user_followup occasion) rides the SAME create transaction, so a worker's
    first claim already sees the correct link. ``intent_id`` (the page's
    command id) makes a lost-response retry of ONE submission deduplicate to
    the same task; no as_of is stamped here because an intent retry must
    rebuild byte-identical content to dedup."""
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
    origin_override = {
        "prior_task_id": str(task_id),
        "research_occasion": "user_followup",
        "followup_note": note[:2000],
    }
    intent_id = str(intent_id or "").strip()
    if intent_id:
        # A retry of ONE submission (e.g. the first response was lost): the
        # intent key returns the already-created follow-up task. The lookup
        # checks the LOGICAL identity (same follow-up text against the same
        # prior case) rather than rebuilding the payload, so a market or
        # evidence refresh between the two arrivals cannot turn the retry
        # into a 409 — the page still recovers the first task's id. A
        # DIFFERENT note or prior under the same id falls through to the
        # intake, which reports the conflict.
        existing = store.find_task_by_idempotency_key(
            "question_intent::{0}".format(intent_id))
        if existing is not None:
            payload = existing.get("input_payload") or {}
            origin = payload.get("origin") if isinstance(payload.get("origin"), dict) else {}
            if (str(payload.get("research_question") or "") == note
                    and str(origin.get("prior_task_id") or "") == str(task_id)):
                return {
                    "task": existing,
                    "status": "DUPLICATE",
                    "event_key": None,
                    "base_key": None,
                    "supersedes_task_id": origin.get("prior_task_id"),
                    "business_intent_id": intent_id,
                }
        return ensure_question_task(
            store, research_question=note, symbols=symbols,
            config=config, actor=actor,
            execution_mode=str(task.get("execution_mode") or "replay"),
            intent_id=intent_id,
            spec_overrides={"origin": origin_override},
            input_owner_binding={"owner": actor, "via": PERSONAL_ORIGIN_VIA})
    return ensure_question_task(
        store, research_question=note, symbols=symbols,
        config=config, actor=actor,
        execution_mode=str(task.get("execution_mode") or "replay"),
        new_research=True,
        spec_overrides={"origin": origin_override},
        input_owner_binding={"owner": actor, "via": PERSONAL_ORIGIN_VIA})


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
