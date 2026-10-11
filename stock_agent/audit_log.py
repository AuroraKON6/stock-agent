from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple


class AuditLogIntegrityError(ValueError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def object_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_hash_chained_jsonl(path: Path) -> Dict[str, Any]:
    records: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    expected_previous = ""
    seen_ids = set()
    if not path.exists():
        return {
            "records": records,
            "errors": errors,
            "record_count": 0,
            "invalid_record_count": 0,
            "last_hash": "",
            "integrity_status": "OK",
        }

    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        return {
            "records": [],
            "errors": [{"line": 0, "reason": "audit log unreadable", "error_type": type(exc).__name__}],
            "record_count": 0,
            "invalid_record_count": 1,
            "last_hash": "",
            "integrity_status": "FAIL",
        }

    chain_broken = False
    for line_number, raw in enumerate(lines, 1):
        text = raw.strip()
        if not text:
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            errors.append({"line": line_number, "reason": "invalid JSON"})
            chain_broken = True
            continue
        if not isinstance(value, dict):
            errors.append({"line": line_number, "reason": "record must be an object"})
            chain_broken = True
            continue
        event_id = str(value.get("event_id") or "").strip()
        event_hash = str(value.get("event_hash") or "").strip()
        if not event_id or not event_hash:
            errors.append({"line": line_number, "reason": "event_id and event_hash are required"})
            chain_broken = True
            continue
        if event_id in seen_ids:
            errors.append({"line": line_number, "reason": "duplicate event_id", "event_id": event_id})
            chain_broken = True
            continue
        supplied_previous = str(value.get("previous_hash") or "")
        unhashed = dict(value)
        unhashed.pop("event_hash", None)
        calculated = object_hash(unhashed)
        if chain_broken or supplied_previous != expected_previous or calculated != event_hash:
            errors.append({
                "line": line_number,
                "reason": "hash chain verification failed",
                "event_id": event_id,
            })
            chain_broken = True
            continue
        records.append(value)
        seen_ids.add(event_id)
        expected_previous = event_hash

    return {
        "records": records,
        "errors": errors,
        "record_count": len(records),
        "invalid_record_count": len(errors),
        "last_hash": expected_previous,
        "integrity_status": "FAIL" if errors else "OK",
    }


def append_hash_chained_jsonl(path: Path, event: Dict[str, Any]) -> Dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _append_lock(path):
        current = load_hash_chained_jsonl(path)
        if current["integrity_status"] != "OK":
            raise AuditLogIntegrityError(
                "Audit log integrity check failed; repair under human control before appending: {0}".format(path)
            )
        record = dict(event)
        record.pop("event_hash", None)
        record["event_id"] = str(record.get("event_id") or uuid.uuid4().hex)
        record["sequence"] = int(current["record_count"]) + 1
        record["previous_hash"] = str(current["last_hash"] or "")
        record["event_hash"] = object_hash(record)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return record


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".{0}.{1}.tmp".format(path.name, uuid.uuid4().hex))
    try:
        with temporary.open("w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _append_lock(path: Path, timeout_seconds: float = 5.0) -> Iterator[None]:
    lock_path = path.with_suffix(path.suffix + ".lock")
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    descriptor = -1
    while descriptor < 0:
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, str(os.getpid()).encode("ascii", errors="replace"))
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError("Audit log is busy: {0}".format(path))
            time.sleep(0.05)
    try:
        yield
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
