"""R09: worker service — long-running loop, graceful drain, slot identity.

The CLI's one-shot ``task-run-next`` stays for manual use; this module is the
SERVICE form for containers: a loop that claims one queued task at a time and
exits cleanly on stop.

Discipline (GLM_REFACTOR_TASKBOOK R09):

- **Stop = stop claiming, finish in-flight, exit.** ``stop`` sets an event;
  the loop stops claiming BEFORE the next claim, lets the current task run to
  a persisted state (the R02 attempt chain + heartbeat already bound its
  external effects), and exits 0. There is no mid-call kill here — a forced
  kill is A43's crash case, handled by lease recovery, never by this loop.
- **No implicit scheduling.** Constructing/running the service NEVER installs
  timers or schedule intents; scheduled operation stays an explicit,
  separately-enabled path (platform startup parameters must not implicitly
  enable the scheduler).
- **Same-slot recovery.** Scheduled intents derive their identity from
  (schedule id, planned slot, business day, scope version) via
  ``scheduled_slot_key`` — a restarted run in the same slot reuses the SAME
  key instead of creating a duplicate research run. Business days are the
  R07 explicit Asia/Shanghai date.
"""
from __future__ import annotations

import hashlib
import json
import signal
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .runtime_layout import business_date, business_date_key
from .task_runtime import TaskStore
from .task_runtime.store import STATUS_QUEUED

WORKER_SERVICE_VERSION = "1.0"
DEFAULT_DRAIN_SECONDS = 30


class WorkerServiceError(RuntimeError):
    pass


def scheduled_slot_key(schedule_id: str, planned_slot: Any, *,
                       scope_version: str = "") -> str:
    """Deterministic identity for ONE scheduled research slot.

    The day bucket is the explicit Shanghai business date of the planned
    moment (R07 clock): a slot planned 2026-10-21T01:00+08 (i.e. 17:00Z on
    the 20th) belongs to the 21st — restarting that slot after a crash or a
    market-closed delay reuses the SAME key, so the store's idempotency
    deduplicates it instead of re-running the research. The slot instant is
    hashed in canonical UTC form, so the same instant spelled in any timezone
    produces the same identity."""
    from datetime import datetime, timezone

    schedule_id = str(schedule_id or "").strip()
    if not schedule_id:
        raise WorkerServiceError("scheduled_slot_key requires schedule_id")
    if isinstance(planned_slot, str):
        try:
            moment = datetime.fromisoformat(planned_slot)
        except ValueError as exc:
            raise WorkerServiceError(
                "planned_slot must be an ISO timestamp or datetime") from exc
    elif isinstance(planned_slot, datetime):
        moment = planned_slot
    else:
        raise WorkerServiceError(
            "planned_slot must be an ISO timestamp or datetime")
    if moment.tzinfo is None:
        raise WorkerServiceError(
            "planned_slot has no timezone; naive timestamps are rejected")
    canonical_utc = moment.astimezone(timezone.utc).isoformat(timespec="seconds")
    day = business_date_key(moment)
    material = json.dumps({
        "schedule_id": schedule_id,
        "business_day": day,
        "planned_slot_utc": canonical_utc,
        "scope_version": str(scope_version or ""),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "schedule::{0}::{1}::{2}".format(
        schedule_id, day, hashlib.sha256(material.encode("utf-8")).hexdigest()[:16])


class WorkerService:
    """One-task-at-a-time loop over the shared TaskStore.

    ``store_factory`` opens a FRESH store connection per claim (the R03
    per-request discipline); nothing here shares a connection across claims.
    """

    def __init__(self, *, store_factory: Callable[[], TaskStore],
                 run_task: Callable[[TaskStore, Dict[str, Any]], Dict[str, Any]],
                 worker_id: str,
                 ttl_seconds: int = 120,
                 idle_sleep_seconds: float = 2.0,
                 max_tasks: int = 0,
                 now: Optional[Callable[[], float]] = None):
        if not str(worker_id or "").strip():
            raise WorkerServiceError("worker_id must not be empty")
        self._store_factory = store_factory
        self._run_task = run_task
        self.worker_id = worker_id
        self.ttl_seconds = max(5, int(ttl_seconds))
        self.idle_sleep_seconds = max(0.05, float(idle_sleep_seconds))
        self.max_tasks = max(0, int(max_tasks))
        self._now = now or time.monotonic

    # ------------------------------------------------------------------ #

    def run_once(self) -> Optional[Dict[str, Any]]:
        """Claim and run exactly one queued task; None when the queue is
        empty. The claim and the run share one fresh store connection."""
        store = self._store_factory()
        try:
            task = store.claim_next(self.worker_id, ttl_seconds=self.ttl_seconds)
            if task is None:
                return None
            return self._run_task(store, task)
        finally:
            store.close()

    def run(self, stop: threading.Event, *, drain_deadline_seconds: float = 0.0) -> Dict[str, Any]:
        """Loop until ``stop`` is set; never claim after stop.

        In-flight semantics: the current task always runs to a persisted
        state. ``drain_deadline_seconds`` bounds how long the loop may WAIT
        IDLE before exiting after stop (0 = exit immediately once idle)."""
        processed = 0
        results: Dict[str, str] = {}
        deadline = self._now() + max(0.0, float(drain_deadline_seconds))
        while not stop.is_set():
            if self.max_tasks and processed >= self.max_tasks:
                break
            outcome = self.run_once()
            if outcome is None:
                # Queue empty: if we were asked to stop (or the drain window
                # closed), exit; otherwise idle-wait.
                if stop.is_set() or (drain_deadline_seconds > 0
                                     and self._now() >= deadline):
                    break
                time.sleep(self.idle_sleep_seconds)
                continue
            processed += 1
            results[str(outcome.get("status") or "unknown")] = \
                results.get(str(outcome.get("status") or "unknown"), 0) + 1
        return {"processed": processed,
                "by_status": results,
                "worker_id": self.worker_id,
                "stopped": stop.is_set(),
                "remaining_queued_hint": "not_checked"}

    # ------------------------------------------------------------------ #

    def install_signal_handlers(self, stop: threading.Event) -> List[int]:
        """SIGTERM/SIGINT -> set ``stop`` (graceful drain starts). Returns the
        signal numbers registered. On platforms without a signal, returns [].

        The handler NEVER kills the in-flight call: mid-call termination is
        the crash-recovery path (lease expiry + R02 fencing), not the drain
        path — killing here would convert a clean stop into outcome-unknown
        external calls."""
        registered: List[int] = []
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                previous = signal.getsignal(signum)

                def _handler(signum, frame, _stop=stop):  # noqa: ANN001
                    _stop.set()

                signal.signal(signum, _handler)
                registered.append(signum)
            except (ValueError, OSError, AttributeError):
                continue
        return registered


def queue_depth(db_path) -> int:
    """Read-only queued-task count for readiness probes (no claim, no write)."""
    store = TaskStore(db_path, read_only=True)
    try:
        return len(store.list_tasks(status=STATUS_QUEUED))
    finally:
        store.close()
