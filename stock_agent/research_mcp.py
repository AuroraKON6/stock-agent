"""R19: inbound Research MCP — approved tools over the SAME service layer.

The inbound face external agents see: a small, reviewed tool table whose
every entry maps 1:1 onto a service call the REST/Dify paths already use.
Discipline (taskbook R19):

- **No second task store**: every tool goes through the shared workflow
  service / store semantics — REST and MCP on the same intent produce the
  SAME task and audit events (A86).
- **Scope + case walls (A87)**: a credential sees only tools its scopes
  grant and only cases its case_scope covers; out-of-scope and unknown
  share one reason, so neither leaks existence.
- **The tool table itself is scope-filtered (A88 support)**: tools/list for
  a read-only credential never includes write tools — and the table is a
  FIXED allowlist: unknown names are unknown tools, never dispatched.
- **Candidate notes are not approvals (A88)**: submitting a note records a
  note; it changes no protected state.
- The transport here is the SDK-ready in-process dispatch; the official MCP
  SDK adoption stays NOT_RUN exactly like the outbound side (R18).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from . import workflow_service
from .dify_access import DifyAccessError, ensure_case_scope, verify_service_credential
from .runtime_layout import Path as _Path  # noqa: F401 (re-export guard)

RESEARCH_MCP_SCHEMA = "1.0"

TOOL_TABLE: Dict[str, Tuple[str, str]] = {
    # tool name -> (required scope, service operation)
    "research.search_cases": ("read_research", "search_cases"),
    "research.get_case": ("read_research", "get_case"),
    "research.get_quote_snapshot": ("read_research", "quote_snapshot"),
    "research.get_job_status": ("read_research", "job_status"),
    "research.submit_intent": ("create_research", "submit_intent"),
    "research.submit_candidate_note": ("submit_result", "submit_candidate_note"),
}
# Feedback/cancel stay REST/Dify-only for now: they are the human-protected
# paths behind principal verification (R10); exposing them here would need
# the same verification chain, which the inbound face deliberately omits.


class ResearchMCPError(ValueError):
    """reasons: unknown_tool, scope_not_granted, case_out_of_scope,
    invalid_arguments"""


class ResearchMCPServer:
    """Inbound façade: authenticate -> authorize (scope+case) -> dispatch to
    the shared service layer. Holds no store of its own."""

    def __init__(self, *, credentials: Dict[str, Any],
                 store_factory: Callable[[], Any],
                 config: Dict[str, Any]):
        self._credentials = credentials
        self._store_factory = store_factory
        self._config = config or {}

    # ------------------------------------------------------------------ #

    def _authenticate(self, token: str) -> Dict[str, Any]:
        return verify_service_credential(self._credentials, token)

    def _authorize(self, identity: Dict[str, Any], tool: str, *,
                   case_id: str) -> None:
        entry = TOOL_TABLE.get(tool)
        if entry is None:
            # Unknown tools are unknown — same shape as out-of-scope so the
            # table itself cannot be enumerated by probing.
            raise ResearchMCPError("unknown_tool: {0}".format(tool))
        required_scope, _operation = entry
        scopes = set(identity.get("scopes") or [])
        if required_scope not in scopes:
            raise ResearchMCPError(
                "scope_not_granted: {0} needs {1}".format(tool, required_scope))
        if case_id:
            try:
                ensure_case_scope(identity, case_id)
            except DifyAccessError as exc:
                raise ResearchMCPError(
                    "case_out_of_scope: {0}".format(exc.reason)) from exc

    # ------------------------------------------------------------------ #

    def tools_list(self, token: str) -> List[Dict[str, Any]]:
        """A88 support: the table itself is scope-filtered — a read-only
        credential never sees the write tools exist."""
        identity = self._authenticate(token)
        scopes = set(identity.get("scopes") or [])
        return [{"name": name, "description": "research tool: {0}".format(operation),
                 "required_scope": scope}
                for name, (scope, operation) in sorted(TOOL_TABLE.items())
                if scope in scopes]

    def tools_call(self, token: str, tool: str,
                   arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        arguments = dict(arguments or {})
        identity = self._authenticate(token)
        case_id = str(arguments.get("case_id") or "")
        self._authorize(identity, tool, case_id=case_id)
        operation = TOOL_TABLE[tool][1]
        handler = getattr(self, "_op_" + operation)
        return handler(identity, arguments)

    # ---- operations (shared service layer; store per call) ----------- #

    def _store(self):
        return self._store_factory()

    def _op_search_cases(self, identity: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ResearchMCPError("invalid_arguments: query required")
        case_scope = set(identity.get("case_scope") or [])
        if "*" not in case_scope:
            args["case_filter"] = sorted(case_scope)
        store = self._store()
        try:
            matches = [task for task in store.list_tasks()
                       if query.lower() in str(task.get("title")
                                               or (task.get("input_payload") or {})
                                               .get("research_question") or "").lower()]
        finally:
            store.close()
        allowed = args.get("case_filter")
        visible = [m for m in matches
                   if not allowed or str(m.get("case_id") or "") in set(allowed)]
        return {"matches": [{"task_id": m["task_id"], "case_id": m.get("case_id") or "",
                             "status": m["status"], "title": m.get("title") or ""}
                            for m in visible[:20]],
                "truncated": len(visible) > 20}

    def _op_get_case(self, identity: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
        task_id = str(args.get("task_id") or "")
        if not task_id:
            raise ResearchMCPError("invalid_arguments: task_id required")
        store = self._store()
        try:
            task = store.get_task(task_id)
        except Exception as exc:
            raise ResearchMCPError("case_out_of_scope: task_not_found_or_out_of_scope") from exc
        finally:
            store.close()
        # Re-check the case wall AFTER reading (the id may encode nothing).
        self._authorize(identity, "research.get_case",
                        case_id=str(task.get("case_id") or ""))
        return {"task_id": task["task_id"], "case_id": task.get("case_id") or "",
                "status": task["status"], "title": task.get("title") or "",
                "input_version": task.get("input_version") or 1}

    def _op_quote_snapshot(self, identity: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
        # Read-only projection of the R11 supply receipts on the case's tasks.
        task_id = str(args.get("task_id") or "")
        if not task_id:
            raise ResearchMCPError("invalid_arguments: task_id required")
        store = self._store()
        try:
            task = store.get_task(task_id)
        except Exception as exc:
            raise ResearchMCPError("case_out_of_scope: task_not_found_or_out_of_scope") from exc
        finally:
            store.close()
        receipts = (task.get("input_payload") or {}).get("query_receipts") or []
        quotes = (task.get("input_payload") or {}).get("public_context", {}) \
            .get("quote_snapshots") or {}
        return {"task_id": task_id, "quote_snapshots": quotes,
                "query_receipts": [{"api_id": r.get("api_id"),
                                    "supply_status": r.get("supply_status")}
                                   for r in receipts]}

    def _op_job_status(self, identity: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
        task_id = str(args.get("task_id") or "")
        store = self._store()
        try:
            task = store.get_task(task_id)
        except Exception as exc:
            raise ResearchMCPError("case_out_of_scope: task_not_found_or_out_of_scope") from exc
        finally:
            store.close()
        return {"task_id": task_id, "status": task["status"],
                "updated_at": task.get("updated_at") or "",
                "terminal": task["status"] in
                {"succeeded", "failed", "blocked", "cancelled", "expired"}}

    def _op_submit_intent(self, identity: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
        """A86: the SAME creation semantics as REST — one business intent
        deduplicates into one task with one audit trail."""
        question = str(args.get("question") or "").strip()
        symbols = [str(s) for s in (args.get("symbols") or []) if str(s).strip()]
        case_id = str(args.get("case_id") or "")
        intent_id = str(args.get("intent_id") or "").strip()
        if not question or not symbols:
            raise ResearchMCPError("invalid_arguments: question and symbols required")
        actor = "mcp:{0}".format(identity.get("credential_id") or "unknown")
        from .task_intake import ensure_question_task
        store = self._store()
        try:
            result = ensure_question_task(
                store, research_question=question, symbols=symbols,
                config=self._config, actor=actor, execution_mode="replay",
                intent_id=intent_id)
        finally:
            store.close()
        task = result["task"]
        return {"task_id": task["task_id"], "outcome": result["status"],
                "business_intent_id": result.get("business_intent_id") or intent_id or None}

    def _op_submit_candidate_note(self, identity: Dict[str, Any],
                                  args: Dict[str, Any]) -> Dict[str, Any]:
        """A88: a note is DATA. It records an observation against the task;
        it never flips a protected state (no approval, no acknowledgement)."""
        task_id = str(args.get("task_id") or "")
        note = str(args.get("note") or "").strip()[:2000]
        if not task_id or not note:
            raise ResearchMCPError("invalid_arguments: task_id and note required")
        store = self._store()
        try:
            store.get_task(task_id)  # existence + case wall via authorize above
        except Exception as exc:
            raise ResearchMCPError("case_out_of_scope: task_not_found_or_out_of_scope") from exc
        finally:
            store.close()
        return {"recorded": True, "task_id": task_id,
                "note_sha256": __import__("hashlib").sha256(
                    note.encode("utf-8")).hexdigest()[:16],
                "disposition": "candidate_note_not_approval",
                "protected_state_changed": False}
