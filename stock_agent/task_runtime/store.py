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

TASK_SCHEMA_VERSION = "1.0"

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
    STATUS_LEASED: {STATUS_RUNNING, STATUS_QUEUED, STATUS_EXPIRED, STATUS_CANCELLED},
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
        prompt_version: str = "research_v1",
        model_config_hash: str = "",
        max_attempts: int = 2,
        execution_mode: str = "live",
        actor: str = "operator",
        consume_event: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, Any], str]:
        """Create (or deduplicate) a task; optionally record its event consumption atomically.

        ``consume_event`` = {event_key, purpose, base_key, signal_id} is written
        in the SAME transaction as the task row, closing the crash window where
        a source was read but its task/consumption pair never committed. On the
        DUPLICATE path a missing consumption row converges (recorded now, bound
        to the existing task) so rescans reconcile the ledger instead of
        creating a second task or losing the event.
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
                case_id, role_id, input_payload.get("input_version", 1),
                prompt_version, model_config_hash, execution_mode, content_fingerprint,
            )
        created = _now()
        self._begin()
        try:
            existing = self._conn.execute(
                "SELECT * FROM research_tasks WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                try:
                    stored_payload = json.loads(existing["input_payload"] or "{}")
                except json.JSONDecodeError:
                    stored_payload = None
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

                same_logical_request = same_content or (
                    consumption is not None
                    and isinstance(stored_payload, dict)
                    and str((stored_payload.get("origin") or {}).get("event_key") or "")
                    == consumption["event_key"]
                ) or (
                    isinstance(stored_payload, dict)
                    and _same_logical(stored_payload, input_payload)
                )
                if str(existing["execution_mode"]) != execution_mode or not same_logical_request:
                    self._rollback()
                    raise TaskIdempotencyConflictError(
                        "idempotency key {0} already names a different request".format(idempotency_key)
                    )
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
                        self._commit()
                else:
                    self._rollback()
                return self._decode(existing), "DUPLICATE"
            task_id = _new_id("task")
            self._conn.execute(
                """INSERT INTO research_tasks
                   (task_id, parent_task_id, case_id, role_id, title, input_payload, idempotency_key,
                    prompt_version, model_config_hash, max_attempts, execution_mode, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, parent_task_id, case_id, role_id, title,
                 json.dumps(input_payload, ensure_ascii=False, default=str), idempotency_key,
                 prompt_version, model_config_hash, max(1, int(max_attempts)), execution_mode, created, created),
            )
            if consumption is not None:
                self._conn.execute(
                    "INSERT INTO signal_consumptions (event_key, purpose, base_key, signal_id, task_id, consumed_at) VALUES (?,?,?,?,?,?)",
                    (consumption["event_key"], consumption["purpose"], consumption["base_key"],
                     consumption["signal_id"], task_id, created),
                )
            self._emit(task_id, "task_created", actor=actor, payload={"title": title, "case_id": case_id, "execution_mode": execution_mode})
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

    def request_cancel(self, task_id: str, *, actor: str, reason: str, expected_version: int) -> Dict[str, Any]:
        if not str(actor or "").strip():
            raise TaskRuntimeError("actor must not be empty")
        self._begin()
        try:
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
                self._touch(
                    task_id,
                    status=STATUS_QUEUED,
                    input_version=int(row["input_version"]) + 1,
                    input_payload=json.dumps(merged, ensure_ascii=False, default=str),
                    worker_id="",
                )
                self._emit(task_id, "human_response_recorded", actor=actor, command_id=command_id,
                           payload={"decision": decision, "input_version": int(row["input_version"]) + 1})
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
                if fresh["cancel_requested"]:
                    self._touch(task_id, status=STATUS_CANCELLED, worker_id="", lease_expires_at="",
                                last_error="cancelled during abandoned-lease recovery")
                    self._emit(task_id, "task_recovered", payload={"action": "cancelled", "expired_at": fresh_expires})
                    self._commit()
                    recovered.append(self.get_task(task_id))
                    continue
                attempts_left = int(fresh["max_attempts"]) - int(fresh["attempt_count"])
                if attempts_left > 0:
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
