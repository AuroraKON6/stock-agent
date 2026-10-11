"""Exact, metadata-only manifests for completed research runs.

The scheduler and the production-pilot reconciler must agree on one concrete
execution.  A mutable ``latest`` projection cannot provide that guarantee, so
this module creates one deterministic manifest per ``run_id`` and refuses to
overwrite it with a different payload.

The manifest deliberately contains paths, sizes, hashes and coordination
metadata only.  It is not a research report, an approval record, a release
manifest, or a trading instruction.
"""

from __future__ import annotations

import json
import re
import stat as stat_module
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .audit_log import atomic_write_text, file_hash, load_hash_chained_jsonl, object_hash
from .config import project_path
from .run_coordination import CheckpointStore, coordination_config


RUN_MANIFEST_VERSION = "1.0"
RUN_MANIFEST_POLICY_VERSION = "34.0"
SAFETY_BOUNDARY = (
    "Exact local run identity and artifact reconciliation only. It never collects data, "
    "calls an LLM, approves research, publishes output, changes positions, or trades."
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


class RunManifestError(RuntimeError):
    """Base error for manifest construction or verification."""


class RunManifestIntegrityError(RunManifestError):
    pass


class RunManifestConflictError(RunManifestError):
    pass


def run_manifest_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return normalized settings while preserving legacy configurations."""

    value = config or {}
    raw = value.get("run_manifest", {}) if isinstance(value, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    coordination = value.get("run_coordination", {}) if isinstance(value, dict) else {}
    coordination_enabled = bool(coordination.get("enabled", False)) if isinstance(coordination, dict) else False
    return {
        "enabled": bool(raw.get("enabled", coordination_enabled)),
        "manifest_dir": str(raw.get("manifest_dir") or "data/run_coordination/manifests"),
        "pointer_path": str(raw.get("pointer_path") or "reports/run_coordination/latest_manifest.json"),
        "require_completed_coordination": bool(raw.get("require_completed_coordination", True)),
        "verify_artifacts_on_load": bool(raw.get("verify_artifacts_on_load", True)),
    }


def _workspace_root() -> Path:
    return project_path(".").resolve()


_REPARSE_ATTRIBUTE = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_reparse_entry(path: Path) -> bool:
    """True for symlinks and Windows reparse points (junctions, mounts)."""

    try:
        info = path.lstat()
    except OSError:
        return False
    if stat_module.S_ISLNK(info.st_mode):
        return True
    return bool(int(getattr(info, "st_file_attributes", 0) or 0) & _REPARSE_ATTRIBUTE)


def _assert_no_reparse_components(candidate: Path, label: str) -> None:
    """Reject paths crossing a symlink/junction on ANY component.

    A leaf-only check is insufficient on Windows: directory junctions are
    reparse points that ``Path.is_symlink`` does not report, and a junctioned
    parent can alias content into (or out of) the workspace. Walking from the
    workspace root to the leaf keeps manifest references lexically verifiable.
    """

    root = _workspace_root()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise RunManifestError(
            "{0} is not lexically under the workspace (aliasing is not verifiable): {1}".format(label, candidate)
        ) from exc
    current = root
    for part in relative.parts:
        current = current / part
        if _is_reparse_entry(current):
            raise RunManifestError("{0} crosses a symlink or reparse point: {1}".format(label, current))


def _resolve_workspace_path(value: Any, label: str, *, must_exist: bool = True) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise RunManifestError("{0} must not be empty".format(label))
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = project_path(raw)
    try:
        resolved = candidate.resolve()
        resolved.relative_to(_workspace_root())
    except (OSError, ValueError) as exc:
        raise RunManifestError("{0} must remain under the workspace".format(label)) from exc
    if candidate.is_symlink() or resolved.is_symlink():
        raise RunManifestError("{0} must not be a symbolic link".format(label))
    _assert_no_reparse_components(candidate, label)
    if must_exist and not resolved.is_file():
        raise RunManifestError("{0} does not exist: {1}".format(label, resolved))
    return resolved


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(_workspace_root()).as_posix()
    except ValueError as exc:
        raise RunManifestError("manifest path escapes the workspace") from exc


def _safe_run_name(run_id: str) -> str:
    value = _SAFE_NAME.sub("_", str(run_id or "").strip()).strip("._")
    if not value:
        raise RunManifestError("run_id must not be empty")
    # Keep the readable prefix while adding a digest to avoid collisions from
    # characters normalized to the same underscore.
    return "run_{0}_{1}".format(value[:72], object_hash(str(run_id))[:12])


def manifest_path_for_run(config: Dict[str, Any], run_id: str) -> Path:
    cfg = run_manifest_config(config)
    directory = _resolve_workspace_path(cfg["manifest_dir"], "manifest_dir", must_exist=False)
    return directory / (_safe_run_name(run_id) + ".json")


def _read_json(path: Path, label: str = "manifest") -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunManifestIntegrityError("{0} is unreadable".format(label)) from exc
    if not isinstance(value, dict):
        raise RunManifestIntegrityError("{0} must be a JSON object".format(label))
    return value


def _validate_sha(value: Any, label: str) -> str:
    text = str(value or "").strip().lower()
    if not _SHA256.fullmatch(text):
        raise RunManifestIntegrityError("{0} must be a SHA-256 value".format(label))
    return text


def _artifact_ref(path_value: Any, label: str, *, required: bool = True) -> Optional[Dict[str, Any]]:
    if not str(path_value or "").strip():
        if required:
            raise RunManifestError("required artifact is missing: {0}".format(label))
        return None
    path = _resolve_workspace_path(path_value, label)
    return {
        "name": str(label),
        "path": _relative(path),
        "sha256": file_hash(path),
        "size": int(path.stat().st_size),
    }


def _normalize_artifacts(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise RunManifestIntegrityError("artifact reference must be an object")
        relative = str(item.get("path") or "").replace("\\", "/").strip()
        if not relative or relative.startswith("/") or ".." in Path(relative).parts:
            raise RunManifestIntegrityError("artifact path is unsafe")
        if relative in seen:
            raise RunManifestIntegrityError("duplicate artifact path: {0}".format(relative))
        seen.add(relative)
        try:
            size = int(item.get("size", 0))
        except (TypeError, ValueError) as exc:
            raise RunManifestIntegrityError("artifact size is invalid") from exc
        result.append({
            "name": str(item.get("name") or relative),
            "path": relative,
            "sha256": _validate_sha(item.get("sha256"), "artifact sha256"),
            "size": max(0, size),
        })
    return sorted(result, key=lambda value: value["path"])


def _report_identity(report: Dict[str, Any]) -> Tuple[str, str, str, Dict[str, Any]]:
    stats = report.get("stats") if isinstance(report.get("stats"), dict) else {}
    coordination = stats.get("run_coordination") if isinstance(stats.get("run_coordination"), dict) else {}
    top_coordination = report.get("run_coordination") if isinstance(report.get("run_coordination"), dict) else {}
    run_ops = stats.get("run_ops") if isinstance(stats.get("run_ops"), dict) else {}
    run_id = str(coordination.get("run_id") or top_coordination.get("run_id") or run_ops.get("run_id") or "")
    mode = str(coordination.get("mode") or top_coordination.get("mode") or run_ops.get("mode") or "")
    status = str(coordination.get("status") or top_coordination.get("status") or "")
    return run_id, mode, status, stats


def _coordination_fact(config: Dict[str, Any], run_id: str) -> Dict[str, Any]:
    coord_cfg = coordination_config(config)
    if not coord_cfg["enabled"]:
        return {
            "enabled": False,
            "status": "UNCOORDINATED",
            "ledger_path": "",
            "ledger_sha256": "",
            "terminal_event": {},
            "checkpoint_count": 0,
            "integrity_status": "NOT_APPLICABLE",
        }
    store = CheckpointStore(config)
    chain = store.ensure_healthy()
    state = store.state_for_run(run_id)
    terminal = {}
    for record in state.get("records", []) or []:
        if record.get("event_type") in {"run_completed", "run_failed"}:
            terminal = {
                "event_id": str(record.get("event_id") or ""),
                "event_hash": str(record.get("event_hash") or ""),
                "event_type": str(record.get("event_type") or ""),
                "occurred_at": str(record.get("occurred_at") or ""),
            }
    ledger_path = _resolve_workspace_path(coord_cfg["checkpoint_path"], "coordination checkpoint ledger", must_exist=False)
    return {
        "enabled": True,
        "status": str(state.get("status") or "UNKNOWN"),
        "ledger_path": _relative(ledger_path),
        "ledger_sha256": file_hash(ledger_path) if ledger_path.is_file() else "",
        "terminal_event": terminal,
        "checkpoint_count": len(state.get("checkpoints", []) or []),
        "record_count": int(chain.get("record_count", 0) or 0),
        "integrity_status": str(chain.get("integrity_status") or "FAIL"),
    }


def _config_ref(config_path: str) -> Dict[str, Any]:
    if not str(config_path or "").strip():
        return {"path": "", "sha256": "", "size": 0}
    path = _resolve_workspace_path(config_path, "config")
    return {"path": _relative(path), "sha256": file_hash(path), "size": int(path.stat().st_size)}


def _add_artifact(items: List[Dict[str, Any]], seen: set, label: str, value: Any, *, required: bool = False) -> None:
    if not str(value or "").strip():
        if required:
            raise RunManifestError("required artifact is missing: {0}".format(label))
        return
    ref = _artifact_ref(value, label, required=required)
    if ref and ref["path"] not in seen:
        items.append(ref)
        seen.add(ref["path"])


def build_run_manifest(
    config: Dict[str, Any],
    report_path: str,
    *,
    mode: str = "agent-run",
    run_id: str = "",
    trigger_source: str = "manual",
    config_path: str = "",
    extra_artifacts: Optional[Dict[str, str]] = None,
    scheduled_summary_path: str = "",
) -> Dict[str, Any]:
    """Build a metadata-only manifest for one exact report.

    ``report_path`` is intentionally mandatory.  The function never searches
    the report directory for a latest file.
    """

    cfg = run_manifest_config(config)
    report = _read_json(_resolve_workspace_path(report_path, "report"), "source report")
    report_file = _resolve_workspace_path(report_path, "report")
    discovered_run_id, discovered_mode, report_coordination_status, stats = _report_identity(report)
    selected_run_id = str(run_id or discovered_run_id).strip()
    if not selected_run_id:
        raise RunManifestError("source report has no run_id; exact manifest cannot be built")
    if run_id and discovered_run_id and str(run_id) != discovered_run_id:
        raise RunManifestError("explicit run_id does not match source report")
    selected_mode = str(mode or discovered_mode or "agent-run").strip()
    if discovered_mode and mode and discovered_mode != mode:
        raise RunManifestError("manifest mode does not match source report")

    coordination = _coordination_fact(config, selected_run_id)
    if cfg["require_completed_coordination"] and coordination["enabled"] and coordination["status"] != "COMPLETED":
        raise RunManifestError(
            "run is not durably completed: {0}".format(coordination["status"] or report_coordination_status or "UNKNOWN")
        )

    artifacts: List[Dict[str, Any]] = []
    seen = set()
    _add_artifact(artifacts, seen, "json_report", str(report_file), required=True)
    stem = report_file.stem
    _add_artifact(artifacts, seen, "daily_report", project_path("reports/daily/{0}.md".format(stem)), required=True)
    _add_artifact(artifacts, seen, "alert_report", project_path("reports/alerts/{0}.jsonl".format(stem)), required=True)

    # These paths are emitted by the pipeline itself.  Prefer the immutable
    # per-run run_ops file over its mutable latest projection.
    run_ops_timestamped = stats.get("run_ops_timestamped_path")
    run_ops_path = run_ops_timestamped or stats.get("run_ops_path")
    _add_artifact(artifacts, seen, "run_ops", run_ops_path, required=coordination["enabled"])
    _add_artifact(artifacts, seen, "review", stats.get("review_path"), required=False)
    _add_artifact(artifacts, seen, "actions", stats.get("actions_path"), required=False)
    for label, value in (extra_artifacts or {}).items():
        _add_artifact(artifacts, seen, str(label), value, required=False)

    config_ref = _config_ref(config_path)
    scheduled_path = ""
    if str(scheduled_summary_path or "").strip():
        scheduled_path = _relative(_resolve_workspace_path(scheduled_summary_path, "scheduled summary", must_exist=False))

    identity_projection = {
        "schema_version": RUN_MANIFEST_VERSION,
        "policy_version": RUN_MANIFEST_POLICY_VERSION,
        "run_id": selected_run_id,
        "mode": selected_mode,
        "trigger_source": str(trigger_source or "manual"),
        "report_generated_at": str(report.get("generated_at") or ""),
        "config": config_ref,
        "coordination": coordination,
        "artifacts": artifacts,
        "scheduled_summary_path": scheduled_path,
    }
    manifest_id = object_hash(identity_projection)
    eligible = bool(
        coordination["status"] == "COMPLETED"
        and coordination["integrity_status"] in {"OK", "NOT_APPLICABLE"}
        and selected_mode == "agent-run"
        and str(trigger_source or "manual") == "scheduled"
    )
    return {
        "schema_version": RUN_MANIFEST_VERSION,
        "policy_version": RUN_MANIFEST_POLICY_VERSION,
        "manifest_id": manifest_id,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "run_id": selected_run_id,
        "mode": selected_mode,
        "trigger_source": str(trigger_source or "manual"),
        "status": "COMPLETED" if coordination["status"] == "COMPLETED" else coordination["status"],
        "eligible_for_pilot": eligible,
        "report_generated_at": str(report.get("generated_at") or ""),
        "config": config_ref,
        "coordination": coordination,
        "artifacts": artifacts,
        "scheduled_summary_path": scheduled_path,
        "identity_projection": identity_projection,
        "safety_boundary": SAFETY_BOUNDARY,
    }


def _verify_manifest_identity(manifest: Dict[str, Any]) -> None:
    if str(manifest.get("schema_version") or "") != RUN_MANIFEST_VERSION:
        raise RunManifestIntegrityError("unsupported run manifest schema")
    if str(manifest.get("policy_version") or "") != RUN_MANIFEST_POLICY_VERSION:
        raise RunManifestIntegrityError("unsupported run manifest policy")
    identity = manifest.get("identity_projection")
    if not isinstance(identity, dict):
        raise RunManifestIntegrityError("manifest identity projection is missing")
    if str(manifest.get("manifest_id") or "") != object_hash(identity):
        raise RunManifestIntegrityError("manifest identity hash does not match content")
    if str(identity.get("run_id") or "") != str(manifest.get("run_id") or ""):
        raise RunManifestIntegrityError("manifest run_id is inconsistent")


def load_run_manifest(
    path_value: str,
    *,
    expected_run_id: str = "",
    expected_report_path: str = "",
    expected_manifest_sha256: str = "",
    verify_files: bool = True,
) -> Dict[str, Any]:
    """Load and verify one explicit manifest and its file references."""

    path = _resolve_workspace_path(path_value, "run manifest")
    manifest = _read_json(path, "run manifest")
    _verify_manifest_identity(manifest)
    if expected_run_id and str(manifest.get("run_id") or "") != str(expected_run_id):
        raise RunManifestIntegrityError("manifest run_id does not match the requested run")
    if expected_manifest_sha256 and file_hash(path) != _validate_sha(expected_manifest_sha256, "manifest sha256"):
        raise RunManifestIntegrityError("manifest file hash does not match scheduled summary")
    expected_relative = ""
    if expected_report_path:
        expected_relative = _relative(_resolve_workspace_path(expected_report_path, "expected report"))
    artifacts = _normalize_artifacts(manifest.get("artifacts") or [])
    report_refs = [item for item in artifacts if item.get("name") == "json_report"]
    if len(report_refs) != 1:
        raise RunManifestIntegrityError("manifest must contain exactly one json_report")
    if expected_relative and report_refs[0]["path"] != expected_relative:
        raise RunManifestIntegrityError("manifest report does not match scheduled summary")
    if verify_files:
        for item in artifacts:
            artifact_path = _resolve_workspace_path(item["path"], "manifest artifact")
            if file_hash(artifact_path) != item["sha256"]:
                raise RunManifestIntegrityError("artifact hash mismatch: {0}".format(item["path"]))
            if int(artifact_path.stat().st_size) != int(item["size"]):
                raise RunManifestIntegrityError("artifact size mismatch: {0}".format(item["path"]))
    manifest["verified_path"] = _relative(path)
    manifest["verified_sha256"] = file_hash(path)
    manifest["verified_artifacts"] = len(artifacts)
    return manifest


def write_run_manifest(
    config: Dict[str, Any],
    report_path: str,
    *,
    mode: str = "agent-run",
    run_id: str = "",
    trigger_source: str = "manual",
    config_path: str = "",
    extra_artifacts: Optional[Dict[str, str]] = None,
    scheduled_summary_path: str = "",
) -> Dict[str, Any]:
    """Write one immutable per-run manifest and a non-authoritative pointer."""

    cfg = run_manifest_config(config)
    if not cfg["enabled"]:
        raise RunManifestError("run manifest is disabled in this configuration")
    manifest = build_run_manifest(
        config,
        report_path,
        mode=mode,
        run_id=run_id,
        trigger_source=trigger_source,
        config_path=config_path,
        extra_artifacts=extra_artifacts,
        scheduled_summary_path=scheduled_summary_path,
    )
    path = manifest_path_for_run(config, str(manifest["run_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    write_status = "RECORDED"
    if path.exists():
        existing = load_run_manifest(str(path), verify_files=False)
        if str(existing.get("manifest_id") or "") != str(manifest.get("manifest_id") or ""):
            raise RunManifestConflictError("immutable run manifest already exists with different content")
        write_status = "DUPLICATE"
    else:
        atomic_write_text(path, encoded)

    digest = file_hash(path)
    pointer_path = _resolve_workspace_path(cfg["pointer_path"], "manifest pointer", must_exist=False)
    pointer = {
        "schema_version": RUN_MANIFEST_VERSION,
        "run_id": manifest["run_id"],
        "manifest_id": manifest["manifest_id"],
        "manifest_path": _relative(path),
        "manifest_sha256": digest,
        "status": manifest["status"],
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "authoritative": False,
    }
    atomic_write_text(pointer_path, json.dumps(pointer, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {
        "status": write_status,
        "manifest": manifest,
        "manifest_path": str(path),
        "manifest_relative_path": _relative(path),
        "manifest_sha256": digest,
        "pointer_path": str(pointer_path),
        "eligible_for_pilot": bool(manifest.get("eligible_for_pilot")),
    }


def bind_scheduled_summary(
    config: Dict[str, Any],
    summary_path: str,
    *,
    require_scheduled: bool = True,
) -> Dict[str, Any]:
    """Resolve and verify the exact manifest named by one scheduler summary."""

    errors: List[str] = []
    try:
        summary_file = _resolve_workspace_path(summary_path, "scheduled summary")
        summary = _read_json(summary_file, "scheduled summary")
    except RunManifestError as exc:
        return {"status": "BLOCKED", "errors": [str(exc)], "summary": {}, "manifest": {}}
    except RunManifestIntegrityError as exc:
        return {"status": "BLOCKED", "errors": [str(exc)], "summary": {}, "manifest": {}}

    if str(summary.get("mode") or "") != "daily":
        errors.append("scheduled summary is not a daily run")
    if int(summary.get("exit_code", 1) or 0) != 0:
        errors.append("scheduled daily run did not complete successfully")
    if require_scheduled and str(summary.get("trigger_source") or "") != "scheduled":
        errors.append("scheduled summary does not have trigger_source=scheduled")
    # Identity fields are MANDATORY: an omitted run_id, report path, or SHA
    # used to be silently coerced to an empty string and skipped comparison,
    # which let an incomplete summary bind. Missing/empty/type-invalid fields
    # block here instead.
    run_id = summary.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        errors.append("summary run_id is missing or not a non-empty string")
        run_id = ""
    report_path = summary.get("report_path")
    if not isinstance(report_path, str) or not report_path.strip():
        errors.append("summary report_path is missing or not a non-empty string")
    manifest_sha = summary.get("run_manifest_sha256")
    if not isinstance(manifest_sha, str) or not manifest_sha.strip():
        errors.append("summary run_manifest_sha256 is missing or not a non-empty string")
    manifest_path = str(summary.get("run_manifest_path") or "").strip()
    if not manifest_path:
        errors.append("scheduled summary has no exact run_manifest_path")
    if errors:
        return {
            "status": "BLOCKED",
            "errors": errors,
            "summary": summary,
            "summary_path": str(summary_file),
            "manifest": {},
            "manifest_path": "",
        }
    try:
        manifest = load_run_manifest(
            manifest_path,
            expected_run_id=run_id,
            expected_report_path=report_path,
            expected_manifest_sha256=manifest_sha,
            verify_files=run_manifest_config(config)["verify_artifacts_on_load"],
        )
    except RunManifestError as exc:
        errors.append(str(exc))
        manifest = {}
    if manifest:
        if str(manifest.get("status") or "") != "COMPLETED":
            errors.append("exact run manifest is not COMPLETED")
        if not bool(manifest.get("eligible_for_pilot")):
            errors.append("exact run manifest is not eligible for scheduled pilot")
        if str(summary.get("run_id") or "") != str(manifest.get("run_id") or ""):
            errors.append("scheduled summary run_id does not match exact manifest")
    return {
        "status": "PASS" if not errors else "BLOCKED",
        "errors": errors,
        "summary": summary,
        "summary_path": str(summary_file),
        "summary_relative_path": _relative(summary_file),
        "summary_sha256": file_hash(summary_file),
        "manifest": manifest,
        "manifest_path": str(_resolve_workspace_path(manifest_path, "run manifest", must_exist=False)) if manifest_path else "",
        "manifest_relative_path": str(manifest.get("verified_path") or "") if manifest else "",
    }

