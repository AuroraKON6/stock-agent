"""Durable run leases and checkpoints for the research pipeline.

This module is deliberately small and local.  It coordinates process-level
execution and records metadata about progress; it does not make research,
approval, publication, trading, or portfolio decisions.  A stale lease is
never silently stolen.  An operator must explicitly run ``run-recover-stale``
with a reason before a new process can use that mode.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .audit_log import (
    _append_lock,
    atomic_write_text,
    file_hash,
    load_hash_chained_jsonl,
    object_hash,
)
from .config import project_path


RUN_COORDINATION_VERSION = "1.0"
RUN_COORDINATION_POLICY_VERSION = "33.0"
SAFETY_BOUNDARY = (
    "Local execution coordination only. It never trades, places orders, changes "
    "positions or configuration, approves research, publishes output, installs "
    "schedules, or silently takes over a stale run."
)

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,96}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\b(?:bearer|token|password|api[_-]?key|secret)\s*[:=]\s*[^\s,;]+"),
)
_ALLOWED_EVENT_FIELDS = {
    "started_at",
    "expired_at",
    "archived_lease_path",
    "status",
    "checkpoint_count",
    "pid",
    "error",
    "error_type",
    "reason",
    "message",
    "stage",
    "idempotency_key",
    "input_fingerprint",
    "output_refs",
}


class RunCoordinationError(RuntimeError):
    """Base error for coordination failures."""


class UnsafeCoordinationPathError(RunCoordinationError):
    pass


class LeaseConflictError(RunCoordinationError):
    def __init__(self, message: str, *, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


class LeaseOwnershipError(RunCoordinationError):
    pass


class ActiveLeaseError(RunCoordinationError):
    pass


class CheckpointIntegrityError(RunCoordinationError):
    pass


class CheckpointConflictError(RunCoordinationError):
    pass


def coordination_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return normalized coordination settings.

    The default is disabled so isolated callers and legacy configurations keep
    their previous behavior.  Production/local configuration can opt in
    explicitly.
    """

    raw = ((config or {}).get("run_coordination") or {}) if isinstance(config or {}, dict) else {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "lease_root": str(raw.get("lease_root") or "data/run_coordination/leases"),
        "recovered_lease_root": str(raw.get("recovered_lease_root") or "data/run_coordination/recovered"),
        "checkpoint_path": str(raw.get("checkpoint_path") or "data/run_coordination/checkpoints.jsonl"),
        "report_dir": str(raw.get("report_dir") or "reports/run_coordination"),
        "lease_ttl_seconds": max(1.0, float(raw.get("lease_ttl_seconds", 900) or 900)),
        "require_explicit_stale_recovery": bool(raw.get("require_explicit_stale_recovery", True)),
        "fail_fast_on_lease_conflict": bool(raw.get("fail_fast_on_lease_conflict", True)),
    }


def _workspace_root() -> Path:
    return project_path(".").resolve()


