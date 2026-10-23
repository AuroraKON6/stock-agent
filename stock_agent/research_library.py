"""R20: research library — a rebuildable index over durable research state.

The library answers "what do we already know about this company/question"
from COMPLETED research: it indexes terminal tasks (question, outcome,
quality verdict, bound evidence, symbols, namespace) and offers structured +
full-text retrieval. Design rules (GLM_REFACTOR_TASKBOOK R20):

- **Rebuildable by construction**: the index is a pure projection of the
  TaskStore + evidence vault projections. Dropping it loses nothing;
  rebuilding reproduces it. The originals (events, snapshots, documents)
  stay the only authority.
- **Revocation propagates (stop condition)**: every bound evidence document
  is re-checked against the CURRENT verified projection at SEARCH time; an
  entry whose evidence left the projection is reported as stale and excluded
  from results unless explicitly requested — a revoked document never keeps
  feeding answers through an old index row.
- **No embedding required**: structured fields + substring full-text over
  question/summary/findings. Semantic retrieval is a later, separately
  justified addition.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from .evidence_context import verified_evidence_documents
from .task_runtime.store import TERMINAL_STATUSES

LIBRARY_SCHEMA = "1.0"


class LibraryError(ValueError):
    pass


def build_library_index(store: Any, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Project terminal tasks into library entries (pure read; no writes).

    Non-terminal tasks are deliberately absent: the library is finished
    research, not a queue view."""
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
        entries.append({
            "task_id": str(task.get("task_id") or ""),
            "status": str(task.get("status")),
            "question": str(payload.get("research_question") or "")[:500],
            "symbols": [str(s) for s in (payload.get("symbols") or [])],
            "summary": str(output.get("summary") or "")[:2000],
            "key_findings": [str(f) for f in (output.get("key_findings") or [])][:20],
            "quality": (output.get("quality") or {}).get("verdict", ""),
            "completed_at": str(task.get("updated_at") or ""),
            "input_version": int(task.get("input_version") or 1),
            "prior_task_id": str(origin.get("prior_task_id") or "") or None,
            "evidence_shas": sorted(set(evidence_shas)),
            "namespace": "personal" if origin.get("research_occasion") else "default",
        })
    entries.sort(key=lambda e: (e["completed_at"], e["task_id"]))
    return {"schema_version": LIBRARY_SCHEMA, "entry_count": len(entries),
            "entries": entries}


def _entry_text(entry: Dict[str, Any]) -> str:
    return " \n ".join([entry.get("question", ""), entry.get("summary", ""),
                        *entry.get("key_findings", [])])


def annotate_revocation(index: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Re-check every entry's evidence against the CURRENT verified vault.

    Entries whose evidence fully left the projection are marked
    ``stale_reason=evidence_revoked``; partially-stale entries keep the
    surviving shas in ``live_evidence_shas`` so consumers see exactly what
    still backs them."""
    verified = verified_evidence_documents(config)
    live_by_entry = []
    for entry in index.get("entries", []):
        shas = entry.get("evidence_shas") or []
        live = [sha for sha in shas if sha in verified]
        entry = dict(entry)
        entry["live_evidence_shas"] = live
        if shas and not live:
            entry["stale_reason"] = "evidence_revoked"
        elif len(live) < len(shas):
            entry["stale_reason"] = "evidence_partially_revoked"
        live_by_entry.append(entry)
    return {**index, "entries": live_by_entry,
            "revocation_checked": True}


def search_library(index: Dict[str, Any], query: str = "", *,
                   symbols: Optional[List[str]] = None,
                   task_id: str = "",
                   since: str = "", until: str = "",
                   status: str = "",
                   include_stale: bool = False) -> Dict[str, Any]:
    """Structured + full-text search over the (revocation-annotated) index.

    Full-text is case-insensitive substring over question/summary/findings.
    Date bounds compare against ``completed_at`` ISO strings lexically."""
    if not isinstance(index, dict) or index.get("schema_version") != LIBRARY_SCHEMA:
        raise LibraryError("not a research library index")
    needle = str(query or "").strip().lower()
    wanted_symbols = {str(s) for s in (symbols or []) if str(s).strip()}
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
        completed = str(entry.get("completed_at") or "")
        if since and completed < str(since):
            continue
        if until and completed > str(until):
            continue
        if needle and needle not in _entry_text(entry).lower():
            continue
        results.append(entry)
    return {"schema_version": LIBRARY_SCHEMA, "query": query,
            "filters": {"symbols": sorted(wanted_symbols), "task_id": task_id,
                        "since": since, "until": until, "status": status,
                        "include_stale": include_stale},
            "result_count": len(results), "results": results}
