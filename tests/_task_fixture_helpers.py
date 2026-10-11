"""Shared task-fixture helpers for cycle/worker tests."""
from __future__ import annotations


def final_replay_response(task_id: str = "placeholder") -> dict:
    """A recorded FINAL replay answer: valid, no human questions, no intents."""
    return {
        "schema_version": "1.0",
        "task_id": task_id,
        "summary": "最终回放结论：基于所附公开上下文，未调用外部模型。",
        "key_findings": ["replay finding"],
        "unknowns": ["replay 未知项"],
        "evidence_refs": [],
        "human_questions": [],
        "model_self_assessed_confidence": 0.5,
    }
