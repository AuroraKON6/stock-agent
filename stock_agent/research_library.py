"""R20: research library — a rebuildable index over durable research state.

The library answers "what do we already know about this company/question"
from COMPLETED research: it indexes terminal tasks (question, outcome,
quality verdict, bound evidence, symbols, namespace) and offers structured +
full-text retrieval. Design rules (GLM_REFACTOR_TASKBOOK R20):

- **Rebuildable by construction**: the index is a pure projection of the
  TaskStore + evidence vault projections. Dropping it loses nothing;
  rebuilding reproduces it. The originals (events, snapshots, documents)
  stay the only authority.
- **Revocation propagates (stop condition)**: the normal retrieval entry
  (:func:`query_library`) re-checks every entry's bound evidence against the
  caller's CURRENT authorized evidence views on EVERY query, so an index
  cached before a revocation stops answering on its next use. An entry whose
  evidence fully or partially left the projection is reported as stale and
  excluded unless an authorized auditor asks for the audit view.
- **Scoped to the caller (stop condition: 检索结果越权)**: ownership is the
  server-side input-owner binding the personal workspace writes inside the
  create transaction, never a payload field. A caller receives shared
  research plus their OWN personal research — nobody else's.
- **No embedding required**: structured fields + substring full-text over
  question/summary/findings. Semantic retrieval is a later, separately
  justified addition.

Report versioning: ``report_version`` identifies the preserved report
itself, independently of the input and of the row. ``input_version`` (store
column) says which immutable input snapshot produced the report;
``report_schema_version`` says which report payload schema the model output
conforms to; ``report_sha256`` covers the report body only (see
:func:`report_identity`), so a personal sign-off added to the output later
does not change it; ``report_version`` is
``rv1:<schema>:<task_id>:<input_version>:<sha256>`` — which case, produced
from which input version, saying what. A linked continuation is a NEW task:
its own task id, its own input version starting at 1 and therefore its own
report version, with ``prior_task_id`` naming the case it continues; the
original report keeps its version and content untouched.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from .audit_log import load_hash_chained_jsonl
from .config import project_path
from .evidence_context import verified_evidence_documents
from .evidence_vault import evidence_vault_config, load_evidence_vault
from .personal_evidence import PERSONAL_ORIGIN_VIA, personal_vault_view
from .task_runtime.store import TERMINAL_STATUSES

LIBRARY_SCHEMA = "1.1"
NAMESPACE_SHARED = "default"
NAMESPACE_PERSONAL = "personal"
# Roster roles allowed to read stale rows (the audit view). The decision
# comes from the research roster record, never from the request.
AUDIT_ROLES = {"quality_reviewer", "compliance_officer"}
# Model-authored report fields: what the report SAYS. Later additions to the
# output object (quality verdict, broker results, a personal sign-off) are
# records ABOUT the report and stay outside the version.
REPORT_BODY_FIELDS = ("schema_version", "task_id", "summary", "key_findings",
                      "unknowns", "evidence_refs", "human_questions",
                      "model_self_assessed_confidence")
REPORT_IDENTITY_SCHEME = "rv1"


class LibraryError(ValueError):
    pass


class LibraryTimeWindowError(LibraryError):
    """A ``since``/``until`` bound that is not an offset-bearing timestamp or
    a bare calendar date."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def report_identity(output: Any, task_id: str = "", input_version: Any = None) -> Dict[str, Any]:
    """Checkable report version for one preserved report.

    ``report_sha256`` is reproducible from the report alone: SHA-256 over
    the canonical JSON (sorted keys, compact separators, UTF-8) of the
    report body, i.e. the ``REPORT_BODY_FIELDS`` present in the output.
    ``report_version`` additionally names the case and the input version
    that produced it, so two continuations of one question never share a
    version even when their text is identical. An empty digest means the
    task carries no report."""
    output = output if isinstance(output, dict) else {}
    body = {field: output[field] for field in REPORT_BODY_FIELDS if field in output}
    schema = str(body.get("schema_version") or "")
    digest = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest() if body else ""
    version = int(input_version or 1)
    return {
        "report_schema_version": schema,
        "report_sha256": digest,
        "input_version": version,
        "report_version": "{0}:{1}:{2}:{3}:{4}".format(
            REPORT_IDENTITY_SCHEME, schema, str(task_id or ""), version, digest) if digest else "",
    }


