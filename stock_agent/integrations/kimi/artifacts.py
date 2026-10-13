"""Kimi integration artifact writer: host-decided names, bounded, atomic.

F01/F06/F07 remediation. Remote ``files[].name`` values are untrusted
metadata: they never decide a local path. Every artifact gets a host-generated
``artifact_id``; the remote name is only inspected to reject obviously hostile
shapes (absolute paths, parent escapes, UNC, drive-relative, ADS, device
names) and to record a sanitized extension from a whitelist. Writes go to a
private staging directory then commit by atomic rename; existing targets are
never overwritten; count and total size are capped.

Native adversarial checks (junction/reparse-point substitution, symlink
swaps, TOCTOU races) are NOT covered here and stay NOT_RUN until a controlled
Windows VM acceptance exists; the checks below are best-effort defense in
depth, not a sandbox claim.
"""
from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Optional, Tuple

ALLOWED_EXTENSIONS = {"csv", "json", "txt", "md"}
# Windows reserved device names (with or without extension) plus DOS devices.
_DEVICE_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}
MAX_ARTIFACT_BYTES = 5 * 1024 * 1024
MAX_ARTIFACTS_PER_RESPONSE = 3

REJECTED_ABSOLUTE = "absolute_path"
REJECTED_PARENT_ESCAPE = "parent_escape"
REJECTED_UNC = "unc_path"
REJECTED_DRIVE_RELATIVE = "drive_relative"
REJECTED_ADS = "alternate_data_stream"
REJECTED_DEVICE_NAME = "device_name"
REJECTED_TRAILING = "trailing_dot_or_space"
REJECTED_BAD_CHARS = "unsafe_characters"


@dataclass
class ArtifactPlan:
    artifact_id: str
    filename: str
    extension: str
    byte_size: int
    sha256: str
    remote_name_rejected: Optional[str] = None
    remote_name_record: str = ""

    @property
    def accepted(self) -> bool:
        return self.remote_name_rejected is None


@dataclass
class ArtifactLimits:
    max_files: int = MAX_ARTIFACTS_PER_RESPONSE
    max_total_bytes: int = MAX_ARTIFACT_BYTES


