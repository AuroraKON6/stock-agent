"""R25: unique-scheduler ownership registry and cutover gate.

The taskbook's cutover (GLM_REFACTOR_TASKBOOK R25) switches the ONE writer
that may schedule research runs from the legacy Windows PowerShell entry
(``scripts/run_daily_research.ps1`` driven by ``StockAgent_*`` scheduled
tasks) to the container worker. Both sides must be able to see who owns
scheduling through a single shared registry, which stays outside the restored
business snapshot:

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
- Both scheduler configurations refer to the same registry from the start.
  Business snapshots and restores do not copy the ownership registry.

``evaluate_cutover_gate`` implements the taskbook's stop conditions as one
pre-switch verdict: unexplained unknown attempts, dual schedulers, a schema
the rollback runtime cannot read, or insufficient capacity each abort the
cutover. The schema check is a pinned-pair policy: SQLite itself happily
opens a newer-shaped file with older code, so the gate compares the runtime
schema version against the plan's ``rollback_supported_schema`` ceiling and
must be re-evaluated whenever either side changes.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

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
ROLLBACK_MANIFEST_FILENAME = "rollback_increments.json"
PREVIOUS_ROLLBACK_MANIFEST_FILENAME = "rollback_increments.previous.json"


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


class CutoverBlockedError(SchedulerOwnershipError):
    """A switch was refused by the stop conditions / readiness checks."""

    def __init__(self, message: str, blockers: Optional[List[str]] = None):
        super().__init__(message)
        self.blockers = list(blockers or [])


def scheduler_ownership_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Normalized settings; disabled until the config opts in."""
    value = config or {}
    raw = value.get("scheduler_ownership", {}) if isinstance(value, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "state_dir": str(raw.get("state_dir") or "data/scheduler_ownership"),
        "scheduler_id": str(raw.get("scheduler_id") or "").strip(),
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
        "freeze_id": "",
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
        foreign = _require_fresh_foreign_heartbeat(state, scheduler_id, stale_seconds)
        if foreign:
            raise DualSchedulerError(
                "fresh heartbeat from second scheduler {0} (within {1}s); aborting".format(
                    foreign, stale_seconds))
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


@contextmanager
def scheduled_intake(root: Path, scheduler_id: str, *,
                     stale_seconds: int = DEFAULT_HEARTBEAT_STALE_SECONDS):
    """Serialize scheduled submission against freeze and owner changes."""
    root = Path(root)
    if not (root / STATE_FILENAME).is_file():
        raise NotActiveSchedulerError("no active scheduler is registered")
    with _RegistryLock(root):
        state = assert_unique_scheduler(root, scheduler_id, stale_seconds=stale_seconds)
        assert_intake_allowed(root)
        if scheduler_id == LEGACY_SCHEDULER_ID and legacy_disabled_path(root).is_file():
            raise OwnershipConflictError("legacy entry is disabled for cutover")
        yield
        state["actors_seen"][scheduler_id] = _now_iso()
        _save_state(root, state)


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
        state["freeze_id"] = uuid4().hex
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
        state["freeze_id"] = ""
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
    with _RegistryLock(root):
        atomic_write_text(legacy_disabled_path(root),
                          json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return marker


def enable_legacy(root: Path, *, actor: str, reason: str = "") -> Dict[str, Any]:
    """Remove the disable marker (rollback re-enables the legacy entry)."""
    path = legacy_disabled_path(root)
    with _RegistryLock(root):
        state = load_state(root)
        if not path.is_file():
            raise OwnershipConflictError("legacy entry is not marked disabled")
        if state["active_scheduler"] != LEGACY_SCHEDULER_ID:
            raise OwnershipConflictError("legacy entry can only be restored after ownership returns")
        marker = json.loads(path.read_text(encoding="utf-8"))
        path.unlink()
        _append_event(root, {
            "event": "legacy_enabled", "generation": state["generation"],
            "actor": str(actor), "reason": str(reason), "at": _now_iso(),
        })
    return marker


# --------------------------------------------------------------------------- #
# Ownership switch (forward and rollback)
# --------------------------------------------------------------------------- #
def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """Use SQLite's WAL-aware read first, including for a live task store."""
    resolved = Path(db_path).resolve()
    uri = resolved.as_uri()
    connection = sqlite3.connect(uri + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA schema_version").fetchone()
        return connection
    except sqlite3.Error as exc:
        connection.close()
        code = getattr(exc, "sqlite_errorcode", 0)
        if (code & 0xff) not in (sqlite3.SQLITE_READONLY, sqlite3.SQLITE_CANTOPEN):
            raise

    # A quiescent baseline on a read-only mount may lack the shared memory
    # sidecar SQLite needs. Immutable mode can ignore the WAL entirely, so
    # only use it for snapshots without any WAL frames to recover.
    wal = resolved.with_name(resolved.name + "-wal")
    try:
        wal_size = wal.stat().st_size
    except FileNotFoundError:
        wal_size = 0
    if wal_size:
        raise OwnershipConflictError(
            "cannot read rollback snapshot {0}: nonempty WAL could not be read; "
            "use a fully checkpointed snapshot or a SQLite backup".format(resolved))
    return sqlite3.connect(uri + "?mode=ro&immutable=1", uri=True)


def _rollback_snapshot(db_path: Path) -> Dict[str, Any]:
    with closing(_open_readonly(db_path)) as connection:
        connection.execute("BEGIN")
        tasks = {row[0]: tuple(row[1:]) for row in connection.execute(
            "SELECT task_id, * FROM research_tasks ORDER BY task_id")}
        outbox = {row[0]: tuple(row[1:]) for row in connection.execute(
            "SELECT sequence, * FROM task_outbox ORDER BY sequence")}
        in_flight = dict(connection.execute(
            "SELECT status, COUNT(*) FROM research_tasks"
            " WHERE status IN ('queued', 'leased', 'running') GROUP BY status"))
    return {"tasks": tasks, "outbox": outbox, "in_flight": in_flight}


def _snapshot_digest(snapshot: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True,
                                    ensure_ascii=False).encode("utf-8")).hexdigest()


def _check_rollback_drain(snapshot: Dict[str, Any]) -> None:
    in_flight = snapshot["in_flight"]
    if in_flight:
        raise OwnershipConflictError("retiring side not drained: {0}".format(
            ", ".join("{0}={1}".format(k, v) for k, v in sorted(in_flight.items()))))


def _build_rollback_manifest(state: Dict[str, Any], baseline_db_path: Path,
                             tasks_db_path: Path) -> Dict[str, Any]:
    if Path(baseline_db_path).resolve() == Path(tasks_db_path).resolve():
        raise OwnershipConflictError("increment reconciliation requires an independent baseline")
    baseline = _rollback_snapshot(baseline_db_path)
    current = _rollback_snapshot(tasks_db_path)
    _check_rollback_drain(current)
    manifest = {
        "generation": state["generation"], "freeze_id": state["freeze_id"],
        "cutover_id": state["cutover_id"], "task_store_sha256": _snapshot_digest(current),
        "baseline_sha256": _snapshot_digest(baseline),
        "baseline_tasks": len(baseline["tasks"]),
        "baseline_outbox_events": len(baseline["outbox"]),
    }
    for table, result_key in (("tasks", "increment_tasks"),
                              ("outbox", "increment_outbox_sequences")):
        missing_or_changed = [key for key, row in baseline[table].items()
                              if current[table].get(key) != row]
        if missing_or_changed:
            raise OwnershipConflictError(
                "baseline {0} differs: {1} missing or changed".format(table, len(missing_or_changed)))
        manifest[result_key] = sorted(current[table].keys() - baseline[table].keys())
    return manifest


def record_rollback_reconciliation(registry_root, baseline_db_path, tasks_db_path) -> Dict[str, Any]:
    """Replace rollback authorization with the result of the latest attempt."""
    root = Path(registry_root)
    with _RegistryLock(root):
        state = load_state(root)
        if not state["intake_frozen"] or not state["freeze_id"]:
            raise OwnershipConflictError("increment reconciliation requires frozen intake")
        manifest_path = root / ROLLBACK_MANIFEST_FILENAME
        # A switch takes this same lock. Archive the active receipt atomically
        # before reading either store so any failed refresh leaves no permit.
        try:
            manifest_path.replace(root / PREVIOUS_ROLLBACK_MANIFEST_FILENAME)
        except FileNotFoundError:
            pass
        audit = {key: state[key] for key in ("generation", "freeze_id", "cutover_id")}
        try:
            manifest = _build_rollback_manifest(state, baseline_db_path, tasks_db_path)
        except (OwnershipConflictError, OSError, sqlite3.Error) as exc:
            _append_event(root, dict(audit, event="rollback_reconciliation_failed",
                                     reason=str(exc), at=_now_iso()))
            if isinstance(exc, OwnershipConflictError):
                raise
            raise OwnershipConflictError("cannot read stores for increment reconciliation: {0}".format(exc)) from exc
        _append_event(root, dict(audit, event="rollback_reconciliation_verified", at=_now_iso(),
                                 baseline_sha256=manifest["baseline_sha256"],
                                 task_store_sha256=manifest["task_store_sha256"],
                                 baseline_tasks=manifest["baseline_tasks"],
                                 baseline_outbox_events=manifest["baseline_outbox_events"]))
        atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        return manifest


def evaluate_rollback_readiness(registry_root, tasks_db_path) -> Dict[str, Any]:
    """Require a drained store and reconciliation of this exact freeze and store."""
    root = Path(registry_root)
    state = load_state(root)
    manifest_path = root / ROLLBACK_MANIFEST_FILENAME
    blockers = []
    in_flight = {}
    try:
        snapshot = _rollback_snapshot(tasks_db_path)
        in_flight = snapshot["in_flight"]
        _check_rollback_drain(snapshot)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (not isinstance(manifest, dict) or not state["freeze_id"]
                or manifest.get("freeze_id") != state["freeze_id"]
                or manifest.get("generation") != state["generation"]
                or manifest.get("cutover_id") != state["cutover_id"]
                or manifest.get("task_store_sha256") != _snapshot_digest(snapshot)):
            blockers.append("increment reconciliation no longer matches this freeze and task store")
    except FileNotFoundError:
        blockers.append("increment reconciliation not recorded (rollback_increments.json missing)")
    except (OSError, ValueError, sqlite3.Error, OwnershipConflictError) as exc:
        blockers.append(str(exc))
    return {"ok": not blockers, "blockers": blockers, "in_flight": in_flight,
            "increment_manifest": str(manifest_path),
            "increment_manifest_present": manifest_path.is_file()}


def record_switch(root: Path, *, to: str, actor: str, reason: str = "",
                  cutover_id: str = "", expected_generation: Optional[int] = None,
                  tasks_db_path: Any = None, state_root: Any = None,
                  runtime_schema_version: str = "",
                  config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Move the unique-scheduler ownership to ``to`` (generation +1).

    Direction-derived preconditions, never optional — the taskbook's stop
    conditions CONSTRAIN the switch itself, not just a separate gate run:

    - FORWARD (legacy -> container): the legacy entry must carry the explicit
      disable marker (no dual-scheduler window may open); the intake must NOT
      be frozen (a forward switch must not race a rollback); and the cutover
      gate must pass over the SAME state (``tasks_db_path``/``state_root``/
      ``runtime_schema_version`` are required — unexplained unknowns, a dual
      scheduler, a non-rollbackable schema or insufficient capacity each
      refuse the switch and leave owner/generation/events untouched).
    - ROLLBACK (container -> legacy): the intake must be frozen first, the
      same gate must pass, the retiring side must have drained (no
      queued/leased/running tasks against ``tasks_db_path``), and the increment
      reconciliation must match the current freeze and task store. The legacy disable marker is cleared by the caller
      right after via ``enable_legacy``.

    On success the OUTGOING scheduler's heartbeat is retired from
    ``actors_seen``: the switch is the authoritative statement that it is no
    longer a legitimate presence, so its pre-switch beat must not veto the
    new owner inside the heartbeat-stale window (its future beats are
    refused organically by ``heartbeat``)."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    forward = str(to) != LEGACY_SCHEDULER_ID
    with _RegistryLock(root):
        state = load_state(root)
        generation = int(state.get("generation") or 0)
        if generation < 1:
            raise OwnershipConflictError("registry not initialized")
        if expected_generation is not None and generation != int(expected_generation):
            raise OwnershipConflictError(
                "generation moved: expected {0}, found {1}".format(expected_generation, generation))
        active = str(state.get("active_scheduler") or "")
        if str(to) not in (LEGACY_SCHEDULER_ID, CONTAINER_SCHEDULER_ID):
            raise OwnershipConflictError("unknown scheduler: {0}".format(to))
        if active == str(to):
            raise OwnershipConflictError(
                "scheduler {0} is already the active owner".format(to))
        if forward:
            if not legacy_disabled_path(root).is_file():
                raise OwnershipConflictError(
                    "legacy entry is not disabled; refusing to create a dual-scheduler window"
                    " (record it with disable_legacy / the PowerShell disable script first)")
            if state.get("intake_frozen"):
                raise OwnershipConflictError(
                    "intake is frozen; a forward switch must not race a rollback")
        elif not state.get("intake_frozen"):
            raise OwnershipConflictError(
                "rollback switch requires frozen intake first (freeze_intake)")
        if not tasks_db_path or not state_root or not runtime_schema_version:
            raise OwnershipConflictError(
                "switch requires tasks_db_path, state_root and runtime_schema_version"
                " so the cutover gate can check the actual store")
        gate_verdict = evaluate_cutover_gate(
            tasks_db_path=tasks_db_path, ownership_root=root, state_root=state_root,
            runtime_schema_version=runtime_schema_version, config=config)
        if not gate_verdict["ok"]:
            raise CutoverBlockedError("cutover switch blocked by stop conditions",
                                      gate_verdict["blockers"])
        if not forward:
            rollback_readiness = evaluate_rollback_readiness(root, tasks_db_path)
            if not rollback_readiness["ok"]:
                raise CutoverBlockedError("rollback switch blocked by readiness checks",
                                          rollback_readiness["blockers"])
        state["active_scheduler"] = str(to)
        state["generation"] = generation + 1
        state["cutover_id"] = str(cutover_id)
        actors = {key: value for key, value in (state.get("actors_seen") or {}).items()
                  if key != active}  # retire the outgoing owner's heartbeat
        state["actors_seen"] = actors
        _save_state(root, state)
        _append_event(root, {
            "event": "switch", "generation": generation + 1, "from": active, "to": str(to),
            "actor": str(actor), "reason": str(reason), "cutover_id": str(cutover_id),
            "gate_ok": True,
            "rollback_readiness_ok": None if forward else True,
            "outgoing_heartbeat_retired": active,
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
        connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
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
        connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
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
            return ()

    runtime_key, supported_key = _key(runtime_schema_version), _key(supported)
    rollbackable = bool(runtime_key and supported_key and runtime_key <= supported_key)
    return {"ok": rollbackable,
            "runtime_schema_version": str(runtime_schema_version),
            "rollback_supported_schema": supported}


def _capacity_check(state_root: Path, min_free_bytes: int) -> Dict[str, Any]:
    root = Path(state_root)
    try:
        free = shutil.disk_usage(root).free
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
    "IntakeFrozenError", "OwnershipConflictError", "CutoverBlockedError",
    "scheduler_ownership_config", "default_state", "load_state",
    "initialize_registry", "heartbeat", "assert_unique_scheduler",
    "intake_allowed", "assert_intake_allowed", "freeze_intake", "thaw_intake",
    "disable_legacy", "enable_legacy", "legacy_disabled_path", "record_switch",
    "evaluate_rollback_readiness", "evaluate_cutover_gate", "ownership_status",
]
