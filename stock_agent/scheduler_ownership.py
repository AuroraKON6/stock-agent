"""R25: unique-scheduler ownership registry and cutover gate.

The taskbook's cutover (GLM_REFACTOR_TASKBOOK R25) switches the ONE writer
that may schedule research runs from the legacy Windows PowerShell entry
(``scripts/run_daily_research.ps1`` driven by ``StockAgent_*`` scheduled
tasks) to the container worker. Both sides must be able to see who owns
scheduling, so the registry is plain JSON on disk inside the state tree
(the same tree backup/restore already moves):

- ``<state_dir>/state.json``  — the current record (one active scheduler,
  a monotonic generation, the intake-freeze flag).
- ``<state_dir>/events.jsonl`` — append-only audit of every transition.
- ``<state_dir>/legacy_disabled.json`` — evidence that the legacy entry was
  explicitly stopped before ownership moved forward (and removed again when
  rollback re-enables it).

Discipline:

- **One active scheduler.** ``assert_unique_scheduler`` refuses to run for a
  scheduler that is not the registered owner, and refuses when a foreign
  scheduler's heartbeat is still fresh (双调度 abort).
- **Freeze before rollback.** ``freeze_intake`` blocks NEW scheduled intents
  while already-queued work still drains; ``record_switch`` back to the
  legacy entry requires the freeze to be in place.
- **Legacy is only ever explicitly disabled/restored.** The forward switch
  requires the disable marker; nothing here unregisters or overwrites the
  legacy scheduled tasks — that is ``scripts/set_scheduled_tasks_enabled.ps1``
  run by a human in the real window.
- The registry is per state tree: after restore, the NEW tree's copy is
  authoritative and the legacy tree stays untouched (the rollback reference
  — R24's "never overwrite the source" rule).

``evaluate_cutover_gate`` implements the taskbook's stop conditions as one
pre-switch verdict: unexplained unknown attempts, dual schedulers, a schema
the rollback runtime cannot read, or insufficient capacity each abort the
cutover. The schema check is a pinned-pair policy: SQLite itself happily
opens a newer-shaped file with older code, so the gate compares the runtime
schema version against the plan's ``rollback_supported_schema`` ceiling and
must be re-evaluated whenever either side changes.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .audit_log import atomic_write_text
from .config import project_path

SCHEDULER_OWNERSHIP_VERSION = "1.0"
LEGACY_SCHEDULER_ID = "legacy-powershell"
CONTAINER_SCHEDULER_ID = "container-worker"
LEGACY_TASK_NAMES = (
    "StockAgent_DailyResearch",
    "StockAgent_DailyDashboard",
    "StockAgent_PilotReconcile",
    "StockAgent_WeeklyReview",
)
LEGACY_DISABLE_SCRIPT = "scripts/set_scheduled_tasks_enabled.ps1"
DEFAULT_HEARTBEAT_STALE_SECONDS = 900
DEFAULT_MIN_FREE_BYTES = 32 * 1024 * 1024

STATE_FILENAME = "state.json"
EVENTS_FILENAME = "events.jsonl"
LEGACY_DISABLED_FILENAME = "legacy_disabled.json"
LOCK_FILENAME = "registry.lock"


class SchedulerOwnershipError(RuntimeError):
    """Base error for registry use and transitions."""


class NotActiveSchedulerError(SchedulerOwnershipError):
    """The caller is not the registered unique scheduler."""


class DualSchedulerError(SchedulerOwnershipError):
    """A second scheduler is active or its heartbeat is still fresh."""


class IntakeFrozenError(SchedulerOwnershipError):
    """New scheduled intake is frozen (rollback window)."""


class OwnershipConflictError(SchedulerOwnershipError):
    """A transition was attempted against stale/missing preconditions."""


def scheduler_ownership_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Normalized settings; disabled until the config opts in."""
    value = config or {}
    raw = value.get("scheduler_ownership", {}) if isinstance(value, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "state_dir": str(raw.get("state_dir") or "data/scheduler_ownership"),
        "heartbeat_stale_seconds": max(5, int(raw.get("heartbeat_stale_seconds")
                                          or DEFAULT_HEARTBEAT_STALE_SECONDS)),
        "min_free_bytes": max(0, int(raw.get("min_free_bytes") or DEFAULT_MIN_FREE_BYTES)),
        "rollback_supported_schema": str(raw.get("rollback_supported_schema") or "").strip(),
    }


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def default_state() -> Dict[str, Any]:
    return {
        "schema_version": SCHEDULER_OWNERSHIP_VERSION,
        "active_scheduler": None,
        "generation": 0,
        "intake_frozen": False,
        "frozen_reason": "",
        "cutover_id": "",
        "actors_seen": {},
        "updated_at": "",
    }


