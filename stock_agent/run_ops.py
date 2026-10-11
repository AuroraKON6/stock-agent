from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Dict, Iterator, List, Optional

from .config import project_path


Clock = Callable[[], float]
ProgressWriter = Callable[[str], None]


def _stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds").replace(":", "").replace("-", "").replace("+", "_")


def _cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("run_ops", {}) or {}
    max_run = float(raw.get("max_run_seconds", 300))
    return {
        "enabled": bool(raw.get("enabled", True)),
        "max_run_seconds": max_run,
        "warn_run_seconds": float(raw.get("warn_run_seconds", max_run * 0.8)),
        "stage_warn_seconds": raw.get("stage_warn_seconds", {}) or {},
        "llm_max_calls": int(raw.get("llm_max_calls", 4)),
        "llm_max_seconds": float(raw.get("llm_max_seconds", 120)),
        "retry_backoff_seconds": float(raw.get("retry_backoff_seconds", 1.0)),
        "progress_enabled": bool(raw.get("progress_enabled", True)),
    }


class RunOpsContext:
    def __init__(
        self,
        config: Dict[str, Any],
        mode: str = "run",
        clock: Optional[Clock] = None,
        writer: Optional[ProgressWriter] = None,
    ) -> None:
        self.config = _cfg(config)
        self.mode = mode
        self.clock = clock or time.perf_counter
        self.writer = writer or config.get("_progress_writer")
        # A coordinated pipeline supplies one durable id so stage telemetry
        # and the cross-process run ledger refer to the same execution.  The
        # legacy timestamp fallback preserves existing standalone callers.
        self.run_id = str(config.get("_run_id") or _stamp())
        self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self._started = self.clock()
        self.stages: List[Dict[str, Any]] = []
        self.degradations: List[Dict[str, Any]] = []
        self.llm_calls: List[Dict[str, Any]] = []
        self.llm_reserved_calls = 0
        self.llm_reserved_seconds = 0.0
        self.events: List[Dict[str, Any]] = []
        self.progress_path = ""
        self._progress_initialized = False

    @contextmanager
    def stage(self, name: str, label: str = "") -> Iterator[None]:
        started = self.clock()
        self.emit("stage_started", name, "RUNNING", label or name)
        error = ""
        try:
            yield
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            duration_ms = int((self.clock() - started) * 1000)
            warn_seconds = self.config["stage_warn_seconds"].get(name)
            status = "OK"
            if error:
                status = "FAIL"
            elif warn_seconds is not None and duration_ms > int(float(warn_seconds) * 1000):
                status = "WARN"
                self.record_degradation(
                    name,
                    "stage_slow",
                    severity="WARN",
                    detail="{0}ms > {1}s".format(duration_ms, warn_seconds),
                )
            self.stages.append({
                "name": name,
                "label": label or name,
                "status": status,
                "duration_ms": duration_ms,
                "warn_seconds": warn_seconds,
                "error": error,
            })
            self.emit("stage_finished", name, status, "{0}ms".format(duration_ms))

    def elapsed_seconds(self) -> float:
        return max(0.0, self.clock() - self._started)

    def remaining_seconds(self) -> float:
        return max(0.0, self.config["max_run_seconds"] - self.elapsed_seconds())

    def effective_timeout(self, requested_seconds: float) -> int:
        if not self.config["enabled"]:
            return int(requested_seconds)
        remaining_run = self.remaining_seconds()
        remaining_llm = max(0.0, self.config["llm_max_seconds"] - self.llm_reserved_seconds)
        value = min(float(requested_seconds), remaining_run, remaining_llm)
        return max(1, int(value))

    def reserve_llm_call(self, component: str, timeout_seconds: float, item_count: int = 1) -> bool:
        if not self.config["enabled"]:
            return True
        timeout_seconds = float(timeout_seconds)
        if self.llm_reserved_calls >= self.config["llm_max_calls"]:
            self.record_degradation(component, "llm_call_budget_exhausted", detail="items={0}".format(item_count))
            return False
        if self.llm_reserved_seconds + timeout_seconds > self.config["llm_max_seconds"]:
            self.record_degradation(component, "llm_time_budget_exhausted", detail="timeout={0}".format(timeout_seconds))
            return False
        if self.elapsed_seconds() + timeout_seconds > self.config["max_run_seconds"]:
            self.record_degradation(component, "run_time_budget_exhausted", detail="timeout={0}".format(timeout_seconds))
            return False
        self.llm_reserved_calls += 1
        self.llm_reserved_seconds += timeout_seconds
        return True

    def record_llm_call(
        self,
        component: str,
        success: bool,
        duration_ms: int,
        error: str = "",
        provider: str = "",
        model: str = "",
        request_id: str = "",
        finish_reason: str = "",
        system_fingerprint: str = "",
        usage: Optional[Dict[str, Any]] = None,
        attempt: int = 0,
        item_count: int = 0,
        error_class: str = "",
        degrade_on_error: bool = True,
    ) -> None:
        """Record non-content call telemetry for audit and SLO reporting."""
        self.llm_calls.append({
            "component": component,
            "provider": str(provider or ""),
            "model": str(model or ""),
            "request_id": str(request_id or "")[:256],
            "finish_reason": str(finish_reason or "")[:64],
            "system_fingerprint": str(system_fingerprint or "")[:256],
            "usage": dict(usage or {}),
            "attempt": max(0, int(attempt or 0)),
            "item_count": max(0, int(item_count or 0)),
            "success": bool(success),
            "duration_ms": int(duration_ms),
            "error_class": str(error_class or "")[:128],
            "error": str(error or ""),
        })
        if error and degrade_on_error:
            self.record_degradation(component, "llm_call_failed", severity="WARN", detail=error)

    def retry_backoff_seconds(self) -> float:
        return max(0.0, float(self.config["retry_backoff_seconds"]))

    def record_degradation(self, component: str, reason: str, severity: str = "WARN", detail: str = "") -> None:
        self.degradations.append({
            "component": str(component),
            "reason": str(reason),
            "severity": str(severity or "WARN").upper(),
            "detail": str(detail or ""),
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        })

    def emit(self, event: str, component: str, status: str, message: str = "") -> None:
        if not self.config["enabled"] or not self.config["progress_enabled"]:
            return
        item = {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event,
            "component": component,
            "status": status,
            "message": message,
        }
        self.events.append(item)
        if callable(self.writer):
            try:
                self.writer("[run_ops] {0} {1}: {2}".format(component, status, message))
            except Exception:
                pass
        try:
            log_dir = project_path("logs/run_ops")
            log_dir.mkdir(parents=True, exist_ok=True)
            progress_path = log_dir / "progress_latest.jsonl"
            if not self._progress_initialized:
                progress_path.write_text("", encoding="utf-8")
                self._progress_initialized = True
            self.progress_path = str(progress_path)
            with progress_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def summary(self) -> Dict[str, Any]:
        elapsed_ms = int(self.elapsed_seconds() * 1000)
        status = "OK"
        if self.config["enabled"] and self.elapsed_seconds() > self.config["warn_run_seconds"]:
            status = "WARN"
        if self.config["enabled"] and self.elapsed_seconds() > self.config["max_run_seconds"]:
            status = "FAIL"
        for stage in self.stages:
            if stage.get("status") == "FAIL":
                status = "FAIL"
            elif stage.get("status") == "WARN" and status == "OK":
                status = "WARN"
        for item in self.degradations:
            if item.get("severity") == "FAIL":
                status = "FAIL"
            elif item.get("severity") == "WARN" and status == "OK":
                status = "WARN"
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "started_at": self.started_at,
            "elapsed_ms": elapsed_ms,
            "overall_status": status,
            "max_run_seconds": self.config["max_run_seconds"],
            "warn_run_seconds": self.config["warn_run_seconds"],
            "stages": list(self.stages),
            "degradations": list(self.degradations),
            "llm_budget": {
                "max_calls": self.config["llm_max_calls"],
                "reserved_calls": self.llm_reserved_calls,
                "max_seconds": self.config["llm_max_seconds"],
                "reserved_seconds": round(self.llm_reserved_seconds, 3),
                "actual_calls": len(self.llm_calls),
            },
            "llm_calls": list(self.llm_calls),
            "progress_path": self.progress_path,
        }