def _sha256_hex(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def classify_remote_name(remote_name: Any) -> Tuple[Optional[str], str]:
    """Inspect a remote-supplied name. Returns (rejection_reason, record).

    The record is a sanitized single token kept for audit (no path
    separators); rejection means the artifact is dropped entirely rather
    than cleaned and used.
    """
    name = remote_name if isinstance(remote_name, str) else ""
    record = name.strip()[:120]
    if not name:
        return REJECTED_BAD_CHARS, ""
    # Order matters: UNC and drive prefixes are more specific than the
    # generic absolute check (PureWindowsPath treats both as absolute).
    if name.startswith("\\\\") or name.startswith("//"):
        return REJECTED_UNC, record
    if re.match(r"^[A-Za-z]:", name):
        return REJECTED_DRIVE_RELATIVE, record
    if name.startswith("/") or PureWindowsPath(name).is_absolute():
        return REJECTED_ABSOLUTE, record
    if ".." in PureWindowsPath(name).parts:
        return REJECTED_PARENT_ESCAPE, record
    # ADS colon anywhere outside a drive root (drive roots rejected above).
    if ":" in name:
        return REJECTED_ADS, record
    stem = PureWindowsPath(name).stem
    if stem.upper() in _DEVICE_NAMES:
        return REJECTED_DEVICE_NAME, record
    # Trailing dots/spaces are checked on the raw string: PureWindowsPath
    # strips them during parsing, hiding the shape it must reject.
    if name != name.strip() or name.rstrip().endswith("."):
        return REJECTED_TRAILING, record
    if re.search(r'[\x00-\x1f<>:"|?*]', name):
        return REJECTED_BAD_CHARS, record
    return None, record


def _extension_for(remote_name: Any) -> str:
    name = remote_name if isinstance(remote_name, str) else ""
    pure = PureWindowsPath(name.replace("\\", "/"))
    ext = pure.suffix.lstrip(".").lower()
    return ext if ext in ALLOWED_EXTENSIONS else "csv"


def plan_artifacts(remote_files: List[Dict[str, Any]], limits: ArtifactLimits) -> List[ArtifactPlan]:
    """Turn the envelope files[] into host-decided artifact plans.

    Rejects beyond the file cap and oversized bodies; every plan carries a
    fresh uuid so same-second concurrent responses can never collide (T07).
    """
    plans: List[ArtifactPlan] = []
    overflow = 0
    total = 0
    for item in (remote_files or [])[: max(0, limits.max_files)]:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if content is None:
            continue
        data = content if isinstance(content, bytes) else str(content).encode("utf-8", "replace")
        reason, record = classify_remote_name(item.get("name"))
        total += len(data)
        if total > limits.max_total_bytes:
            raise ArtifactLimitError(
                "artifact payload exceeds total byte cap ({0})".format(limits.max_total_bytes))
        artifact_id = "ka_{0}".format(uuid.uuid4().hex)
        ext = _extension_for(item.get("name"))
        plans.append(ArtifactPlan(
            artifact_id=artifact_id,
            filename="{0}.{1}".format(artifact_id, ext),
            extension=ext,
            byte_size=len(data),
            sha256=_sha256_hex(data),
            remote_name_rejected=reason,
            remote_name_record=record,
        ))
    overflow = max(0, len([f for f in (remote_files or []) if isinstance(f, dict)]) - limits.max_files)
    if overflow > 0:
        raise ArtifactLimitError(
            "response carried {0} files beyond the cap of {1}".format(overflow, limits.max_files))
    return plans


class ArtifactLimitError(RuntimeError):
    """Bounded abort: nothing has been committed when this is raised."""


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.stat().st_file_attributes  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False
    return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def commit_artifacts(plans: List[ArtifactPlan], contents: Dict[str, bytes],
                     artifact_root: Path) -> List[Dict[str, Any]]:
    """Commit accepted plans into ``artifact_root`` (created if missing).

    Stages bytes under a private uuid directory, checks that neither the root
    nor the target carries a reparse-point attribute, then renames into
    place. An existing target is an error, never an overwrite. Returns the
    committed records (same dicts the caller persists in the query ledger).
    """
    artifact_root = Path(artifact_root)
    artifact_root.mkdir(parents=True, exist_ok=True)
    if _is_reparse_point(artifact_root):
        raise ArtifactLimitError("artifact root is a reparse point; refusing to write")
    staged: List[Tuple[Path, Path]] = []
    committed: List[Dict[str, Any]] = []
    try:
        for plan in plans:
            if not plan.accepted:
                continue
            data = contents[plan.artifact_id]
            if len(data) != plan.byte_size or _sha256_hex(data) != plan.sha256:
                raise ArtifactLimitError("artifact bytes changed between planning and commit")
            final = artifact_root / plan.filename
            if final.exists() or final.is_symlink():
                raise ArtifactLimitError("artifact target already exists: {0}".format(plan.filename))
            staging_dir = artifact_root / ("_staging_" + uuid.uuid4().hex)
            staging_dir.mkdir()
            staged_path = staging_dir / plan.filename
            with open(staged_path, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if _is_reparse_point(staging_dir) or _is_reparse_point(final.parent):
                raise ArtifactLimitError("staging path traverses a reparse point; refusing")
            os.replace(str(staged_path), str(final))
            staged.append((staging_dir, final))
            committed.append({
                "artifact_id": plan.artifact_id,
                "filename": plan.filename,
                "bytes": plan.byte_size,
                "sha256": plan.sha256,
                "extension": plan.extension,
                "remote_name_rejected": plan.remote_name_rejected,
                "remote_name_record": plan.remote_name_record,
                "path": str(final),
            })
    finally:
        for staging_dir, _ in staged:
            _quiet_rmdir(staging_dir)
        # staging dirs that never reached the list (failure paths)
        for child in artifact_root.glob("_staging_*"):
            _quiet_rmdir(child)
    return committed


def _quiet_rmdir(path: Path) -> None:
    try:
        for item in path.iterdir():
            if item.is_file() or item.is_symlink():
                item.unlink()
        path.rmdir()
    except OSError:
        pass
