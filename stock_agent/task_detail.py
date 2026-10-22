"""GLM-06 minimal case detail: one read-only projection per research task.

Answers, for one task: identity and origin, current status and row version,
the research question, what evidence actually reached the model (delivery
record from GLM-05), the research rounds (whitelisted intents, executed
results, actual model per round), the latest AI output, the questions
waiting on the human, and current errors. Read-only by construction: it
touches no store mutation API. Scope labels state plainly that this is the
personal research namespace — never an institutional approval surface.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .models import now_iso

DETAIL_SCHEMA_VERSION = "1.0"
SCOPE_NOTE = "personal_research_only_not_institutional"


def task_detail(store: Any, task_id: str, *, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    task = store.get_task(task_id)  # raises TaskRuntimeError for unknown ids
    input_payload = task.get("input_payload") or {}
    origin = input_payload.get("origin") if isinstance(input_payload.get("origin"), dict) else {}
    model_call = task.get("model_call") if isinstance(task.get("model_call"), dict) else {}
    output = task.get("output") if isinstance(task.get("output"), dict) else {}
    human_request = task.get("human_request") if isinstance(task.get("human_request"), dict) else {}

    questions = []
    if human_request.get("question"):
        questions.append({
            "question": str(human_request.get("question") or ""),
            "why_needed": str(human_request.get("why_needed") or ""),
            "options": [str(option) for option in (human_request.get("options") or [])][:5],
        })
    for item in human_request.get("questions") or []:
        if isinstance(item, dict) and str(item.get("question") or "").strip():
            entry = {
                "question": str(item.get("question") or ""),
                "why_needed": str(item.get("why_needed") or ""),
                "options": [str(option) for option in (item.get("options") or [])][:5],
            }
            if entry not in questions:
                questions.append(entry)

    acknowledgement = output.get("personal_acknowledgement") if isinstance(output.get("personal_acknowledgement"), dict) else None

    # Round records are authoritative in the outbox stream (persisted per
    # round, even mid-run); the row's model_call.rounds is a terminal snapshot
    # used only as a fallback.
    rounds: list = []
    seen_rounds = set()
    for event in store.events(task_id, limit=500):
        if event.get("event_type") != "research_round":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        record = dict(payload)
        record["recorded_at"] = str(event.get("created_at") or "")
        rounds.append(record)
        try:
            seen_rounds.add(int(record.get("round") or 0))
        except (TypeError, ValueError):
            pass
    if not rounds:
        rounds = [dict(item) for item in (model_call.get("rounds") or []) if isinstance(item, dict)]
    else:
        for item in (model_call.get("rounds") or []):
            if isinstance(item, dict):
                try:
                    number = int(item.get("round") or 0)
                except (TypeError, ValueError):
                    continue
                if number and number not in seen_rounds:
                    rounds.append(dict(item))

    return {
        "schema_version": DETAIL_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "scope_note": SCOPE_NOTE,
        "task_id": task["task_id"],
        "case_id": str(task.get("case_id") or ""),
        "title": str(task.get("title") or ""),
        "status": str(task.get("status") or ""),
        "execution_mode": str(task.get("execution_mode") or "live"),
        "is_replay": str(task.get("execution_mode") or "live") == "replay",
        "input_version": int(task.get("input_version") or 1),
        "row_version": int(task.get("version") or 1),
        "attempt_count": int(task.get("attempt_count") or 0),
        "max_attempts": int(task.get("max_attempts") or 0),
        "cancel_requested": bool(task.get("cancel_requested")),
        "created_at": str(task.get("created_at") or ""),
        "updated_at": str(task.get("updated_at") or ""),
        "origin": origin,
        "research_question": str((input_payload.get("research_question") or ""))[:500],
        "symbols": [str(symbol) for symbol in (input_payload.get("symbols") or [])],
        "bound_signal_ids": [str(item) for item in (input_payload.get("bound_signal_ids") or [])],
        "evidence_delivery": model_call.get("context_delivery") if isinstance(model_call.get("context_delivery"), dict) else {},
        "outbound_context": model_call.get("outbound_context") if isinstance(model_call.get("outbound_context"), dict) else {},
        "research_rounds": rounds,
        "model_call": {
            "mode": str(model_call.get("mode") or ""),
            "provider": str(model_call.get("provider") or ""),
            "request_model": str(model_call.get("request_model") or ""),
            "actual_model": str(model_call.get("model") or ""),
            "called": bool(model_call.get("called")),
            "usage": dict(model_call.get("usage") or {}),
            "duration_seconds": model_call.get("duration_seconds"),
            "prompt_version": str(model_call.get("prompt_version") or ""),
            "error": str(model_call.get("error") or ""),
            "governance": model_call.get("governance") if isinstance(model_call.get("governance"), dict) else {},
            # R06: the quality verdict that gate-checked this run, plus the
            # preserved draft when completion was refused for uncited specific
            # claims (the task sits blocked with the gap draft readable here).
            "quality": model_call.get("quality") if isinstance(model_call.get("quality"), dict) else {},
            "evidence_gap_draft": model_call.get("evidence_gap_draft") if isinstance(model_call.get("evidence_gap_draft"), dict) else None,
        },
        "output": output,
        "output_quality": output.get("quality") if isinstance(output.get("quality"), dict) else {},
        "personal_acknowledgement": acknowledgement,
        "pending_human_questions": questions,
        "last_error": str(task.get("last_error") or ""),
    }
