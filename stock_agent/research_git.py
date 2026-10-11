from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from typing import Any, Dict, Iterable, List

from .analysis import min_priority_passes
from .config import project_path
from .models import now_iso


SAFE_RESEARCH_NOTE = "Research-only workflow. No auto trading, order placement, or position rebalance."

FORBIDDEN_RESEARCH_TERMS = {
    "auto_trade",
    "place_order",
    "order placement",
    "rebalance",
    "buy",
    "sell",
    "自动交易",
    "自动下单",
    "下单",
    "买入",
    "卖出",
    "调仓",
    "加仓",
    "减仓",
}

RESEARCH_EVENTS = {
    "stage": "research_draft_staged",
    "commit": "research_draft_committed",
    "approve": "research_draft_approved",
    "reject": "research_draft_rejected",
    "defer": "research_draft_deferred",
}

TERMINAL_STATUSES = {"approved", "rejected"}


def stage_research_draft(
    *,
    entity_type: str,
    entity_id: str,
    title: str,
    thesis: str,
    evidence_refs: Iterable[str] | None = None,
    review_questions: Iterable[str] | None = None,
    actor: str = "agent",
    source: str = "research_git",
) -> Dict[str, Any]:
    entity_type = _require_text(entity_type, "entity_type")
    entity_id = _require_text(entity_id, "entity_id")
    title = _require_text(title, "title")
    thesis = _require_text(thesis, "thesis")
    evidence = _string_list(evidence_refs or [])
    questions = _string_list(review_questions or [])
    _reject_forbidden_text([title, thesis, *evidence, *questions])

    draft_id = _draft_id(entity_type, entity_id, title, thesis, evidence, questions)
    record = _base_record(
        event_type=RESEARCH_EVENTS["stage"],
        draft_id=draft_id,
        status="draft",
        actor=actor,
        source=source,
        payload={
            "entity_type": entity_type,
            "entity_id": entity_id,
            "title": title,
            "thesis": thesis,
            "evidence_refs": evidence,
            "review_questions": questions,
        },
    )
    _append_record(record)
    _emit_research_draft_event(record)
    return record


def commit_research_draft(
    draft_id: str,
    message: str,
    actor: str = "user",
) -> Dict[str, Any]:
    draft = _require_existing_draft(draft_id)
    if draft.get("status") in TERMINAL_STATUSES:
        raise ValueError("Cannot commit terminal research draft: {0}".format(draft_id))
    message = _require_text(message, "message")
    _reject_forbidden_text([message])
    commit_hash = _commit_hash(draft_id, message, draft.get("thesis", ""))
    record = _base_record(
        event_type=RESEARCH_EVENTS["commit"],
        draft_id=draft_id,
        status="committed",
        actor=actor,
        source="research_git",
        payload={
            "message": message,
            "commit_hash": commit_hash,
        },
    )
    record["commit_hash"] = commit_hash
    _append_record(record)
    _emit_research_draft_event(record)
    return record


def approve_research_draft(
    draft_id: str,
    notes: str = "",
    actor: str = "user",
) -> Dict[str, Any]:
    return _record_human_gate(
        draft_id=draft_id,
        action="approve",
        status="approved",
        notes=notes,
        actor=actor,
        required_statuses={"committed", "deferred"},
    )


def reject_research_draft(
    draft_id: str,
    notes: str = "",
    actor: str = "user",
) -> Dict[str, Any]:
    return _record_human_gate(
        draft_id=draft_id,
        action="reject",
        status="rejected",
        notes=notes,
        actor=actor,
        required_statuses={"committed", "deferred"},
    )


def defer_research_draft(
    draft_id: str,
    notes: str = "",
    actor: str = "user",
) -> Dict[str, Any]:
    return _record_human_gate(
        draft_id=draft_id,
        action="defer",
        status="deferred",
        notes=notes,
        actor=actor,
        required_statuses={"draft", "committed", "deferred"},
    )


def load_research_git_records() -> List[Dict[str, Any]]:
    root = project_path("data/research_git")
    if not root.exists():
        return []
    records: List[Dict[str, Any]] = []
    for path in sorted(root.glob("research_git_*.jsonl")):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if isinstance(data, dict):
                    records.append(data)
        except (OSError, json.JSONDecodeError):
            continue
    return records


