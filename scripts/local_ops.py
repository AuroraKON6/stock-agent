"""Cross-platform local entry points for macOS and Linux.

The Windows entry points stay the reviewed ``scripts/*.ps1`` files. This module
is their POSIX counterpart: same locked interpreter (``.venv312``), same
config precedence (local config, else the announced development example), same
loopback-only workbench, same strict PID identity checks before any signal. On
Windows every subcommand hands off to the matching PowerShell script, so the
two platforms never carry two Windows implementations.

Usage:
    python3 scripts/local_ops.py run-once [--config CONFIG]
    python3 scripts/local_ops.py watch [--config CONFIG] [--interval-minutes N]
    python3 scripts/local_ops.py start-agent | stop-agent
    python3 scripts/local_ops.py start-workbench | stop-workbench [options]
    python3 scripts/local_ops.py go-live-check [--config CONFIG] [--skip-tests]

Safety properties shared with the PowerShell scripts:
  * No broad name-based process matching: a recorded PID is acted on only when
    its command line still names this project's entry point, config, host and
    port (or /health authenticates the instance id).
  * start is idempotent; a stale or foreign PID/metadata file is cleaned, a
    live instance for other settings is reported and never touched.
  * No collection, LLM, approval, publication or trading action is performed
    by this module itself.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
IS_WINDOWS = os.name == "nt"
HEALTH_TIMEOUT_SECONDS = 60
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
LOCK_TIMEOUT_SECONDS = 60

# PowerShell counterpart of each subcommand (the only Windows implementation).
PS1 = {
    "run-once": "run_once.ps1",
    "watch": "watch.ps1",
    "start-agent": "start_agent.ps1",
    "stop-agent": "stop_agent.ps1",
    "start-workbench": "start_research_workbench.ps1",
    "stop-workbench": "stop_research_workbench.ps1",
    "go-live-check": "go_live_check.ps1",
}


class OpsError(RuntimeError):
    """Refusal that is reported as a message plus a non-zero exit code."""


# --------------------------------------------------------------------------- #
# Resolution helpers
# --------------------------------------------------------------------------- #

def locked_python() -> Path:
    """The project's locked interpreter; PATH python is never a fallback."""
    relative = ".venv312/Scripts/python.exe" if IS_WINDOWS else ".venv312/bin/python"
    candidate = ROOT / relative
    if not candidate.is_file():
        raise OpsError(
            "Locked interpreter not found: {0} (expected .venv312; "
            "PATH python is not a supported fallback)".format(candidate))
    return candidate


def resolve_config(explicit: Optional[str]) -> Dict[str, Any]:
    """Prefer the real local config; announce the development example."""
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = ROOT / path
        return {"path": path.resolve(), "source": "explicit"}
    local = ROOT / "config" / "watchlist.local.json"
    if local.is_file():
        return {"path": local.resolve(), "source": "local"}
    return {"path": (ROOT / "config" / "watchlist.example.json").resolve(),
            "source": "example"}


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _utf8_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _now_stamp(separator: str = "_") -> str:
    """Local timestamp: ``yyyyMMdd_HHmmss``, or ``yyyyMMdd-HHmmss`` for workbench logs."""
    return datetime.now().strftime("%Y%m%d{0}%H%M%S".format(separator))


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


# --------------------------------------------------------------------------- #
# Process identity (POSIX)
# --------------------------------------------------------------------------- #

