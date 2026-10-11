"""Version-bound local draft review, in the existing task DB and atomic outbox."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import uuid

from .evidence_excerpts import bind_summary_draft, EvidenceExcerptError, verify_excerpt_selection
from .local_identity import authenticate_local_actor, local_actor_binding, LocalIdentityError
from .task_runtime import TaskRuntimeError, TaskVersionConflictError, TaskIdempotencyConflictError

DECISIONS = {"supported", "reject", "request_changes", "revoke"}
SCOPE = "local_summary_support_review_not_institutional_approval"


class SummaryReviewError(TaskRuntimeError):
    pass


def _now():
    return datetime.now(timezone.utc)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _context_hash(task):
    output = dict(task["output"])
    output.pop("personal_acknowledgement", None)
    return _hash({key: task[key] for key in (
        "input_payload", "input_version", "execution_mode", "prompt_version", "model_config_hash", "model_call",
    )} | {"output": output})


@contextmanager
def _transaction(store):
    store._begin()
    try:
        yield
        store._commit()
    except BaseException:
        store._rollback()
        raise


def _task(store, task_id):
    task = store.get_task(task_id)
    if task["status"] not in {"awaiting_human", "succeeded"} or task["cancel_requested"]:
        raise SummaryReviewError("summary review needs a completed draft or awaiting-human task")
    if not isinstance(task["output"].get("summary"), str) or not task["output"]["summary"].strip():
        raise SummaryReviewError("task has no persisted research summary")
    if task["input_payload"].get("data_classification") != "public":
        raise SummaryReviewError("task scope is not public")
    return task


def _sources(config, task, selectors, text, kind):
    from .research_worker import build_safe_research_context
    bound = bind_summary_draft(config, selectors, text, kind)
    safe, _ = build_safe_research_context(task["input_payload"], config)
    for selector in selectors:
        entry = safe["evidence_summaries"].get(selector["document_id"])
        if not entry or entry["sha256"] != selector["document_sha256"]:
            raise SummaryReviewError("excerpt is not currently bound to this task's verified signals")
    return bound


def _check_version(task, expected):
    if type(expected) is not int or expected != task["version"]:
        raise TaskVersionConflictError("task version changed; reread the draft before deciding")


def _receipt(store, command_id, request):
    if not isinstance(command_id, str) or not 1 <= len(command_id.strip()) <= 160:
        raise SummaryReviewError("a bounded nonempty command_id is required")
    row = store._conn.execute("SELECT result FROM idempotency_receipts WHERE command_id=?", (command_id,)).fetchone()
    if row is None:
        return None
    result = json.loads(row["result"])
    if result.get("request_sha256") != _hash(request):
        raise TaskIdempotencyConflictError("command_id belongs to a different operation/content")
    return result["draft_id"]


def _record_receipt(store, command_id, task_id, request, draft_id):
    store._conn.execute("INSERT INTO idempotency_receipts(command_id,task_id,result,created_at) VALUES(?,?,?,?)",
                        (command_id, task_id, _json({"request_sha256": _hash(request), "draft_id": draft_id}), _now().isoformat()))


def submit_draft(store, task_id, *, config, actor, selectors, text, kind, expected_version, command_id):
    request = dict(operation="submit_summary", task_id=task_id, actor=actor, selectors=selectors,
                   text=text, kind=kind, expected_version=expected_version)
    with _transaction(store):
        task = _task(store, task_id)
        author = authenticate_local_actor(config, actor, task["input_payload"].get("symbols"))
        duplicate = _receipt(store, command_id, request)
        if duplicate:
            return review_state(store, task_id, config=config, draft_id=duplicate)
        _check_version(task, expected_version)
        if kind == "model_inference" and text != task["output"]["summary"]:
            raise SummaryReviewError("model draft must equal the persisted task summary; edits are human_judgment")
        bound = _sources(config, task, selectors, text, kind)
        revision = store._conn.execute("SELECT COALESCE(MAX(revision),0)+1 FROM task_summary_drafts WHERE task_id=?",
                                       (task_id,)).fetchone()[0]
        now = _now()
        draft_id = "summary_" + uuid.uuid4().hex
        payload = dict(draft=bound, selectors=selectors, author=author, context_sha256=_context_hash(task),
                       task_input_version=task["input_version"], source_task_version=task["version"],
                       execution_mode=task["execution_mode"], model_called=task["model_call"].get("called") is True,
                       role_id=task["role_id"], task_prompt_version=task["prompt_version"],
                       model_prompt_version=str(task["model_call"].get("prompt_version") or ""),
                       expires_at=(now + timedelta(hours=24)).isoformat(), scope=SCOPE, external_eligible=False)
        store._conn.execute("INSERT INTO task_summary_drafts VALUES(?,?,?,?,?)",
                            (draft_id, task_id, revision, _json(payload), now.isoformat()))
        # Keep artifact-only mutations visible to the existing task version/SSE contract.
        store._touch(task_id, last_error=task["last_error"])
        store._emit(task_id, "summary_draft_submitted", actor=actor, command_id=command_id,
                    payload={"draft_id": draft_id, "revision": revision, "summary_id": bound["summary_id"], "scope": SCOPE})
        _record_receipt(store, command_id, task_id, request, draft_id)
    return review_state(store, task_id, config=config, draft_id=draft_id)


def _load(store, task_id, draft_id=""):
    exists = store._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_summary_drafts'").fetchone()
    if not exists:
        return None, []
    if draft_id:
        row = store._conn.execute("SELECT * FROM task_summary_drafts WHERE task_id=? AND draft_id=?", (task_id, draft_id)).fetchone()
    else:
        row = store._conn.execute("SELECT * FROM task_summary_drafts WHERE task_id=? ORDER BY revision DESC LIMIT 1", (task_id,)).fetchone()
    if row is None:
        if draft_id:
            raise SummaryReviewError("unknown summary draft for this task")
        return None, []
    item = dict(row)
    item["payload"] = json.loads(item["payload"])
    reviews = [json.loads(r["payload"]) for r in store._conn.execute(
        "SELECT payload FROM task_summary_reviews WHERE draft_id=? ORDER BY sequence", (item["draft_id"],))]
    return item, reviews


def review_state(store, task_id, *, config, draft_id="", include_local_content=False):
    item, reviews = _load(store, task_id, draft_id)
    state = {"task_id": task_id, "status": "not_submitted", "scope": SCOPE,
             "external_eligible": False, "institutional_approval": False}
    if item is None:
        return state
    payload = item["payload"]
    state.update(draft_id=item["draft_id"], revision=item["revision"], summary_id=payload["draft"]["summary_id"],
                 execution_mode=payload["execution_mode"], model_called=payload["model_called"],
                 expires_at=payload["expires_at"], decision_count=len(reviews),
                 last_decision=reviews[-1]["decision"] if reviews else "", status="pending")
    latest, _ = _load(store, task_id)
    if latest["draft_id"] != item["draft_id"]:
        state["status"] = "superseded"
    else:
        try:
            task = _task(store, task_id)
            if _context_hash(task) != payload["context_sha256"]:
                raise SummaryReviewError("task context changed")
            author = local_actor_binding(config, payload["author"]["actor_id"], task["input_payload"].get("symbols"))
            if author != payload["author"]:
                raise SummaryReviewError("author binding or policy changed")
            bound = _sources(config, task, payload["selectors"], payload["draft"]["text"], payload["draft"]["content_kind"])
            if bound != payload["draft"]:
                raise SummaryReviewError("stored draft version mismatch")
            if _now() >= datetime.fromisoformat(payload["expires_at"]):
                state["status"] = "expired"
            elif reviews:
                review = reviews[-1]
                reviewer = local_actor_binding(config, review["reviewer"]["actor_id"], task["input_payload"].get("symbols"), review=True)
                if reviewer != review["reviewer"] or review["summary_id"] != bound["summary_id"]:
                    raise SummaryReviewError("reviewer binding or reviewed version changed")
                state["status"] = {"supported": "supported", "reject": "rejected", "request_changes": "changes_requested",
                                   "revoke": "revoked"}[review["decision"]]
        except (LocalIdentityError, EvidenceExcerptError, TaskRuntimeError, ValueError, KeyError, TypeError) as exc:
            state.update(status="invalidated", reason=str(exc))
    if include_local_content:
        state["local_draft"] = payload
        state["review_history"] = reviews
        try:
            state["local_excerpts"] = [verify_excerpt_selection(config, selector) for selector in payload["selectors"]]
        except EvidenceExcerptError:
            state["local_excerpts"] = []
    return state


def review_draft(store, task_id, *, config, actor, draft_id, summary_id, decision, notes, expected_version, command_id):
    if not isinstance(draft_id, str) or not draft_id:
        raise SummaryReviewError("explicit draft_id required")
    if not isinstance(decision, str) or decision not in DECISIONS or not isinstance(notes, str) or not notes.strip() or len(notes) > 4000:
        raise SummaryReviewError("a valid review decision and 1..4000 characters of rationale are required")
    request = dict(operation="review_summary", task_id=task_id, actor=actor, draft_id=draft_id, summary_id=summary_id,
                   decision=decision, notes=notes, expected_version=expected_version)
    with _transaction(store):
        task = store.get_task(task_id)
        reviewer = authenticate_local_actor(config, actor, task["input_payload"].get("symbols"), review=True)
        duplicate = _receipt(store, command_id, request)
        if duplicate:
            return review_state(store, task_id, config=config, draft_id=duplicate)
        _check_version(task, expected_version)
        item, reviews = _load(store, task_id, draft_id)
        if item is None:
            raise SummaryReviewError("explicit draft_id required")
        payload = item["payload"]
        if reviewer["actor_id"] == payload["author"]["actor_id"] or reviewer["subject"] == payload["author"]["subject"]:
            raise SummaryReviewError("independent actor and Windows account required")
        if not summary_id or summary_id != payload["draft"]["summary_id"]:
            raise SummaryReviewError("review must bind the exact summary_id")
        state = review_state(store, task_id, config=config, draft_id=draft_id)
        if decision == "revoke":
            if not reviews or reviews[-1]["decision"] != "supported":
                raise SummaryReviewError("only a recorded support decision can be revoked")
        elif state["status"] != "pending":
            raise SummaryReviewError("review requires the current valid pending draft")
        record = dict(review_id="review_" + uuid.uuid4().hex, draft_id=draft_id, summary_id=summary_id,
                      reviewer=reviewer, decision=decision, notes=notes, created_at=_now().isoformat(),
                      task_version=task["version"],
                      context_sha256=payload["context_sha256"], scope=SCOPE, external_eligible=False)
        store._conn.execute("INSERT INTO task_summary_reviews(review_id,draft_id,payload,created_at) VALUES(?,?,?,?)",
                            (record["review_id"], draft_id, _json(record), record["created_at"]))
        store._touch(task_id, last_error=task["last_error"])
        store._emit(task_id, "summary_review_recorded", actor=actor, command_id=command_id,
                    payload={"draft_id": draft_id, "summary_id": summary_id, "decision": decision, "scope": SCOPE})
        _record_receipt(store, command_id, task_id, request, draft_id)
    return review_state(store, task_id, config=config, draft_id=draft_id)
