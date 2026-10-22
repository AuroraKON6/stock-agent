"""Persistent research task runtime: transactional task store + outbox.

First-slice implementation of plan W03. One SQLite database owns the new
task domain (research tasks, outbox events, idempotency receipts); the
existing JSONL governance ledgers keep their original authority and are
never written here. Writes go through BEGIN IMMEDIATE transactions so a
task state change and its outbox event commit atomically (single-writer
discipline with WAL and a busy timeout). The connection allows cross-thread use because the
task board serves requests from handler threads; every board access is
serialized by the board lock, preserving the single-writer discipline.

Statuses: queued -> leased -> running -> succeeded / failed / blocked /
awaiting_human / cancelled / expired. Recovery re-queues an abandoned
lease only when attempts remain; an old worker holding a stale fencing
token can never finalize a task it no longer owns.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TASK_SCHEMA_VERSION = "1.3"  # R03: +principal_verifications (atomic claim), +workflow_dispatches

STATUS_QUEUED = "queued"
STATUS_LEASED = "leased"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"
STATUS_AWAITING_HUMAN = "awaiting_human"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"

TERMINAL_STATUSES = {STATUS_SUCCEEDED, STATUS_FAILED, STATUS_BLOCKED, STATUS_CANCELLED, STATUS_EXPIRED}

ALLOWED_TRANSITIONS: Dict[str, set] = {
    STATUS_QUEUED: {STATUS_LEASED, STATUS_CANCELLED},
    # A leased task may terminally fail or block: a worker can crash (or hit
    # a governance gate) after claiming but before mark_running, and that
    # outcome must be recordable instead of stuck in leased forever.
    STATUS_LEASED: {STATUS_RUNNING, STATUS_QUEUED, STATUS_EXPIRED, STATUS_CANCELLED,
                    STATUS_FAILED, STATUS_BLOCKED},
    STATUS_RUNNING: {STATUS_SUCCEEDED, STATUS_FAILED, STATUS_BLOCKED, STATUS_AWAITING_HUMAN, STATUS_QUEUED, STATUS_EXPIRED, STATUS_CANCELLED},
    STATUS_FAILED: set(),
    STATUS_BLOCKED: {STATUS_QUEUED},
    STATUS_AWAITING_HUMAN: {STATUS_QUEUED, STATUS_SUCCEEDED, STATUS_BLOCKED, STATUS_CANCELLED},
    STATUS_SUCCEEDED: set(),
    STATUS_CANCELLED: set(),
    STATUS_EXPIRED: set(),
}

EXECUTION_MODES = {"live", "replay"}
HUMAN_DECISIONS = {"acknowledge", "reject", "provide_input"}

# R02 outbound-attempt state machine (blueprint 4.2): a model call is first
# reserved (budget event), then PREPARED (row written), then DISPATCHED (the
# external request is about to leave), and ends in exactly one terminal state.
# outcome_unknown NEVER auto-transitions to a retry: only a real reconciliation
# (provider receipt or human with reason) closes it, and the budget stays held.
ATTEMPT_PREPARED = "prepared"
ATTEMPT_DISPATCHED = "dispatched"
ATTEMPT_SUCCEEDED = "succeeded"
ATTEMPT_FAILED_KNOWN = "failed_known"
ATTEMPT_OUTCOME_UNKNOWN = "outcome_unknown"
ATTEMPT_RECONCILED = "reconciled"
ATTEMPT_TERMINAL_STATES = {ATTEMPT_SUCCEEDED, ATTEMPT_FAILED_KNOWN, ATTEMPT_RECONCILED}
ATTEMPT_SENT_VALUES = {"yes", "no", "unknown"}
RECONCILIATION_RESOLUTIONS = {"confirmed_charged", "confirmed_not_charged"}

# Human input patches may only adjust research content, never identity or
# classification fields; the worker re-runs the egress gate on every attempt.
ALLOWED_INPUT_PATCH_KEYS = {
    "research_question",
    "public_context",
    "extra_note",
    "operator_note",
    "replay_response",
}


class TaskRuntimeError(RuntimeError):
    pass


class TaskTransitionError(TaskRuntimeError):
    pass


class TaskVersionConflictError(TaskRuntimeError):
    pass


class TaskFencingError(TaskRuntimeError):
    pass


class TaskIdempotencyConflictError(TaskRuntimeError):
    pass


class PrincipalVerificationError(TaskRuntimeError):
    """Atomic claim of a one-command principal receipt failed (R03).

    ``reason`` mirrors the gateway's pending-parking reasons so the JSONL
    pre-check and the authoritative store claim can never disagree.
    """

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return "{0}_{1}".format(prefix, uuid.uuid4().hex[:16])


_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_tasks (
    task_id TEXT PRIMARY KEY,
    parent_task_id TEXT NOT NULL DEFAULT '',
    case_id TEXT NOT NULL DEFAULT '',
    role_id TEXT NOT NULL DEFAULT 'research_analyst',
    title TEXT NOT NULL DEFAULT '',
    input_version INTEGER NOT NULL DEFAULT 1,
    input_payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',
    version INTEGER NOT NULL DEFAULT 1,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT NOT NULL,
    business_intent_id TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    model_config_hash TEXT NOT NULL DEFAULT '',
    execution_mode TEXT NOT NULL DEFAULT 'live',
    model_call TEXT NOT NULL DEFAULT '{}',
    output TEXT NOT NULL DEFAULT '{}',
    human_request TEXT NOT NULL DEFAULT '{}',
    last_error TEXT NOT NULL DEFAULT '',
    worker_id TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency ON research_tasks(idempotency_key);
CREATE TABLE IF NOT EXISTS task_outbox (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    command_id TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_task ON task_outbox(task_id, sequence);
CREATE TABLE IF NOT EXISTS idempotency_receipts (
    command_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_platform_runs (
    run_key TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    business_intent_id TEXT NOT NULL DEFAULT '',
    workflow_version TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_platform_runs_task ON task_platform_runs(task_id, recorded_at);
CREATE TABLE IF NOT EXISTS task_input_snapshots (
    task_id TEXT NOT NULL,
    input_version INTEGER NOT NULL,
    payload TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (task_id, input_version)
);
CREATE TABLE IF NOT EXISTS task_invocation_attempts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    worker_id TEXT NOT NULL DEFAULT '',
    fencing_token INTEGER NOT NULL DEFAULT 0,
    round_index INTEGER NOT NULL DEFAULT 1,
    provider TEXT NOT NULL DEFAULT '',
    request_model TEXT NOT NULL DEFAULT '',
    prompt_sha256 TEXT NOT NULL DEFAULT '',
    budget_event_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    sent TEXT NOT NULL DEFAULT 'unknown',
    error TEXT NOT NULL DEFAULT '',
    deadline_seconds INTEGER NOT NULL DEFAULT 0,
    heartbeat_beats INTEGER NOT NULL DEFAULT 0,
    heartbeat_healthy INTEGER NOT NULL DEFAULT 1,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL DEFAULT '',
    reconciled_at TEXT NOT NULL DEFAULT '',
    reconciled_by TEXT NOT NULL DEFAULT '',
    reconciliation TEXT NOT NULL DEFAULT '',
    reconciliation_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_invocation_attempts_task ON task_invocation_attempts(task_id, started_at);
CREATE TABLE IF NOT EXISTS principal_verifications (
    verification_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL,
    command_id TEXT NOT NULL DEFAULT '',
    used_at TEXT NOT NULL DEFAULT '',
    issued_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_dispatches (
    dispatch_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    issued_generation INTEGER NOT NULL,
    input_version INTEGER NOT NULL,
    bundle_id TEXT NOT NULL,
    bundle_hash TEXT NOT NULL DEFAULT '',
    workflow_version TEXT NOT NULL,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    issued_at TEXT NOT NULL,
    consumed_at TEXT NOT NULL DEFAULT '',
    consumed_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_workflow_dispatches_task ON workflow_dispatches(task_id, issued_at);
CREATE TABLE IF NOT EXISTS signal_consumptions (
    event_key TEXT NOT NULL,
    purpose TEXT NOT NULL,
    base_key TEXT NOT NULL DEFAULT '',
    signal_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL,
    consumed_at TEXT NOT NULL,
    PRIMARY KEY (event_key, purpose)
);
CREATE INDEX IF NOT EXISTS idx_consumptions_base ON signal_consumptions(base_key, consumed_at);
CREATE TABLE IF NOT EXISTS task_summary_drafts (
    draft_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(task_id, revision)
);
CREATE TABLE IF NOT EXISTS task_summary_reviews (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id TEXT NOT NULL UNIQUE,
    draft_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summary_reviews_draft ON task_summary_reviews(draft_id, sequence);
"""