def write_run_summary(ops: RunOpsContext) -> Dict[str, str]:
    summary = ops.summary()
    log_dir = project_path("logs/run_ops")
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = summary["run_id"]
    json_path = log_dir / "run_ops_{0}.json".format(stamp)
    md_path = log_dir / "run_ops_{0}.md".format(stamp)
    latest_json = log_dir / "latest.json"
    latest_md = log_dir / "latest.md"
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    markdown = _render_summary_markdown(summary)
    json_path.write_text(text, encoding="utf-8")
    latest_json.write_text(text, encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
    latest_md.write_text(markdown, encoding="utf-8")
    return {
        "run_ops_path": str(latest_json),
        "run_ops_report_path": str(latest_md),
        "run_ops_timestamped_path": str(json_path),
        "run_ops_timestamped_report_path": str(md_path),
    }


def _render_summary_markdown(summary: Dict[str, Any]) -> str:
    lines = [
        "# Runtime Operations",
        "",
        "- status: {0}".format(summary.get("overall_status", "unknown")),
        "- elapsed_ms: {0}".format(summary.get("elapsed_ms", 0)),
        "- mode: {0}".format(summary.get("mode", "")),
        "",
        "## Stages",
        "",
    ]
    for stage in summary.get("stages", []) or []:
        lines.append("- [{0}] {1}: {2}ms".format(stage.get("status", ""), stage.get("name", ""), stage.get("duration_ms", 0)))
    lines.extend(["", "## Degradations", ""])
    degradations = summary.get("degradations", []) or []
    if not degradations:
        lines.append("- none")
    for item in degradations:
        lines.append("- [{0}] {1}: {2} {3}".format(
            item.get("severity", ""),
            item.get("component", ""),
            item.get("reason", ""),
            item.get("detail", ""),
        ).strip())
    return "\n".join(lines)
