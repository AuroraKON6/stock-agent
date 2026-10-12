"""GLM-10 todo view: what needs a real human, honestly aggregated.

Read-only aggregation of everything waiting on a person: research tasks
asking a question (oldest first, with age), governance-blocked tasks
(with the denial reason and the handling entry point), and the review
queue's pending count from its own projection. Actions stay in the
existing authenticated paths (CLI / human-response); this view never
batch-approves anything.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

TODO_VIEW_SCHEMA_VERSION = "1.0"
TODO_VIEW_SCOPE_NOTE = "personal_research_workbench_not_institutional_approval"
BLOCKED_NOTE = "处置入口：CLI human-response / task-cancel（页面写路径待 ADR-002 验收）"


def _age_minutes(updated_at: str, now: Optional[datetime] = None) -> int:
    try:
        stamp = datetime.fromisoformat(str(updated_at or ""))
    except ValueError:
        return 0
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return max(0, int(((now or datetime.now().astimezone()) - stamp).total_seconds() // 60))


def build_todo_view(config: Dict[str, Any], *, store: Optional[Any] = None,
                    now: Optional[datetime] = None) -> Dict[str, Any]:
    from .models import now_iso

    awaiting: list = []
    blocked: list = []
    if store is not None:
        for task in store.list_tasks(status="awaiting_human"):
            request = task.get("human_request") if isinstance(task.get("human_request"), dict) else {}
            awaiting.append({
                "task_id": str(task.get("task_id") or ""),
                "title": str(task.get("title") or ""),
                "question": str(request.get("question") or "")[:300],
                "why_needed": str(request.get("why_needed") or "")[:300],
                "options": [str(option) for option in (request.get("options") or [])][:5],
                "updated_at": str(task.get("updated_at") or ""),
                "age_minutes": _age_minutes(str(task.get("updated_at") or ""), now),
            })
        awaiting.sort(key=lambda item: item["updated_at"])  # oldest first

        for task in store.list_tasks(status="blocked"):
            blocked.append({
                "task_id": str(task.get("task_id") or ""),
                "title": str(task.get("title") or ""),
                "error": str(task.get("last_error") or "")[:200],
                "updated_at": str(task.get("updated_at") or ""),
                "note": BLOCKED_NOTE,
            })
        blocked.sort(key=lambda item: item["updated_at"])

    # Review queue: read its own latest projection; absence is honest.
    from .review_queue import load_latest_review_queue

    snapshot = load_latest_review_queue(config)
    if snapshot:
        items = snapshot.get("items") or []
        pending = [item for item in items
                   if isinstance(item, dict) and str(item.get("status") or "pending") == "pending"]
        review_queue = {
            "available": True,
            "pending_count": len(pending),
            "generated_at": str(snapshot.get("generated_at") or ""),
        }
    else:
        review_queue = {"available": False, "pending_count": 0, "generated_at": ""}

    return {
        "schema_version": TODO_VIEW_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "scope_note": TODO_VIEW_SCOPE_NOTE,
        "counts": {"awaiting_human": len(awaiting), "blocked": len(blocked)},
        "awaiting_human": awaiting,
        "blocked": blocked,
        "review_queue": review_queue,
    }
