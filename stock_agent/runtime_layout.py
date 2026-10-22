"""R07: runtime layout — explicit roots, path safety, UTC/Shanghai clock.

Three disciplines for container readiness (GLM_REFACTOR_TASKBOOK R07):

1. **Five explicit roots, frozen at init.** ``APP_CONFIG_ROOT``,
   ``APP_STATE_ROOT``, ``APP_EVIDENCE_ROOT``, ``APP_REPORT_ROOT`` and
   ``APP_TEMP_ROOT`` resolve ONCE (env -> config -> PROJECT_ROOT defaults)
   into an immutable ``RuntimeLayout`` that services receive explicitly.
   Production path resolution never rides the test-only contextvar override
   in ``config.project_path``: threads share the frozen snapshot, and each
   call is pure (A35).

2. **Path safety.** All derived paths go through ``resolve_within_root`` /
   ``ensure_within``: single safe segments only (no separators, no ``..``,
   no absolute injection) and every resolved path must stay inside its
   declared root — escape attempts raise (A36). Chinese/space paths are
   ordinary paths; nothing lowercases or folds case, so Linux case
   sensitivity is preserved byte-for-byte.

3. **UTC persistence, Shanghai business days.** Persisted timestamps are
   UTC (``utc_now_iso``); budget days / business days / trading slots are
   the EXPLICIT Asia/Shanghai calendar date (``business_date``), so the
   midnight boundary never depends on the host timezone. Naive inputs and
   unknown timezone names are rejected; clock skew has one fixed tolerance
   shared with the event contract.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LAYOUT_SCHEMA_VERSION = "1.0"

ROOT_ENV_VARS = {
    "config_root": "APP_CONFIG_ROOT",
    "state_root": "APP_STATE_ROOT",
    "evidence_root": "APP_EVIDENCE_ROOT",
    "report_root": "APP_REPORT_ROOT",
    "temp_root": "APP_TEMP_ROOT",
}
ROOT_KEYS = frozenset(ROOT_ENV_VARS)

# One fixed skew tolerance between local fetch time and upstream timestamps,
# shared with the event contract (event_contract keeps its own copy for
# backwards compatibility; new code uses this one).
CLOCK_SKEW_TOLERANCE_SECONDS = 300

SHANGHAI_TZ_NAME = "Asia/Shanghai"


class LayoutError(ValueError):
    pass


# ---------------------------------------------------------------------------
# clock
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """Persisted timestamps are UTC, always with an explicit offset."""
    return utc_now().isoformat(timespec="seconds")


def shanghai_tz() -> ZoneInfo:
    return ZoneInfo(SHANGHAI_TZ_NAME)


def _to_aware(moment: Union[datetime, str, None], *, what: str) -> datetime:
    if isinstance(moment, str):
        try:
            parsed = datetime.fromisoformat(moment)
        except ValueError as exc:
            raise LayoutError("{0} is not an ISO timestamp".format(what)) from exc
    elif isinstance(moment, datetime):
        parsed = moment
    elif moment is None:
        return utc_now()
    else:
        raise LayoutError("{0} must be a datetime or ISO string".format(what))
    if parsed.tzinfo is None:
        # Naive input = unknown timezone = refused, never guessed.
        raise LayoutError("{0} has no timezone; naive timestamps are rejected".format(what))
    return parsed


def business_date(moment: Union[datetime, str, None] = None, *,
                  tz_name: str = SHANGHAI_TZ_NAME) -> date:
    """The explicit business/budget day: the calendar date in Asia/Shanghai.

    The day boundary is the Shanghai midnight, independent of the host
    timezone: 2026-10-20T17:00+00:00 is already 2026-10-21 in Shanghai, so a
    budget key derived from it belongs to the 21st."""
    if tz_name != SHANGHAI_TZ_NAME:
        try:
            ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise LayoutError("unknown timezone {0!r}".format(tz_name)) from exc
    return _to_aware(moment, what="moment").astimezone(shanghai_tz()).date()


def business_date_key(moment: Union[datetime, str, None] = None) -> str:
    return business_date(moment).isoformat()


def clock_skew_status(observed_at: Union[datetime, str, None],
                      fetched_at: Union[datetime, str]) -> Dict[str, Any]:
    """Fixed cross-midnight/skew rule: upstream timestamps may trail the
    local fetch by up to CLOCK_SKEW_TOLERANCE_SECONDS; anything beyond is
    'skewed' (flagged, preserved — never silently corrected)."""
    observed = _to_aware(observed_at, what="observed_at")
    fetched = _to_aware(fetched_at, what="fetched_at")
    skew = (observed - fetched).total_seconds()
    return {
        "status": "ok" if abs(skew) <= CLOCK_SKEW_TOLERANCE_SECONDS else "skewed",
        "skew_seconds": round(skew, 3),
        "tolerance_seconds": CLOCK_SKEW_TOLERANCE_SECONDS,
    }


# ---------------------------------------------------------------------------
# layout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeLayout:
    """Immutable five-root layout. Shared across threads by construction:
    every field is a resolved absolute path and no method mutates state."""

    project_root: Path
    config_root: Path
    state_root: Path
    evidence_root: Path
    report_root: Path
    temp_root: Path

    def snapshot(self) -> Dict[str, str]:
        """Serialization for backup manifests / status projections: plain
        absolute-path strings, no secrets, no contents."""
        return {
            "schema_version": LAYOUT_SCHEMA_VERSION,
            "project_root": str(self.project_root),
            "config_root": str(self.config_root),
            "state_root": str(self.state_root),
            "evidence_root": str(self.evidence_root),
            "report_root": str(self.report_root),
            "temp_root": str(self.temp_root),
        }

    def resolve_within_root(self, root: Path, *segments: str) -> Path:
        """Join SINGLE safe segments under ``root`` and verify containment.

        Separators, ``..``, empty and absolute segments are rejected before
        any filesystem call (A36 escape guard); the joined path is resolved
        and re-checked so symlinked intermediate directories cannot redirect
        it outside the root."""
        target_root = Path(root).resolve()
        for segment in segments:
            text = str(segment)
            if not text or text in (".", "..") \
                    or "/" in text or "\\" in text \
                    or (len(text) > 1 and text[1] == ":"):
                raise LayoutError(
                    "unsafe path segment {0!r}: single names only, no separators/parent/absolute".format(text))
        candidate = target_root.joinpath(*[str(s) for s in segments])
        return ensure_within(target_root, candidate)


def ensure_within(root: Path, path: Path) -> Path:
    """Resolve ``path`` and require it to stay inside ``root`` (both sides
    resolved; case is compared as-is, preserving Linux sensitivity)."""
    resolved_root = Path(root).resolve()
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise LayoutError(
            "path {0} escapes the declared root {1}".format(path, resolved_root)) from exc
    return resolved


def _resolve_root(value: Union[str, Path], *, key: str, project_root: Path) -> Path:
    text = str(value or "").strip()
    if not text:
        raise LayoutError("runtime root {0} is empty".format(key))
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return candidate.resolve()


def resolve_layout(config: Optional[Dict[str, Any]] = None,
                   env: Optional[Dict[str, str]] = None) -> RuntimeLayout:
    """Resolve the five roots ONCE: env wins, then the config section, then
    PROJECT_ROOT defaults. Unknown keys in the config section are refused
    (fail closed). The result is frozen — callers hand it to services; no
    thread re-derives production paths from ambient context."""
    from .config import PROJECT_ROOT

    environment = dict(os.environ if env is None else (env or {}))
    section = {}
    if config is not None:
        raw = (config or {}).get("runtime_layout") or {}
        if not isinstance(raw, dict):
            raise LayoutError("runtime_layout config section must be an object")
        unknown = set(raw) - ROOT_KEYS
        if unknown:
            raise LayoutError("unknown runtime_layout keys: {0}".format(sorted(unknown)))
        section = raw

    project_root = Path(PROJECT_ROOT).resolve()
    values: Dict[str, Path] = {
        "config_root": project_root / "config",
        "state_root": project_root / "data",
        "evidence_root": project_root / "data" / "evidence_vault",
        "report_root": project_root / "reports",
        "temp_root": Path(tempfile.gettempdir()) / "stock-agent",
    }
    for key in ROOT_KEYS:
        source = environment.get(ROOT_ENV_VARS[key]) or section.get(key)
        if source:
            values[key] = _resolve_root(source, key=key, project_root=project_root)
    return RuntimeLayout(project_root=project_root,
                         config_root=values["config_root"],
                         state_root=values["state_root"],
                         evidence_root=values["evidence_root"],
                         report_root=values["report_root"],
                         temp_root=values["temp_root"])


# ---------------------------------------------------------------------------
# legacy absolute-path map (history stays byte-identical)
# ---------------------------------------------------------------------------


class PathMap:
    """Resolve LEGACY absolute paths recorded in past events against the
    current layout WITHOUT rewriting them: the stored value is returned
    unchanged (it is part of a hashed history), and the mapping only decides
    where the SAME logical artifact lives now. Unknown paths pass through
    as-is, flagged read-only."""

    def __init__(self, mapping: Dict[str, str]):
        self._mapping = {str(k): str(v) for k, v in (mapping or {}).items()}

    @classmethod
    def from_layout(cls, layout: RuntimeLayout, *, legacy_project_root: str = "") -> "PathMap":
        legacy = str(legacy_project_root or layout.project_root)
        return cls({
            "{0}/config".format(legacy.replace("\\", "/")): str(layout.config_root),
            "{0}/data".format(legacy.replace("\\", "/")): str(layout.state_root),
            "{0}/data/evidence_vault".format(legacy.replace("\\", "/")): str(layout.evidence_root),
            "{0}/reports".format(legacy.replace("\\", "/")): str(layout.report_root),
        })

    def resolve(self, stored_path: str) -> Dict[str, Any]:
        text = str(stored_path or "")
        normalized = text.replace("\\", "/")
        for legacy_prefix, current_root in sorted(self._mapping.items(), key=lambda kv: -len(kv[0])):
            if normalized.startswith(legacy_prefix + "/"):
                relocated = Path(current_root) / normalized[len(legacy_prefix) + 1:]
                return {"stored": text, "resolved": str(relocated),
                        "relocated": True, "read_only_original": True}
        return {"stored": text, "resolved": text, "relocated": False,
                "read_only_original": True}
