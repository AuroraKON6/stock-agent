from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List

from .config import project_path
from .models import now_iso
from .research_git import project_research_git_state


SAFE_WORKSPACE_NOTE = "Workspace objects are research evidence and comments only; no automatic trading or config changes."


def build_workspace_inbox_from_report(report_path: str = "") -> Dict[str, Any]:
    path = _resolve_report_path(report_path)
    data = _read_json(path)
    signals = data.get("signals", []) if isinstance(data, dict) else []
    research_state = project_research_git_state()
    draft_index = _drafts_by_signal(research_state.get("drafts", {}) or {})

    inbox: Dict[str, Any] = {
        "generated_at": now_iso(),
        "workspace": "stock_agent_research_workspace",
        "report_path": str(path),
        "documents": [
            {
                "document_id": _object_id("document", str(path)),
                "kind": "json_report",
                "path": str(path),
                "generated_at": data.get("generated_at", ""),
            }
        ],
        "signals": {},
        "evidence_items": [],
        "comments": [],
        "gap_tasks": [],
        "research_drafts": {},
        "object_counts": {},
        "safety_boundary": SAFE_WORKSPACE_NOTE,
    }

    for signal in signals:
        if not isinstance(signal, dict):
            continue
        signal_id = str(signal.get("signal_id") or "")
        if not signal_id:
            continue
        signal_obj = _signal_object(signal)
        drafts = draft_index.get(signal_id, [])
        signal_obj["research_draft_ids"] = [draft.get("draft_id", "") for draft in drafts]
        for draft in drafts:
            draft_id = str(draft.get("draft_id") or "")
            if draft_id:
                inbox["research_drafts"][draft_id] = {
                    "draft_id": draft_id,
                    "entity_type": draft.get("entity_type", ""),
                    "entity_id": draft.get("entity_id", ""),
                    "title": draft.get("title", ""),
                    "status": draft.get("status", ""),
                    "updated_at": draft.get("updated_at", ""),
                }

        evidence = _evidence_for_signal(signal)
        comments = _comments_for_signal(signal)
        gaps = _gap_tasks_for_signal(signal)
        signal_obj["evidence_item_ids"] = [item["evidence_id"] for item in evidence]
        signal_obj["comment_ids"] = [item["comment_id"] for item in comments]
        signal_obj["gap_task_ids"] = [item["task_id"] for item in gaps]
        inbox["signals"][signal_id] = signal_obj
        inbox["evidence_items"].extend(evidence)
        inbox["comments"].extend(comments)
        inbox["gap_tasks"].extend(gaps)

    inbox["object_counts"] = {
        "documents": len(inbox["documents"]),
        "signals": len(inbox["signals"]),
        "evidence_items": len(inbox["evidence_items"]),
        "comments": len(inbox["comments"]),
        "gap_tasks": len(inbox["gap_tasks"]),
        "research_drafts": len(inbox["research_drafts"]),
    }
    return inbox


def write_workspace_inbox(inbox: Dict[str, Any] | None = None) -> Dict[str, str]:
    inbox = inbox or build_workspace_inbox_from_report()
    stamp = str(inbox.get("generated_at") or now_iso()).replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
    data_dir = project_path("data/workspace/inbox")
    report_dir = project_path("reports/workspace")
    data_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    workspace_path = data_dir / "workspace_inbox_{0}.json".format(stamp)
    report_json = report_dir / "workspace_inbox_{0}.json".format(stamp)
    latest_json = report_dir / "latest.json"
    report_md = report_dir / "workspace_inbox_{0}.md".format(stamp)
    latest_md = report_dir / "latest.md"

    payload = json.dumps(inbox, ensure_ascii=False, indent=2, default=str)
    workspace_path.write_text(payload, encoding="utf-8")
    report_json.write_text(payload, encoding="utf-8")
    latest_json.write_text(payload, encoding="utf-8")
    markdown = _render_workspace_markdown(inbox)
    report_md.write_text(markdown, encoding="utf-8-sig")
    latest_md.write_text(markdown, encoding="utf-8-sig")
    return {
        "workspace_inbox_path": str(workspace_path),
        "workspace_report_json_path": str(report_json),
        "workspace_latest_json_path": str(latest_json),
        "workspace_report_path": str(report_md),
        "workspace_latest_path": str(latest_md),
    }


def sync_workspace_inbox(report_path: str = "") -> Dict[str, Any]:
    inbox = build_workspace_inbox_from_report(report_path)
    return {**inbox, **write_workspace_inbox(inbox)}


def _signal_object(signal: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "signal_id": signal.get("signal_id", ""),
        "title": signal.get("title", ""),
        "priority": signal.get("priority", ""),
        "source": signal.get("source", ""),
        "source_type": signal.get("source_type", ""),
        "url": signal.get("url", ""),
        "topics": list(signal.get("topics", []) or []),
        "symbols": list(signal.get("symbols", []) or []),
        "reasons": list(signal.get("reasons", []) or []),
    }


def _evidence_for_signal(signal: Dict[str, Any]) -> List[Dict[str, Any]]:
    signal_id = str(signal.get("signal_id") or "")
    payload = signal.get("payload", {}) or {}
    evidence: List[Dict[str, Any]] = []
    url = str(signal.get("url") or "").strip()
    if url:
        evidence.append(_evidence(signal_id, "source_url", url, {"url": url}))
    filing = payload.get("filing_evidence")
    if isinstance(filing, dict):
        evidence.append(_evidence(signal_id, "filing_evidence", "payload.filing_evidence", _compact_dict(filing)))
    source_quality = payload.get("source_quality")
    if isinstance(source_quality, dict):
        evidence.append(_evidence(signal_id, "source_quality", "payload.source_quality", _compact_dict(source_quality)))
    chain = signal.get("evidence_chain")
    if isinstance(chain, dict) and chain:
        evidence.append(_evidence(signal_id, "evidence_chain", "signal.evidence_chain", _compact_dict(chain)))
    role = signal.get("role_synthesis")
    if isinstance(role, dict) and role:
        evidence.append(_evidence(signal_id, "role_synthesis", "signal.role_synthesis", _compact_dict(role)))
    return evidence