def process_command(pid: int) -> Optional[str]:
    """Command line of ``pid``, or None when no such process exists."""
    if pid <= 0:
        return None
    try:
        completed = subprocess.run(
            ["ps", "-ww", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _contains(text: str, *forms: str) -> bool:
    return any(form in text for form in forms)


def _has_option(cmd_norm: str, name: str, value: str) -> bool:
    """``--name value`` or ``--name=value``, with the value ending at the option.

    A plain substring test would let ``--port 8767`` accept ``--port 87670``."""
    return re.search(r"--{0}[ =]{1}(?![0-9a-z_.:-])".format(re.escape(name), re.escape(value)),
                     cmd_norm) is not None


def is_workbench_process(pid: int, config_abs: Path, host: str, port: int,
                         command: Optional[str] = None) -> bool:
    """Strict identity check mirroring Test-LiveWorkbenchProcess.

    True only when the command line names the workbench entry point, this
    project's config path, and the given host/port."""
    cmd = command if command is not None else process_command(pid)
    if not cmd:
        return False
    cmd_norm = cmd.lower()
    host_lower = host.strip().lower()
    config_forms = (config_abs.as_posix().lower(), str(config_abs).lower())
    return (_contains(cmd_norm, "-m stock_agent review-workbench")
            and _contains(cmd_norm, *config_forms)
            and _has_option(cmd_norm, "host", host_lower)
            and _has_option(cmd_norm, "port", str(int(port))))


def is_watch_process(pid: int, command: Optional[str] = None) -> bool:
    """True only when the command line is this checkout's ``watch`` daemon.

    The daemon records no config of its own, so identity is the project's
    locked interpreter plus the entry point; config may differ per start."""
    cmd = command if command is not None else process_command(pid)
    if not cmd:
        return False
    cmd_norm = cmd.lower()
    return (_contains(cmd_norm, "-m stock_agent watch")
            and str(ROOT / ".venv312").lower() in cmd_norm)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_pid_file(path: Path) -> int:
    """Positive PID from a PID file, else 0 (absent, empty or malformed)."""
    try:
        text = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return 0
    return int(text) if text.isdigit() else 0


def _base_url_host(host: str) -> str:
    return "[::1]" if host.strip().lower() == "::1" else "127.0.0.1"


def _url(host: str, port: int, suffix: str) -> str:
    return "http://{0}:{1}{2}".format(_base_url_host(host), int(port), suffix)


def _http_json(method: str, url: str, *, headers: Optional[Dict[str, str]] = None,
               timeout: float = 2) -> Optional[Dict[str, Any]]:
    request = urllib.request.Request(url, method=method, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback only
            loaded = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def health_matches(host: str, port: int, instance_id: str, pid: int) -> bool:
    """/health authenticates the recorded instance id AND the recorded PID."""
    if not instance_id:
        return False
    health = _http_json("GET", _url(host, port, "/health"))
    if not health:
        return False
    try:
        return (str(health.get("instance_id")) == instance_id
                and int(health.get("pid")) == int(pid))
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Windows hand-off
# --------------------------------------------------------------------------- #

def run_windows(counterpart: str, arguments: Sequence[str]) -> int:
    """Windows keeps one implementation: delegate to the reviewed .ps1 file."""
    script = ROOT / "scripts" / PS1[counterpart]
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", str(script), *arguments]
    completed = subprocess.run(command, cwd=str(ROOT))
    return int(completed.returncode)


# --------------------------------------------------------------------------- #
# run-once / watch
# --------------------------------------------------------------------------- #

def _run_cli(arguments: Sequence[str]) -> int:
    completed = subprocess.run(
        [str(locked_python()), "-B", "-m", "stock_agent", *arguments],
        cwd=str(ROOT), env=_utf8_env())
    return int(completed.returncode)


def cmd_run_once(args: argparse.Namespace) -> int:
    config = resolve_config(args.config)
    if config["source"] == "example":
        print("WARNING: config/watchlist.local.json not found; using development example config.")
    return _run_cli(["run", "--config", str(config["path"])])


def cmd_watch(args: argparse.Namespace) -> int:
    config = resolve_config(args.config)
    if config["source"] == "example":
        print("WARNING: config/watchlist.local.json not found; using development example config.")
    return _run_cli(["watch", "--config", str(config["path"]),
                     "--interval-minutes", str(args.interval_minutes)])


# --------------------------------------------------------------------------- #
# start-agent / stop-agent (background ``watch`` daemon)
# --------------------------------------------------------------------------- #

AGENT_PID_FILE = ROOT / "data" / "stock_agent.pid"


def _state_lock(path: Path):
    """Serialize start/stop of one state directory against concurrent callers.

    Without it two simultaneous starts both read "no PID file", both launch,
    and only the second PID is recorded -- the first daemon becomes
    untrackable. Advisory and blocking; released when the block ends or the
    holder exits."""
    import fcntl  # POSIX only; Windows hands off to the .ps1 scripts

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                raise OpsError("Timed out waiting for the operations lock: {0}".format(path))
            time.sleep(0.05)

    class _Held:
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            return False

    return _Held()


def _agent_lock() -> Path:
    # Resolved per call so a relocated ROOT keeps its lock next to its PID file.
    return ROOT / "data" / "stock_agent.lock"


def cmd_start_agent(args: argparse.Namespace) -> int:
    with _state_lock(_agent_lock()):
        return _start_agent(args)


def _start_agent(args: argparse.Namespace) -> int:
    config = resolve_config(args.config)
    interval = str(args.interval_minutes)
    config_text = _display(config["path"])
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    log_dir = ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    existing = _read_pid_file(AGENT_PID_FILE)
    if existing:
        if is_watch_process(existing):
            print("Stock agent is already running. PID: {0}".format(existing))
            return 0
        AGENT_PID_FILE.unlink(missing_ok=True)

    if config["source"] == "example":
        print("WARNING: config/watchlist.local.json not found; using development example config.")
    stamp = _now_stamp()
    stdout = log_dir / "stock-agent-{0}.out.log".format(stamp)
    stderr = log_dir / "stock-agent-{0}.err.log".format(stamp)
    command = [str(locked_python()), "-B", "-m", "stock_agent", "watch",
               "--config", config_text, "--interval-minutes", interval]
    with open(stdout, "ab") as out_handle, open(stderr, "ab") as err_handle:
        process = subprocess.Popen(
            command, cwd=str(ROOT), env=_utf8_env(), stdin=subprocess.DEVNULL,
            stdout=out_handle, stderr=err_handle, start_new_session=True)
    AGENT_PID_FILE.write_text("{0}\n".format(process.pid), encoding="ascii")
    time.sleep(3)
    if process.poll() is not None:
        AGENT_PID_FILE.unlink(missing_ok=True)
        print("Stock agent exited during start-up with code {0}.".format(process.returncode))
        print("Stderr log: {0}".format(stderr))
        return 1
    print("Stock agent started. PID: {0}".format(process.pid))
    print("Log: {0}".format(stdout))
    return 0


def cmd_stop_agent(args: argparse.Namespace) -> int:
    with _state_lock(_agent_lock()):
        return _stop_agent(args)


def _stop_agent(args: argparse.Namespace) -> int:
    recorded = _read_pid_file(AGENT_PID_FILE)
    if not recorded:
        if AGENT_PID_FILE.exists():
            # Present but malformed: it names nothing, so it is stale.
            AGENT_PID_FILE.unlink(missing_ok=True)
            print("Invalid PID file cleaned.")
        else:
            print("Stock agent is not running.")
        return 0
    if not _pid_alive(recorded):
        AGENT_PID_FILE.unlink(missing_ok=True)
        print("Process no longer exists. PID file cleaned.")
        return 0
    if not is_watch_process(recorded):
        AGENT_PID_FILE.unlink(missing_ok=True)
        print("Recorded PID {0} is now a different process; refusing to kill it. "
              "PID file cleaned.".format(recorded))
        return 0
    _terminate(recorded)
    AGENT_PID_FILE.unlink(missing_ok=True)
    print("Stock agent stopped. PID: {0}".format(recorded))
    return 0


def _terminate(pid: int) -> None:
    """Stop an already-validated PID: SIGTERM, then SIGKILL if it lingers."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(50):
        if not _pid_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return
    for _ in range(50):
        if not _pid_alive(pid):
            return
        time.sleep(0.1)


# --------------------------------------------------------------------------- #
# start-workbench / stop-workbench
# --------------------------------------------------------------------------- #

def _state_paths(state_directory: str) -> Dict[str, Path]:
    given = Path(state_directory)
    directory = given if given.is_absolute() else ROOT / given
    directory = directory.resolve()
    return {"dir": directory, "pid": directory / "workbench.pid",
            "meta": directory / "workbench.json"}


def _meta_int(meta: Dict[str, Any], key: str) -> int:
    try:
        return int(meta.get(key))
    except (TypeError, ValueError):
        return 0


def _meta_str(meta: Dict[str, Any], key: str) -> str:
    value = meta.get(key)
    return value if isinstance(value, str) else ""


def _meta_config(value: str) -> Optional[Path]:
    if not value:
        return None
    path = Path(value)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def cmd_start_workbench(args: argparse.Namespace) -> int:
    # Validate first: a refused start must not create state, not even a lock.
    host = args.host.strip().lower()
    if host not in LOOPBACK_HOSTS:
        print("Refusing to start: Host '{0}' is not a loopback address (127.0.0.1, localhost, ::1)."
              .format(args.host))
        return 1
    if not 1 <= int(args.port) <= 65535:
        print("Refusing to start: Port must be 1-65535.")
        return 1
    config_abs = resolve_config(args.config)["path"]
    if not config_abs.is_file():
        print("Config not found: {0}".format(config_abs))
        return 1
    python = locked_python()
    state = _state_paths(args.state_directory)
    with _state_lock(state["dir"] / "workbench.lock"):
        return _start_workbench(args, host, config_abs, python, state)


def _start_workbench(args: argparse.Namespace, host: str, config_abs: Path,
                     python: Path, state: Dict[str, Path]) -> int:
    state["dir"].mkdir(parents=True, exist_ok=True)
    health_url = _url(host, args.port, "/health")
    display_url = _url(host, args.port, "/?view=runtime")

    # Idempotency: trust existing metadata only after strict validation.
    meta = _read_json(state["meta"])
    if meta is not None:
        meta_pid, meta_port = _meta_int(meta, "pid"), _meta_int(meta, "port")
        meta_host, meta_config = _meta_str(meta, "host"), _meta_config(_meta_str(meta, "config"))
        meta_instance = _meta_str(meta, "instance_id")
        ours = ((meta_config is not None
                 and is_workbench_process(meta_pid, meta_config, meta_host, meta_port))
                or health_matches(meta_host, meta_port, meta_instance, meta_pid))
        if ours:
            if meta_port == int(args.port) and meta_host == host and meta_config == config_abs:
                print("Research workbench is already running. PID: {0}".format(meta_pid))
                print("URL: {0}".format(display_url))
                print("Config: {0}".format(config_abs))
                return 0
            print("A research workbench instance is already running for a different config/port "
                  "(PID {0}, port {1}).".format(meta_pid, meta_port))
            print("Stop it first or start with the same settings; not overwriting its metadata.")
            return 1
        state["meta"].unlink(missing_ok=True)
        state["pid"].unlink(missing_ok=True)

    pid_text = _read_pid_file(state["pid"])
    if pid_text and is_workbench_process(pid_text, config_abs, host, int(args.port)):
        print("Research workbench is already running. PID: {0}".format(pid_text))
        print("URL: {0}".format(display_url))
        print("Config: {0}".format(config_abs))
        return 0
    state["pid"].unlink(missing_ok=True)

    stamp = _now_stamp("-")
    stdout = state["dir"] / "workbench-{0}.out.log".format(stamp)
    stderr = state["dir"] / "workbench-{0}.err.log".format(stamp)
    command = [str(python), "-B", "-m", "stock_agent", "review-workbench",
               "--config", str(config_abs), "--host", host, "--port", str(int(args.port))]
    with open(stdout, "ab") as out_handle, open(stderr, "ab") as err_handle:
        process = subprocess.Popen(
            command, cwd=str(ROOT), env=_utf8_env(), stdin=subprocess.DEVNULL,
            stdout=out_handle, stderr=err_handle, start_new_session=True)

    print("Started research workbench PID {0}. Waiting for health at {1} ...".format(
        process.pid, health_url))

    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    health: Optional[Dict[str, Any]] = None
    while time.monotonic() < deadline:
        data = _http_json("GET", health_url)
        if (data and data.get("status") == "ok"
                and str(data.get("instance_id") or "").strip()
                and _as_int(data.get("pid")) == process.pid):
            health = data
            break
        if process.poll() is not None:
            break
        time.sleep(1)

    if health is None:
        if process.poll() is None:
            print("Workbench did not become healthy within {0} seconds.".format(HEALTH_TIMEOUT_SECONDS))
            _terminate(process.pid)
        else:
            print("Workbench process exited before becoming healthy. See stderr log.")
        state["pid"].unlink(missing_ok=True)
        state["meta"].unlink(missing_ok=True)
        print("Stdout log: {0}".format(stdout))
        print("Stderr log: {0}".format(stderr))
        return 1

    _write_json(state["meta"], {
        "pid": process.pid,
        "project_root": str(ROOT),
        "config": str(config_abs),
        "host": host,
        "port": int(args.port),
        "url": display_url,
        "health_url": health_url,
        "instance_id": str(health.get("instance_id")),
        "started_at": datetime.now().astimezone().isoformat(),
        "stdout": str(stdout),
        "stderr": str(stderr),
    })
    state["pid"].write_text("{0}\n".format(process.pid), encoding="ascii")

    print("Research workbench started.")
    print("PID: {0}".format(process.pid))
    print("URL: {0}".format(display_url))
    print("Config: {0}".format(config_abs))
    print("Stdout log: {0}".format(stdout))
    print("Stderr log: {0}".format(stderr))
    if not args.no_browser:
        webbrowser.open(display_url)
    return 0


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def cmd_stop_workbench(args: argparse.Namespace) -> int:
    state = _state_paths(args.state_directory)
    with _state_lock(state["dir"] / "workbench.lock"):
        return _stop_workbench(args, state)


def _stop_workbench(args: argparse.Namespace, state: Dict[str, Path]) -> int:
    meta = _read_json(state["meta"])
    if meta is None:
        if state["pid"].exists():
            state["pid"].unlink(missing_ok=True)
            print("No workbench metadata found; removed stale PID file.")
        else:
            print("Research workbench is not running.")
        return 0

    recorded = _meta_int(meta, "pid")
    meta_port, meta_host = _meta_int(meta, "port"), _meta_str(meta, "host")
    meta_config = _meta_config(_meta_str(meta, "config"))
    meta_instance = _meta_str(meta, "instance_id")
    if recorded <= 0 or meta_config is None:
        state["meta"].unlink(missing_ok=True)
        state["pid"].unlink(missing_ok=True)
        print("Workbench metadata was invalid; cleaned stale metadata.")
        return 0

    ours = (is_workbench_process(recorded, meta_config, meta_host, meta_port)
            or health_matches(meta_host, meta_port, meta_instance, recorded))
    if not ours:
        # Gone, or the PID now belongs to something else: never kill it.
        state["meta"].unlink(missing_ok=True)
        state["pid"].unlink(missing_ok=True)
        if _pid_alive(recorded):
            print("Recorded PID {0} is now a different process; refusing to kill it. "
                  "Removed stale workbench metadata.".format(recorded))
        else:
            print("Recorded workbench process (PID {0}) is no longer running; "
                  "cleaned stale metadata.".format(recorded))
        return 0

    # Prefer the authenticated loopback shutdown endpoint.
    if meta_instance:
        response = _http_json("POST", _url(meta_host, meta_port, "/shutdown"),
                              headers={"X-Workbench-Instance": meta_instance}, timeout=3)
        if response is not None and str(response.get("status")) == "stopping":
            for _ in range(20):
                if not _pid_alive(recorded):
                    break
                time.sleep(0.1)
    if _pid_alive(recorded):
        _terminate(recorded)
    state["meta"].unlink(missing_ok=True)
    state["pid"].unlink(missing_ok=True)
    print("Research workbench stopped. PID: {0}".format(recorded))
    print("URL was: {0}".format(_meta_str(meta, "url")))
    return 0


# --------------------------------------------------------------------------- #
# go-live-check (read-only controlled Go/No-Go checklist)
# --------------------------------------------------------------------------- #

GO_LIVE_STEPS = [
    ("pi_runtime_validation", ["pi-runtime-status", "--require-ready"]),
    ("pi_handoff_validation", ["agent-handoff", "--check"]),
    ("production_pilot_sync", ["production-pilot-sync"]),
    ("workspace_sync", ["workspace-sync"]),
    ("dashboard", ["dashboard"]),
    ("alerts", ["alerts"]),
    ("final_doctor", ["doctor"]),
    ("production_pilot_status", ["production-pilot-status"]),
]
GO_LIVE_NO_CONFIG = {"workspace_sync"}
EXPECTED_SCHEDULED_TASKS = [
    "StockAgent_DailyResearch",
    "StockAgent_PilotReconcile",
    "StockAgent_DailyDashboard",
    "StockAgent_WeeklyReview",
]


def go_live_steps(config: str, *, skip_tests: bool) -> List[Dict[str, Any]]:
    """Ordered read-only steps of the controlled checklist."""
    steps = []
    if not skip_tests:
        steps.append({"name": "unit_tests", "cli": None})
    for name, cli in GO_LIVE_STEPS:
        steps.append({"name": name, "cli": cli + ([] if name in GO_LIVE_NO_CONFIG else ["--config", config])})
    return steps


def scheduled_task_rows(platform: str = sys.platform) -> List[Dict[str, str]]:
    """Task Scheduler state per expected task; not applicable off Windows."""
    rows = []
    for name in EXPECTED_SCHEDULED_TASKS:
        if platform == "win32":
            rows.append({"task_name": name, "installed": "unknown", "state": "unqueried",
                         "next_run_time": "", "last_task_result": ""})
        else:
            rows.append({"task_name": name, "installed": "not_applicable",
                         "state": "windows_task_scheduler_only",
                         "next_run_time": "", "last_task_result": ""})
    return rows


def _go_live_exit(doctor_overall: str, technical_readiness: str, step_failure: bool) -> int:
    if step_failure or doctor_overall in {"FAIL", "ERROR"} or technical_readiness != "READY":
        return 1
    return 3  # the business decision is never GO without a real human Go


def cmd_go_live_check(args: argparse.Namespace) -> int:
    config = resolve_config(args.config)
    config_text = _display(config["path"])
    python = locked_python()
    stamp = _now_stamp()
    log_dir = ROOT / "logs" / "go_live"
    log_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now().astimezone().isoformat()
    steps: List[Dict[str, Any]] = []
    for step in go_live_steps(config_text, skip_tests=args.skip_tests):
        safe_name = step["name"]
        stdout_path = log_dir / "go_live_{0}.{1}.out.log".format(stamp, safe_name)
        stderr_path = log_dir / "go_live_{0}.{1}.err.log".format(stamp, safe_name)
        cli = step["cli"]
        # The unit-test step goes through the isolation runner, never a bare
        # unittest discover that could write into the real checkout.
        command = ([str(python), str(ROOT / "scripts" / "run_isolated_tests.py")] if cli is None
                   else [str(python), "-m", "stock_agent", *cli])
        step_started = datetime.now().astimezone().isoformat()
        print(">>> {0}".format(safe_name))
        with open(stdout_path, "wb") as out_handle, open(stderr_path, "wb") as err_handle:
            try:
                code = int(subprocess.run(command, cwd=str(ROOT), env=_utf8_env(),
                                          stdout=out_handle, stderr=err_handle).returncode)
            except OSError as exc:
                err_handle.write((str(exc) + "\n").encode("utf-8"))
                code = 1
        if code != 0:
            print("    [WARN] {0} exited with code {1}".format(safe_name, code))
        steps.append({
            "name": safe_name,
            "command": " ".join(shlex.quote(part) for part in command),
            "started_at": step_started,
            "finished_at": datetime.now().astimezone().isoformat(),
            "exit_code": code,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        })

    doctor = _read_json(ROOT / "reports" / "health" / "latest.json") or {}
    pilot = _read_json(ROOT / "reports" / "production_pilot" / "latest.json") or {}
    doctor_overall = str(doctor.get("overall_status") or "ERROR")
    decision = str(pilot.get("go_live_decision") or "NO_GO")
    readiness = str(pilot.get("technical_readiness") or "BLOCKED")
    rows = scheduled_task_rows()
    summary = {
        "started_at": started,
        "finished_at": datetime.now().astimezone().isoformat(),
        "stamp": stamp,
        "project_root": str(ROOT),
        "config": config_text,
        "overall_status": decision,
        "technical_readiness": readiness,
        "doctor_status": doctor_overall,
        "exit_code": 0,
        "scheduled_tasks_installed": None if not IS_WINDOWS else False,
        "scheduled_tasks": rows,
        "steps": steps,
        "safety_boundary": ("Internal research only; no automatic approval, activation, "
                            "publication, trading, orders, or position changes."),
    }
    summary["exit_code"] = _go_live_exit(doctor_overall, readiness,
                                         any(item["exit_code"] != 0 for item in steps))
    summary_path = log_dir / "go_live_{0}.summary.json".format(stamp)
    _write_json(summary_path, summary)

    print("")
    print("=== Controlled Go/No-Go Summary ===")
    print("Technical readiness: {0}".format(readiness))
    print("Business decision: {0}".format(decision))
    print("Doctor: {0}".format(doctor_overall))
    tasks = summary["scheduled_tasks_installed"]
    print("Scheduled tasks installed: {0}".format(
        "not applicable on this platform (Windows Task Scheduler only)" if tasks is None else tasks))
    print("Exit code: {0}".format(summary["exit_code"]))
    print("Summary: {0}".format(summary_path))
    if decision != "GO":
        print("[NO_GO] Technical health does not authorize business production use.")
    return int(summary["exit_code"])


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    run_once = commands.add_parser("run-once", help="one research run in the foreground")
    run_once.add_argument("--config")
    run_once.set_defaults(handler=cmd_run_once)

    watch = commands.add_parser("watch", help="continuous runs in the foreground")
    watch.add_argument("--config")
    watch.add_argument("--interval-minutes", type=float, default=30)
    watch.set_defaults(handler=cmd_watch)

    start_agent = commands.add_parser("start-agent", help="start the background watch daemon")
    start_agent.add_argument("--config")
    start_agent.add_argument("--interval-minutes", type=float, default=30)
    start_agent.set_defaults(handler=cmd_start_agent)

    stop_agent = commands.add_parser("stop-agent", help="stop the background watch daemon")
    stop_agent.add_argument("--config")
    stop_agent.set_defaults(handler=cmd_stop_agent)

    for name, help_text in (("start-workbench", "start the loopback Research Runtime Workbench"),
                            ("stop-workbench", "stop the Research Runtime Workbench")):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--config")
        sub.add_argument("--state-directory", default="logs/workbench")
        if name == "start-workbench":
            sub.add_argument("--host", default="127.0.0.1")
            sub.add_argument("--port", type=int, default=8767)
            sub.add_argument("--no-browser", action="store_true")
        sub.set_defaults(handler=cmd_start_workbench if name == "start-workbench"
                         else cmd_stop_workbench)

    go_live = commands.add_parser("go-live-check", help="controlled read-only Go/No-Go checklist")
    go_live.add_argument("--config")
    go_live.add_argument("--skip-tests", action="store_true")
    go_live.set_defaults(handler=cmd_go_live_check)
    return parser


def _windows_arguments(args: argparse.Namespace) -> List[str]:
    """Translate parsed options back into the PowerShell parameter names."""
    arguments: List[str] = []
    if getattr(args, "config", None):
        arguments += ["-Config", args.config]
    if getattr(args, "interval_minutes", None) is not None and args.command in {"watch", "start-agent"}:
        arguments += ["-IntervalMinutes", str(args.interval_minutes)]
    if args.command == "start-workbench":
        arguments += ["-HostName", args.host, "-Port", str(args.port),
                      "-StateDirectory", args.state_directory]
        if args.no_browser:
            arguments.append("-NoBrowser")
    elif args.command == "stop-workbench":
        arguments += ["-StateDirectory", args.state_directory]
    elif args.command == "go-live-check" and args.skip_tests:
        arguments.append("-SkipTests")
    return arguments


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if IS_WINDOWS:
        return run_windows(args.command, _windows_arguments(args))
    try:
        return int(args.handler(args))
    except OpsError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