class TaskStore:
    def __init__(self, db_path: Path | str, *, read_only: bool = False):
        self.db_path = Path(db_path)
        self.read_only = read_only
        if read_only:
            self._conn = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True,
                                         timeout=30, isolation_level=None, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(_SCHEMA)
        self._migrate_legacy_schema()

    def _migrate_legacy_schema(self) -> None:
        """Bring pre-1.1 databases up to the R01 schema (additive only).

        Existing rows keep their data: ``business_intent_id`` backfills as ''
        and the partial unique index ignores empty values. History is never
        rewritten here — payload/column version divergence is REPORTED via
        ``input_version_divergence_report``, never repaired by guessing.
        """

        columns = {str(row["name"]) for row in self._conn.execute("PRAGMA table_info(research_tasks)")}
        if "business_intent_id" not in columns:
            self._conn.execute(
                "ALTER TABLE research_tasks ADD COLUMN business_intent_id TEXT NOT NULL DEFAULT ''")
        # Only non-empty intents must be unique; '' is the shared legacy default.
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_business_intent "
            "ON research_tasks(business_intent_id) WHERE business_intent_id <> ''"
        )

    def close(self) -> None:
        self._conn.close()

    # -------- low-level helpers --------

    def _begin(self) -> None:
        if self.read_only:
            raise TaskRuntimeError("read-only task store")
        self._conn.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._conn.commit()

    def _rollback(self) -> None:
        try:
            self._conn.rollback()
        except sqlite3.Error:
            pass

    def _emit(self, task_id: str, event_type: str, actor: str = "system", command_id: str = "", payload: Optional[Dict[str, Any]] = None) -> str:
        event_id = _new_id("evt")
        self._conn.execute(
            "INSERT INTO task_outbox (event_id, task_id, event_type, actor, command_id, payload, created_at) VALUES (?,?,?,?,?,?,?)",
            (event_id, task_id, event_type, actor, command_id, json.dumps(payload or {}, ensure_ascii=False, default=str), _now()),
        )
        return event_id

    def _task_row(self, task_id: str) -> sqlite3.Row:
        row = self._conn.execute("SELECT * FROM research_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskRuntimeError("unknown task_id: {0}".format(task_id))
        return row

    @staticmethod
    def _decode(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        for key in ("input_payload", "model_call", "output", "human_request"):
            try:
                item[key] = json.loads(item.get(key) or "{}")
            except json.JSONDecodeError:
                item[key] = {}
        return item

    def _touch(self, task_id: str, **columns: Any) -> int:
        """Apply one state mutation; every mutation bumps the row version.

        The version increment is what makes ``expected_version`` optimistic
        locking real: a page holding an older version can never decide on a
        newer state. All writes are parameter-bound (never string-built).
        """

        if not columns:
            return 0
        assignments = ", ".join("{0} = ?".format(key) for key in columns)
        assignments = "version = version + 1, " + assignments
        values = list(columns.values()) + [_now(), task_id]
        return self._conn.execute(
            "UPDATE research_tasks SET {0}, updated_at = ? WHERE task_id = ?".format(assignments), values
        ).rowcount

    # -------- public API --------

    def create_task(
        self,
        *,
        title: str,
        input_payload: Dict[str, Any],
        case_id: str = "",
        role_id: str = "research_analyst",
        parent_task_id: str = "",
        idempotency_key: str = "",
        business_intent_id: str = "",
        prompt_version: str = "research_v1",
        model_config_hash: str = "",
        max_attempts: int = 2,
        execution_mode: str = "live",
        actor: str = "operator",
        consume_event: Optional[Dict[str, Any]] = None,
        platform_run: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, Any], str]:
        """Create (or deduplicate) a task; optionally record its event consumption atomically.

        ``consume_event`` = {event_key, purpose, base_key, signal_id} is written
        in the SAME transaction as the task row, closing the crash window where
        a source was read but its task/consumption pair never committed. On the
        DUPLICATE path a missing consumption row converges (recorded now, bound
        to the existing task) so rescans reconcile the ledger instead of
        creating a second task or losing the event.

        R01 semantics:

        - The ``input_version`` COLUMN is the single authority. The payload's
          same-name field is injected to match it at write time and is only a
          compatibility projection — readers must use the column.
        - ``business_intent_id`` is the stable business identity. When given,
          dedup runs on it FIRST (platform retries reuse one task); platform
          executions (Dify run ids, ...) are recorded as separate
          ``task_platform_runs`` association rows instead of leaking into the
          idempotency key or the payload fingerprint.
        - An immutable ``task_input_snapshots`` row is written at creation and
          at every provide_input version bump.
        """

        if execution_mode not in EXECUTION_MODES:
            raise TaskRuntimeError("execution_mode must be live or replay")
        if not str(title or "").strip():
            raise TaskRuntimeError("task title must not be empty")
        if not isinstance(input_payload, dict):
            raise TaskRuntimeError("input_payload must be a JSON object")
        classification = str(input_payload.get("data_classification") or "")
        if classification != "public":
            raise TaskRuntimeError("task input must be classified public for external model egress")
        # Authoritative input version: taken from the payload once, validated,
        # stored in the column, and mirrored into the payload projection.
        raw_version = input_payload.get("input_version", 1)
        if isinstance(raw_version, bool) or not isinstance(raw_version, int) or raw_version < 1:
            raise TaskRuntimeError("input_version must be a positive integer")
        input_version = int(raw_version)
        payload = dict(input_payload)
        payload["input_version"] = input_version
        input_payload = payload
        business_intent_id = str(business_intent_id or "").strip()
        if business_intent_id and not idempotency_key:
            # Intent-scoped key so two different intents with byte-identical
            # content can never collide on the default content key.
            idempotency_key = "intent::{0}".format(business_intent_id)
        run_record: Optional[Dict[str, str]] = None
        if platform_run is not None:
            if not isinstance(platform_run, dict):
                raise TaskRuntimeError("platform_run must be an object")
            run_platform = str(platform_run.get("platform") or "").strip()
            run_id = str(platform_run.get("run_id") or "").strip()
            if not run_platform or not run_id:
                raise TaskRuntimeError("platform_run requires platform and run_id")
            run_record = {
                "platform": run_platform,
                "run_id": run_id,
                "workflow_version": str(platform_run.get("workflow_version") or "").strip(),
                "actor": str(platform_run.get("actor") or "").strip(),
            }
        consumption: Optional[Dict[str, str]] = None
        if consume_event is not None:
            if not isinstance(consume_event, dict):
                raise TaskRuntimeError("consume_event must be an object")
            event_key = str(consume_event.get("event_key") or "").strip()
            purpose = str(consume_event.get("purpose") or "").strip()
            if not event_key or not purpose:
                raise TaskRuntimeError("consume_event requires event_key and purpose")
            consumption = {
                "event_key": event_key,
                "purpose": purpose,
                "base_key": str(consume_event.get("base_key") or "").strip(),
                "signal_id": str(consume_event.get("signal_id") or "").strip(),
            }
        if not idempotency_key:
            # Default identity: same case/role/prompt/model/mode AND the exact
            # same input content. Independent requests (different question,
            # different context, different mode) must never share a key.
            content_fingerprint = self._content_fingerprint(input_payload)
            idempotency_key = "idem::{0}::{1}::{2}::{3}::{4}::{5}::{6}".format(
                case_id, role_id, input_version,
                prompt_version, model_config_hash, execution_mode, content_fingerprint,
            )
        created = _now()
        self._begin()
        try:
            existing = None
            matched_by_intent = False
            if business_intent_id:
                existing = self._conn.execute(
                    "SELECT * FROM research_tasks WHERE business_intent_id = ?", (business_intent_id,)
                ).fetchone()
                matched_by_intent = existing is not None
            if existing is None:
                existing = self._conn.execute(
                    "SELECT * FROM research_tasks WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
            if existing is not None:
                try:
                    stored_payload = json.loads(existing["input_payload"] or "{}")
                except json.JSONDecodeError:
                    stored_payload = None
                if matched_by_intent:
                    # Intent identity: same business request must reuse this
                    # task. Content is compared WITHOUT platform run ids (a
                    # retried platform run is not new content); anything else
                    # differing under the same intent is an explicit conflict.
                    if str(existing["execution_mode"]) != execution_mode or not (
                        isinstance(stored_payload, dict)
                        and self._logical_content_fingerprint(stored_payload)
                        == self._logical_content_fingerprint(input_payload)
                    ):
                        self._rollback()
                        raise TaskIdempotencyConflictError(
                            "business intent {0} already names a different request".format(business_intent_id))
                    same_logical_request = True
                else:
                    same_content = (
                        stored_payload is not None
                        and self._content_fingerprint(stored_payload) == self._content_fingerprint(input_payload)
                    )
                    # For event- and question-derived tasks the logical request IS
                    # the stable identity (event key / question hash): a rebuilt
                    # public context (later signals, refreshed cache) is not a
                    # different request. Byte equality stays mandatory for every
                    # other key.
                    def _same_logical(payload_a: Dict[str, Any], payload_b: Dict[str, Any]) -> bool:
                        origin_a = payload_a.get("origin") if isinstance(payload_a.get("origin"), dict) else {}
                        origin_b = payload_b.get("origin") if isinstance(payload_b.get("origin"), dict) else {}
                        if str(origin_a.get("event_key") or "") and \
                                origin_a.get("event_key") == origin_b.get("event_key"):
                            return True
                        sha_a = str(origin_a.get("question_sha256") or "")
                        if sha_a and sha_a == str(origin_b.get("question_sha256") or "") \
                                and origin_a.get("kind") == origin_b.get("kind") == "question":
                            return True
                        return False

                    same_logical_request = str(existing["execution_mode"]) == execution_mode and (
                        same_content
                        or (
                            consumption is not None
                            and isinstance(stored_payload, dict)
                            and str((stored_payload.get("origin") or {}).get("event_key") or "")
                            == consumption["event_key"]
                        )
                        or (
                            isinstance(stored_payload, dict)
                            and _same_logical(stored_payload, input_payload)
                        )
                    )
                if not same_logical_request:
                    self._rollback()
                    raise TaskIdempotencyConflictError(
                        "idempotency key {0} already names a different request".format(idempotency_key)
                    )
                dirty = False
                if consumption is not None:
                    row = self._conn.execute(
                        "SELECT task_id FROM signal_consumptions WHERE event_key = ? AND purpose = ?",
                        (consumption["event_key"], consumption["purpose"]),
                    ).fetchone()
                    if row is not None and str(row["task_id"]) != str(existing["task_id"]):
                        self._rollback()
                        raise TaskIdempotencyConflictError(
                            "event {0} already consumed by task {1}".format(
                                consumption["event_key"], row["task_id"]))
                    if row is None:
                        # Crash-window convergence: the task exists but its
                        # consumption record was never committed.
                        self._conn.execute(
                            "INSERT INTO signal_consumptions (event_key, purpose, base_key, signal_id, task_id, consumed_at) VALUES (?,?,?,?,?,?)",
                            (consumption["event_key"], consumption["purpose"], consumption["base_key"],
                             consumption["signal_id"], str(existing["task_id"]), _now()),
                        )
                        dirty = True
                if run_record is not None:
                    self._record_platform_run(str(existing["task_id"]), business_intent_id, run_record)
                    dirty = True
                if dirty:
                    self._commit()
                else:
                    self._rollback()
                return self._decode(existing), "DUPLICATE"
            task_id = _new_id("task")
            self._conn.execute(
                """INSERT INTO research_tasks
                   (task_id, parent_task_id, case_id, role_id, title, input_version, input_payload,
                    idempotency_key, business_intent_id,
                    prompt_version, model_config_hash, max_attempts, execution_mode, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, parent_task_id, case_id, role_id, title, input_version,
                 json.dumps(input_payload, ensure_ascii=False, default=str), idempotency_key,
                 business_intent_id,
                 prompt_version, model_config_hash, max(1, int(max_attempts)), execution_mode, created, created),
            )
            self._write_input_snapshot(task_id, input_version, input_payload,
                                       created_by=actor, reason="created", created_at=created)
            if run_record is not None:
                self._record_platform_run(task_id, business_intent_id, run_record)
            if consumption is not None:
                self._conn.execute(
                    "INSERT INTO signal_consumptions (event_key, purpose, base_key, signal_id, task_id, consumed_at) VALUES (?,?,?,?,?,?)",
                    (consumption["event_key"], consumption["purpose"], consumption["base_key"],
                     consumption["signal_id"], task_id, created),
                )
            self._emit(task_id, "task_created", actor=actor,
                       payload={"title": title, "case_id": case_id, "execution_mode": execution_mode,
                                "input_version": input_version, "business_intent_id": business_intent_id})
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return self.get_task(task_id), "RECORDED"

    def get_task(self, task_id: str) -> Dict[str, Any]:
        return self._decode(self._task_row(task_id))

    def list_tasks(self, status: str = "") -> List[Dict[str, Any]]:
        if status:
            rows = self._conn.execute("SELECT * FROM research_tasks WHERE status = ? ORDER BY created_at", (status,)).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM research_tasks ORDER BY created_at").fetchall()
        return [self._decode(row) for row in rows]

    def events(self, task_id: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        if task_id:
            rows = self._conn.execute(
                "SELECT * FROM task_outbox WHERE task_id = ? ORDER BY sequence LIMIT ?", (task_id, int(limit))
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM task_outbox ORDER BY sequence DESC LIMIT ?", (int(limit),)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.get("payload") or "{}")
            except json.JSONDecodeError:
                item["payload"] = {}
            result.append(item)
        return result

    def events_after(self, sequence: int, limit: int = 100) -> List[Dict[str, Any]]:
        """Return outbox events strictly after ``sequence`` in ascending order.

        This is the cursor the SSE endpoint replays from after a reconnect
        (``Last-Event-ID``), so a dropped connection never loses events.
        """

        rows = self._conn.execute(
            "SELECT * FROM task_outbox WHERE sequence > ? ORDER BY sequence LIMIT ?",
            (int(sequence), int(limit)),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.get("payload") or "{}")
            except json.JSONDecodeError:
                item["payload"] = {}
            result.append(item)
        return result

    def _check_transition(self, current: str, target: str) -> None:
        if target not in ALLOWED_TRANSITIONS.get(current, set()):
            raise TaskTransitionError("transition {0} -> {1} is not allowed".format(current, target))

    @staticmethod
    def _content_fingerprint(payload: Any) -> str:
        canonical = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    # Volatile platform execution identifiers never belong to business content:
    # a retried platform run is the same request, not new content.
    _PLATFORM_RUN_ORIGIN_KEYS = ("dify_run_id", "platform_run_id", "n8n_execution_id")

    @classmethod
    def _logical_content_fingerprint(cls, payload: Any) -> str:
        if not isinstance(payload, dict):
            return cls._content_fingerprint(payload)
        origin = payload.get("origin")
        if isinstance(origin, dict) and any(key in origin for key in cls._PLATFORM_RUN_ORIGIN_KEYS):
            stripped_origin = {key: value for key, value in origin.items()
                               if key not in cls._PLATFORM_RUN_ORIGIN_KEYS}
            return cls._content_fingerprint(dict(payload, origin=stripped_origin))
        return cls._content_fingerprint(payload)

    def _write_input_snapshot(self, task_id: str, input_version: int, payload: Dict[str, Any],
                              *, created_by: str, reason: str, created_at: str = "") -> str:
        """Insert the immutable snapshot for (task, input_version). Call inside a transaction."""

        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        try:
            self._conn.execute(
                "INSERT INTO task_input_snapshots (task_id, input_version, payload, payload_sha256, created_at, created_by, reason) VALUES (?,?,?,?,?,?,?)",
                (task_id, int(input_version), payload_json, payload_sha256,
                 created_at or _now(), str(created_by or ""), str(reason or "")),
            )
        except sqlite3.IntegrityError as exc:
            raise TaskRuntimeError(
                "input snapshot for task {0} version {1} already exists; snapshots are immutable".format(
                    task_id, input_version)) from exc
        return payload_sha256

    def _record_platform_run(self, task_id: str, business_intent_id: str, run: Dict[str, str]) -> Dict[str, Any]:
        """Associate one platform execution with a task. Call inside a transaction.

        Same run + same task is an idempotent no-op (a retried create must not
        append a second association); the same run id naming a DIFFERENT task
        is a conflict, because one platform execution cannot drive two tasks.
        """

        run_key = "{0}::{1}".format(run["platform"], run["run_id"])
        row = self._conn.execute(
            "SELECT * FROM task_platform_runs WHERE run_key = ?", (run_key,)
        ).fetchone()
        if row is not None:
            if str(row["task_id"]) != str(task_id):
                raise TaskIdempotencyConflictError(
                    "platform run {0} already associated with task {1}".format(run_key, row["task_id"]))
            return dict(row)
        self._conn.execute(
            "INSERT INTO task_platform_runs (run_key, platform, run_id, task_id, business_intent_id, workflow_version, actor, recorded_at) VALUES (?,?,?,?,?,?,?,?)",
            (run_key, run["platform"], run["run_id"], task_id, str(business_intent_id or ""),
             run.get("workflow_version", ""), run.get("actor", ""), _now()),
        )
        return {
            "run_key": run_key, "platform": run["platform"], "run_id": run["run_id"],
            "task_id": task_id, "business_intent_id": str(business_intent_id or ""),
            "workflow_version": run.get("workflow_version", ""), "actor": run.get("actor", ""),
        }

    def list_platform_runs(self, task_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM task_platform_runs WHERE task_id = ? ORDER BY recorded_at, run_key", (str(task_id),)
        ).fetchall()
        return [dict(row) for row in rows]

    def get_platform_run(self, platform: str, run_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM task_platform_runs WHERE run_key = ?",
            ("{0}::{1}".format(str(platform), str(run_id)),),
        ).fetchone()
        return dict(row) if row is not None else None

    def input_snapshots(self, task_id: str) -> List[Dict[str, Any]]:
        """Immutable per-version input history (payload decoded, sha preserved)."""

        rows = self._conn.execute(
            "SELECT * FROM task_input_snapshots WHERE task_id = ? ORDER BY input_version", (str(task_id),)
        ).fetchall()
        result: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.get("payload") or "{}")
            except json.JSONDecodeError:
                item["payload"] = {}
            result.append(item)
        return result

    def find_task_by_idempotency_key(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM research_tasks WHERE idempotency_key = ?", (str(idempotency_key),)
        ).fetchone()
        return self._decode(row) if row is not None else None

    def input_version_divergence_report(self) -> Dict[str, Any]:
        """READ-ONLY diagnostic: where the payload projection disagrees with the column.

        R01 makes the column authoritative; legacy rows may disagree. This
        reports each divergence with a suggested mapping so a human can review
        a repair plan. It never modifies history.
        """

        rows = self._conn.execute(
            "SELECT task_id, input_version, input_payload, created_at, updated_at FROM research_tasks"
        ).fetchall()
        entries: List[Dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["input_payload"] or "{}")
            except json.JSONDecodeError:
                payload = None
            payload_version = payload.get("input_version") if isinstance(payload, dict) else None
            column_version = int(row["input_version"])
            if payload_version is None and not isinstance(payload, dict):
                entries.append({
                    "task_id": str(row["task_id"]), "column_version": column_version,
                    "payload_version": None, "divergence": "payload_not_valid_json",
                    "suggested_mapping": "keep column version {0}; rebuild payload from event history".format(column_version),
                    "updated_at": str(row["updated_at"] or ""),
                })
                continue
            if payload_version != column_version:
                suggested = column_version
                if isinstance(payload_version, int) and not isinstance(payload_version, bool) \
                        and payload_version > column_version:
                    suggested = payload_version
                entries.append({
                    "task_id": str(row["task_id"]), "column_version": column_version,
                    "payload_version": payload_version,
                    "divergence": "payload_column_mismatch",
                    "suggested_mapping": "candidate authoritative version {0}; requires human review, no auto-repair".format(suggested),
                    "updated_at": str(row["updated_at"] or ""),
                })
        return {
            "schema_version": "r01-diagnostic-1",
            "generated_at": _now(),
            "divergent_tasks": len(entries),
            "note": "read-only diagnostic; repair plans require human review, history is never auto-rewritten",
            "entries": entries,
        }

    def claim_next(self, worker_id: str, ttl_seconds: int = 120) -> Optional[Dict[str, Any]]:
        if not str(worker_id or "").strip():
            raise TaskRuntimeError("worker_id must not be empty")
        expires = (datetime.now().astimezone() + timedelta(seconds=max(5, int(ttl_seconds)))).isoformat(timespec="seconds")
        self._begin()
        try:
            row = self._conn.execute(
                "SELECT * FROM research_tasks WHERE status = ? ORDER BY created_at LIMIT 1", (STATUS_QUEUED,)
            ).fetchone()
            if row is None or row["cancel_requested"]:
                self._rollback()
                if row is not None and row["cancel_requested"]:
                    self._begin()
                    self._touch(row["task_id"], status=STATUS_CANCELLED)
                    self._emit(row["task_id"], "task_cancelled", payload={"reason": "cancelled before claim"})
                    self._commit()
                return None
            task_id = row["task_id"]
            self._touch(
                task_id,
                status=STATUS_LEASED,
                worker_id=worker_id,
                lease_expires_at=expires,
                fencing_token=int(row["fencing_token"]) + 1,
                attempt_count=int(row["attempt_count"]) + 1,
            )
            self._emit(task_id, "task_claimed", actor=worker_id, payload={"lease_expires_at": expires, "fencing_token": int(row["fencing_token"]) + 1})
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return self.get_task(task_id)

    def mark_running(self, task_id: str, *, worker_id: str, fencing_token: int, expected_version: int) -> Dict[str, Any]:
        self._begin()
        try:
            row = self._task_row(task_id)
            self._assert_worker(row, worker_id, fencing_token)
            if int(row["version"]) != int(expected_version):
                raise TaskVersionConflictError("expected_version {0} does not match current {1}".format(expected_version, row["version"]))
            self._check_transition(row["status"], STATUS_RUNNING)
            self._touch(task_id, status=STATUS_RUNNING)
            self._emit(task_id, "task_running", actor=worker_id)
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return self.get_task(task_id)

    def renew_lease(self, task_id: str, *, worker_id: str, fencing_token: int, ttl_seconds: int = 120) -> Dict[str, Any]:
        expires = (datetime.now().astimezone() + timedelta(seconds=max(5, int(ttl_seconds)))).isoformat(timespec="seconds")
        self._begin()
        try:
            row = self._task_row(task_id)
            self._assert_worker(row, worker_id, fencing_token)
            if row["status"] not in {STATUS_LEASED, STATUS_RUNNING}:
                raise TaskTransitionError("cannot renew a task in status {0}".format(row["status"]))
            self._touch(task_id, lease_expires_at=expires)
            self._emit(task_id, "lease_renewed", actor=worker_id, payload={"lease_expires_at": expires})
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return self.get_task(task_id)

    def complete(self, task_id: str, *, worker_id: str, fencing_token: int, output: Dict[str, Any], model_call: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._begin()
        try:
            row = self._task_row(task_id)
            self._assert_worker(row, worker_id, fencing_token)
            if row["cancel_requested"]:
                self._check_transition(row["status"], STATUS_CANCELLED)
                self._touch(task_id, status=STATUS_CANCELLED)
                self._emit(task_id, "task_cancelled", actor=worker_id, payload={"reason": "cancel requested before completion"})
                self._commit()
                return self.get_task(task_id)
            self._check_transition(row["status"], STATUS_SUCCEEDED)
            self._touch(
                task_id,
                status=STATUS_SUCCEEDED,
                output=json.dumps(output or {}, ensure_ascii=False, default=str),
                model_call=json.dumps(model_call or {}, ensure_ascii=False, default=str),
                worker_id=worker_id,
            )
            self._emit(task_id, "task_succeeded", actor=worker_id)
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return self.get_task(task_id)

    def fail(self, task_id: str, *, worker_id: str, fencing_token: int, error: str, retryable: bool = True, model_call: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._begin()
        try:
            row = self._task_row(task_id)
            self._assert_worker(row, worker_id, fencing_token)
            attempts_left = int(row["max_attempts"]) - int(row["attempt_count"])
            call_json = json.dumps(model_call or {}, ensure_ascii=False, default=str)
            if row["cancel_requested"]:
                self._check_transition(row["status"], STATUS_CANCELLED)
                self._touch(task_id, status=STATUS_CANCELLED, last_error=str(error or ""), model_call=call_json)
                self._emit(task_id, "task_cancelled", actor=worker_id, payload={"reason": "cancelled during failure handling"})
                self._commit()
                return self.get_task(task_id)
            if retryable and attempts_left > 0:
                self._check_transition(row["status"], STATUS_QUEUED)
                self._touch(task_id, status=STATUS_QUEUED, last_error=str(error or ""), worker_id="", lease_expires_at="", model_call=call_json)
                self._emit(task_id, "task_retry_scheduled", actor=worker_id, payload={"error": str(error)[:500], "attempts_left": attempts_left})
            else:
                self._check_transition(row["status"], STATUS_FAILED)
                self._touch(task_id, status=STATUS_FAILED, last_error=str(error or ""), worker_id=worker_id, model_call=call_json)
                self._emit(task_id, "task_failed", actor=worker_id, payload={"error": str(error)[:500], "retryable": bool(retryable)})
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return self.get_task(task_id)

    def mark_blocked(self, task_id: str, *, worker_id: str, fencing_token: int, error: str, model_call: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Record a governance/human gate rejection with the call audit intact."""

        self._begin()
        try:
            row = self._task_row(task_id)
            self._assert_worker(row, worker_id, fencing_token)
            self._check_transition(row["status"], STATUS_BLOCKED)
            self._touch(
                task_id,
                status=STATUS_BLOCKED,
                last_error=str(error or "")[:2000],
                model_call=json.dumps(model_call or {}, ensure_ascii=False, default=str),
                worker_id=worker_id,
            )
            self._emit(task_id, "task_blocked", actor=worker_id, payload={"error": str(error)[:500]})
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return self.get_task(task_id)

    def request_awaiting_human(
        self,
        task_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        human_request: Dict[str, Any],
        output: Optional[Dict[str, Any]] = None,
        model_call: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not isinstance(human_request, dict) or not str(human_request.get("question") or "").strip():
            raise TaskRuntimeError("human_request.question must not be empty")
        self._begin()
        try:
            row = self._task_row(task_id)
            self._assert_worker(row, worker_id, fencing_token)
            if row["cancel_requested"]:
                # A cancellation that landed during the run wins: the task
                # terminates instead of opening a human decision.
                self._check_transition(row["status"], STATUS_CANCELLED)
                self._touch(task_id, status=STATUS_CANCELLED)
                self._emit(task_id, "task_cancelled", actor=worker_id, payload={"reason": "cancel requested before human handoff"})
                self._commit()
                return self.get_task(task_id)
            self._check_transition(row["status"], STATUS_AWAITING_HUMAN)
            record = dict(human_request)
            record.setdefault("questions", [])
            self._touch(
                task_id,
                status=STATUS_AWAITING_HUMAN,
                human_request=json.dumps(record, ensure_ascii=False, default=str),
                output=json.dumps(output or {}, ensure_ascii=False, default=str),
                model_call=json.dumps(model_call or {}, ensure_ascii=False, default=str),
                worker_id=worker_id,
            )
            self._emit(task_id, "human_requested", actor=worker_id, payload=record)
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return self.get_task(task_id)

    def request_cancel(self, task_id: str, *, actor: str, reason: str, expected_version: int,
                       principal_verification: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not str(actor or "").strip():
            raise TaskRuntimeError("actor must not be empty")
        self._begin()
        try:
            # R03: atomic claim in the SAME transaction as the cancellation
            # (see record_human_response); a protected cancel without a valid
            # one-command receipt never mutates the task.
            if principal_verification is not None:
                if not isinstance(principal_verification, dict):
                    self._rollback()
                    raise TaskRuntimeError("principal_verification must be an object")
                verdict = self._claim_verification_in_txn(
                    str(principal_verification.get("verification_id") or ""),
                    actor=str(principal_verification.get("actor") or actor),
                    command_id=str(principal_verification.get("command_id") or "cancel:{0}".format(task_id)),
                    task_id=task_id,
                    action="cancel",
                    issued_fallback=principal_verification.get("issued_fallback"))
                if not verdict.get("claimed"):
                    self._rollback()
                    raise PrincipalVerificationError(
                        str(verdict.get("reason") or "principal_verification_required"))
            row = self._task_row(task_id)
            if int(row["version"]) != int(expected_version):
                raise TaskVersionConflictError("expected_version {0} does not match current {1}".format(expected_version, row["version"]))
            if row["status"] in TERMINAL_STATUSES:
                raise TaskTransitionError("task is already terminal: {0}".format(row["status"]))
            if row["status"] in (STATUS_QUEUED, STATUS_AWAITING_HUMAN):
                # No live worker decision is pending in these states, so the
                # cancellation takes effect immediately.
                self._touch(task_id, status=STATUS_CANCELLED, cancel_requested=1)
                self._emit(task_id, "task_cancelled", actor=actor, payload={"reason": str(reason or "")[:500]})
            else:
                self._touch(task_id, cancel_requested=1)
                self._emit(task_id, "task_cancel_requested", actor=actor, payload={"reason": str(reason or "")[:500]})
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return self.get_task(task_id)

    def record_human_response(
        self,
        task_id: str,
        *,
        actor: str,
        decision: str,
        expected_version: int,
        command_id: str,
        input_patch: Optional[Dict[str, Any]] = None,
        notes: str = "",
        principal_verification: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not str(actor or "").strip():
            raise TaskRuntimeError("actor must not be empty")
        if decision not in HUMAN_DECISIONS:
            raise TaskRuntimeError("decision must be one of {0}".format(sorted(HUMAN_DECISIONS)))
        request_sha256 = self._content_fingerprint({
            "operation": "human_response", "task_id": task_id, "actor": actor,
            "decision": decision, "expected_version": expected_version,
            "input_patch": input_patch, "notes": notes,
        })
        self._begin()
        try:
            if command_id:
                receipt = self._conn.execute(
                    "SELECT * FROM idempotency_receipts WHERE command_id = ?", (command_id,)
                ).fetchone()
                if receipt is not None:
                    recorded = json.loads(receipt["result"])
                    if receipt["task_id"] != task_id or recorded.get("request_sha256") != request_sha256:
                        raise TaskIdempotencyConflictError("command_id is not an exact replay of this human response")
                    self._rollback()
                    return self.get_task(task_id)
            # R03: the claim runs INSIDE this transaction — verification
            # consumption and the task state change commit or roll back
            # together, so two concurrent commands can never both spend one
            # receipt even when both passed the gateway's friendly pre-check.
            if principal_verification is not None:
                if not isinstance(principal_verification, dict):
                    self._rollback()
                    raise TaskRuntimeError("principal_verification must be an object")
                verdict = self._claim_verification_in_txn(
                    str(principal_verification.get("verification_id") or ""),
                    actor=str(principal_verification.get("actor") or actor),
                    command_id=str(principal_verification.get("command_id") or command_id),
                    task_id=task_id,
                    action=str(principal_verification.get("action") or "feedback"),
                    issued_fallback=principal_verification.get("issued_fallback"))
                if not verdict.get("claimed"):
                    self._rollback()
                    raise PrincipalVerificationError(
                        str(verdict.get("reason") or "principal_verification_required"))
            row = self._task_row(task_id)
            if int(row["version"]) != int(expected_version):
                raise TaskVersionConflictError("expected_version {0} does not match current {1}".format(expected_version, row["version"]))
            if row["status"] != STATUS_AWAITING_HUMAN:
                raise TaskTransitionError("human response requires awaiting_human, got {0}".format(row["status"]))
            if row["cancel_requested"]:
                raise TaskTransitionError("task was cancelled; no further human responses are accepted")
            if decision == "provide_input":
                if not isinstance(input_patch, dict) or not input_patch:
                    raise TaskRuntimeError("provide_input requires a non-empty input_patch object")
                illegal = sorted(set(input_patch) - ALLOWED_INPUT_PATCH_KEYS)
                if illegal:
                    raise TaskTransitionError("input_patch touches fields outside the allowlist: {0}".format(", ".join(illegal)))
                try:
                    current_payload = json.loads(row["input_payload"] or "{}")
                except json.JSONDecodeError as exc:
                    raise TaskRuntimeError("task input_payload is not valid JSON") from exc
                merged = dict(current_payload)
                merged.update(input_patch)
                if str(merged.get("data_classification") or "").lower() != "public":
                    raise TaskTransitionError("patched input must remain classified public for external egress")
                new_version = int(row["input_version"]) + 1
                # The column is authoritative; the payload field is synced to it
                # so no reader can ever see two different versions again.
                merged["input_version"] = new_version
                snapshot_sha256 = self._write_input_snapshot(
                    task_id, new_version, merged, created_by=actor, reason="human_provide_input")
                self._touch(
                    task_id,
                    status=STATUS_QUEUED,
                    input_version=new_version,
                    input_payload=json.dumps(merged, ensure_ascii=False, default=str),
                    worker_id="",
                )
                self._emit(task_id, "human_response_recorded", actor=actor, command_id=command_id,
                           payload={"decision": decision, "input_version": new_version,
                                    "input_snapshot_sha256": snapshot_sha256})
            elif decision == "acknowledge":
                try:
                    current_output = json.loads(row["output"] or "{}")
                except json.JSONDecodeError as exc:
                    raise TaskRuntimeError("task output is not valid JSON") from exc
                output = dict(current_output)
                output["personal_acknowledgement"] = {
                    "actor": actor,
                    "at": _now(),
                    "notes": str(notes or ""),
                    "scope": "personal_research_acknowledgement_not_institutional_approval",
                }
                self._touch(task_id, status=STATUS_SUCCEEDED, output=json.dumps(output, ensure_ascii=False, default=str))
                self._emit(task_id, "personal_acknowledgement", actor=actor, command_id=command_id,
                           payload={"notes": str(notes or "")[:500]})
            else:
                self._touch(task_id, status=STATUS_BLOCKED, last_error=str(notes or "human rejected")[:500])
                self._emit(task_id, "human_response_recorded", actor=actor, command_id=command_id,
                           payload={"decision": decision})
            if command_id:
                self._conn.execute(
                    "INSERT INTO idempotency_receipts (command_id, task_id, result, created_at) VALUES (?,?,?,?)",
                    (command_id, task_id, json.dumps({"decision": decision, "request_sha256": request_sha256}), _now()),
                )
            self._commit()
        except sqlite3.IntegrityError as exc:
            self._rollback()
            raise TaskIdempotencyConflictError("duplicate command_id: {0}".format(command_id)) from exc
        except BaseException:
            self._rollback()
            raise
        return self.get_task(task_id)

    def recover_abandoned(self, *, ttl_grace_seconds: int = 0) -> List[Dict[str, Any]]:
        """Requeue or expire tasks whose lease expired without a terminal state.

        The old worker's fencing token is left untouched and raised on the next
        claim, so a late write from the abandoned worker is rejected rather
        than silently accepted.
        """

        now = datetime.now().astimezone() - timedelta(seconds=max(0, int(ttl_grace_seconds)))
        recovered: List[Dict[str, Any]] = []
        rows = self._conn.execute(
            "SELECT * FROM research_tasks WHERE status IN (?, ?)", (STATUS_LEASED, STATUS_RUNNING)
        ).fetchall()
        for row in rows:
            expires = str(row["lease_expires_at"] or "")
            if not expires:
                continue
            try:
                expired_at = datetime.fromisoformat(expires)
            except ValueError:
                continue
            if expired_at > now:
                continue
            task_id = row["task_id"]
            self._begin()
            try:
                fresh = self._task_row(task_id)
                if fresh["status"] not in {STATUS_LEASED, STATUS_RUNNING}:
                    self._rollback()
                    continue
                # Re-verify the CURRENT lease inside the write transaction: the
                # snapshot row may be stale if the worker renewed between the
                # scan and this transaction. BEGIN IMMEDIATE holds the write
                # lock, so anything that commits after this point cannot have
                # been observed here.
                fresh_expires = str(fresh["lease_expires_at"] or "")
                try:
                    fresh_expired_at = datetime.fromisoformat(fresh_expires)
                except ValueError:
                    self._rollback()
                    continue
                if not fresh_expires or fresh_expired_at > datetime.now().astimezone() - timedelta(seconds=max(0, int(ttl_grace_seconds))):
                    self._rollback()
                    continue
                # R02: close the dead worker's attempt chain FIRST (its own
                # transaction). A dispatched attempt becomes outcome_unknown —
                # the request may have been sent, so the task can no longer be
                # blindly requeued; a prepared-only attempt provably never left.
                self._rollback()
                attempt_recovery = self.recover_stale_invocation_attempts(
                    task_id, error="lease expired; worker presumed dead")
                self._begin()
                fresh = self._task_row(task_id)
                if fresh["status"] not in {STATUS_LEASED, STATUS_RUNNING}:
                    # The stale worker's lease ended in a terminal state (or a
                    # live worker won the race during attempt recovery): the
                    # snapshot decision no longer applies.
                    self._rollback()
                    continue
                if fresh["cancel_requested"]:
                    self._touch(task_id, status=STATUS_CANCELLED, worker_id="", lease_expires_at="",
                                last_error="cancelled during abandoned-lease recovery")
                    self._emit(task_id, "task_recovered", payload={"action": "cancelled", "expired_at": fresh_expires})
                    self._commit()
                    recovered.append(self.get_task(task_id))
                    continue
                attempts_left = int(fresh["max_attempts"]) - int(fresh["attempt_count"])
                if attempt_recovery["outcome_unknown"] > 0:
                    # At least one request may have reached the provider: no
                    # auto re-dispatch. Manual reconciliation gates any retry.
                    self._touch(task_id, status=STATUS_BLOCKED, worker_id="", lease_expires_at="",
                                last_error="lease expired with a dispatched invocation of unknown outcome; manual reconciliation required (attempt chain preserved)")
                    self._emit(task_id, "task_recovered", payload={
                        "action": "blocked_unknown_invocation", "expired_at": fresh_expires,
                        "attempt_recovery": attempt_recovery})
                elif attempts_left > 0:
                    self._touch(task_id, status=STATUS_QUEUED, worker_id="", lease_expires_at="",
                                last_error="lease expired; task requeued by recovery")
                    self._emit(task_id, "task_recovered", payload={"action": "requeued", "expired_at": fresh_expires})
                else:
                    self._touch(task_id, status=STATUS_EXPIRED, last_error="lease expired with no attempts left")
                    self._emit(task_id, "task_recovered", payload={"action": "expired", "expired_at": fresh_expires})
                self._commit()
            except BaseException:
                self._rollback()
                raise
            recovered.append(self.get_task(task_id))
        return recovered

    def _assert_worker(self, row: sqlite3.Row, worker_id: str, fencing_token: int) -> None:
        if row["status"] not in {STATUS_LEASED, STATUS_RUNNING, STATUS_AWAITING_HUMAN}:
            raise TaskTransitionError("worker action requires an active task, got {0}".format(row["status"]))
        if str(row["worker_id"] or "") != str(worker_id):
            raise TaskFencingError("task is leased by another worker")
        if int(row["fencing_token"]) != int(fencing_token):
            raise TaskFencingError("stale fencing token {0} (current {1}); the task was reclaimed".format(fencing_token, row["fencing_token"]))

    def summary(self) -> Dict[str, Any]:
        rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM research_tasks GROUP BY status").fetchall()
        counts = {row["status"]: int(row["n"]) for row in rows}
        outbox_count = int(self._conn.execute("SELECT COUNT(*) FROM task_outbox").fetchone()[0])
        return {"schema_version": TASK_SCHEMA_VERSION, "task_counts": counts, "outbox_events": outbox_count}

    # -------- event consumption ledger --------

    def find_consumption(self, event_key: str, purpose: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM signal_consumptions WHERE event_key = ? AND purpose = ?",
            (str(event_key), str(purpose)),
        ).fetchone()
        return dict(row) if row is not None else None

    def consumed_event_keys(self, event_keys: List[str]) -> set:
        if not event_keys:
            return set()
        found: set = set()
        for start in range(0, len(event_keys), 500):
            chunk = [str(item) for item in event_keys[start:start + 500]]
            placeholders = ",".join("?" for _ in chunk)
            rows = self._conn.execute(
                "SELECT event_key FROM signal_consumptions WHERE event_key IN ({0})".format(placeholders),
                chunk,
            ).fetchall()
            found.update(str(row["event_key"]) for row in rows)
        return found

    def latest_consumption_for_base(self, base_key: str, *, purpose: str = "") -> Optional[Dict[str, Any]]:
        """Most recent consumption sharing an event's base identity (revision chain)."""
        if purpose:
            row = self._conn.execute(
                "SELECT * FROM signal_consumptions WHERE base_key = ? AND purpose = ? "
                "ORDER BY consumed_at DESC, event_key DESC LIMIT 1",
                (str(base_key), str(purpose)),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM signal_consumptions WHERE base_key = ? "
                "ORDER BY consumed_at DESC, event_key DESC LIMIT 1",
                (str(base_key),),
            ).fetchone()
        return dict(row) if row is not None else None

    def consumption_summary(self) -> Dict[str, Any]:
        consumed = int(self._conn.execute("SELECT COUNT(*) FROM signal_consumptions").fetchone()[0])
        orphan = int(self._conn.execute(
            "SELECT COUNT(*) FROM signal_consumptions c WHERE NOT EXISTS "
            "(SELECT 1 FROM research_tasks t WHERE t.task_id = c.task_id)").fetchone()[0])
        return {"consumed_events": consumed, "orphan_tasks": orphan}

    # -------- research round audit (GLM-05) --------

    def record_research_attempt_start(self, task_id: str, *, worker_id: str,
                                      fencing_token: int, attempt: Dict[str, Any]) -> Dict[str, Any]:
        """Persist attempt identity and input fingerprint BEFORE the model call."""
        self._begin()
        try:
            row = self._task_row(task_id)
            self._assert_worker(row, worker_id, fencing_token)
            self._emit(task_id, "research_attempt_started", actor=worker_id,
                       payload=dict(attempt or {}))
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return self.get_task(task_id)

    def reserve_model_attempt(self, task_id: str, *, worker_id: str, fencing_token: int,
                              attempt: Dict[str, Any], daily_limit: int = 0) -> Dict[str, Any]:
        """Atomically count today's model attempts and record one if under limit.

        The count and the insert share one BEGIN IMMEDIATE transaction, so
        concurrent workers (separate connections) can never both spend the
        last slot. The recorded event IS the reservation — no model call may
        start without it — and a denial records nothing, so denied checks
        never consume budget. R07: the budget day is the EXPLICIT
        Asia/Shanghai business date (fixed midnight boundary), counted by
        parsing each attempt event's timestamp — never by a host-local
        date-prefix match.
        """
        from ..runtime_layout import business_date, business_date_key

        today = business_date_key()
        self._begin()
        try:
            row = self._task_row(task_id)
            self._assert_worker(row, worker_id, fencing_token)
            count = 0
            for event in self._conn.execute(
                "SELECT created_at FROM task_outbox WHERE event_type = 'research_attempt_started'"
            ).fetchall():
                try:
                    stamp = datetime.fromisoformat(str(event["created_at"] or ""))
                except ValueError:
                    continue
                if stamp.tzinfo is None:
                    stamp = stamp.astimezone()
                if business_date(stamp).isoformat() == today:
                    count += 1
            if daily_limit > 0 and count >= int(daily_limit):
                self._rollback()
                return {
                    "reserved": False,
                    "reason": "daily model-call budget exhausted: {0}/{1} attempts today".format(
                        count, daily_limit),
                    "calls_today": count,
                    "limit": int(daily_limit),
                }
            event_id = self._emit(task_id, "research_attempt_started", actor=worker_id,
                                  payload=dict(attempt or {}))
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return {"reserved": True, "reason": "ok",
                "calls_today": count + 1, "limit": int(daily_limit), "event_id": event_id}

    def record_research_round(self, task_id: str, *, worker_id: str,
                              fencing_token: int, record: Dict[str, Any]) -> Dict[str, Any]:
        """Persist one completed research round (intents, results, usage)."""
        self._begin()
        try:
            row = self._task_row(task_id)
            self._assert_worker(row, worker_id, fencing_token)
            self._emit(task_id, "research_round", actor=worker_id, payload=dict(record or {}))
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return self.get_task(task_id)

    # -------- outbound invocation attempt chain (R02) --------

    def _attempt_row(self, attempt_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM task_invocation_attempts WHERE attempt_id = ?", (str(attempt_id),)
        ).fetchone()
        if row is None:
            raise TaskRuntimeError("unknown attempt_id: {0}".format(attempt_id))
        return row

    def prepare_invocation_attempt(self, task_id: str, *, worker_id: str, fencing_token: int,
                                   round_index: int, provider: str, request_model: str,
                                   prompt_sha256: str, budget_event_id: str = "",
                                   deadline_seconds: int = 0) -> Dict[str, Any]:
        """Write the attempt row BEFORE the external request may leave.

        'prepared' means reserved and about to dispatch: nothing has been sent
        yet, so a crash between prepare and dispatch is provably charge-free.
        The budget reservation event id is linked here, tying the attempt chain
        to the budget ledger in one place.
        """
        self._begin()
        try:
            row = self._task_row(task_id)
            self._assert_worker(row, worker_id, fencing_token)
            attempt_id = _new_id("att")
            self._conn.execute(
                "INSERT INTO task_invocation_attempts (attempt_id, task_id, worker_id, fencing_token, round_index, provider, request_model, prompt_sha256, budget_event_id, state, sent, deadline_seconds, started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (attempt_id, task_id, str(worker_id), int(fencing_token), int(round_index),
                 str(provider or ""), str(request_model or ""), str(prompt_sha256 or ""),
                 str(budget_event_id or ""), ATTEMPT_PREPARED, "no", int(deadline_seconds), _now()),
            )
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return dict(self._attempt_row(attempt_id))

    def mark_invocation_dispatched(self, attempt_id: str, *, worker_id: str, fencing_token: int) -> Dict[str, Any]:
        """prepared -> dispatched: from here on, 'was it sent?' can only be answered
        by the terminal record or a reconciliation — never assumed 'no'."""
        self._begin()
        try:
            attempt = self._attempt_row(attempt_id)
            row = self._task_row(str(attempt["task_id"]))
            self._assert_worker(row, worker_id, fencing_token)
            if attempt["state"] != ATTEMPT_PREPARED:
                raise TaskTransitionError(
                    "invocation attempt {0} is {1}, only prepared can dispatch".format(attempt_id, attempt["state"]))
            self._conn.execute(
                "UPDATE task_invocation_attempts SET state = ?, sent = 'unknown' WHERE attempt_id = ?",
                (ATTEMPT_DISPATCHED, str(attempt_id)),
            )
            self._emit(str(attempt["task_id"]), "invocation_dispatched", actor=str(worker_id),
                       payload={"attempt_id": str(attempt_id), "round_index": int(attempt["round_index"])})
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return dict(self._attempt_row(attempt_id))

    def complete_invocation_attempt(self, attempt_id: str, *, worker_id: str, fencing_token: int,
                                    state: str, sent: str, error: str = "",
                                    heartbeat_beats: int = 0, heartbeat_healthy: bool = True) -> Dict[str, Any]:
        """dispatched -> succeeded | failed_known | outcome_unknown (terminal).

        ``sent`` records the delivery fact: 'no' failures are safe to retry;
        'unknown' means the request may have reached (and been charged by) the
        provider and must never be auto-retried.
        """
        if state not in {ATTEMPT_SUCCEEDED, ATTEMPT_FAILED_KNOWN, ATTEMPT_OUTCOME_UNKNOWN}:
            raise TaskRuntimeError("attempt completion state must be succeeded, failed_known or outcome_unknown")
        if sent not in ATTEMPT_SENT_VALUES:
            raise TaskRuntimeError("sent must be one of {0}".format(sorted(ATTEMPT_SENT_VALUES)))
        if state == ATTEMPT_OUTCOME_UNKNOWN and sent != "unknown":
            raise TaskRuntimeError("outcome_unknown requires sent='unknown'")
        self._begin()
        try:
            attempt = self._attempt_row(attempt_id)
            row = self._task_row(str(attempt["task_id"]))
            self._assert_worker(row, worker_id, fencing_token)
            if attempt["state"] != ATTEMPT_DISPATCHED:
                raise TaskTransitionError(
                    "invocation attempt {0} is {1}, only dispatched can complete".format(attempt_id, attempt["state"]))
            self._conn.execute(
                "UPDATE task_invocation_attempts SET state = ?, sent = ?, error = ?, ended_at = ?, heartbeat_beats = ?, heartbeat_healthy = ? WHERE attempt_id = ?",
                (state, sent, str(error or "")[:2000], _now(), int(heartbeat_beats),
                 1 if heartbeat_healthy else 0, str(attempt_id)),
            )
            event_type = {
                ATTEMPT_SUCCEEDED: "invocation_succeeded",
                ATTEMPT_FAILED_KNOWN: "invocation_failed_known",
                ATTEMPT_OUTCOME_UNKNOWN: "invocation_outcome_unknown",
            }[state]
            self._emit(str(attempt["task_id"]), event_type, actor=str(worker_id),
                       payload={"attempt_id": str(attempt_id), "sent": sent,
                                "error": str(error or "")[:500]})
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return dict(self._attempt_row(attempt_id))

    def invocation_attempts(self, task_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM task_invocation_attempts WHERE task_id = ? ORDER BY started_at, attempt_id",
            (str(task_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def outstanding_unknown_attempts(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM task_invocation_attempts WHERE state = ? ORDER BY started_at",
            (ATTEMPT_OUTCOME_UNKNOWN,),
        ).fetchall()
        return [dict(row) for row in rows]

    def recover_stale_invocation_attempts(self, task_id: str, *, error: str = "") -> Dict[str, int]:
        """System-level crash recovery for a dead worker's attempts (no lease assert).

        A DISPATCHED attempt whose worker died is outcome_unknown: the request
        may have been sent, so it enters reconciliation exactly like a timeout.
        A still-PREPARED attempt provably never left (dispatch marks the send
        point), so it closes as failed_known sent='no' and is safe to retry.
        """
        counts = {"outcome_unknown": 0, "failed_known_not_sent": 0}
        self._begin()
        try:
            row = self._task_row(task_id)
            for attempt in self._conn.execute(
                "SELECT * FROM task_invocation_attempts WHERE task_id = ? AND state IN (?, ?)",
                (str(task_id), ATTEMPT_PREPARED, ATTEMPT_DISPATCHED),
            ).fetchall():
                if attempt["state"] == ATTEMPT_DISPATCHED:
                    self._conn.execute(
                        "UPDATE task_invocation_attempts SET state = ?, sent = 'unknown', error = ?, ended_at = ? WHERE attempt_id = ?",
                        (ATTEMPT_OUTCOME_UNKNOWN, str(error or "worker died with request in flight")[:2000],
                         _now(), attempt["attempt_id"]),
                    )
                    self._emit(task_id, "invocation_outcome_unknown", actor="system:recovery",
                               payload={"attempt_id": attempt["attempt_id"], "sent": "unknown",
                                        "reason": "worker_died_dispatched"})
                    counts["outcome_unknown"] += 1
                else:
                    self._conn.execute(
                        "UPDATE task_invocation_attempts SET state = ?, sent = 'no', error = ?, ended_at = ? WHERE attempt_id = ?",
                        (ATTEMPT_FAILED_KNOWN, str(error or "worker died before dispatch")[:2000],
                         _now(), attempt["attempt_id"]),
                    )
                    self._emit(task_id, "invocation_failed_known", actor="system:recovery",
                               payload={"attempt_id": attempt["attempt_id"], "sent": "no",
                                        "reason": "worker_died_before_dispatch"})
                    counts["failed_known_not_sent"] += 1
            self._commit()
        except BaseException:
            self._rollback()
            raise
        _ = row  # task existence asserted; no task mutation happens here
        return counts

    def reconcile_invocation_attempt(self, attempt_id: str, *, actor: str, resolution: str,
                                     reason: str, requeue_if_blocked: bool = False) -> Dict[str, Any]:
        """Close an outcome_unknown attempt with a real receipt or a human decision.

        Never invents a charge fact: the caller must bring the resolution
        (provider receipt said charged / a human with authority said not
        charged) and a non-empty reason; both are recorded with the actor and
        timestamp so responsibility is auditable. ``requeue_if_blocked`` only
        acts when the task still sits in blocked — it can never resurrect a
        terminal task. The budget ledger is NOT refunded here: a
        confirmed_not_charged reconciliation gates a retry; it does not erase
        the reservation history.
        """
        actor = str(actor or "").strip()
        if not actor:
            raise TaskRuntimeError("reconciliation actor must not be empty")
        if resolution not in RECONCILIATION_RESOLUTIONS:
            raise TaskRuntimeError("resolution must be one of {0}".format(sorted(RECONCILIATION_RESOLUTIONS)))
        if not str(reason or "").strip():
            raise TaskRuntimeError("reconciliation reason must not be empty")
        self._begin()
        try:
            attempt = self._attempt_row(attempt_id)
            task_id = str(attempt["task_id"])
            if attempt["state"] == ATTEMPT_RECONCILED:
                if str(attempt["reconciliation"]) != resolution:
                    self._rollback()
                    raise TaskIdempotencyConflictError(
                        "attempt {0} was already reconciled as {1}".format(attempt_id, attempt["reconciliation"]))
                self._rollback()
                result = dict(attempt)
                result["replay"] = True
                return result
            if attempt["state"] != ATTEMPT_OUTCOME_UNKNOWN:
                self._rollback()
                raise TaskTransitionError(
                    "only outcome_unknown attempts can be reconciled; attempt is {0}".format(attempt["state"]))
            self._conn.execute(
                "UPDATE task_invocation_attempts SET state = ?, reconciled_at = ?, reconciled_by = ?, reconciliation = ?, reconciliation_reason = ? WHERE attempt_id = ?",
                (ATTEMPT_RECONCILED, _now(), actor, resolution, str(reason or "")[:2000], str(attempt_id)),
            )
            self._emit(task_id, "invocation_reconciled", actor=actor, command_id="",
                       payload={"attempt_id": str(attempt_id), "resolution": resolution,
                                "reason": str(reason or "")[:500]})
            requeued = False
            if requeue_if_blocked and resolution == "confirmed_not_charged":
                task = self._task_row(task_id)
                if task["status"] == STATUS_BLOCKED:
                    self._check_transition(STATUS_BLOCKED, STATUS_QUEUED)
                    self._touch(task_id, status=STATUS_QUEUED, worker_id="", lease_expires_at="",
                                last_error="requeued after confirmed_not_charged reconciliation of {0}".format(attempt_id))
                    self._emit(task_id, "task_requeued", actor=actor,
                               payload={"reason": "invocation reconciled as not charged",
                                        "attempt_id": str(attempt_id)})
                    requeued = True
            self._commit()
        except BaseException:
            self._rollback()
            raise
        result = dict(self._attempt_row(attempt_id))
        result["requeued"] = requeued
        return result

    # -------- principal verification receipts (R03: atomic consumption) ------

    def register_principal_verification(self, verification_id: str, *, actor: str,
                                        role: str = "", task_id: str = "", action: str = "",
                                        expires_at: str) -> Dict[str, Any]:
        """Backend-issued one-command receipt, transactionally claimable.

        The JSONL ledger in data/dify keeps its audit copy; THIS row is the
        claim authority: two concurrent commands can never both consume one
        receipt (the UPDATE is guarded by used_at='').
        """
        verification_id = str(verification_id or "").strip()
        actor = str(actor or "").strip()
        if not verification_id or not actor:
            raise TaskRuntimeError("principal verification requires verification_id and actor")
        if not str(expires_at or "").strip():
            raise TaskRuntimeError("principal verification requires expires_at")
        record = {
            "verification_id": verification_id, "actor": actor, "role": str(role or ""),
            "task_id": str(task_id or ""), "action": str(action or ""),
            "expires_at": str(expires_at), "command_id": "", "used_at": "",
            "issued_at": _now(),
        }
        self._begin()
        try:
            existing = self._conn.execute(
                "SELECT * FROM principal_verifications WHERE verification_id = ?",
                (verification_id,)).fetchone()
            if existing is not None:
                same = all(str(existing[key]) == record[key]
                           for key in ("actor", "role", "task_id", "action", "expires_at"))
                if not same:
                    self._rollback()
                    raise TaskIdempotencyConflictError(
                        "verification {0} was issued for a different principal/scope".format(verification_id))
                self._rollback()
                return dict(existing)
            self._conn.execute(
                "INSERT INTO principal_verifications (verification_id, actor, role, task_id, action, expires_at, command_id, used_at, issued_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (record["verification_id"], record["actor"], record["role"], record["task_id"],
                 record["action"], record["expires_at"], "", "", record["issued_at"]),
            )
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return record

    def _claim_verification_in_txn(self, verification_id: str, *, actor: str,
                                   command_id: str, task_id: str = "", action: str = "",
                                   issued_fallback: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Claim logic for an ALREADY-OPEN transaction (no begin/commit here).

        This is what makes the claim atomic with the task mutation it guards:
        record_human_response / request_cancel call it inside their own BEGIN
        IMMEDIATE, so consumption and the state change commit or roll back
        together (F07)."""
        verification_id = str(verification_id or "").strip()
        actor = str(actor or "").strip()
        command_id = str(command_id or "")
        row = self._conn.execute(
            "SELECT * FROM principal_verifications WHERE verification_id = ?",
            (verification_id,)).fetchone()
        if row is None and issued_fallback is not None:
            fb = dict(issued_fallback)
            self._conn.execute(
                "INSERT OR IGNORE INTO principal_verifications (verification_id, actor, role, task_id, action, expires_at, command_id, used_at, issued_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (verification_id, str(fb.get("actor") or ""), str(fb.get("role") or ""),
                 str(fb.get("task_id") or ""), str(fb.get("action") or ""),
                 str(fb.get("expires_at") or ""), "", "", str(fb.get("issued_at") or _now())),
            )
            row = self._conn.execute(
                "SELECT * FROM principal_verifications WHERE verification_id = ?",
                (verification_id,)).fetchone()
        if row is None:
            return {"claimed": False, "reason": "principal_verification_unknown"}
        if str(row["actor"]) != actor:
            return {"claimed": False, "reason": "principal_actor_mismatch"}
        if str(row["task_id"]) and task_id and str(row["task_id"]) != str(task_id):
            return {"claimed": False, "reason": "principal_task_mismatch"}
        if str(row["action"]) and action and str(row["action"]) != str(action):
            return {"claimed": False, "reason": "principal_action_mismatch"}
        try:
            expiry = datetime.fromisoformat(str(row["expires_at"]))
        except ValueError:
            return {"claimed": False, "reason": "principal_verification_invalid"}
        if expiry.tzinfo is None:
            expiry = expiry.astimezone()
        if expiry <= datetime.now().astimezone():
            return {"claimed": False, "reason": "principal_verification_expired"}
        if str(row["used_at"]):
            if str(row["command_id"]) == command_id:
                # Idempotent re-claim for the SAME command: the task-level
                # command receipt above this layer deduplicates the effect.
                return {"claimed": True, "reason": "already_claimed_same_command"}
            return {"claimed": False, "reason": "principal_verification_already_used"}
        changed = self._conn.execute(
            "UPDATE principal_verifications SET used_at = ?, command_id = ? "
            "WHERE verification_id = ? AND used_at = ''",
            (_now(), command_id, verification_id),
        ).rowcount
        if changed != 1:
            # Another writer claimed it between the read and the guarded
            # UPDATE — this is the A12 race loser.
            return {"claimed": False, "reason": "principal_verification_already_used"}
        return {"claimed": True, "reason": "ok",
                "verification": {key: str(row[key]) for key in
                                 ("verification_id", "actor", "task_id", "action", "expires_at")}}

    def claim_principal_verification(self, verification_id: str, *, actor: str,
                                     command_id: str, task_id: str = "", action: str = "",
                                     issued_fallback: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Atomically consume a receipt: exactly one command ever succeeds.

        ``issued_fallback`` carries a legacy (JSONL-only) issuance so receipts
        issued before this table existed migrate in on first use — insert then
        claim inside ONE transaction. A stale row is never resurrected.
        """
        self._begin()
        try:
            verdict = self._claim_verification_in_txn(
                verification_id, actor=actor, command_id=command_id,
                task_id=task_id, action=action, issued_fallback=issued_fallback)
            if verdict.get("claimed"):
                self._commit()
            else:
                self._rollback()
        except BaseException:
            self._rollback()
            raise
        return verdict

    # -------- backend-issued workflow dispatches (R03: adoption binding) -----

    def issue_workflow_dispatch(self, task_id: str, *, input_version: int, bundle_id: str,
                                bundle_hash: str, workflow_version: str,
                                actor: str = "backend") -> Dict[str, Any]:
        """Backend issues the ONLY credential a platform run may submit against.

        The receipt freezes the full binding at issue time: task row version
        (generation), fencing token, input_version, bundle identity and
        workflow version. Any task mutation bumps the row version, so a late
        submission against an old generation is detectable (A15)."""
        self._begin()
        try:
            row = self._task_row(task_id)
            dispatch_id = _new_id("wd")
            record = {
                "dispatch_id": dispatch_id, "task_id": task_id,
                "issued_generation": int(row["version"]),
                "input_version": int(input_version),
                "bundle_id": str(bundle_id or ""), "bundle_hash": str(bundle_hash or ""),
                "workflow_version": str(workflow_version or ""),
                "fencing_token": int(row["fencing_token"]),
                "issued_at": _now(), "consumed_at": "", "consumed_by": "",
            }
            self._conn.execute(
                "INSERT INTO workflow_dispatches (dispatch_id, task_id, issued_generation, input_version, bundle_id, bundle_hash, workflow_version, fencing_token, issued_at, consumed_at, consumed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (record["dispatch_id"], record["task_id"], record["issued_generation"],
                 record["input_version"], record["bundle_id"], record["bundle_hash"],
                 record["workflow_version"], record["fencing_token"], record["issued_at"], "", ""),
            )
            self._emit(task_id, "workflow_dispatch_issued", actor=str(actor or "backend"),
                       payload={"dispatch_id": dispatch_id,
                                "issued_generation": record["issued_generation"],
                                "input_version": record["input_version"],
                                "bundle_id": record["bundle_id"],
                                "workflow_version": record["workflow_version"]})
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return record

    def validate_workflow_dispatch(self, dispatch_id: str, *, task_id: str,
                                   input_version: int, bundle_id: str,
                                   workflow_version: str) -> Dict[str, Any]:
        """Read-only full-binding validation; returns {ok, reason, dispatch}."""
        row = self._conn.execute(
            "SELECT * FROM workflow_dispatches WHERE dispatch_id = ?", (str(dispatch_id),)
        ).fetchone()
        if row is None:
            return {"ok": False, "reason": "dispatch_unknown", "dispatch": None}
        dispatch = dict(row)
        if str(row["task_id"]) != str(task_id):
            return {"ok": False, "reason": "dispatch_task_mismatch", "dispatch": dispatch}
        if str(row["consumed_at"]):
            return {"ok": False, "reason": "dispatch_already_consumed", "dispatch": dispatch}
        task = self._task_row(str(task_id))
        if int(task["version"]) != int(row["issued_generation"]):
            # The task moved on (input patch, cancel request, state change):
            # this dispatch was issued against a world that no longer exists.
            # Generation is checked BEFORE bindings on purpose — comparing
            # bindings against a dead generation would be meaningless.
            return {"ok": False, "reason": "dispatch_generation_stale", "dispatch": dispatch}
        if str(row["workflow_version"]) != str(workflow_version or "") \
                or str(row["bundle_id"]) != str(bundle_id or "") \
                or int(row["input_version"]) != int(input_version):
            return {"ok": False, "reason": "dispatch_binding_mismatch", "dispatch": dispatch}
        if str(task["status"]) == STATUS_CANCELLED or task["status"] in TERMINAL_STATUSES:
            return {"ok": False, "reason": "dispatch_task_not_active", "dispatch": dispatch}
        return {"ok": True, "reason": "ok", "dispatch": dispatch}

    def consume_workflow_dispatch(self, dispatch_id: str, *, task_id: str,
                                  input_version: int, bundle_id: str,
                                  workflow_version: str, actor: str,
                                  result_ref: str = "") -> Dict[str, Any]:
        """Adoption step: re-validate inside the write transaction, then mark
        consumed exactly once (a second adoption of the same dispatch fails)."""
        self._begin()
        try:
            verdict = self.validate_workflow_dispatch(
                dispatch_id, task_id=task_id, input_version=int(input_version),
                bundle_id=str(bundle_id or ""), workflow_version=str(workflow_version or ""))
            if not verdict["ok"]:
                self._rollback()
                raise TaskTransitionError(
                    "workflow dispatch rejected: {0}".format(verdict["reason"]))
            changed = self._conn.execute(
                "UPDATE workflow_dispatches SET consumed_at = ?, consumed_by = ? "
                "WHERE dispatch_id = ? AND consumed_at = ''",
                (_now(), str(actor or ""), str(dispatch_id)),
            ).rowcount
            if changed != 1:
                self._rollback()
                raise TaskTransitionError("workflow dispatch rejected: dispatch_already_consumed")
            self._emit(str(task_id), "workflow_dispatch_consumed", actor=str(actor or ""),
                       payload={"dispatch_id": str(dispatch_id), "result_ref": str(result_ref or "")[:200]})
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return dict(self._conn.execute(
            "SELECT * FROM workflow_dispatches WHERE dispatch_id = ?", (str(dispatch_id),)
        ).fetchone())

    def cancel_if_requested(self, task_id: str, *, worker_id: str, fencing_token: int) -> Dict[str, Any]:
        """Worker-side cancellation point: transition to cancelled when the flag is set.

        Returns the task unchanged (no transition) when no cancellation was
        requested; a stale worker is rejected by fencing like any other action.
        """
        self._begin()
        try:
            row = self._task_row(task_id)
            self._assert_worker(row, worker_id, fencing_token)
            if row["cancel_requested"] and row["status"] in (STATUS_LEASED, STATUS_RUNNING):
                self._touch(task_id, status=STATUS_CANCELLED, worker_id=worker_id)
                self._emit(task_id, "task_cancelled", actor=worker_id,
                           payload={"reason": "cancel requested during research run"})
                self._commit()
                return self.get_task(task_id)
            self._rollback()
            return self._decode(row)
        except BaseException:
            self._rollback()
            raise