def _comments_for_signal(signal: Dict[str, Any]) -> List[Dict[str, Any]]:
    signal_id = str(signal.get("signal_id") or "")
    comments: List[Dict[str, Any]] = []
    llm = signal.get("llm_analysis")
    if isinstance(llm, dict) and llm:
        thesis = str(llm.get("key_thesis") or "").strip()
        if thesis:
            comments.append(_comment(signal_id, "llm_thesis", "llm_agent", thesis, {
                "confidence": llm.get("confidence", ""),
                "impact_direction": llm.get("impact_direction", ""),
                "horizon": llm.get("horizon", ""),
            }))
        for item in llm.get("risk_points", []) or []:
            text = str(item or "").strip()
            if text:
                comments.append(_comment(signal_id, "risk_point", "llm_agent", text, {}))
        for item in llm.get("review_questions", []) or []:
            text = str(item or "").strip()
            if text:
                comments.append(_comment(signal_id, "review_question", "llm_agent", text, {}))
    for item in signal.get("research_questions", []) or []:
        text = str(item or "").strip()
        if text:
            comments.append(_comment(signal_id, "review_question", "research_framework", text, {}))
    role = signal.get("role_synthesis")
    if isinstance(role, dict):
        disagreement = str(role.get("key_disagreement") or "").strip()
        if disagreement:
            comments.append(_comment(signal_id, "role_comment", "multi_role_agent", disagreement, {
                "decision_gate": role.get("decision_gate", ""),
            }))
    return comments


def _gap_tasks_for_signal(signal: Dict[str, Any]) -> List[Dict[str, Any]]:
    signal_id = str(signal.get("signal_id") or "")
    tasks = []
    for item in signal.get("evidence_gap_tasks", []) or []:
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("task_id") or _object_id("gap", signal_id, item.get("gap", "")))
        tasks.append({
            "task_id": task_id,
            "signal_id": signal_id,
            "gap": item.get("gap", ""),
            "status": item.get("status", "open"),
        })
    return tasks


def _evidence(signal_id: str, kind: str, ref: str, detail: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "evidence_id": _object_id("evidence", signal_id, kind, ref),
        "signal_id": signal_id,
        "kind": kind,
        "ref": ref,
        "detail": detail,
    }


def _comment(signal_id: str, kind: str, author: str, text: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "comment_id": _object_id("comment", signal_id, kind, author, text),
        "signal_id": signal_id,
        "kind": kind,
        "author": author,
        "text": text[:1000],
        "meta": meta,
    }


def _drafts_by_signal(drafts: Dict[str, Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    result: Dict[str, List[Dict[str, Any]]] = {}
    for draft in drafts.values():
        if str(draft.get("entity_type") or "") != "signal":
            continue
        entity_id = str(draft.get("entity_id") or "")
        if entity_id:
            result.setdefault(entity_id, []).append(draft)
    return result


def _resolve_report_path(report_path: str):
    if report_path:
        return project_path(report_path)
    report_dir = project_path("reports/json")
    if not report_dir.exists():
        raise ValueError("No reports/json directory found")
    files = sorted(report_dir.glob("*.json"))
    if not files:
        raise ValueError("No JSON reports found in reports/json")
    return files[-1]


def _read_json(path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Cannot read workspace report JSON: {0}".format(path)) from exc
    return data if isinstance(data, dict) else {}


def _compact_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    blocked = {"raw", "raw_blob", "html", "content", "body", "text"}
    result = {}
    for key, value in data.items():
        key_text = str(key)
        if key_text in blocked:
            continue
        if isinstance(value, (dict, list)):
            encoded = json.dumps(value, ensure_ascii=False, default=str)
            result[key_text] = encoded[:500]
        else:
            result[key_text] = str(value)[:500]
    return result


def _render_workspace_markdown(inbox: Dict[str, Any]) -> str:
    counts = inbox.get("object_counts", {}) or {}
    lines = [
        "# Workspace Inbox",
        "",
        "- generated_at: {0}".format(inbox.get("generated_at", "")),
        "- report_path: {0}".format(inbox.get("report_path", "")),
        "- signals: {0}".format(counts.get("signals", 0)),
        "- evidence_items: {0}".format(counts.get("evidence_items", 0)),
        "- comments: {0}".format(counts.get("comments", 0)),
        "- gap_tasks: {0}".format(counts.get("gap_tasks", 0)),
        "- research_drafts: {0}".format(counts.get("research_drafts", 0)),
        "- safety_boundary: {0}".format(inbox.get("safety_boundary", SAFE_WORKSPACE_NOTE)),
        "",
        "## Signals",
    ]
    signals = inbox.get("signals", {}) or {}
    if not signals:
        lines.append("- no workspace signals")
    for signal in signals.values():
        lines.append("- [{0}] {1} | evidence={2} | comments={3} | drafts={4}".format(
            signal.get("priority", ""),
            signal.get("title", ""),
            len(signal.get("evidence_item_ids", []) or []),
            len(signal.get("comment_ids", []) or []),
            ", ".join(signal.get("research_draft_ids", []) or []) or "-",
        ))
    return "\n".join(lines)


def _object_id(*parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