def load_state(root: Path) -> Dict[str, Any]:
    """Read the registry record; a missing file is the empty default."""
    path = Path(root) / STATE_FILENAME
    if not path.is_file():
        return default_state()
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise SchedulerOwnershipError("scheduler ownership state is not valid JSON: {0}".format(path))
    if not isinstance(record, dict) or str(record.get("schema_version") or "") != SCHEDULER_OWNERSHIP_VERSION:
        raise SchedulerOwnershipError(
            "unsupported scheduler ownership state version at {0}".format(path))
    merged = default_state()
    merged.update(record)
    return merged


def _append_event(root: Path, event: Dict[str, Any]) -> None:
    path = Path(root) / EVENTS_FILENAME
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class _RegistryLock:
    """Cross-process exclusive lock via O_EXCL create + bounded retry."""

    def __init__(self, root: Path, timeout: float = 10.0):
        self._path = Path(root) / LOCK_FILENAME
        self._timeout = timeout
        self._held = False

    def __enter__(self) -> "_RegistryLock":
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                handle = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(handle, str(os.getpid()).encode("ascii"))
                os.close(handle)
                self._held = True
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise SchedulerOwnershipError(
                        "scheduler ownership registry is locked by another process: {0}".format(self._path))
                time.sleep(0.05)

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._held:
            try:
                self._path.unlink()
            except FileNotFoundError:
                pass