def _controlled_path(value: Union[str, Path], label: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise UnsafeCoordinationPathError("{0} must not be empty".format(label))
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = project_path(raw)
    try:
        resolved = candidate.resolve()
        resolved.relative_to(_workspace_root())
    except (OSError, ValueError) as exc:
        raise UnsafeCoordinationPathError(
            "{0} must remain under the workspace".format(label)
        ) from exc
    return resolved


def _safe_mode(mode: str) -> str:
    value = str(mode or "").strip()
    if not _SAFE_TOKEN.fullmatch(value):
        raise RunCoordinationError("invalid run mode")
    return value


def _safe_stage(stage: str) -> str:
    value = str(stage or "").strip()
    if not _SAFE_TOKEN.fullmatch(value):
        raise RunCoordinationError("invalid checkpoint stage")
    return value


def _safe_event_type(event_type: str) -> str:
    value = str(event_type or "").strip()
    if not _SAFE_TOKEN.fullmatch(value):
        raise RunCoordinationError("invalid coordination event type")
    return value


def _redact(value: Any, limit: int = 512) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text[:limit]


def _now(value: Optional[datetime] = None) -> datetime:
    current = value or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current


def _iso(value: Optional[datetime] = None) -> str:
    return _now(value).isoformat(timespec="seconds")


def _parse_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_expired(value: Any, now: Optional[datetime] = None) -> bool:
    expires = _parse_time(value)
    return expires is None or expires <= _now(now)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunCoordinationError("coordination metadata is unreadable") from exc
    if not isinstance(value, dict):
        raise RunCoordinationError("coordination metadata must be an object")
    return value


def _atomic_json(path: Path, value: Dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _relative_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(_workspace_root()).as_posix()
    except ValueError as exc:
        raise UnsafeCoordinationPathError("path escapes the workspace") from exc


def _lease_display_path(path: Path, lease_root: Path) -> str:
    """Return a non-throwing diagnostic path, including for unsafe symlinks."""

    try:
        return _relative_path(path)
    except UnsafeCoordinationPathError:
        try:
            return (Path(path).absolute().relative_to(Path(lease_root).absolute())).as_posix()
        except ValueError:
            return Path(path).name


def _validate_hash(value: Any, label: str) -> str:
    text = str(value or "").strip().lower()
    if not _SHA256.fullmatch(text):
        raise RunCoordinationError("{0} must be a SHA-256 fingerprint".format(label))
    return text


def fingerprint(value: Any) -> str:
    """Return a deterministic SHA-256 fingerprint for metadata-only inputs."""

    return object_hash(value)


class RunLease:
    """One per-mode, process-level lease backed by an atomically created file."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]],
        mode: str,
        *,
        run_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> None:
        self.config = coordination_config(config)
        self.mode = _safe_mode(mode)
        self.path = _controlled_path(self.config["lease_root"], "lease_root") / (self.mode + ".json")
        self.run_id = str(run_id or new_run_id(self.mode))
        self._token = secrets.token_urlsafe(32)
        self._token_hash = hashlib.sha256(self._token.encode("utf-8")).hexdigest()
        self.acquired = False
        self.released = False
        self.acquired_at = _now(now)

    @property
    def token_hash(self) -> str:
        """Expose only the digest for diagnostics; the raw token never leaves memory."""

        return self._token_hash

    def _metadata(self, current: datetime) -> Dict[str, Any]:
        expires = current + timedelta(seconds=self.config["lease_ttl_seconds"])
        return {
            "schema_version": RUN_COORDINATION_VERSION,
            "policy_version": RUN_COORDINATION_POLICY_VERSION,
            "mode": self.mode,
            "run_id": self.run_id,
            "token_sha256": self._token_hash,
            "acquired_at": _iso(self.acquired_at),
            "heartbeat_at": _iso(current),
            "expires_at": _iso(expires),
            "state": "ACTIVE",
            "pid": int(os.getpid()),
            "host": _redact(socket.gethostname(), 128),
            "safety_boundary": SAFETY_BOUNDARY,
        }

    def acquire(self) -> "RunLease":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        metadata = self._metadata(self.acquired_at)
        encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        descriptor = -1
        try:
            descriptor = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        except FileExistsError as exc:
            details: Dict[str, Any] = {"mode": self.mode, "lease_path": _relative_path(self.path)}
            try:
                existing = _read_json(self.path)
                details.update({
                    "existing_run_id": str(existing.get("run_id") or ""),
                    "existing_expires_at": str(existing.get("expires_at") or ""),
                    "stale": _is_expired(existing.get("expires_at"), self.acquired_at),
                })
            except RunCoordinationError:
                details["stale"] = False
                details["malformed"] = True
            raise LeaseConflictError(
                "run mode is already leased; use explicit run-recover-stale only after confirming expiry",
                details=details,
            ) from exc
        except OSError as exc:
            raise RunCoordinationError("unable to create run lease") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        self.acquired = True
        self.released = False
        return self

    def _assert_owner(self) -> Dict[str, Any]:
        if not self.acquired or self.released:
            raise LeaseOwnershipError("run lease is not owned by this process")
        if not self.path.exists():
            raise LeaseOwnershipError("run lease no longer exists")
        current = _read_json(self.path)
        if (
            str(current.get("run_id") or "") != self.run_id
            or str(current.get("token_sha256") or "") != self._token_hash
        ):
            raise LeaseOwnershipError("run lease ownership has changed")
        return current

    def heartbeat(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        self._assert_owner()
        current = _now(now)
        metadata = self._metadata(current)
        _atomic_json(self.path, metadata)
        return metadata

    def release(self) -> bool:
        if not self.acquired or self.released:
            return False
        self._assert_owner()
        try:
            self.path.unlink()
        except FileNotFoundError:
            self.released = True
            return False
        self.released = True
        return True

    def __enter__(self) -> "RunLease":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


def new_run_id(mode: str) -> str:
    return "{0}_{1}_{2}".format(
        _safe_mode(mode),
        datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z"),
        uuid.uuid4().hex[:12],
    )


def _normalize_output_refs(refs: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for item in refs or []:
        if not isinstance(item, dict):
            raise RunCoordinationError("output reference must be an object")
        relative = str(item.get("path") or "").replace("\\", "/").strip()
        if not relative or relative.startswith("/") or ".." in Path(relative).parts:
            raise UnsafeCoordinationPathError("output reference must be workspace-relative")
        digest = _validate_hash(item.get("sha256"), "output sha256")
        try:
            size = max(0, int(item.get("size", 0)))
        except (TypeError, ValueError) as exc:
            raise RunCoordinationError("output size must be an integer") from exc
        normalized.append({"path": relative, "sha256": digest, "size": size})
    return sorted(normalized, key=lambda value: value["path"])


class CheckpointStore:
    """Append-only, hash-chained run ledger with stage idempotency."""

    def __init__(self, config_or_path: Union[Dict[str, Any], str, Path]) -> None:
        if isinstance(config_or_path, (str, Path)):
            self.config = coordination_config({"run_coordination": {"enabled": True, "checkpoint_path": str(config_or_path)}})
        else:
            self.config = coordination_config(config_or_path)
        self.path = _controlled_path(self.config["checkpoint_path"], "checkpoint_path")

    def _load(self) -> Dict[str, Any]:
        result = load_hash_chained_jsonl(self.path)
        if result.get("integrity_status") != "OK":
            raise CheckpointIntegrityError("checkpoint ledger integrity verification failed")
        return result

    def ensure_healthy(self) -> Dict[str, Any]:
        return self._load()

    def _append(self, event: Dict[str, Any]) -> Dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The shared audit helper already defines the chain format.  Holding
        # its lock here lets duplicate checks and append happen atomically.
        with _append_lock(self.path):
            current = load_hash_chained_jsonl(self.path)
            if current.get("integrity_status") != "OK":
                raise CheckpointIntegrityError("checkpoint ledger integrity verification failed")
            record = dict(event)
            record.pop("event_hash", None)
            record["event_id"] = str(record.get("event_id") or uuid.uuid4().hex)
            record["sequence"] = int(current.get("record_count", 0)) + 1
            record["previous_hash"] = str(current.get("last_hash") or "")
            record["event_hash"] = object_hash(record)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return record

    def append_event(self, event_type: str, *, run_id: str = "", mode: str = "", **fields: Any) -> Dict[str, Any]:
        value = {
            "schema_version": RUN_COORDINATION_VERSION,
            "policy_version": RUN_COORDINATION_POLICY_VERSION,
            "event_type": _safe_event_type(event_type),
            "run_id": _redact(run_id, 160),
            "mode": _redact(mode, 96),
            "occurred_at": _iso(),
            "safety_boundary": SAFETY_BOUNDARY,
        }
        for key, item in fields.items():
            if key in {"error", "reason", "message"}:
                value[key] = _redact(item)
            elif key in {"stage", "status", "idempotency_key", "input_fingerprint"}:
                value[key] = _redact(item, 256)
            elif key == "output_refs":
                value[key] = _normalize_output_refs(item)
            elif key in {"checkpoint_count", "pid"}:
                value[key] = int(item or 0)
            elif key in {"error_type"}:
                value[key] = _redact(item, 128)
            elif key in _ALLOWED_EVENT_FIELDS:
                value[key] = _redact(item, 256)
            else:
                # Unknown fields are represented only by a digest.  This
                # prevents a caller from turning the coordination ledger into
                # a research-content or credential storage channel.
                value[key + "_sha256"] = fingerprint(item)
        return self._append(value)

    def start_run(self, run_id: str, mode: str, *, started_at: Optional[datetime] = None) -> Dict[str, Any]:
        chain = self._load()
        for record in chain["records"]:
            if record.get("event_type") == "run_started" and record.get("run_id") == run_id:
                return {"status": "DUPLICATE", "record": record}
        record = self.append_event(
            "run_started",
            run_id=run_id,
            mode=mode,
            started_at=_iso(started_at),
            status="RUNNING",
        )
        return {"status": "RECORDED", "record": record}

    def checkpoint(
        self,
        run_id: str,
        mode: str,
        stage: str,
        input_fingerprint: str,
        *,
        output_refs: Optional[Iterable[Dict[str, Any]]] = None,
        status: str = "COMPLETED",
    ) -> Dict[str, Any]:
        stage = _safe_stage(stage)
        input_fingerprint = _validate_hash(input_fingerprint, "input_fingerprint")
        refs = _normalize_output_refs(output_refs)
        key = "{0}|{1}|{2}".format(run_id, stage, input_fingerprint)
        expected = {
            "run_id": str(run_id),
            "mode": str(mode),
            "stage": stage,
            "input_fingerprint": input_fingerprint,
            "output_refs": refs,
            "status": str(status or "COMPLETED").upper(),
            "idempotency_key": key,
        }
        with _append_lock(self.path):
            current = load_hash_chained_jsonl(self.path)
            if current.get("integrity_status") != "OK":
                raise CheckpointIntegrityError("checkpoint ledger integrity verification failed")
            for record in current["records"]:
                if record.get("event_type") != "checkpoint":
                    continue
                if record.get("idempotency_key") != key:
                    continue
                comparable = {field: record.get(field) for field in expected}
                if comparable == expected:
                    return {"status": "DUPLICATE", "record": record}
                raise CheckpointConflictError("checkpoint idempotency key has conflicting payload")
            # _append() takes the same lock, so write the record inline.
            event = {
                "schema_version": RUN_COORDINATION_VERSION,
                "policy_version": RUN_COORDINATION_POLICY_VERSION,
                "event_type": "checkpoint",
                "run_id": _redact(run_id, 160),
                "mode": _redact(mode, 96),
                "occurred_at": _iso(),
                "stage": stage,
                "input_fingerprint": input_fingerprint,
                "output_refs": refs,
                "status": expected["status"],
                "idempotency_key": key,
                "safety_boundary": SAFETY_BOUNDARY,
            }
            record = self._append_unlocked(event, current)
            return {"status": "RECORDED", "record": record}

    def _append_unlocked(self, event: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
        record = dict(event)
        record.pop("event_hash", None)
        record["event_id"] = str(record.get("event_id") or uuid.uuid4().hex)
        record["sequence"] = int(current.get("record_count", 0)) + 1
        record["previous_hash"] = str(current.get("last_hash") or "")
        record["event_hash"] = object_hash(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def _terminal(self, run_id: str, mode: str, event_type: str, *, output_refs: Optional[Iterable[Dict[str, Any]]] = None, error: str = "") -> Dict[str, Any]:
        if event_type not in {"run_completed", "run_failed"}:
            raise RunCoordinationError("invalid terminal event")
        refs = _normalize_output_refs(output_refs)
        chain = self._load()
        for record in reversed(chain["records"]):
            if record.get("run_id") != run_id:
                continue
            if record.get("event_type") == event_type:
                return {"status": "DUPLICATE", "record": record}
            if record.get("event_type") in {"run_completed", "run_failed"}:
                raise CheckpointConflictError("run already has a different terminal state")
        fields: Dict[str, Any] = {"status": "COMPLETED" if event_type == "run_completed" else "FAILED"}
        if refs:
            fields["output_refs"] = refs
        if error:
            fields["error"] = _redact(error)
            fields["error_type"] = type(error).__name__ if isinstance(error, BaseException) else "RuntimeError"
        record = self.append_event(event_type, run_id=run_id, mode=mode, **fields)
        return {"status": "RECORDED", "record": record}

    def complete(self, run_id: str, mode: str, output_refs: Optional[Iterable[Dict[str, Any]]] = None) -> Dict[str, Any]:
        return self._terminal(run_id, mode, "run_completed", output_refs=output_refs)

    def fail(self, run_id: str, mode: str, error: Any) -> Dict[str, Any]:
        return self._terminal(run_id, mode, "run_failed", error=_redact(error))

    def state_for_run(self, run_id: str) -> Dict[str, Any]:
        chain = self._load()
        records = [record for record in chain["records"] if record.get("run_id") == run_id]
        status = "UNKNOWN"
        for record in records:
            event_type = record.get("event_type")
            if event_type == "run_started":
                status = "RUNNING"
            elif event_type == "run_completed":
                status = "COMPLETED"
            elif event_type == "run_failed":
                status = "FAILED"
        return {
            "run_id": run_id,
            "status": status,
            "record_count": len(records),
            "checkpoints": [record for record in records if record.get("event_type") == "checkpoint"],
            "records": records,
        }


class CoordinatedRun:
    """Own a lease and its durable run ledger for one pipeline invocation."""

    def __init__(self, config: Dict[str, Any], mode: str, *, now: Optional[datetime] = None) -> None:
        self.config = dict(config)
        self.mode = _safe_mode(mode)
        self.run_id = new_run_id(self.mode)
        self.lease = RunLease(config, self.mode, run_id=self.run_id, now=now)
        self.store = CheckpointStore(config)
        self.started_at = _now(now)
        self.status = "NEW"
        self._terminal_record: Optional[Dict[str, Any]] = None
        self._released = False

    def acquire(self) -> "CoordinatedRun":
        self.lease.acquire()
        try:
            self.store.start_run(self.run_id, self.mode, started_at=self.started_at)
        except Exception:
            try:
                self.lease.release()
            except Exception:
                pass
            raise
        self.status = "RUNNING"
        return self

    def heartbeat(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        return self.lease.heartbeat(now=now)

    def checkpoint(
        self,
        stage: str,
        input_fingerprint: str,
        *,
        output_refs: Optional[Iterable[Dict[str, Any]]] = None,
        status: str = "COMPLETED",
    ) -> Dict[str, Any]:
        if self.status not in {"RUNNING"}:
            raise RunCoordinationError("cannot checkpoint a terminal run")
        return self.store.checkpoint(
            self.run_id,
            self.mode,
            stage,
            input_fingerprint,
            output_refs=output_refs,
            status=status,
        )

    def complete(self, output_refs: Optional[Iterable[Dict[str, Any]]] = None) -> Dict[str, Any]:
        if self.status == "COMPLETED":
            return {"status": "DUPLICATE", "record": self._terminal_record or {}}
        if self.status == "FAILED":
            raise CheckpointConflictError("cannot complete a failed run")
        result = self.store.complete(self.run_id, self.mode, output_refs)
        self._terminal_record = result.get("record")
        self.status = "COMPLETED"
        return result

    def fail(self, error: Any) -> Dict[str, Any]:
        if self.status == "FAILED":
            return {"status": "DUPLICATE", "record": self._terminal_record or {}}
        if self.status == "COMPLETED":
            return {"status": "DUPLICATE", "record": self._terminal_record or {}}
        result = self.store.fail(self.run_id, self.mode, error)
        self._terminal_record = result.get("record")
        self.status = "FAILED"
        return result

    def release(self) -> bool:
        if self._released:
            return False
        self._released = True
        return self.lease.release()

    def summary(self) -> Dict[str, Any]:
        checkpoint_count = 0
        try:
            state = self.store.state_for_run(self.run_id)
            checkpoint_count = len(state.get("checkpoints", []))
        except RunCoordinationError:
            pass
        return {
            "schema_version": RUN_COORDINATION_VERSION,
            "policy_version": RUN_COORDINATION_POLICY_VERSION,
            "run_id": self.run_id,
            "mode": self.mode,
            "status": self.status,
            "started_at": _iso(self.started_at),
            "checkpoint_count": checkpoint_count,
            "lease_path": _relative_path(self.lease.path),
            "checkpoint_path": _relative_path(self.store.path),
            "safety_boundary": SAFETY_BOUNDARY,
        }

    def __enter__(self) -> "CoordinatedRun":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if exc is not None and self.status == "RUNNING":
            self.fail(exc)
        self.release()


def begin_coordinated_run(config: Dict[str, Any], mode: str) -> Optional[CoordinatedRun]:
    """Acquire coordination unless disabled or explicitly isolated."""

    cfg = coordination_config(config)
    profile = config.get("run_profile", {}) if isinstance(config, dict) else {}
    if not cfg["enabled"] or (isinstance(profile, dict) and profile.get("isolated")):
        return None
    return CoordinatedRun(config, mode).acquire()


def checkpoint_stage(
    config: Dict[str, Any],
    stage: str,
    metadata: Any,
    *,
    output_refs: Optional[Iterable[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Record a stage using a metadata fingerprint, if a run owns coordination."""

    coordinator = config.get("_run_coordination") if isinstance(config, dict) else None
    if coordinator is None:
        return None
    return coordinator.checkpoint(stage, fingerprint(metadata), output_refs=output_refs)


def checkpoint_output_refs(result: Any) -> List[Dict[str, Any]]:
    """Hash generated files and return references without reading content into the ledger."""

    root = _workspace_root()
    refs: List[Dict[str, Any]] = []
    seen = set()
    for attr in ("report_path", "json_path", "alert_path", "review_path", "actions_path"):
        raw = getattr(result, attr, "") or ""
        if not raw:
            continue
        path = Path(str(raw))
        if not path.is_absolute():
            path = project_path(str(path))
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise UnsafeCoordinationPathError("pipeline output escapes the workspace") from exc
        if relative in seen:
            continue
        seen.add(relative)
        if not resolved.is_file() or resolved.is_symlink():
            continue
        refs.append({"path": relative, "sha256": file_hash(resolved), "size": resolved.stat().st_size})
    return sorted(refs, key=lambda value: value["path"])


def _lease_paths(config: Dict[str, Any]) -> Tuple[Path, Path]:
    cfg = coordination_config(config)
    return (
        _controlled_path(cfg["lease_root"], "lease_root"),
        _controlled_path(cfg["recovered_lease_root"], "recovered_lease_root"),
    )


def recover_stale_lease(
    config: Dict[str, Any],
    mode: str,
    reason: str,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Archive one expired lease after an explicit operator reason."""

    mode = _safe_mode(mode)
    reason_text = _redact(reason, 512).strip()
    if not reason_text:
        raise RunCoordinationError("stale recovery requires a non-empty reason")
    lease_root, recovered_root = _lease_paths(config)
    lease_path = lease_root / (mode + ".json")
    current = _now(now)
    if not lease_path.exists():
        return {
            "status": "NOT_FOUND",
            "mode": mode,
            "lease_path": _relative_path(lease_path),
            "reason": reason_text,
        }
    metadata = _read_json(lease_path)
    if not _is_expired(metadata.get("expires_at"), current):
        raise ActiveLeaseError("active lease cannot be recovered")
    store = CheckpointStore(config)
    store.ensure_healthy()
    recovered_root.mkdir(parents=True, exist_ok=True)
    stamp = current.strftime("%Y%m%dT%H%M%S%z")
    run_token = re.sub(r"[^A-Za-z0-9_.-]", "_", str(metadata.get("run_id") or "unknown"))[:80]
    destination = recovered_root / "{0}_{1}_{2}.json".format(mode, stamp, run_token or uuid.uuid4().hex[:8])
    if destination.exists():
        destination = recovered_root / "{0}_{1}_{2}_{3}.json".format(mode, stamp, run_token, uuid.uuid4().hex[:8])
    os.replace(str(lease_path), str(destination))
    try:
        record = store.append_event(
            "lease_recovered",
            run_id=str(metadata.get("run_id") or ""),
            mode=mode,
            reason=reason_text,
            archived_lease_path=_relative_path(destination),
            expired_at=str(metadata.get("expires_at") or ""),
            status="RECOVERED",
        )
    except Exception:
        # Keep the operation fail-closed if the audit append cannot be made.
        try:
            os.replace(str(destination), str(lease_path))
        except OSError:
            pass
        raise
    return {
        "status": "RECOVERED",
        "mode": mode,
        "run_id": str(metadata.get("run_id") or ""),
        "lease_path": _relative_path(lease_path),
        "archived_path": _relative_path(destination),
        "reason": reason_text,
        "event_id": record.get("event_id", ""),
    }


def _run_states(records: Sequence[Dict[str, Any]], mode: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    states: Dict[str, Dict[str, Any]] = {}
    for record in records:
        run_id = str(record.get("run_id") or "")
        record_mode = str(record.get("mode") or "")
        if not run_id or (mode and record_mode != mode):
            continue
        state = states.setdefault(run_id, {
            "run_id": run_id,
            "mode": record_mode,
            "status": "UNKNOWN",
            "started_at": "",
            "last_at": "",
            "checkpoint_count": 0,
        })
        state["last_at"] = str(record.get("occurred_at") or state["last_at"])
        event_type = record.get("event_type")
        if event_type == "run_started":
            state["status"] = "RUNNING"
            state["started_at"] = str(record.get("started_at") or record.get("occurred_at") or "")
        elif event_type == "checkpoint":
            state["checkpoint_count"] += 1
        elif event_type == "run_completed":
            state["status"] = "COMPLETED"
        elif event_type == "run_failed":
            state["status"] = "FAILED"
    return states


def build_run_coordination_status(
    config: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
    mode: str = "",
) -> Dict[str, Any]:
    """Build a read-only technical status and separately bounded production status."""

    cfg = coordination_config(config)
    selected_mode = _safe_mode(mode) if mode else ""
    lease_root, recovered_root = _lease_paths(config)
    checkpoint_path = _controlled_path(cfg["checkpoint_path"], "checkpoint_path")
    technical_blockers: List[str] = []
    warnings: List[str] = []
    active: List[Dict[str, Any]] = []
    stale: List[Dict[str, Any]] = []
    malformed: List[Dict[str, Any]] = []
    current = _now(now)

    if lease_root.exists():
        for path in sorted(lease_root.glob("*.json")):
            if selected_mode and path.stem != selected_mode:
                continue
            try:
                metadata = _read_json(path)
                item = {
                    "mode": str(metadata.get("mode") or path.stem),
                    "run_id": str(metadata.get("run_id") or ""),
                    "expires_at": str(metadata.get("expires_at") or ""),
                    "heartbeat_at": str(metadata.get("heartbeat_at") or ""),
                    "pid": int(metadata.get("pid") or 0),
                    "lease_path": _relative_path(path),
                }
                if _is_expired(metadata.get("expires_at"), current):
                    stale.append(item)
                else:
                    active.append(item)
            except (RunCoordinationError, OSError, ValueError):
                malformed.append({"lease_path": _lease_display_path(path, lease_root), "reason": "unreadable_or_invalid"})

    if stale:
        technical_blockers.append("stale lease requires explicit recovery: {0}".format(len(stale)))
    if malformed:
        technical_blockers.append("malformed lease metadata: {0}".format(len(malformed)))

    chain = load_hash_chained_jsonl(checkpoint_path)
    if chain.get("integrity_status") != "OK":
        technical_blockers.append("checkpoint ledger integrity failed")
    states = _run_states(chain.get("records", []) or [], selected_mode or None)
    incomplete = [state for state in states.values() if state.get("status") == "RUNNING"]
    if incomplete:
        warnings.append("incomplete run records require operator review: {0}".format(len(incomplete)))
    if active:
        warnings.append("active lease(s) present: {0}".format(len(active)))
    if not cfg["enabled"]:
        warnings.append("run coordination is disabled in this configuration")

    last_run = None
    ordered_states = sorted(states.values(), key=lambda value: str(value.get("last_at") or ""), reverse=True)
    if ordered_states:
        last_run = ordered_states[0]

    production_blockers = [
        "local coordination evidence is not an approved production control",
        "cross-host/global lease authority is not configured",
        "business production enablement requires independent human approvals",
    ]
    return {
        "schema_version": RUN_COORDINATION_VERSION,
        "policy_version": RUN_COORDINATION_POLICY_VERSION,
        "generated_at": _iso(current),
        "enabled": cfg["enabled"],
        "mode_filter": selected_mode,
        "technical_status": "PASS" if not technical_blockers else "BLOCKED",
        "production_readiness": "BLOCKED",
        "status": "PASS" if not technical_blockers else "BLOCKED",
        "active_leases": active,
        "stale_leases": stale,
        "malformed_leases": malformed,
        "incomplete_runs": incomplete,
        "last_run": last_run,
        "ledger": {
            "path": _relative_path(checkpoint_path),
            "integrity_status": chain.get("integrity_status", "FAIL"),
            "record_count": int(chain.get("record_count", 0) or 0),
            "last_hash": str(chain.get("last_hash") or ""),
            "invalid_record_count": int(chain.get("invalid_record_count", 0) or 0),
        },
        "lease_root": _relative_path(lease_root),
        "recovered_lease_root": _relative_path(recovered_root),
        "technical_blockers": technical_blockers,
        "warnings": warnings,
        "production_blockers": production_blockers,
        "safety_boundary": SAFETY_BOUNDARY,
    }


def _render_status_markdown(snapshot: Dict[str, Any]) -> str:
    lines = [
        "# Run Coordination Status",
        "",
        "- Generated: `{0}`".format(snapshot.get("generated_at", "")),
        "- Technical status: `{0}`".format(snapshot.get("technical_status", "BLOCKED")),
        "- Production readiness: `{0}`".format(snapshot.get("production_readiness", "BLOCKED")),
        "- Ledger: `{0}` ({1} records, integrity `{2}`)".format(
            snapshot.get("ledger", {}).get("path", ""),
            snapshot.get("ledger", {}).get("record_count", 0),
            snapshot.get("ledger", {}).get("integrity_status", "FAIL"),
        ),
        "- Active leases: `{0}`; stale leases: `{1}`".format(
            len(snapshot.get("active_leases", []) or []),
            len(snapshot.get("stale_leases", []) or []),
        ),
        "",
        "## Technical Blockers",
    ]
    blockers = snapshot.get("technical_blockers", []) or []
    lines.extend(["- " + str(item) for item in blockers] or ["- None"])
    lines.extend(["", "## Warnings"])
    warnings = snapshot.get("warnings", []) or []
    lines.extend(["- " + str(item) for item in warnings] or ["- None"])
    lines.extend(["", "## Production Boundary"])
    lines.extend(["- " + str(item) for item in snapshot.get("production_blockers", []) or []])
    lines.extend(["", "> {0}".format(snapshot.get("safety_boundary", SAFETY_BOUNDARY))])
    return "\n".join(lines) + "\n"


def write_run_coordination_status(
    config: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
    mode: str = "",
) -> Dict[str, Any]:
    snapshot = build_run_coordination_status(config, now=now, mode=mode)
    report_dir = _controlled_path(coordination_config(config)["report_dir"], "report_dir")
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = _now(now).strftime("%Y%m%dT%H%M%S%z")
    suffix = uuid.uuid4().hex[:8]
    json_path = report_dir / "run_coordination_{0}_{1}.json".format(stamp, suffix)
    md_path = report_dir / "run_coordination_{0}_{1}.md".format(stamp, suffix)
    latest_json = report_dir / "latest.json"
    latest_md = report_dir / "latest.md"
    encoded = json.dumps(snapshot, ensure_ascii=False, indent=2)
    markdown = _render_status_markdown(snapshot)
    atomic_write_text(json_path, encoded)
    atomic_write_text(md_path, markdown)
    atomic_write_text(latest_json, encoded)
    atomic_write_text(latest_md, markdown)
    return {
        "snapshot": snapshot,
        "latest_path": str(latest_json),
        "latest_report_path": str(latest_md),
        "timestamped_path": str(json_path),
        "timestamped_report_path": str(md_path),
    }


def load_latest_run_coordination(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = coordination_config(config or {})
    path = _controlled_path(cfg["report_dir"], "report_dir") / "latest.json"
    if not path.exists():
        return {}
    try:
        return _read_json(path)
    except RunCoordinationError:
        return {}