def _owner_binding(store: Any, task_id: str) -> str:
    """Trusted owner of a task's input: the server-side binding row, only
    when the personal workspace entry wrote it. No binding, or any other
    ``via``, means shared research."""
    reader = getattr(store, "input_owner_binding", None)
    if reader is None:
        return ""
    binding = reader(task_id) or {}
    if str(binding.get("via") or "") != PERSONAL_ORIGIN_VIA:
        return ""
    return str(binding.get("owner") or "").strip()


def build_library_index(store: Any, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Project terminal tasks into library entries (pure read; no writes).

    Non-terminal tasks are deliberately absent: the library is finished
    research, not a queue view. ``namespace`` and ``owner`` come from the
    trusted ownership record: ``personal`` names the one owner; ``default``
    is shared research. ``research_occasion`` keeps the origin's occasion
    marker (rerun / follow-up) for the timeline — it is NOT an ownership
    signal."""
    entries: List[Dict[str, Any]] = []
    for task in store.list_tasks():
        if task.get("status") not in TERMINAL_STATUSES:
            continue
        payload = task.get("input_payload") or {}
        output = task.get("output") or {}
        origin = payload.get("origin") or {}
        evidence_shas = sorted({
            str(ref.get("sha256"))
            for ref in (payload.get("public_context") or {}).get("evidence_summaries", {}).values()
            if isinstance(ref, dict) and ref.get("sha256")
        })
        bundle = (task.get("model_call") or {}).get("fact_bundle") or {}
        for document in bundle.get("evidence_documents") or []:
            sha = str(document.get("sha256") or "")
            if sha:
                evidence_shas.append(sha)
        owner = _owner_binding(store, str(task.get("task_id") or ""))
        # Where each bound original was AUTHORIZED when the report was made:
        # the worker's delivery record names the namespace per document. A
        # SHA the record does not name keeps ``unrecorded`` and is resolved
        # at check time from the task's own namespace (see annotate_revocation).
        recorded = ((task.get("model_call") or {}).get("context_delivery") or {}).get("evidence_namespaces") or {}
        sources = {}
        for sha in sorted(set(evidence_shas)):
            source = recorded.get("document:sha256:{0}".format(sha))
            sources[sha] = source if source in ("shared", "personal") else "unrecorded"
        # What the worker ACTUALLY delivered for each original — the vault
        # title it took from the authorizing namespace. The older delivery
        # record carries this but no namespace marker; when two namespaces
        # hold the same bytes, the delivered title is the record's own
        # evidence of which copy the report used.
        delivered_titles = {}
        for item in (((task.get("model_call") or {}).get("outbound_context") or {})
                     .get("evidence_summaries") or {}).values():
            if isinstance(item, dict) and item.get("sha256") and item.get("summary"):
                delivered_titles[str(item["sha256"])] = str(item["summary"])
        entries.append({
            "task_id": str(task.get("task_id") or ""),
            "status": str(task.get("status")),
            "question": str(payload.get("research_question") or "")[:500],
            "symbols": [str(s) for s in (payload.get("symbols") or [])],
            "summary": str(output.get("summary") or "")[:2000],
            "key_findings": [str(f) for f in (output.get("key_findings") or [])][:20],
            "quality": (output.get("quality") or {}).get("verdict", ""),
            "completed_at": str(task.get("updated_at") or ""),
            "prior_task_id": str(origin.get("prior_task_id") or "") or None,
            "research_occasion": str(origin.get("research_occasion") or ""),
            "evidence_shas": sorted(set(evidence_shas)),
            "evidence_sources": sources,
            "delivered_titles": delivered_titles,
            "namespace": NAMESPACE_PERSONAL if owner else NAMESPACE_SHARED,
            "owner": owner or None,
            **report_identity(output, str(task.get("task_id") or ""), task.get("input_version")),
        })
    entries.sort(key=_sort_key)
    return {"schema_version": LIBRARY_SCHEMA, "entry_count": len(entries),
            "entries": entries}


def _entry_text(entry: Dict[str, Any]) -> str:
    return " \n ".join([entry.get("question", ""), entry.get("summary", ""),
                        *entry.get("key_findings", [])])


def verified_shas(views: Iterable[Dict[str, Any]]) -> set:
    """SHAs of the originals currently verified across the given views."""
    live: set = set()
    for view in views:
        live.update(verified_evidence_documents(view or {}))
    return live


def _sort_key(entry: Dict[str, Any]):
    """Order by the real completion instant; a completion without a parsable
    offset-bearing timestamp sorts after those that have one, by its raw
    text. ``task_id`` keeps same-moment entries in a stable order."""
    raw = str(entry.get("completed_at") or "")
    try:
        completed = datetime.fromisoformat(raw)
        if completed.utcoffset() is None:
            raise ValueError
        return (0, completed.timestamp(), str(entry.get("task_id") or ""))
    except ValueError:
        return (1, raw, str(entry.get("task_id") or ""))


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _bound(text: str, name: str):
    """Parse a time bound. Accepted: an offset-bearing ISO timestamp
    (compared as an instant) or a bare ``YYYY-MM-DD`` calendar date
    (returned as a ``date``, compared against the completion's own local
    calendar day: ``since`` includes that day, ``until`` includes that whole
    day). Anything else — naive timestamps, partial or impossible dates — is
    rejected rather than guessed."""
    text = str(text or "").strip()
    if not text:
        return None
    if _DATE_ONLY.match(text):
        try:
            return datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            pass
    elif text:
        try:
            moment = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
            if moment.tzinfo is not None and moment.utcoffset() is not None:
                return moment
        except ValueError:
            pass
    raise LibraryTimeWindowError(
        "invalid {0}: use an offset-bearing timestamp (2026-11-24T17:08:52+08:00 "
        "or 2026-11-24T09:08:52Z) or a calendar date (2026-11-24)".format(name))


def _day_start(day):
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def _day_end(day):
    return _day_start(day + timedelta(days=1)) - timedelta(seconds=1)


def _inverted(since, until) -> bool:
    """Whether the window is empty. Two timestamps: plain instant order. A
    calendar date is read in the completion's own offset, so against a
    timestamp it is only certainly inverted when no offset in the full
    UTC-14..+14 range leaves a match: the EARLIEST possible start of the
    ``since`` day is still after ``until``, or ``since`` is already after
    the LATEST possible end of the ``until`` day. Any other mixed pair is
    a legal window and is evaluated per completion."""
    if since is None or until is None:
        return False
    if isinstance(since, datetime) and isinstance(until, datetime):
        return since > until
    if isinstance(since, datetime):           # until is a date
        return since > _day_end(until) + timedelta(hours=12)
    if isinstance(until, datetime):           # since is a date
        return _day_start(since) - timedelta(hours=10) > until
    return since > until


def _in_window(completed_at: str, since, until) -> bool:
    """Window test on real instants. ``since``/``until`` are ``None``, an
    aware ``datetime`` or a ``date``. A completion without an offset-bearing
    timestamp lies outside every bounded window."""
    if since is None and until is None:
        return True
    try:
        completed = datetime.fromisoformat(str(completed_at or ""))
        if completed.tzinfo is None or completed.utcoffset() is None:
            return False
    except ValueError:
        return False
    if isinstance(since, datetime) and completed < since:
        return False
    if isinstance(until, datetime) and completed > until:
        return False
    if not isinstance(since, datetime) and since is not None and completed.date() < since:
        return False
    if not isinstance(until, datetime) and until is not None and completed.date() > until:
        return False
    return True


def annotate_revocation(index: Dict[str, Any], config: Dict[str, Any], *,
                        actor: str = "") -> Dict[str, Any]:
    """Re-check every entry's evidence against the CURRENT authorized views.

    Each original is judged in the namespace that authorized it WHEN THE
    REPORT WAS MADE — ``evidence_sources`` from the worker's delivery record
    — never by which vault holds the same bytes at query time:
    ``shared`` is checked in the shared vault, ``personal`` in the report
    OWNER's personal namespace. A later copy of the same bytes in another
    vault therefore cannot keep a withdrawn original answering, in either
    direction.

    Historical records that do not name a source (``unrecorded``) recover
    it from what the ledgers can prove. Exactly one namespace ever held the
    bytes: that one authorized them. Held nowhere: judged in the task's own
    namespace, which fails. Both the shared vault and the owner's namespace
    hold them: the title the worker recorded as delivered names the copy
    the report used when exactly one of the two ledgers ever recorded that
    title. When both did (same title), or no delivered title is on record,
    and the copies disagree on whether they are still verified, the entry is
    ``source_unverified`` —
    excluded by default, kept for the audit view with ``unverified_sources``.

    An entry whose evidence fully left its authorizing projection is
    ``evidence_revoked``; one that lost part of it is
    ``evidence_partially_revoked`` with the surviving shas in
    ``live_evidence_shas``. BOTH are stale and excluded by default: the
    summary may rest on the revoked original. An explicit ``include_stale``
    (audit) still shows them, labelled."""
    state_cache: Dict[str, tuple] = {}

    def namespace_state(owner: str):
        """(SHAs the namespace HOLDS in any status, SHAs it verifies now, every
        title its ledger EVER recorded per SHA). ``owner`` empty names the
        shared vault."""
        if owner not in state_cache:
            try:
                view = config if not owner else personal_vault_view(config, owner)
                vault = load_evidence_vault(view or {})
                documents = [document for document in (vault.get("documents") or {}).values()
                             if isinstance(document, dict) and document.get("sha256")]
                held = {str(document["sha256"]) for document in documents}
                titles: Dict[str, set] = {str(document["sha256"]): set() for document in documents}
                for record in load_hash_chained_jsonl(
                        project_path(evidence_vault_config(view or {})["events_path"]))["records"]:
                    if record.get("event_type") in ("evidence_captured", "evidence_quarantined"):
                        titles.setdefault(str((record.get("payload") or {}).get("sha256")), set()).add(
                            str((record.get("payload") or {}).get("title") or ""))
                live = set(verified_evidence_documents(view or {}).keys())
            except Exception:  # an unresolvable namespace holds and verifies nothing
                held, live, titles = set(), set(), {}  # type: ignore[assignment]
            state_cache[owner] = (held, live, titles)
        return state_cache[owner]

    checked = []
    for entry in index.get("entries", []):
        shas = entry.get("evidence_shas") or []
        personal = entry.get("namespace") == NAMESPACE_PERSONAL
        owner = str(entry.get("owner") or "")
        sources = entry.get("evidence_sources") or {}

        titles = entry.get("delivered_titles") or {}

        def judge(sha, _owner=owner, _sources=sources, _personal=personal,
                  _titles=titles):
            source = _sources.get(sha)
            if source not in ("shared", "personal"):
                # Historical record: recover the source from what the record
                # and the ledgers can prove. Exactly one namespace ever held
                # these bytes -> that one authorized them. Held nowhere ->
                # judged (and failing) in the task's own namespace.
                namespaces = [("shared", *namespace_state(""))] if not _personal else [
                    ("shared", *namespace_state("")), ("personal", *namespace_state(_owner))]
                holders = [state for state in namespaces if sha in state[1]]
                if len(holders) == 1:
                    source = holders[0][0]
                elif not holders:
                    source = NAMESPACE_PERSONAL if _personal else NAMESPACE_SHARED
                else:
                    # Both hold them: the title the worker delivered is the
                    # record's own statement of which copy it used. It names
                    # the copy when exactly one ledger ever recorded it.
                    delivered = str(_titles.get(sha) or "")
                    named = [state[0] for state in holders if delivered and delivered in state[3].get(sha, ())]
                    if len(named) == 1:
                        source = named[0]
                    elif all(sha in state[2] for state in holders):
                        return True, False   # either copy still backs the report
                    elif not any(sha in state[2] for state in holders):
                        return False, False  # withdrawn everywhere
                    else:
                        # The copies disagree and nothing on record tells
                        # them apart: needs review, excluded by default.
                        return False, True
            return sha in namespace_state(_owner if source == "personal" else "")[1], False

        live, unverified = [], []
        for sha in shas:
            ok, needs_review = judge(sha)
            if ok:
                live.append(sha)
            if needs_review:
                unverified.append(sha)
        entry = dict(entry)
        entry["live_evidence_shas"] = live
        entry.pop("unverified_sources", None)
        if shas and not live and not unverified:
            entry["stale_reason"] = "evidence_revoked"
        elif unverified:
            entry["stale_reason"] = "source_unverified"
            entry["unverified_sources"] = unverified
        elif len(live) < len(shas):
            entry["stale_reason"] = "evidence_partially_revoked"
        else:
            entry.pop("stale_reason", None)
        checked.append(entry)
    return {**index, "entries": checked, "revocation_checked": True}


def can_audit(config: Dict[str, Any], actor: str) -> bool:
    """Whether the actor may read stale rows: an ACTIVE HUMAN roster
    principal holding an audit role. Nobody else — a request cannot grant
    it to itself."""
    from .research_operations import load_operations_inputs

    actor = str(actor or "").strip()
    if not actor:
        return False
    try:
        principal = (load_operations_inputs(config or {}).get("principal_map") or {}).get(actor)
    except Exception:  # an unreadable roster grants nothing
        return False
    return bool(principal and principal.get("active")
                and principal.get("principal_type") == "human"
                and AUDIT_ROLES & set(principal.get("roles") or []))


def _authorized(entry: Dict[str, Any], actor: str) -> bool:
    """Shared research, or the caller's OWN personal research."""
    namespace = entry.get("namespace")
    if namespace == NAMESPACE_PERSONAL:
        return bool(actor) and entry.get("owner") == actor
    return namespace == NAMESPACE_SHARED and "owner" in entry


def query_library(index: Dict[str, Any], config: Dict[str, Any], actor: str, *,
                   query: str = "", symbols: Optional[List[str]] = None,
                   task_id: str = "", since: str = "", until: str = "",
                   status: str = "", include_stale: bool = False,
                   timeline_of: str = "") -> Dict[str, Any]:
    """The normal retrieval entry: authorized, revocation-checked per query.

    ``actor`` is the server-verified session principal. Every call (1)
    re-checks the — possibly cached — index against the actor's CURRENT
    authorized evidence views, (2) keeps shared research plus the actor's own
    personal research only, and (3) applies the search filters.
    ``include_stale`` is honored only for an authorized auditor and is
    reported back as ``audit_granted``. ``timeline_of`` narrows the result
    to one case and the continuations linked to it by ``prior_task_id``."""
    actor = str(actor or "").strip()
    if not actor:
        raise LibraryError("library queries are scoped: an authenticated actor is required")
    since_at, until_at = _bound(since, "since"), _bound(until, "until")
    if _inverted(since_at, until_at):
        raise LibraryTimeWindowError("invalid time window: since is after until")
    checked = annotate_revocation(index, config, actor=actor)
    scoped = [entry for entry in checked["entries"] if _authorized(entry, actor)]
    audit = bool(include_stale) and can_audit(config, actor)
    result = search_library({**checked, "entries": scoped}, query,
                            symbols=symbols, task_id=task_id, since=since, until=until,
                            since_at=since_at, until_at=until_at, status=status,
                            include_stale=audit)
    if timeline_of:
        result["results"] = [entry for entry in result["results"]
                             if entry["task_id"] == timeline_of
                             or entry.get("prior_task_id") == timeline_of]
        result["result_count"] = len(result["results"])
    result.update({
        "actor": actor,
        "scope": {"shared": True, "personal_owner": actor, "out_of_scope_hidden": True},
        "revocation_checked": True,
        "audit_granted": audit,
        "timeline_of": timeline_of,
    })
    result["filters"]["include_stale"] = audit
    return result


def search_library(index: Dict[str, Any], query: str = "", *,
                   symbols: Optional[List[str]] = None,
                   task_id: str = "",
                   since: str = "", until: str = "",
                   status: str = "",
                   include_stale: bool = False,
                   since_at=None, until_at=None) -> Dict[str, Any]:
    """Structured + full-text filter over a revocation-annotated index.

    This is the filter step, not a retrieval entry: it trusts the
    annotation it is given and never re-checks the vault, so an index
    annotated before a later revocation would still answer. Callers must go
    through :func:`query_library`, which annotates on every query; an index
    that was never annotated is refused.

    Full-text is case-insensitive substring over question/summary/findings.
    Time bounds are real instants: an offset-bearing timestamp compares as a
    moment whatever its offset, a bare calendar date covers that whole day
    in the completion's own offset (``until`` is inclusive), and an
    unparseable bound is refused. :func:`query_library` parses the bounds
    once and hands them over pre-parsed."""
    if not isinstance(index, dict) or index.get("schema_version") != LIBRARY_SCHEMA:
        raise LibraryError("not a research library index")
    if not index.get("revocation_checked"):
        raise LibraryError("index is not revocation-checked: query through query_library")
    needle = str(query or "").strip().lower()
    wanted_symbols = {str(s) for s in (symbols or []) if str(s).strip()}
    if since_at is None and until_at is None and (since or until):
        since_at, until_at = _bound(since, "since"), _bound(until, "until")
    results = []
    for entry in index.get("entries", []):
        if not include_stale and entry.get("stale_reason"):
            continue
        if task_id and entry.get("task_id") != task_id:
            continue
        if status and entry.get("status") != status:
            continue
        if wanted_symbols and not (wanted_symbols & set(entry.get("symbols") or [])):
            continue
        if not _in_window(entry.get("completed_at"), since_at, until_at):
            continue
        if needle and needle not in _entry_text(entry).lower():
            continue
        results.append(entry)
    return {"schema_version": LIBRARY_SCHEMA, "query": query,
            "filters": {"symbols": sorted(wanted_symbols), "task_id": task_id,
                        "since": since, "until": until, "status": status,
                        "include_stale": include_stale},
            "result_count": len(results), "results": results}