def _save_state(root: Path, state: Dict[str, Any]) -> None:
    state["updated_at"] = _now_iso()
    atomic_write_text(
        Path(root) / STATE_FILENAME,
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def initialize_registry(root: Path, *, first_scheduler: str, actor: str,
                        reason: str = "") -> Dict[str, Any]:
    """Create the registry with its first active scheduler (generation 1)."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with _RegistryLock(root):
        current = load_state(root)
        if int(current.get("generation") or 0) > 0:
            raise OwnershipConflictError(
                "registry already initialized at generation {0}".format(current.get("generation")))
        state = default_state()
        state.update({
            "active_scheduler": str(first_scheduler),
            "generation": 1,
            "updated_at": _now_iso(),
        })
        _save_state(root, state)
        _append_event(root, {
            "event": "initialize", "generation": 1, "to": str(first_scheduler),
            "actor": str(actor), "reason": str(reason), "at": state["updated_at"],
        })
        return state


def _require_fresh_foreign_heartbeat(
        state: Dict[str, Any], scheduler_id: str,
        stale_seconds: int, *, now: Optional[datetime] = None) -> Optional[str]:
    """Return the id of any OTHER scheduler whose heartbeat is still fresh."""
    now = now or datetime.now().astimezone()
    for other, last_seen in (state.get("actors_seen") or {}).items():
        if str(other) == str(scheduler_id):
            continue
        moment = _parse_iso(str(last_seen))
        if moment is None:
            continue
        age = (now - moment).total_seconds()
        if age <= stale_seconds:
            return str(other)
    return None


def heartbeat(root: Path, scheduler_id: str, *,
              stale_seconds: int = DEFAULT_HEARTBEAT_STALE_SECONDS) -> Dict[str, Any]:
    """Record that ``scheduler_id`` is alive; refuse foreign/dual presence."""
    root = Path(root)
    with _RegistryLock(root):
        state = load_state(root)
        active = state.get("active_scheduler")
        if not active:
            raise NotActiveSchedulerError(
                "no active scheduler is registered; refusing heartbeat from {0}".format(scheduler_id))
        if str(active) != str(scheduler_id):
            raise DualSchedulerError(
                "scheduler {0} is not the active owner ({1}); a second scheduler"
                " must never run".format(scheduler_id, active))
        actors = dict(state.get("actors_seen") or {})
        actors[str(scheduler_id)] = _now_iso()
        state["actors_seen"] = actors
        _save_state(root, state)
        return state


def assert_unique_scheduler(root: Path, scheduler_id: str, *,
                            stale_seconds: int = DEFAULT_HEARTBEAT_STALE_SECONDS,
                            now: Optional[datetime] = None) -> Dict[str, Any]:
    """The unique-scheduler guard for the scheduling entry itself."""
    state = load_state(root)
    active = state.get("active_scheduler")
    if not active:
        raise NotActiveSchedulerError(
            "no active scheduler is registered (state generation {0})".format(state.get("generation")))
    if str(active) != str(scheduler_id):
        raise DualSchedulerError(
            "scheduler {0} is not the active owner ({1})".format(scheduler_id, active))
    foreign = _require_fresh_foreign_heartbeat(state, scheduler_id, stale_seconds, now=now)
    if foreign:
        raise DualSchedulerError(
            "fresh heartbeat from second scheduler {0} (within {1}s); aborting".format(
                foreign, stale_seconds))
    return state


def intake_allowed(root: Path) -> bool:
    state = load_state(root)
    return not bool(state.get("intake_frozen"))


def assert_intake_allowed(root: Path) -> None:
    state = load_state(root)
    if state.get("intake_frozen"):
        raise IntakeFrozenError(
            "new scheduled intake is frozen: {0}".format(state.get("frozen_reason") or "(no reason recorded)"))


def freeze_intake(root: Path, *, actor: str, reason: str, cutover_id: str = "") -> Dict[str, Any]:
    """Rollback step 1: stop NEW intake; queued work keeps draining."""
    root = Path(root)
    with _RegistryLock(root):
        state = load_state(root)
        if state.get("intake_frozen"):
            raise OwnershipConflictError("intake is already frozen")
        state["intake_frozen"] = True
        state["frozen_reason"] = str(reason)
        state["cutover_id"] = str(cutover_id)
        _save_state(root, state)
        _append_event(root, {
            "event": "freeze", "generation": int(state["generation"]),
            "actor": str(actor), "reason": str(reason),
            "cutover_id": str(cutover_id), "at": state["updated_at"],
        })
        return state


def thaw_intake(root: Path, *, actor: str, reason: str = "") -> Dict[str, Any]:
    root = Path(root)
    with _RegistryLock(root):
        state = load_state(root)
        if not state.get("intake_frozen"):
            raise OwnershipConflictError("intake is not frozen")
        state["intake_frozen"] = False
        state["frozen_reason"] = ""
        _save_state(root, state)
        _append_event(root, {
            "event": "thaw", "generation": int(state["generation"]),
            "actor": str(actor), "reason": str(reason), "at": state["updated_at"],
        })
        return state


# --------------------------------------------------------------------------- #
# Legacy entry disable marker — the ONLY representation of "the old PowerShell
# entry is stopped" the forward switch accepts.
# --------------------------------------------------------------------------- #
def legacy_disabled_path(root: Path) -> Path:
    return Path(root) / LEGACY_DISABLED_FILENAME


def disable_legacy(root: Path, *, actor: str, cutover_id: str = "",
                   simulated: bool = False, method: str = "") -> Dict[str, Any]:
    """Record that the legacy entry was explicitly stopped.

    ``simulated=True`` marks a rehearsal record: the real effect comes from
    running ``scripts/set_scheduled_tasks_enabled.ps1 -Disable`` on the
    Windows host in the real window, and the marker names that method."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    marker = {
        "schema_version": SCHEDULER_OWNERSHIP_VERSION,
        "disabled_at": _now_iso(),
        "actor": str(actor),
        "cutover_id": str(cutover_id),
        "task_names": list(LEGACY_TASK_NAMES),
        "method": str(method) or "{0} -Disable".format(LEGACY_DISABLE_SCRIPT),
        "simulated": bool(simulated),
    }
    atomic_write_text(legacy_disabled_path(root),
                      json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return marker


def enable_legacy(root: Path, *, actor: str, reason: str = "") -> Dict[str, Any]:
    """Remove the disable marker (rollback re-enables the legacy entry)."""
    path = legacy_disabled_path(root)
    if not path.is_file():
        raise OwnershipConflictError("legacy entry is not marked disabled")
    marker = json.loads(path.read_text(encoding="utf-8"))
    path.unlink()
    with _RegistryLock(root):
        _append_event(root, {
            "event": "legacy_enabled", "generation": int(load_state(root).get("generation") or 0),
            "actor": str(actor), "reason": str(reason), "at": _now_iso(),
        })
    return marker


# --------------------------------------------------------------------------- #
# Ownership switch (forward and rollback)
# --------------------------------------------------------------------------- #
def record_switch(root: Path, *, to: str, actor: str, reason: str = "",
                  cutover_id: str = "", expected_generation: Optional[int] = None) -> Dict[str, Any]:
    """Move the unique-scheduler ownership to ``to`` (generation +1).

    Direction-derived preconditions, never optional:
    - FORWARD (legacy -> container): the legacy entry must carry the explicit
      disable marker (no dual-scheduler window may open), and the intake must
      NOT be frozen (a forward switch must not race a rollback).
    - ROLLBACK (container -> legacy): the intake must be frozen first; the
      legacy disable marker is cleared by the caller right after via
      ``enable_legacy``."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with _RegistryLock(root):
        state = load_state(root)
        generation = int(state.get("generation") or 0)
        if generation < 1:
            raise OwnershipConflictError("registry not initialized")
        if expected_generation is not None and generation != int(expected_generation):
            raise OwnershipConflictError(
                "generation moved: expected {0}, found {1}".format(expected_generation, generation))
        active = str(state.get("active_scheduler") or "")
        if active == str(to):
            raise OwnershipConflictError(
                "scheduler {0} is already the active owner".format(to))
        forward = str(to) != LEGACY_SCHEDULER_ID
        if forward and not legacy_disabled_path(root).is_file():
            raise OwnershipConflictError(
                "legacy entry is not disabled; refusing to create a dual-scheduler window"
                " (record it with disable_legacy / the PowerShell disable script first)")
        if forward and state.get("intake_frozen"):
            raise OwnershipConflictError(
                "intake is frozen; a forward switch must not race a rollback")
        if not forward and not state.get("intake_frozen"):
            raise OwnershipConflictError(
                "rollback switch requires frozen intake first (freeze_intake)")
        state["active_scheduler"] = str(to)
        state["generation"] = generation + 1
        state["cutover_id"] = str(cutover_id)
        _save_state(root, state)
        _append_event(root, {
            "event": "switch", "generation": generation + 1, "from": active, "to": str(to),
            "actor": str(actor), "reason": str(reason), "cutover_id": str(cutover_id),
            "at": state["updated_at"],
        })
        return state


# --------------------------------------------------------------------------- #
# Cutover gate — the taskbook's stop conditions as one pre-switch verdict.
# --------------------------------------------------------------------------- #
def _unexplained_unknown_attempts(db_path: Path) -> Dict[str, Any]:
    """Attempts whose send outcome cannot be explained at cutover time.

    ``dispatched`` rows have no terminal record (was it sent? unknown);
    ``outcome_unknown`` rows are tracked-but-unresolved reconciliation work.
    Both abort the cutover until reconciled."""
    connection = None
    try:
        connection = sqlite3.connect("file:{0}?mode=ro".format(Path(db_path).resolve()), uri=True)
        dispatched = int(connection.execute(
            "SELECT COUNT(*) FROM task_invocation_attempts WHERE state = 'dispatched'").fetchone()[0])
        outcome_unknown = int(connection.execute(
            "SELECT COUNT(*) FROM task_invocation_attempts WHERE state = 'outcome_unknown'").fetchone()[0])
    except sqlite3.OperationalError as exc:
        return {"ok": False, "error": "cannot read attempt chain: {0}".format(exc),
                "dispatched_without_terminal": None, "outcome_unknown": None}
    finally:
        if connection is not None:
            connection.close()
    return {"ok": dispatched == 0 and outcome_unknown == 0,
            "dispatched_without_terminal": dispatched,
            "outcome_unknown": outcome_unknown}


def _schema_rollback_check(db_path: Path, runtime_schema_version: str,
                           rollback_supported_schema: str) -> Dict[str, Any]:
    """Pinned-pair schema policy (see module docstring)."""
    supported = str(rollback_supported_schema or "").strip()
    if not supported:
        return {"ok": False, "error": "rollback_supported_schema is not configured;"
                " the cutover plan must pin the schema pair"}
    connection = None
    try:
        connection = sqlite3.connect("file:{0}?mode=ro".format(Path(db_path).resolve()), uri=True)
        tables = int(connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN"
            " ('research_tasks','task_outbox','task_invocation_attempts')").fetchone()[0])
    except sqlite3.OperationalError as exc:
        return {"ok": False, "error": "cannot open task store: {0}".format(exc),
                "runtime_schema_version": str(runtime_schema_version),
                "rollback_supported_schema": supported}
    finally:
        if connection is not None:
            connection.close()
    if tables != 3:
        return {"ok": False, "error": "task store is missing expected tables",
                "runtime_schema_version": str(runtime_schema_version),
                "rollback_supported_schema": supported}

    def _key(version: str) -> tuple:
        try:
            return tuple(int(part) for part in str(version).split("."))
        except ValueError:
            return (str(version),)

    rollbackable = _key(runtime_schema_version) <= _key(supported)
    return {"ok": rollbackable,
            "runtime_schema_version": str(runtime_schema_version),
            "rollback_supported_schema": supported}


def _capacity_check(state_root: Path, min_free_bytes: int) -> Dict[str, Any]:
    root = Path(state_root)
    try:
        stats = os.statvfs(root)
        free = int(stats.f_bavail) * int(stats.f_frsize)
    except OSError as exc:
        return {"ok": False, "error": "cannot stat state root {0}: {1}".format(root, exc),
                "free_bytes": None, "min_free_bytes": int(min_free_bytes)}
    return {"ok": free >= int(min_free_bytes), "free_bytes": free,
            "min_free_bytes": int(min_free_bytes)}


def evaluate_cutover_gate(*, tasks_db_path, ownership_root, state_root,
                          runtime_schema_version: str,
                          config: Optional[Dict[str, Any]] = None,
                          now: Optional[datetime] = None) -> Dict[str, Any]:
    """One pre-switch verdict over the four taskbook stop conditions."""
    cfg = scheduler_ownership_config(config)
    state = load_state(ownership_root)
    unknown = _unexplained_unknown_attempts(tasks_db_path)
    active = str(state.get("active_scheduler") or "")
    foreign = _require_fresh_foreign_heartbeat(
        state, active, cfg["heartbeat_stale_seconds"], now=now)
    legacy_marker = legacy_disabled_path(ownership_root).is_file()
    dual = {
        "ok": bool(active) and foreign is None,
        "active_scheduler": active,
        "fresh_foreign_scheduler": foreign,
        "legacy_entry_disabled_marker": legacy_marker,
    }
    if not active:
        dual["error"] = "no active scheduler registered"
    elif foreign:
        dual["error"] = "fresh heartbeat from second scheduler {0}".format(foreign)
    schema = _schema_rollback_check(tasks_db_path, runtime_schema_version,
                                    cfg["rollback_supported_schema"])
    capacity = _capacity_check(state_root, cfg["min_free_bytes"])
    checks = {"unexplained_unknown": unknown, "dual_scheduler": dual,
              "schema_rollback": schema, "capacity": capacity}
    blockers: List[str] = []
    if not unknown.get("ok"):
        blockers.append("unexplained unknown attempts: dispatched_without_terminal={0},"
                        " outcome_unknown={1}".format(unknown.get("dispatched_without_terminal"),
                                                      unknown.get("outcome_unknown")))
    if not dual.get("ok"):
        blockers.append("dual scheduler: {0}".format(dual.get("error")))
    if not schema.get("ok"):
        blockers.append("non-rollbackable schema: {0} (runtime {1} > supported {2})".format(
            schema.get("error") or "runtime schema exceeds the pinned rollback ceiling",
            schema.get("runtime_schema_version"), schema.get("rollback_supported_schema")))
    if not capacity.get("ok"):
        blockers.append("insufficient capacity: free={0} bytes < required={1} bytes".format(
            capacity.get("free_bytes"), capacity.get("min_free_bytes")))
    return {"ok": not blockers, "checks": checks, "blockers": blockers}


def ownership_status(root: Path) -> Dict[str, Any]:
    """Read-only summary for the CLI / observability."""
    state = load_state(root)
    events_path = Path(root) / EVENTS_FILENAME
    events = 0
    if events_path.is_file():
        with open(events_path, encoding="utf-8") as handle:
            events = sum(1 for line in handle if line.strip())
    return {
        "state": state,
        "registry_events": events,
        "legacy_disabled": legacy_disabled_path(root).is_file(),
        "intake_allowed": not bool(state.get("intake_frozen")),
    }


__all__ = [
    "SCHEDULER_OWNERSHIP_VERSION", "LEGACY_SCHEDULER_ID", "CONTAINER_SCHEDULER_ID",
    "LEGACY_TASK_NAMES", "LEGACY_DISABLE_SCRIPT",
    "SchedulerOwnershipError", "NotActiveSchedulerError", "DualSchedulerError",
    "IntakeFrozenError", "OwnershipConflictError",
    "scheduler_ownership_config", "default_state", "load_state",
    "initialize_registry", "heartbeat", "assert_unique_scheduler",
    "intake_allowed", "assert_intake_allowed", "freeze_intake", "thaw_intake",
    "disable_legacy", "enable_legacy", "legacy_disabled_path", "record_switch",
    "evaluate_cutover_gate", "ownership_status",
]