def project_research_git_state(records: Iterable[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    record_list = list(records if records is not None else load_research_git_records())
    drafts: Dict[str, Dict[str, Any]] = {}
    for record in record_list:
        draft_id = str(record.get("draft_id") or "")
        if not draft_id:
            continue
        event_type = str(record.get("event_type") or "")
        payload = record.get("payload", {}) or {}
        state = drafts.setdefault(draft_id, {"draft_id": draft_id, "history": []})
        if event_type == RESEARCH_EVENTS["stage"]:
            state.update({
                "entity_type": payload.get("entity_type", ""),
                "entity_id": payload.get("entity_id", ""),
                "title": payload.get("title", ""),
                "thesis": payload.get("thesis", ""),
                "evidence_refs": payload.get("evidence_refs", []),
                "review_questions": payload.get("review_questions", []),
            })
        elif event_type == RESEARCH_EVENTS["commit"]:
            state.update({
                "commit_hash": payload.get("commit_hash", ""),
                "commit_message": payload.get("message", ""),
                "committed_by": record.get("actor", ""),
            })
        elif event_type in {
            RESEARCH_EVENTS["approve"],
            RESEARCH_EVENTS["reject"],
            RESEARCH_EVENTS["defer"],
        }:
            state.update({
                "human_notes": payload.get("notes", ""),
                "human_actor": record.get("actor", ""),
            })
        state["status"] = str(record.get("status") or state.get("status") or "draft")
        state["updated_at"] = record.get("recorded_at", state.get("updated_at", ""))
        state["forbidden_actions_note"] = record.get("forbidden_actions_note", SAFE_RESEARCH_NOTE)
        state["history"].append(_history_item(record))

    status_counts = Counter(str(item.get("status", "draft")) for item in drafts.values())
    return {
        "generated_at": now_iso(),
        "workflow": "Research-as-Git",
        "drafts": drafts,
        "draft_count": len(drafts),
        "status_counts": dict(status_counts),
        "pending_review": [
            item for item in drafts.values()
            if item.get("status") in {"committed", "deferred"}
        ],
        "human_gate_counts": {
            "approved": status_counts.get("approved", 0),
            "rejected": status_counts.get("rejected", 0),
            "deferred": status_counts.get("deferred", 0),
            "pending": status_counts.get("committed", 0) + status_counts.get("deferred", 0),
        },
        "records": record_list,
        "safety_boundary": SAFE_RESEARCH_NOTE,
    }


def write_research_git_summary(state: Dict[str, Any] | None = None) -> Dict[str, str]:
    state = state or project_research_git_state()
    out_dir = project_path("reports/research_git")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(state.get("generated_at") or now_iso()).replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
    json_path = out_dir / "research_git_{0}.json".format(stamp)
    md_path = out_dir / "research_git_{0}.md".format(stamp)
    latest_json = out_dir / "latest.json"
    latest_md = out_dir / "latest.md"
    payload = json.dumps(state, ensure_ascii=False, indent=2, default=str)
    json_path.write_text(payload, encoding="utf-8")
    latest_json.write_text(payload, encoding="utf-8")
    markdown = _render_summary_markdown(state)
    md_path.write_text(markdown, encoding="utf-8-sig")
    latest_md.write_text(markdown, encoding="utf-8-sig")
    return {
        "research_git_summary_json_path": str(json_path),
        "research_git_summary_path": str(md_path),
        "research_git_summary_latest_json_path": str(latest_json),
        "research_git_summary_latest_path": str(latest_md),
    }


def stage_llm_research_drafts(
    signals: Iterable[Any],
    config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    config = config or {}
    cfg = config.get("research_git", {}) or {}
    enabled = bool(cfg.get("enabled", False))
    auto_stage = bool(cfg.get("auto_stage_from_llm", True))
    stats: Dict[str, Any] = {
        "research_git_enabled": enabled,
        "research_git_auto_stage_enabled": auto_stage,
        "research_git_candidates_count": 0,
        "research_git_auto_staged_count": 0,
        "research_git_skipped_existing_count": 0,
        "research_git_skipped_filtered_count": 0,
        "research_git_skipped_unsafe_count": 0,
        "research_git_errors": [],
        "research_git_draft_ids": [],
    }
    if not enabled or not auto_stage:
        stats.update({
            "research_git_draft_count": 0,
            "research_git_status_counts": {},
            "research_git_pending_review_count": 0,
            "research_git_summary_path": "",
            "research_git_summary_json_path": "",
            "research_git_summary_latest_path": "",
            "research_git_summary_latest_json_path": "",
        })
        return stats

    min_priority = str(cfg.get("min_priority") or "P2")
    min_confidence = float(cfg.get("min_confidence", 0.7))
    require_human = bool(cfg.get("require_human_decision_needed", True))
    existing = project_research_git_state().get("drafts", {}) or {}

    for signal in signals:
        analysis = _signal_get(signal, "llm_analysis", {}) or {}
        if not isinstance(analysis, dict):
            stats["research_git_skipped_filtered_count"] += 1
            continue
        if not analysis:
            stats["research_git_skipped_filtered_count"] += 1
            continue
        if not min_priority_passes(_signal_get(signal, "priority", "P3"), min_priority):
            stats["research_git_skipped_filtered_count"] += 1
            continue
        human_needed = bool(analysis.get("human_decision_needed"))
        if require_human and not human_needed:
            stats["research_git_skipped_filtered_count"] += 1
            continue
        if not human_needed and float(analysis.get("confidence") or 0.0) < min_confidence:
            stats["research_git_skipped_filtered_count"] += 1
            continue

        draft_args = _llm_signal_to_draft_args(signal, analysis)
        draft_id = _draft_id(
            draft_args["entity_type"],
            draft_args["entity_id"],
            draft_args["title"],
            draft_args["thesis"],
            draft_args["evidence_refs"],
            draft_args["review_questions"],
        )
        stats["research_git_candidates_count"] += 1
        if draft_id in existing:
            stats["research_git_skipped_existing_count"] += 1
            continue
        try:
            draft = stage_research_draft(**draft_args, actor="llm_agent")
        except ValueError as exc:
            stats["research_git_skipped_unsafe_count"] += 1
            stats["research_git_errors"].append(str(exc))
            continue
        existing[draft["draft_id"]] = draft
        stats["research_git_auto_staged_count"] += 1
        stats["research_git_draft_ids"].append(draft["draft_id"])

    return _with_summary_paths(stats)


def stage_llm_research_drafts_from_report(
    report_path: str = "",
    config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    path = _resolve_report_path(report_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Cannot read report JSON: {0}".format(path)) from exc
    signals = data.get("signals", []) if isinstance(data, dict) else []
    cfg = config or {
        "research_git": {
            "enabled": True,
            "auto_stage_from_llm": True,
            "min_priority": "P2",
            "min_confidence": 0.7,
            "require_human_decision_needed": True,
        }
    }
    if "research_git" not in cfg:
        cfg = {**cfg, "research_git": {"enabled": True, "auto_stage_from_llm": True}}
    stats = stage_llm_research_drafts(signals, cfg)
    stats["research_git_source_report_path"] = str(path)
    return stats


def _llm_signal_to_draft_args(signal: Any, analysis: Dict[str, Any]) -> Dict[str, Any]:
    signal_id = _require_text(_signal_get(signal, "signal_id", ""), "signal_id")
    title = "LLM research draft: {0}".format(_require_text(_signal_get(signal, "title", ""), "title"))
    thesis = _require_text(
        analysis.get("key_thesis") or _signal_get(signal, "summary", ""),
        "llm key_thesis",
    )
    evidence_refs = [
        "signal:{0}".format(signal_id),
        "source:{0}".format(_signal_get(signal, "source", "")),
        "priority:{0}".format(_signal_get(signal, "priority", "")),
    ]
    url = str(_signal_get(signal, "url", "") or "").strip()
    if url:
        evidence_refs.append("url:{0}".format(url))
    for item in analysis.get("evidence_refs", []) or []:
        text = str(item or "").strip()
        if text:
            evidence_refs.append(text)
    risk_points = [str(item) for item in analysis.get("risk_points", []) or [] if str(item or "").strip()]
    review_questions = [
        str(item) for item in analysis.get("review_questions", []) or [] if str(item or "").strip()
    ]
    for risk in risk_points[:3]:
        review_questions.append("Risk to verify: {0}".format(risk))
    review_questions.append("Confirm LLM thesis confidence={0}".format(analysis.get("confidence", "")))
    return {
        "entity_type": "signal",
        "entity_id": signal_id,
        "title": title,
        "thesis": thesis,
        "evidence_refs": evidence_refs,
        "review_questions": review_questions,
    }


def _signal_get(signal: Any, key: str, default: Any = "") -> Any:
    if isinstance(signal, dict):
        return signal.get(key, default)
    return getattr(signal, key, default)


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


def _with_summary_paths(stats: Dict[str, Any]) -> Dict[str, Any]:
    summary_paths = write_research_git_summary()
    stats.update(summary_paths)
    state = project_research_git_state()
    stats["research_git_draft_count"] = state.get("draft_count", 0)
    stats["research_git_status_counts"] = state.get("status_counts", {})
    stats["research_git_pending_review_count"] = len(state.get("pending_review", []) or [])
    return stats


def _record_human_gate(
    *,
    draft_id: str,
    action: str,
    status: str,
    notes: str,
    actor: str,
    required_statuses: set[str],
) -> Dict[str, Any]:
    draft = _require_existing_draft(draft_id)
    current_status = str(draft.get("status") or "")
    if current_status not in required_statuses:
        raise ValueError(
            "Research draft {0} is {1}; expected one of {2}".format(
                draft_id,
                current_status,
                ", ".join(sorted(required_statuses)),
            )
        )
    notes = str(notes or "")
    _reject_forbidden_text([notes])
    record = _base_record(
        event_type=RESEARCH_EVENTS[action],
        draft_id=draft_id,
        status=status,
        actor=actor,
        source="manual_research_gate",
        payload={"notes": notes},
    )
    _append_record(record)
    _emit_research_draft_event(record)
    return record


def _require_existing_draft(draft_id: str) -> Dict[str, Any]:
    draft_id = _require_text(draft_id, "draft_id")
    state = project_research_git_state()
    draft = state.get("drafts", {}).get(draft_id)
    if not draft:
        raise ValueError("Unknown research draft: {0}".format(draft_id))
    return draft


def _base_record(
    *,
    event_type: str,
    draft_id: str,
    status: str,
    actor: str,
    source: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    recorded_at = now_iso()
    return {
        "record_id": "research_git:{0}:{1}".format(_compact_time(recorded_at), uuid.uuid4().hex[:8]),
        "event_type": event_type,
        "draft_id": draft_id,
        "status": status,
        "payload": payload,
        "actor": str(actor or "user"),
        "source": str(source or "research_git"),
        "recorded_at": recorded_at,
        "forbidden_actions_note": SAFE_RESEARCH_NOTE,
    }


def _append_record(record: Dict[str, Any]) -> None:
    path = _record_log_path(str(record.get("recorded_at") or now_iso()))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _emit_research_draft_event(record: Dict[str, Any]) -> None:
    try:
        from . import brokerage_architecture
        original_project_path = brokerage_architecture.project_path
        brokerage_architecture.project_path = project_path
        try:
            brokerage_architecture.record_research_draft_event(record)
        finally:
            brokerage_architecture.project_path = original_project_path
    except Exception:
        return


def _record_log_path(recorded_at: str):
    date_part = recorded_at[:10].replace("-", "")
    return project_path("data/research_git/research_git_{0}.jsonl".format(date_part))


def _draft_id(
    entity_type: str,
    entity_id: str,
    title: str,
    thesis: str,
    evidence_refs: List[str],
    review_questions: List[str],
) -> str:
    raw = json.dumps({
        "entity_type": entity_type,
        "entity_id": entity_id,
        "title": title,
        "thesis": thesis,
        "evidence_refs": evidence_refs,
        "review_questions": review_questions,
    }, ensure_ascii=False, sort_keys=True)
    return "draft_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _commit_hash(draft_id: str, message: str, thesis: str) -> str:
    return hashlib.sha1("{0}|{1}|{2}".format(draft_id, message, thesis).encode("utf-8")).hexdigest()[:8]


def _history_item(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "event_type": record.get("event_type", ""),
        "status": record.get("status", ""),
        "actor": record.get("actor", ""),
        "recorded_at": record.get("recorded_at", ""),
        "record_id": record.get("record_id", ""),
    }


def _render_summary_markdown(state: Dict[str, Any]) -> str:
    lines = ["# Research-as-Git Summary", ""]
    lines.append("- generated_at: {0}".format(state.get("generated_at", "")))
    lines.append("- draft_count: {0}".format(state.get("draft_count", 0)))
    lines.append("- safety_boundary: {0}".format(state.get("safety_boundary", SAFE_RESEARCH_NOTE)))
    lines.append("")
    lines.append("## Status Counts")
    counts = state.get("status_counts", {}) or {}
    if counts:
        for status, count in sorted(counts.items()):
            lines.append("- {0}: {1}".format(status, count))
    else:
        lines.append("- no research drafts")
    lines.append("")
    lines.append("## Pending Review")
    pending = state.get("pending_review", []) or []
    if pending:
        for item in pending[:20]:
            lines.append("- [{0}] {1} | {2}".format(
                item.get("status", ""),
                item.get("draft_id", ""),
                item.get("title", ""),
            ))
    else:
        lines.append("- no pending research drafts")
    return "\n".join(lines)


def _require_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("{0} is required".format(name))
    return text


def _string_list(values: Iterable[Any]) -> List[str]:
    result: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if text:
            result.append(text)
    return result


def _reject_forbidden_text(values: Iterable[str]) -> None:
    combined = "\n".join(str(value or "") for value in values).lower()
    execution_terms = {
        "auto_trade",
        "auto trading",
        "automatic order",
        "execute_order",
        "place_order",
        "order placement",
        "submit_order",
        "\u81ea\u52a8\u4ea4\u6613",
        "\u81ea\u52a8\u4e0b\u5355",
        "\u4e0b\u5355",
    }
    for term in execution_terms:
        if term.lower() in combined:
            raise ValueError("Unsafe research draft contains forbidden action term: {0}".format(term))


def _compact_time(value: str) -> str:
    return str(value or now_iso()).replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
