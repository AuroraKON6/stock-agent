"""R02: outbound invocation governance — classification, heartbeat, deadlines.

Platform-agnostic companion to the TaskStore attempt chain
(``task_invocation_attempts``). Three responsibilities:

1. **Failure classification** — decide, from the runtime error, whether an
   external request provably never left this machine (safe to retry), reached
   the provider and received a definitive answer (known failure), or may have
   been sent with no usable answer (outcome unknown — never auto-retried).
   ``pi_runtime`` tags its errors with ``failure_stage``/``sent_hint`` where it
   knows; message markers are the fallback for untagged errors.

2. **In-call lease heartbeat (F04)** — a renewal failure during a model call
   means the worker may already have lost the task; the in-flight call's
   result is still recorded (it may be charged), but NO new outbound request
   may start afterwards. The heartbeat uses its OWN store connection so it
   never interleaves with the worker thread's transactions.

3. **Deadline / lease relationship** — the heartbeat interval is derived from
   the lease TTL (at least two beats per TTL window), so a call longer than
   the TTL keeps its lease alive instead of silently expiring under it.

The container-side verification of these semantics (SIGTERM drain, kill -9)
belongs to R09; nothing here touches Docker or process supervision.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


class ModelCallNotSent(RuntimeError):
    """The request provably never left this machine — a bounded retry is safe."""

    def __init__(self, original: BaseException, detail: str = ""):
        super().__init__(str(original))
        self.original = original
        self.detail = detail


class ModelOutcomeUnknown(RuntimeError):
    """The request may have reached the provider with no usable answer.

    Budget stays held; the attempt enters reconciliation; auto-retry is
    forbidden (A07/A08).
    """

    def __init__(self, original: BaseException, detail: str = ""):
        super().__init__(str(original))
        self.original = original
        self.detail = detail


class ModelKnownFailure(RuntimeError):
    """A definitive failure AFTER the request reached the provider (auth,
    quota, provider-side rejection, unusable response). The charge outcome is
    the provider's own statement, not a guess — retry only as a deliberate new
    attempt decided above this layer."""

    def __init__(self, original: BaseException, detail: str = ""):
        super().__init__(str(original))
        self.original = original
        self.detail = detail


# Strong pre-send markers: the network stack itself reports the request could
# not be constructed/routed. Anything not listed is NOT assumed pre-send.
_NOT_SENT_MARKERS = (
    "connection refused",
    "connection reset before request",  # conservative alias, kept explicit
    "getaddrinfo",
    "name or service not known",
    "failed to resolve",
    "temporary failure in name resolution",
    "no route to host",
    "network is unreachable",
    "network unreachable",
    "econnrefused",
    "enotconn",
    "pi agent runtime is disabled",
    "not set, skipped pi agent call",
    "pi isolated settings missing",
    "pi binary not found",
    "pi runtime not installed",
    "pi version mismatch",
    "reviewed-method supply chain blocked",
)

# Definitive provider answers: an HTTP status or a provider-side stop reason
# came back, so the request WAS sent and the provider spoke.
_SENT_DEFINITIVE_MARKERS = (
    "http error 4", "http error 5", "status 4", "status 5",
    "statuscode=4", "statuscode=5", "401", "403", "429",
    "unauthorized", "insufficient", "quota",
    "unusable stop reason", "no assistant text",
    "emitted a tool call", "json stream", "audit append failed",
)


def classify_exception(exc: BaseException) -> Tuple[str, str]:
    """Return ``(sent, stage)`` where sent is 'no' | 'yes' | 'unknown'.

    Structural hints (``failure_stage``/``sent_hint`` set by pi_runtime) take
    precedence; untagged errors fall back to message markers and default to
    ``unknown`` — the conservative answer that forbids auto-retry.
    """
    stage = str(getattr(exc, "failure_stage", "") or "")
    hint = str(getattr(exc, "sent_hint", "") or "")
    text = "{0}: {1}".format(type(exc).__name__, str(exc)).lower()
    if hint in ("no", "yes", "unknown"):
        return hint, stage or "unknown"
    for marker in _NOT_SENT_MARKERS:
        if marker in text:
            return "no", stage or "preflight"
    for marker in _SENT_DEFINITIVE_MARKERS:
        if marker in text:
            return "yes", stage or "process"
    return "unknown", stage or "unknown"


def classify_to_exception(exc: BaseException) -> RuntimeError:
    """Map a raw model-call exception to the worker-level typed failure."""
    sent, stage = classify_exception(exc)
    detail = "sent={0} stage={1}".format(sent, stage)
    if sent == "no":
        return ModelCallNotSent(exc, detail)
    if sent == "yes":
        return ModelKnownFailure(exc, detail)
    return ModelOutcomeUnknown(exc, detail)


# ---------------------------------------------------------------------------
# lease heartbeat
# ---------------------------------------------------------------------------


def heartbeat_interval_seconds(ttl_seconds: int) -> int:
    """At least two beats per TTL window: interval = ttl/3, floor 5s.

    A beat failing does not kill the in-flight call (its outcome still needs
    recording); it only revokes this worker's right to START new outbound
    requests.
    """
    ttl = max(5, int(ttl_seconds))
    return max(5, ttl // 3)


class HeartbeatRunner:
    """Background lease renewal while a synchronous model call blocks."""

    def __init__(self, *, renew_fn: Callable[[], bool], interval_seconds: int,
                 sleeper: Callable[[float], None] = time.sleep):
        self._renew_fn = renew_fn
        self._interval = max(0.05, float(interval_seconds))
        self._sleeper = sleeper
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.beats = 0
        self.healthy = True
        self.last_error = ""

    def start(self) -> "HeartbeatRunner":
        if self._thread is not None:
            raise RuntimeError("heartbeat already started")
        self._thread = threading.Thread(target=self._run, name="lease-heartbeat", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                ok = bool(self._renew_fn())
            except BaseException as exc:  # renewal failure is data, not a crash
                ok = False
                self.last_error = "{0}: {1}".format(type(exc).__name__, str(exc)[:300])
            if not ok:
                self.healthy = False
                if not self.last_error:
                    self.last_error = "renewal callback reported failure"
                return
            self.beats += 1

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self._interval * 2 + 1))

    def __enter__(self) -> "HeartbeatRunner":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.stop()
        return False


@contextmanager
def lease_heartbeat(db_path, task_id: str, *, worker_id: str, fencing_token: int,
                    ttl_seconds: int, interval_seconds: Optional[int] = None):
    """Heartbeat with its OWN store connection for one long-running call.

    Opening a second SQLite connection is deliberate: the worker thread holds
    the primary connection and may be mid-transaction when a beat fires; a
    shared connection would interleave BEGIN/COMMIT across threads.
    """
    from .task_runtime.store import TaskStore

    interval = interval_seconds if interval_seconds is not None \
        else heartbeat_interval_seconds(ttl_seconds)
    renewal_store = TaskStore(db_path)

    def _renew() -> bool:
        renewal_store.renew_lease(task_id, worker_id=worker_id,
                                  fencing_token=fencing_token, ttl_seconds=ttl_seconds)
        return True

    runner = HeartbeatRunner(renew_fn=_renew, interval_seconds=interval)
    runner.start()
    try:
        yield runner
    finally:
        runner.stop()
        renewal_store.close()


def call_deadline_seconds(*, provider_timeout: int, case_time_limit: int,
                          elapsed_seconds: float) -> int:
    """Bound one call's wall clock by the CASE deadline, not just the provider
    timeout — a round may never push the task past its total budget."""
    remaining = int(case_time_limit - float(elapsed_seconds))
    if remaining <= 0:
        return 0
    return max(1, min(int(provider_timeout), remaining))


def summarize(runner: Optional[HeartbeatRunner]) -> Dict[str, Any]:
    """Audit projection of one heartbeat run (counts only, no secrets)."""
    if runner is None:
        return {"beats": 0, "healthy": True, "interval_seconds": None}
    return {
        "beats": runner.beats,
        "healthy": runner.healthy,
        "interval_seconds": runner._interval,
        "last_error": runner.last_error[:300],
    }
