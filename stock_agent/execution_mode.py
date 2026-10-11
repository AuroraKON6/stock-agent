"""GLM-04 remainder: single research engine per event stream.

The batch pipeline (agent-run) and the task engine (event intake + task
worker) must never analyze the same signal stream in parallel: that would
double-call models on one event. A single config field
``task_runtime.event_intake_mode`` picks the authoritative engine
("batch" default, "task" once the scheduled path is switched). The gates
fail closed and state the setting to change; user-initiated question tasks
are deliberately outside this gate (they are not the event stream).
"""
from __future__ import annotations

from typing import Any, Dict

EVENT_INTAKE_BATCH = "batch"
EVENT_INTAKE_TASK = "task"
_ALLOWED_MODES = {EVENT_INTAKE_BATCH, EVENT_INTAKE_TASK}


def resolve_event_intake_mode(config: Dict[str, Any]) -> str:
    raw = str(((config or {}).get("task_runtime") or {}).get("event_intake_mode")
              or EVENT_INTAKE_BATCH).strip().lower()
    if raw not in _ALLOWED_MODES:
        raise ValueError(
            "task_runtime.event_intake_mode must be one of {0}, got {1!r}".format(
                sorted(_ALLOWED_MODES), raw))
    return raw


def assert_agent_run_allowed(config: Dict[str, Any]) -> None:
    """Refuse the batch research run when the task engine owns the event stream."""
    mode = resolve_event_intake_mode(config)
    if mode == EVENT_INTAKE_TASK:
        raise PermissionError(
            "agent-run is not the authoritative event engine: "
            "task_runtime.event_intake_mode=task. Use task-rescan + task-run-next; "
            "switch the setting back to batch to resume scheduled batch research.")


def assert_event_rescan_allowed(config: Dict[str, Any]) -> None:
    """Refuse event-task creation while the batch engine owns the event stream."""
    mode = resolve_event_intake_mode(config)
    if mode == EVENT_INTAKE_BATCH:
        raise ValueError(
            "event rescan would double-analyze signals already researched by the "
            "batch engine (task_runtime.event_intake_mode=batch). Switch the "
            "setting to task before enabling scheduled event intake; question "
            "tasks remain available.")
