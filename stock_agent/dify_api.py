"""DF-02: the controlled Dify-facing API — service layer (offline core).

Implements the Dify adoption plan's minimal interface contract as a thin,
audited layer over the AUTHORITATIVE paths:

- create research -> TaskStore.create_task (idempotent; one logical task per
  repeated input, platform retries included);
- research status -> minimized public view + read-only budget receipt
  (budget_guard reuse);
- fact bundle -> frozen, referenceable, revocable fact set per
  (case_id, input_version); content only for public + external-send-approved
  facts;
- candidate results -> bound to task, Dify run, APPROVED workflow version and
  the bundle hash; citations must sit inside the delivered set; replay is
  idempotent, divergent replay is a conflict, and results for cancelled or
  terminal tasks are rejected (stale results never overwrite current state);
- feedback / cancellation -> relayed to TaskStore.record_human_response /
  request_cancel ONLY when backed by a backend-issued principal verification
  that a Dify credential cannot mint; unverified submissions are parked as
  pending input and never execute (Dify form replies are unverified input).

Dify never touches the business database: the gateway owns the TaskStore
reference and returns field-allowlisted JSON views. All calls (allowed and
denied) are appended to an audit ledger under data/dify/.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from . import dify_access
from .budget_guard import may_start_model_call
from .task_runtime.store import (
    TERMINAL_STATUSES,
    STATUS_CANCELLED,
    STATUS_SUCCEEDED,
    STATUS_AWAITING_HUMAN,
    TaskRuntimeError,
    TaskIdempotencyConflictError,
    TaskTransitionError,
    TaskVersionConflictError,
)

LEDGER_ACCESS_LOG = "data/dify/access_log.jsonl"
LEDGER_FACT_BUNDLES = "data/dify/fact_bundles.jsonl"
LEDGER_CANDIDATE_RESULTS = "data/dify/candidate_results.jsonl"
LEDGER_PENDING_FEEDBACK = "data/dify/pending_feedback.jsonl"
LEDGER_PRINCIPAL_VERIFICATIONS = "data/dify/principal_verifications.jsonl"

_MAX_SUMMARY_CHARS = 4000
_MAX_DRAFT_CHARS = 20000
_DEFAULT_VERIFICATION_TTL_SECONDS = 900


class DifySubmissionError(Exception):
    """State/validation rejection of an otherwise authorized submission.

    reason codes: workflow_version_not_approved, fact_bundle_revoked,
    bundle_mismatch, citation_not_in_bundle, citation_not_deliverable,
    task_cancelled_result_rejected, task_terminal, result_conflict_same_run,
    invalid_result.
    """

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


def _now_iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.now().astimezone()).isoformat()


def _ledger_records(config: Dict[str, Any], name: str) -> List[Dict[str, Any]]:
    """Read an append-only JSONL ledger (module-level so read-only status
    projections never need to construct a gateway or open the task store)."""
    from .config import project_path

    path = project_path(name)
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _canonical_sha256(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DifyGateway:
    """The Dify-facing service layer. Construct with the authoritative store."""

    def __init__(self, store: Any, config: Dict[str, Any], *,
                 credentials: Optional[Dict[str, Any]] = None):
        self.store = store
        self.config = config or {}
        self.settings = dify_access.dify_config(self.config)
        self.credentials = credentials if credentials is not None \
            else dify_access.load_service_credentials(self.config)
        # Anchor ledger paths ONCE, in the constructing thread's context:
        # HTTP handler threads do not inherit the project-path override
        # (contextvars do not cross socketserver thread boundaries), so every
        # later append/read must use these absolute paths instead of calling
        # project_path() from the handler thread.
        from .config import project_path

        self._ledger_paths = {name: project_path(name) for name in (
            LEDGER_ACCESS_LOG, LEDGER_FACT_BUNDLES, LEDGER_CANDIDATE_RESULTS,
            LEDGER_PENDING_FEEDBACK, LEDGER_PRINCIPAL_VERIFICATIONS,
        )}

    # ------------------------------------------------------------------ #
    # ledger plumbing (append-only JSONL under data/dify/)               #
    # ------------------------------------------------------------------ #

    def _ledger_path(self, name: str):
        return self._ledger_paths[name]

    def _append_ledger(self, name: str, record: Dict[str, Any]) -> None:
        path = self._ledger_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _read_ledger(self, name: str) -> List[Dict[str, Any]]:
        path = self._ledger_path(name)
        if not path.exists():
            return []
        records: List[Dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def _audit(self, credential_id: str, operation: str, *, allowed: bool,
               reason: str = "", executed: Optional[bool] = None,
               **targets: Any) -> None:
        self._append_ledger(LEDGER_ACCESS_LOG, {
            "record_type": "dify_access",
            "ts": _now_iso(),
            "credential_id": credential_id,
            "operation": operation,
            "allowed": allowed,
            "executed": executed,
            "reason": reason,
            "targets": {key: value for key, value in targets.items() if value},
        })

    # ------------------------------------------------------------------ #
    # gating                                                             #
    # ------------------------------------------------------------------ #

    def _gate(self, token: str, scope: str, *, case_id: str = "",
              operation: str = "") -> Dict[str, Any]:
        operation = operation or scope
        if not self.settings.get("enabled"):
            self._audit("", operation, allowed=False, reason="dify_disabled", case_id=case_id)
            raise dify_access.DifyAccessError("dify_disabled", "dify integration is not enabled")
        try:
            identity = dify_access.verify_service_credential(self.credentials, token)
        except dify_access.DifyAccessError as exc:
            self._audit("", operation, allowed=False, reason=exc.reason, case_id=case_id)
            raise
        try:
            dify_access.ensure_scope(identity, scope)
            if case_id:
                dify_access.ensure_case_scope(identity, case_id)
        except dify_access.DifyAccessError as exc:
            self._audit(identity["credential_id"], operation, allowed=False,
                        reason=exc.reason, case_id=case_id)
            raise
        self._audit(identity["credential_id"], operation, allowed=True,
                    reason="ok", case_id=case_id)
        return identity

    def _gate_task(self, token: str, scope: str, task_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Gate a scope plus task lookup; not-found and out-of-scope share one
        reason so task ids cannot be enumerated through the API."""
        identity = self._gate(token, scope, operation=scope)
        try:
            task = self.store.get_task(str(task_id or ""))
        except TaskRuntimeError:
            raise dify_access.DifyAccessError("task_not_found_or_out_of_scope", str(task_id))
        case_id = str(task.get("case_id") or "")
        try:
            dify_access.ensure_case_scope(identity, case_id)
        except dify_access.DifyAccessError:
            self._audit(identity["credential_id"], scope, allowed=False,
                        reason="task_not_found_or_out_of_scope", task_id=task_id)
            raise dify_access.DifyAccessError("task_not_found_or_out_of_scope", str(task_id))
        return identity, task

    def _ensure_approved_workflow(self, workflow_version: str, *,
                                  credential_id: str, operation: str) -> None:
        if str(workflow_version or "") not in set(self.settings.get("approved_workflow_versions") or []):
            self._audit(credential_id, operation, allowed=False,
                        reason="workflow_version_not_approved",
                        workflow_version=str(workflow_version or ""))
            raise DifySubmissionError(
                "workflow_version_not_approved",
                "workflow_version {0!r} is not in the approved list; publish "
                "the flow as a reviewed candidate version first".format(workflow_version))

    # ------------------------------------------------------------------ #
    # public view (minimization boundary)                                #
    # ------------------------------------------------------------------ #

    def _public_task_view(self, task: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
        payload = task.get("input_payload") or {}
        origin = payload.get("origin") or {}
        output = task.get("output") or {}
        status = str(task.get("status") or "")
        human_request = task.get("human_request") or {}
        references = output.get("references") if isinstance(output, dict) else None
        if not isinstance(references, list):
            references = []
        view: Dict[str, Any] = {
            "task_id": str(task.get("task_id") or ""),
            "case_id": str(task.get("case_id") or ""),
            "title": str(task.get("title") or ""),
            "status": status,
            "version": int(task.get("version") or 0),
            "terminal": status in TERMINAL_STATUSES,
            "cancel_requested": bool(task.get("cancel_requested")),
            "input_version": payload.get("input_version"),
            "draft_summary": str(output.get("summary") or "") if isinstance(output, dict) else "",
            "references": references,
            "budget": may_start_model_call(self.store, self.config, now=now),
            "poll": dict(self.settings.get("poll") or {}),
        }
        if origin.get("kind") == "dify":
            view["dify"] = {
                "dify_run_id": str(origin.get("dify_run_id") or ""),
                "workflow_version": str(origin.get("workflow_version") or ""),
            }
        if status == STATUS_AWAITING_HUMAN and str(human_request.get("question") or ""):
            view["awaiting_human"] = {"question": str(human_request["question"])}
        return view

    # ------------------------------------------------------------------ #
    # contract: create research                                          #
    # ------------------------------------------------------------------ #

    def create_research(self, token: str, *, title: str, case_id: str,
                        input_version: int, research_question: str,
                        public_context: str = "", dify_run_id: str,
                        workflow_version: str, idempotency_key: str = "",
                        prompt_version: str = "research_v1",
                        execution_mode: str = "live", now: Optional[datetime] = None) -> Dict[str, Any]:
        identity = self._gate(token, "create_research", case_id=str(case_id or ""))
        self._ensure_approved_workflow(workflow_version,
                                       credential_id=identity["credential_id"],
                                       operation="create_research")
        title = str(title or "").strip()
        question = str(research_question or "").strip()
        run_id = str(dify_run_id or "").strip()
        if not title:
            raise ValueError("title must not be empty")
        if not question:
            raise ValueError("research_question must not be empty")
        if not run_id:
            raise ValueError("dify_run_id must not be empty")
        if isinstance(input_version, bool) or not isinstance(input_version, int) or input_version < 1:
            raise ValueError("input_version must be a positive integer")
        input_payload = {
            "data_classification": "public",
            "input_version": int(input_version),
            "research_question": question,
            "public_context": str(public_context or ""),
            "origin": {
                "kind": "dify",
                "dify_run_id": run_id,
                "workflow_version": str(workflow_version or ""),
            },
        }
        task, outcome = self.store.create_task(
            title=title, input_payload=input_payload, case_id=str(case_id or ""),
            idempotency_key=str(idempotency_key or ""), prompt_version=str(prompt_version or "research_v1"),
            execution_mode=str(execution_mode or "live"), actor="dify:{0}".format(identity["credential_id"]))
        payload = task.get("input_payload") or {}
        origin = payload.get("origin") or {}
        return {
            "task_id": str(task.get("task_id") or ""),
            "case_id": str(task.get("case_id") or ""),
            "status": str(task.get("status") or ""),
            "version": int(task.get("version") or 0),
            "outcome": outcome,
            "idempotency_key": str(task.get("idempotency_key") or ""),
            "dify": {
                "dify_run_id": str(origin.get("dify_run_id") or ""),
                "workflow_version": str(origin.get("workflow_version") or ""),
            },
            "budget": may_start_model_call(self.store, self.config, now=now),
        }

    # ------------------------------------------------------------------ #
    # contract: research status                                          #
    # ------------------------------------------------------------------ #

    def research_status(self, token: str, *, task_id: str,
                        now: Optional[datetime] = None) -> Dict[str, Any]:
        _, task = self._gate_task(token, "read_research", str(task_id or ""))
        return self._public_task_view(task, now=now)

    # ------------------------------------------------------------------ #
    # fact bundles (issued by the backend, read by Dify)                 #
    # ------------------------------------------------------------------ #

    def issue_fact_bundle(self, *, case_id: str, input_version: int, facts: List[Dict[str, Any]],
                          actor: str = "backend", now: Optional[datetime] = None) -> Dict[str, Any]:
        """Backend-side: freeze a referenceable fact set for (case, version).

        Content is delivered only for facts that are classification=public AND
        external_send_allowed; everything else is recorded as a reference with
        an exclusion reason so citations can be checked against what was
        actually delivered. Not exposed to Dify credentials.
        """
        if not str(case_id or "").strip():
            raise ValueError("case_id must not be empty")
        if isinstance(input_version, bool) or not isinstance(input_version, int) or input_version < 1:
            raise ValueError("input_version must be a positive integer")
        if not isinstance(facts, list) or not facts:
            raise ValueError("facts must be a non-empty list")
        delivered: List[Dict[str, Any]] = []
        for fact in facts:
            if not isinstance(fact, dict) or not str(fact.get("fact_id") or "").strip():
                raise ValueError("each fact needs a fact_id")
            fact_id = str(fact["fact_id"])
            classification = str(fact.get("classification") or "public")
            external_ok = bool(fact.get("external_send_allowed"))
            entry = {
                "fact_id": fact_id,
                "kind": str(fact.get("kind") or ""),
                "source": str(fact.get("source") or ""),
                "classification": classification,
                "external_send_allowed": external_ok,
            }
            if classification == "public" and external_ok:
                entry["content"] = str(fact.get("content") or "")
            else:
                entry["content"] = None
                entry["excluded_reason"] = (
                    "classification_not_public" if classification != "public"
                    else "external_send_not_allowed")
            delivered.append(entry)
        bundle_hash = _canonical_sha256({
            "case_id": str(case_id), "input_version": int(input_version), "facts": delivered})
        bundle_id = "fb-{0}".format(_canonical_sha256({
            "case_id": str(case_id), "input_version": int(input_version),
            "bundle_hash": bundle_hash})[:16])
        record = {
            "event": "issued", "bundle_id": bundle_id, "case_id": str(case_id),
            "input_version": int(input_version), "facts": delivered,
            "bundle_hash": bundle_hash, "actor": str(actor or "backend"),
            "created_at": _now_iso(now),
        }
        self._append_ledger(LEDGER_FACT_BUNDLES, record)
        return {"bundle_id": bundle_id, "bundle_hash": bundle_hash,
                "case_id": str(case_id), "input_version": int(input_version),
                "delivered_count": sum(1 for item in delivered if item["content"] is not None),
                "total_count": len(delivered)}

    def revoke_fact_bundle(self, *, bundle_id: str, reason: str,
                           actor: str = "backend", now: Optional[datetime] = None) -> Dict[str, Any]:
        bundle_id = str(bundle_id or "").strip()
        if not bundle_id:
            raise ValueError("bundle_id must not be empty")
        for event in self._read_ledger(LEDGER_FACT_BUNDLES):
            if event.get("event") == "revoked" and str(event.get("bundle_id")) == bundle_id:
                return {"bundle_id": bundle_id, "revoked": True, "already": True}
        self._append_ledger(LEDGER_FACT_BUNDLES, {
            "event": "revoked", "bundle_id": bundle_id,
            "reason": str(reason or ""), "actor": str(actor or "backend"),
            "created_at": _now_iso(now),
        })
        return {"bundle_id": bundle_id, "revoked": True, "already": False}

    def _latest_bundle(self, case_id: str, input_version: int) -> Optional[Dict[str, Any]]:
        latest: Optional[Dict[str, Any]] = None
        for event in self._read_ledger(LEDGER_FACT_BUNDLES):
            if event.get("event") != "issued":
                continue
            if str(event.get("case_id")) != str(case_id):
                continue
            try:
                if int(event.get("input_version")) != int(input_version):
                    continue
            except (TypeError, ValueError):
                continue
            latest = event
        return latest

    def _bundle_revocation(self, bundle_id: str) -> Optional[Dict[str, Any]]:
        for event in self._read_ledger(LEDGER_FACT_BUNDLES):
            if event.get("event") == "revoked" and str(event.get("bundle_id")) == bundle_id:
                return event
        return None

    def read_fact_bundle(self, token: str, *, case_id: str, input_version: int,
                         now: Optional[datetime] = None) -> Dict[str, Any]:
        self._gate(token, "read_fact_bundle", case_id=str(case_id or ""))
        if isinstance(input_version, bool) or not isinstance(input_version, int):
            raise ValueError("input_version must be an integer")
        bundle = self._latest_bundle(str(case_id), input_version)
        if bundle is None:
            raise dify_access.DifyAccessError(
                "fact_bundle_not_found",
                "no fact bundle for case {0} input_version {1}".format(case_id, input_version))
        revocation = self._bundle_revocation(str(bundle.get("bundle_id")))
        if revocation is not None:
            raise DifySubmissionError(
                "fact_bundle_revoked",
                "bundle {0} was revoked: {1}".format(
                    bundle.get("bundle_id"), revocation.get("reason")))
        facts = bundle.get("facts") or []
        referenceable = [dict(fact) for fact in facts if fact.get("content") is not None]
        return {
            "bundle_id": str(bundle.get("bundle_id")),
            "case_id": str(bundle.get("case_id")),
            "input_version": bundle.get("input_version"),
            "bundle_hash": str(bundle.get("bundle_hash")),
            "revoked": False,
            "issued_at": str(bundle.get("created_at")),
            "facts": referenceable,
        }

    # ------------------------------------------------------------------ #
    # contract: submit candidate result                                  #
    # ------------------------------------------------------------------ #

    def submit_candidate_result(self, token: str, *, task_id: str, dify_run_id: str,
                                workflow_version: str, bundle_id: str,
                                citations: List[str], result: Dict[str, Any],
                                attempt_receipt: Optional[Dict[str, Any]] = None,
                                now: Optional[datetime] = None) -> Dict[str, Any]:
        identity, task = self._gate_task(token, "submit_result", str(task_id or ""))
        task_id = str(task.get("task_id") or "")
        self._ensure_approved_workflow(workflow_version,
                                       credential_id=identity["credential_id"],
                                       operation="submit_result")
        status = str(task.get("status") or "")
        if status == STATUS_CANCELLED:
            self._audit(identity["credential_id"], "submit_result", allowed=False,
                        reason="task_cancelled_result_rejected", task_id=task_id)
            raise DifySubmissionError("task_cancelled_result_rejected",
                                      "task {0} was cancelled; late results cannot overwrite state".format(task_id))
        if status in TERMINAL_STATUSES:
            self._audit(identity["credential_id"], "submit_result", allowed=False,
                        reason="task_terminal", task_id=task_id, status=status)
            raise DifySubmissionError("task_terminal",
                                      "task {0} is terminal ({1})".format(task_id, status))
        payload = task.get("input_payload") or {}
        input_version = payload.get("input_version")
        case_id = str(task.get("case_id") or "")
        bundle = self._latest_bundle(case_id, input_version)
        if bundle is None or str(bundle.get("bundle_id")) != str(bundle_id or ""):
            raise DifySubmissionError(
                "bundle_mismatch",
                "bundle_id does not match the current bundle for case {0} input_version {1}".format(
                    case_id, input_version))
        revocation = self._bundle_revocation(str(bundle_id))
        if revocation is not None:
            raise DifySubmissionError("fact_bundle_revoked",
                                      "bundle {0} was revoked: {1}".format(
                                          bundle_id, revocation.get("reason")))
        delivered_ids = {str(fact.get("fact_id")) for fact in (bundle.get("facts") or [])
                         if fact.get("content") is not None}
        referenced_ids = {str(fact.get("fact_id")) for fact in (bundle.get("facts") or [])}
        if not isinstance(citations, list) or not citations:
            raise ValueError("citations must be a non-empty list of fact ids")
        for citation in citations:
            citation = str(citation)
            if citation in delivered_ids:
                continue
            if citation in referenced_ids:
                raise DifySubmissionError(
                    "citation_not_deliverable",
                    "fact {0} was referenced but its content was not in the delivered set".format(citation))
            raise DifySubmissionError(
                "citation_not_in_bundle",
                "fact {0} is not part of the delivered bundle".format(citation))
        if not isinstance(result, dict):
            raise ValueError("result must be an object")
        summary = str(result.get("summary") or "").strip()
        if not summary:
            raise ValueError("result.summary must be a non-empty string")
        if len(summary) > _MAX_SUMMARY_CHARS:
            raise ValueError("result.summary exceeds {0} chars".format(_MAX_SUMMARY_CHARS))
        draft = result.get("draft_markdown")
        if draft is not None:
            if not isinstance(draft, str):
                raise ValueError("result.draft_markdown must be a string")
            if len(draft) > _MAX_DRAFT_CHARS:
                raise ValueError("result.draft_markdown exceeds {0} chars".format(_MAX_DRAFT_CHARS))
        result_sha256 = _canonical_sha256(result)
        run_id = str(dify_run_id or "")
        existing = None
        for event in self._read_ledger(LEDGER_CANDIDATE_RESULTS):
            if event.get("task_id") == task_id and event.get("dify_run_id") == run_id \
                    and str(event.get("workflow_version")) == str(workflow_version) \
                    and str(event.get("bundle_id")) == str(bundle_id):
                existing = event
        if existing is not None:
            if str(existing.get("result_sha256")) == result_sha256:
                receipt = dict(existing)
                receipt["outcome"] = "DUPLICATE"
                return receipt
            raise DifySubmissionError(
                "result_conflict_same_run",
                "this Dify run already submitted a different result for the task")
        receipt = {
            "record_type": "dify_candidate_result",
            "result_id": "cr-{0}".format(_canonical_sha256({
                "task_id": task_id, "dify_run_id": run_id,
                "workflow_version": str(workflow_version),
                "bundle_hash": bundle.get("bundle_hash"), "result": result})[:16]),
            "task_id": task_id,
            "case_id": case_id,
            "input_version": input_version,
            "dify_run_id": run_id,
            "workflow_version": str(workflow_version),
            "bundle_id": str(bundle_id),
            "bundle_hash": str(bundle.get("bundle_hash")),
            "citations": [str(item) for item in citations],
            "task_version": int(task.get("version") or 0),
            "result_sha256": result_sha256,
            "result": result,
            "attempt_receipt": dict(attempt_receipt or {}),
            "status": "recorded",
            "recorded_at": _now_iso(now),
        }
        self._append_ledger(LEDGER_CANDIDATE_RESULTS, receipt)
        receipt = dict(receipt)
        receipt["outcome"] = "RECORDED"
        return receipt

    # ------------------------------------------------------------------ #
    # principal verification (backend-issued; never mintable by Dify)    #
    # ------------------------------------------------------------------ #

    def issue_principal_verification(self, *, actor: str, role: str = "",
                                     ttl_seconds: int = _DEFAULT_VERIFICATION_TTL_SECONDS,
                                     now: Optional[datetime] = None) -> str:
        """Backend-side: bind a REAL local identity to a one-command receipt.

        Issued by the human-facing path (local identity check), never exposed
        through a Dify credential scope. Single-use per command_id and
        short-lived; a Dify-side caller can at worst relay a stolen receipt
        once — every use lands in the audit ledger.
        """
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor must not be empty")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds < 1:
            raise ValueError("ttl_seconds must be a positive integer")
        moment = now or datetime.now().astimezone()
        verification_id = "pv-{0}".format(_canonical_sha256({
            "actor": actor, "role": str(role or ""), "issued_at": moment.isoformat(),
        })[:16])
        self._append_ledger(LEDGER_PRINCIPAL_VERIFICATIONS, {
            "event": "issued", "verification_id": verification_id, "actor": actor,
            "role": str(role or ""), "issued_at": moment.isoformat(),
            "expires_at": (moment + timedelta(seconds=int(ttl_seconds))).isoformat(),
        })
        return verification_id

    def _verify_principal(self, verification_id: str, actor: str, command_id: str,
                          *, now: Optional[datetime] = None) -> Tuple[bool, str]:
        verification_id = str(verification_id or "").strip()
        actor = str(actor or "").strip()
        if not verification_id or not actor:
            return False, "principal_verification_required"
        issued = None
        for event in self._read_ledger(LEDGER_PRINCIPAL_VERIFICATIONS):
            if event.get("event") == "issued" and str(event.get("verification_id")) == verification_id:
                issued = event
        if issued is None:
            return False, "principal_verification_unknown"
        if str(issued.get("actor")) != actor:
            return False, "principal_actor_mismatch"
        try:
            expiry = datetime.fromisoformat(str(issued.get("expires_at")))
        except ValueError:
            return False, "principal_verification_invalid"
        if expiry.tzinfo is None:
            expiry = expiry.astimezone()
        if expiry <= (now or datetime.now().astimezone()):
            return False, "principal_verification_expired"
        for event in self._read_ledger(LEDGER_PRINCIPAL_VERIFICATIONS):
            if event.get("event") == "used" and str(event.get("verification_id")) == verification_id:
                if str(event.get("command_id")) != str(command_id):
                    return False, "principal_verification_already_used"
        return True, "ok"

    def _mark_verification_used(self, verification_id: str, command_id: str,
                                task_id: str, now: Optional[datetime] = None) -> None:
        self._append_ledger(LEDGER_PRINCIPAL_VERIFICATIONS, {
            "event": "used", "verification_id": str(verification_id),
            "command_id": str(command_id), "task_id": str(task_id),
            "used_at": _now_iso(now),
        })

    # ------------------------------------------------------------------ #
    # contract: feedback / cancellation (protected — needs a principal)  #
    # ------------------------------------------------------------------ #

    def submit_feedback(self, token: str, *, task_id: str, command_id: str,
                        expected_version: int, decision: str,
                        input_patch: Optional[Dict[str, Any]] = None, notes: str = "",
                        actor: str = "", verification_id: str = "",
                        now: Optional[datetime] = None) -> Dict[str, Any]:
        identity, task = self._gate_task(token, "submit_feedback", str(task_id or ""))
        task_id = str(task.get("task_id") or "")
        if not str(command_id or "").strip():
            raise ValueError("command_id must not be empty")
        valid, reason = self._verify_principal(verification_id, actor, command_id, now=now)
        if not valid:
            self._append_ledger(LEDGER_PENDING_FEEDBACK, {
                "record_type": "dify_pending_feedback",
                "task_id": task_id, "command_id": str(command_id),
                "submitted_actor": str(actor or ""), "decision": str(decision or ""),
                "expected_version": expected_version, "notes": str(notes or ""),
                "reason": reason, "created_at": _now_iso(now),
            })
            self._audit(identity["credential_id"], "submit_feedback", allowed=True,
                        executed=False, reason=reason, task_id=task_id)
            return {"status": "pending_verification", "executed": False, "reason": reason,
                    "task_id": task_id}
        try:
            task = self.store.record_human_response(
                task_id, actor=str(actor), decision=str(decision),
                expected_version=int(expected_version), command_id=str(command_id),
                input_patch=input_patch, notes=str(notes or ""))
        except (TaskRuntimeError, TaskVersionConflictError, TaskIdempotencyConflictError,
                TaskTransitionError):
            self._audit(identity["credential_id"], "submit_feedback", allowed=True,
                        executed=False, reason="task_store_rejected", task_id=task_id)
            raise
        self._mark_verification_used(verification_id, command_id, task_id, now=now)
        self._audit(identity["credential_id"], "submit_feedback", allowed=True,
                    executed=True, reason="ok", task_id=task_id)
        return {"status": "recorded", "executed": True, "task_id": task_id,
                "task": self._public_task_view(task, now=now)}

    def request_cancel(self, token: str, *, task_id: str, expected_version: int,
                       reason: str, actor: str = "", verification_id: str = "",
                       now: Optional[datetime] = None) -> Dict[str, Any]:
        identity, task = self._gate_task(token, "submit_feedback", str(task_id or ""))
        task_id = str(task.get("task_id") or "")
        command_id = "cancel:{0}".format(task_id)
        valid, why = self._verify_principal(verification_id, actor, command_id, now=now)
        if not valid:
            self._append_ledger(LEDGER_PENDING_FEEDBACK, {
                "record_type": "dify_pending_feedback",
                "task_id": task_id, "command_id": command_id,
                "submitted_actor": str(actor or ""), "decision": "cancel",
                "expected_version": expected_version, "notes": str(reason or ""),
                "reason": why, "created_at": _now_iso(now),
            })
            self._audit(identity["credential_id"], "cancel", allowed=True,
                        executed=False, reason=why, task_id=task_id)
            return {"status": "pending_verification", "executed": False, "reason": why,
                    "task_id": task_id}
        try:
            task = self.store.request_cancel(
                task_id, actor=str(actor), reason=str(reason or ""),
                expected_version=int(expected_version))
        except (TaskRuntimeError, TaskVersionConflictError, TaskTransitionError):
            self._audit(identity["credential_id"], "cancel", allowed=True,
                        executed=False, reason="task_store_rejected", task_id=task_id)
            raise
        self._mark_verification_used(verification_id, command_id, task_id, now=now)
        self._audit(identity["credential_id"], "cancel", allowed=True,
                    executed=True, reason="ok", task_id=task_id)
        return {"status": str(task.get("status") or ""), "executed": True,
                "task_id": task_id, "task": self._public_task_view(task, now=now)}


def build_dify_access_status(config: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Read-only layered status for the ``dify-access-status`` CLI.

    Never emits token hashes or plaintext tokens, never touches the network,
    and never opens the task database — ledger counts only.
    """
    settings = dify_access.dify_config(config)
    registry = dify_access.load_service_credentials(config)
    moment = now or datetime.now().astimezone()
    credentials_view = []
    for entry in registry.get("credentials", []):
        credentials_view.append({
            "credential_id": str(entry.get("credential_id")),
            "active": bool(entry.get("active")),
            "expired": dify_access.is_credential_expired(entry, moment),
            "scopes": list(entry.get("scopes") or []),
            "case_scope": list(entry.get("case_scope") or []),
        })

    def _count(name: str, predicate) -> int:
        return sum(1 for event in _ledger_records(config, name) if predicate(event))

    status = {
        "schema_version": "1.0",
        "generated_at": _now_iso(moment),
        "scope_note": "只读状态投影：不联网、不打开任务库、不输出任何令牌哈希",
        "enabled": settings["enabled"],
        "approved_workflow_versions": settings["approved_workflow_versions"],
        "poll": settings["poll"],
        "credentials_configured": len(credentials_view),
        "credentials": credentials_view,
        "ledgers": {
            "access_log_entries": _count(LEDGER_ACCESS_LOG, lambda event: True),
            "fact_bundles_issued": _count(LEDGER_FACT_BUNDLES, lambda e: e.get("event") == "issued"),
            "fact_bundles_revoked": _count(LEDGER_FACT_BUNDLES, lambda e: e.get("event") == "revoked"),
            "candidate_results": _count(LEDGER_CANDIDATE_RESULTS, lambda event: True),
            "pending_feedback": _count(LEDGER_PENDING_FEEDBACK, lambda event: True),
            "principal_verifications_issued": _count(
                LEDGER_PRINCIPAL_VERIFICATIONS, lambda e: e.get("event") == "issued"),
        },
    }
    if not settings["enabled"]:
        status["blocked"] = ["dify integration disabled（真实启用需负责人配置 dify.enabled=true）"]
    return status
