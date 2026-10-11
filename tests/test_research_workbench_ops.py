"""Tests for the Research Runtime Workbench operational scripts.

The workbench is a loopback-only local review server started and stopped through
scripts/start_research_workbench.ps1 and scripts/stop_research_workbench.ps1.
These tests guard the bounded-operations contract: strict CIM command-line
identity validation, idempotency, PID-scoped kills only, and no broad name-based
process matching. They follow the repo convention of reading the scripts from
disk (see test_scheduled_workspace_ops.py) plus a non-admin PowerShell AST parse
check where powershell.exe is available.
"""
import hashlib
import json
import locale
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

try:
    import win32com.client  # type: ignore
    _HAS_WIN32COM = True
except ImportError:  # pragma: no cover - depends on optional pywin32
    _HAS_WIN32COM = False

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

START_SCRIPT = SCRIPTS_DIR / "start_research_workbench.ps1"
STOP_SCRIPT = SCRIPTS_DIR / "stop_research_workbench.ps1"
SHORTCUTS_SCRIPT = SCRIPTS_DIR / "create_research_workbench_shortcuts.ps1"

# Byte-for-byte snapshots of the watcher scripts. They change only through an
# explicit, reviewed update: Z-002 (2026-09-18) unified start_agent.ps1 onto the
# locked .venv312 interpreter, so the pinned hash was recomputed for that change.
START_AGENT_SHA256 = "cb5201b241ce1d072779fbb3f896dad14a924a32137184db5d4dd291e03fe400"
STOP_AGENT_SHA256 = "c40c5acf3fa9c8ec9b6f5a9adb50d0437ae7d78f52409898c57251deef7690bf"


def _read(relative: str) -> str:
    return (SCRIPTS_DIR / relative).read_text(encoding="utf-8")


class StartScriptContentTests(unittest.TestCase):
    def test_parameters_and_defaults(self):
        script = _read("start_research_workbench.ps1")
        self.assertIn('param(', script)
        self.assertIn('[string]$Config = "config/watchlist.local.json"', script)
        self.assertIn('[Alias("Host")]', script)
        self.assertIn('[string]$HostName = "127.0.0.1"', script)
        self.assertIn('[ValidateRange(1, 65535)]', script)
        self.assertIn('[int]$Port = 8767', script)
        self.assertIn('[switch]$NoBrowser', script)

    def test_loopback_only_bind(self):
        script = _read("start_research_workbench.ps1")
        self.assertIn("127.0.0.1", script)
        self.assertIn("localhost", script)
        self.assertIn("::1", script)
        self.assertIn("Refusing to start: Host", script)

    def test_strict_cim_identity_validation(self):
        script = _read("start_research_workbench.ps1")
        self.assertIn("Get-CimInstance Win32_Process", script)
        self.assertIn("-m stock_agent review-workbench", script)
        self.assertIn("--host ", script)
        self.assertIn("--port ", script)
        # Identity must include the absolute config path for this project.
        self.assertIn("Resolve-ProjectConfigPath", script)
        self.assertIn("ConfigAbs", script)

    def test_state_directory_defaults_and_isolation(self):
        script = _read("start_research_workbench.ps1")
        # PID/metadata/log state defaults to the project-relative logs/workbench
        # and is overridable through a -StateDirectory param (isolated per run).
        self.assertIn('[string]$StateDirectory = "logs/workbench"', script)
        self.assertIn("workbench.json", script)
        self.assertIn("workbench.pid", script)
        self.assertIn('"workbench-$Stamp.out.log"', script)
        self.assertIn('"workbench-$Stamp.err.log"', script)
        # Relative paths resolve under the project root; absolute paths are used
        # unchanged so tests can isolate state in a temporary directory.
        self.assertIn("[System.IO.Path]::IsPathRooted($StateDirectory)", script)
        self.assertIn('$WorkbenchDir = [System.IO.Path]::GetFullPath($StateDirectory)', script)
        self.assertIn('$WorkbenchDir = [System.IO.Path]::GetFullPath((Join-Path $Root $StateDirectory))', script)
        self.assertIn('$PidFile = Join-Path $WorkbenchDir "workbench.pid"', script)
        self.assertIn('$MetaFile = Join-Path $WorkbenchDir "workbench.json"', script)
        # The stop script honors the same default and the same isolation rules.
        stop = _read("stop_research_workbench.ps1")
        self.assertIn('[string]$StateDirectory = "logs/workbench"', stop)
        self.assertIn("[System.IO.Path]::IsPathRooted($StateDirectory)", stop)
        self.assertIn('$PidFile = Join-Path $WorkbenchDir "workbench.pid"', stop)
        self.assertIn('$MetaFile = Join-Path $WorkbenchDir "workbench.json"', stop)

    def test_idempotent_reuse_of_live_instance(self):
        script = _read("start_research_workbench.ps1")
        self.assertIn("Research workbench is already running. PID:", script)
        self.assertIn("Test-LiveWorkbenchProcess", script)
        # A live instance for a different config/port must never be clobbered.
        self.assertIn("not overwriting its metadata", script)

    def test_hidden_launch_and_health_poll(self):
        script = _read("start_research_workbench.ps1")
        self.assertIn("Start-Process", script)
        self.assertIn("-WindowStyle Hidden", script)
        self.assertIn("-WorkingDirectory $Root", script)
        self.assertIn("-RedirectStandardOutput", script)
        self.assertIn("-RedirectStandardError", script)
        self.assertIn("-PassThru", script)
        self.assertIn("/health", script)
        self.assertIn("Invoke-RestMethod", script)
        self.assertIn("instance_id", script)
        self.assertIn("-TimeoutSec 2", script)
        self.assertIn("$ProcessExited", script)

    def test_failure_kills_only_spawned_pid_and_cleans_metadata(self):
        script = _read("start_research_workbench.ps1")
        self.assertIn("Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue", script)
        self.assertIn("Remove-Item -LiteralPath $PidFile", script)
        self.assertIn("Remove-Item -LiteralPath $MetaFile", script)
        self.assertIn("exit 1", script)

    def test_opens_runtime_view_unless_nobrowser(self):
        script = _read("start_research_workbench.ps1")
        self.assertIn("/?view=runtime", script)
        self.assertIn("-not $NoBrowser", script)
        self.assertIn("Start-Process $DisplayUrl", script)


class StopScriptContentTests(unittest.TestCase):
    def test_stops_only_recorded_pid(self):
        script = _read("stop_research_workbench.ps1")
        self.assertIn("Stop-Process -Id $recPid -Force", script)
        self.assertIn("workbench.json", script)
        self.assertIn("workbench.pid", script)

    def test_same_strict_validation_as_start(self):
        script = _read("stop_research_workbench.ps1")
        self.assertIn("Get-CimInstance Win32_Process", script)
        self.assertIn("-m stock_agent review-workbench", script)
        self.assertIn("Test-LiveWorkbenchProcess", script)

    def test_refuses_reused_pid_and_cleans_metadata(self):
        script = _read("stop_research_workbench.ps1")
        self.assertIn("refusing to kill", script)
        self.assertIn("Remove-Item -LiteralPath $MetaFile", script)
        self.assertIn("Remove-Item -LiteralPath $PidFile", script)

    def test_no_broad_name_based_kill(self):
        script = _read("stop_research_workbench.ps1")
        self.assertNotIn("Stop-Process -Name", script)
        self.assertNotIn("Get-Process -Name", script)
        self.assertNotIn("taskkill", script)
        self.assertIn("Stop-Process -Id $recPid", script)

    def test_idempotent_stop(self):
        script = _read("stop_research_workbench.ps1")
        self.assertIn("Research workbench is not running.", script)
        self.assertIn("exit 0", script)


class ShortcutsScriptContentTests(unittest.TestCase):
    def test_desktop_shortcuts_via_powershell(self):
        script = _read("create_research_workbench_shortcuts.ps1")
        self.assertIn('[Environment]::GetFolderPath("Desktop")', script)
        self.assertIn("WScript.Shell", script)
        self.assertIn("powershell.exe", script)
        # Shortcut arguments route through powershell.exe with a quoted -File path.
        self.assertIn("-NoProfile -ExecutionPolicy Bypass -File \"", script)
        self.assertIn("+ $StartScript +", script)
        self.assertIn("+ $StopScript +", script)
        self.assertIn(".WorkingDirectory = $ProjectRoot", script)

    def test_uniquely_named_start_and_stop(self):
        script = _read("create_research_workbench_shortcuts.ps1")
        self.assertIn("ResearchWorkbenchStart.lnk", script)
        self.assertIn("ResearchWorkbenchStop.lnk", script)

    def test_never_overwrites_unrelated_names(self):
        script = _read("create_research_workbench_shortcuts.ps1")
        self.assertIn("Refusing to overwrite existing unrelated shortcut", script)
        self.assertIn("Test-IsOurShortcut", script)


class AgentScriptPreservationTests(unittest.TestCase):
    def test_start_agent_preserved_exactly(self):
        data = (SCRIPTS_DIR / "start_agent.ps1").read_bytes()
        self.assertEqual(
            hashlib.sha256(data).hexdigest(),
            START_AGENT_SHA256,
            "scripts/start_agent.ps1 changed; it must be preserved exactly",
        )

    def test_stop_agent_preserved_exactly(self):
        data = (SCRIPTS_DIR / "stop_agent.ps1").read_bytes()
        self.assertEqual(
            hashlib.sha256(data).hexdigest(),
            STOP_AGENT_SHA256,
            "scripts/stop_agent.ps1 changed; it must be preserved exactly",
        )


def _parse_with_powershell(path: Path) -> tuple:
    """Run PowerShell's AST parser (no execution) and return (ok, detail)."""
    ps_script = (
        "$tokens = $null; $errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        "'{path}', [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors -and $errors.Count -gt 0) {{ "
        "$errors | ForEach-Object {{ Write-Output ('PSERR: ' + $_.Message) }}; exit 1 "
        "}} else {{ Write-Output 'PARSE_OK' }}"
    ).format(path=str(path).replace("'", "''"))
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()


def _free_high_port() -> int:
    """Bind an ephemeral loopback port and release it for the workbench to use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run_ps1(script: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)] + list(args)
    # The start script spawns a long-lived workbench process. That grandchild
    # inherits the PowerShell host's stdout/stderr, so PIPE capture would keep
    # communicate() blocked even after powershell.exe exits. Redirect to
    # on-disk temp files instead: wait only on the PowerShell process, then
    # read the files back. Handles are closed on timeout/error.
    out_f = tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", delete=False)
    err_f = tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", delete=False)
    out_name, err_name = out_f.name, err_f.name
    stdout = stderr = ""
    returncode = None
    try:
        try:
            completed = subprocess.run(cmd, stdout=out_f, stderr=err_f, timeout=timeout)
            returncode = completed.returncode
        finally:
            out_f.close()
            err_f.close()
        # PowerShell writes using the console/system codepage, not UTF-8;
        # read back with the locale default (as text=True did) to match the
        # strings the assertions expect.
        with open(out_name, encoding=locale.getpreferredencoding(False), errors="replace") as f:
            stdout = f.read()
        with open(err_name, encoding=locale.getpreferredencoding(False), errors="replace") as f:
            stderr = f.read()
    finally:
        for name in (out_name, err_name):
            try:
                os.unlink(name)
            except OSError:
                pass
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


def _wait_healthy(url: str, timeout: int = 90) -> bool:
    """Poll an HTTP URL until it returns 200 or the timeout elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _read_shortcut_links(desktop: Path) -> dict:
    """Read both workbench shortcuts' (target, arguments, workingdir) via WScript.Shell."""
    shell = win32com.client.Dispatch("WScript.Shell")
    links = {}
    for name in ("ResearchWorkbenchStart", "ResearchWorkbenchStop"):
        lnk = os.path.join(str(desktop), name + ".lnk")
        sc = shell.CreateShortcut(lnk)
        links[name] = (sc.TargetPath, sc.Arguments, sc.WorkingDirectory)
    return links


def _powershell_home() -> str:
    """Return the Windows PowerShell installation directory ($PSHOME)."""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NoLogo", "-Command", "Write-Output $PSHOME"],
        capture_output=True, text=True, timeout=60,
    )
    return (result.stdout or "").strip()


@unittest.skipUnless(shutil.which("powershell.exe"), "powershell.exe not available")
class WorkbenchLifecycleBehaviorTests(unittest.TestCase):
    """Behavioral start/idempotent/health/stop/repeated-stop on an isolated state dir."""

    @classmethod
    def setUpClass(cls):
        cls._needs = shutil.which("powershell.exe") is not None
        cls._python_ok = shutil.which("python") is not None
        if not (cls._needs and cls._python_ok):
            cls.fail("powershell.exe and python are required for lifecycle behavior tests")

    def _stop_and_reap(self, state: Path, pid: int):
        # Best-effort teardown: run the stop script, then force-kill a survivor.
        _run_ps1(STOP_SCRIPT, "-StateDirectory", str(state), timeout=60)
        if pid and pid > 0:
            kill = "Stop-Process -Id {0} -Force -ErrorAction SilentlyContinue".format(pid)
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", kill],
                capture_output=True, text=True, timeout=60,
            )

    def test_start_idempotent_health_stop_repeated_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            port = _free_high_port()
            args = ["-StateDirectory", str(state), "-Port", str(port), "-NoBrowser"]
            pid = 0
            try:
                # Start -> healthy, metadata persisted.
                start = _run_ps1(START_SCRIPT, *args)
                self.assertEqual(start.returncode, 0, start.stdout + start.stderr)
                self.assertTrue(_wait_healthy("http://127.0.0.1:{0}/health".format(port)))

                meta_file = state / "workbench.json"
                pid_file = state / "workbench.pid"
                self.assertTrue(meta_file.exists(), "workbench.json was not written to -StateDirectory")
                self.assertTrue(pid_file.exists(), "workbench.pid was not written to -StateDirectory")
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                pid = int(meta["pid"])
                self.assertEqual(int(pid_file.read_text().strip()), pid)
                self.assertEqual(meta["port"], port)
                # stdout/stderr logs also live in the isolated state dir.
                self.assertTrue(Path(meta["stdout"]).exists(), "workbench stdout log not under state dir")

                # Idempotent second start reuses the live instance.
                again = _run_ps1(START_SCRIPT, *args)
                self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
                self.assertIn("already running", again.stdout.lower())

                # Stop kills the recorded PID and cleans metadata.
                stop = _run_ps1(STOP_SCRIPT, "-StateDirectory", str(state))
                self.assertEqual(stop.returncode, 0, stop.stdout + stop.stderr)
                self.assertIn("stopped", stop.stdout.lower())
                self.assertFalse(meta_file.exists())
                self.assertFalse(pid_file.exists())

                # Repeated stop is idempotent and exits 0.
                again_stop = _run_ps1(STOP_SCRIPT, "-StateDirectory", str(state))
                self.assertEqual(again_stop.returncode, 0, again_stop.stdout + again_stop.stderr)
                self.assertIn("not running", again_stop.stdout.lower())
            finally:
                self._stop_and_reap(state, pid)


@unittest.skipUnless(shutil.which("powershell.exe"), "powershell.exe not available")
class ReusedPidSafetyTests(unittest.TestCase):
    """The stop script must never kill a process that reused a recorded PID."""

    def test_reused_pid_is_not_killed_but_metadata_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            proc = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-Command", "Start-Sleep -Seconds 120"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                pid = proc.pid
                fake = {
                    "pid": pid,
                    "project_root": str(PROJECT_ROOT),
                    "config": r"C:\__nonexistent__\watchlist.json",
                    "host": "127.0.0.1",
                    "port": 1,
                    "url": "http://127.0.0.1:1/?view=runtime",
                    "health_url": "http://127.0.0.1:1/health",
                }
                (state / "workbench.json").write_text(
                    json.dumps(fake), encoding="utf-8"
                )
                (state / "workbench.pid").write_text(str(pid), encoding="ascii")

                stop = _run_ps1(STOP_SCRIPT, "-StateDirectory", str(state))
                self.assertEqual(stop.returncode, 0, stop.stdout + stop.stderr)

                # The unrelated process must still be alive, and metadata cleaned.
                self.assertIsNone(proc.poll(), "stop script killed an unrelated process")
                self.assertFalse((state / "workbench.json").exists())
                self.assertFalse((state / "workbench.pid").exists())
            finally:
                proc.kill()
                proc.wait()


@unittest.skipUnless(_HAS_WIN32COM and shutil.which("powershell.exe"),
                     "pywin32 and powershell.exe required for shortcut behavior tests")
class WorkbenchShortcutBehaviorTests(unittest.TestCase):
    def test_creates_both_links_with_expected_target_arguments_and_workdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp)
            result = _run_ps1(SHORTCUTS_SCRIPT, "-DesktopPath", str(desktop))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            links = _read_shortcut_links(desktop)
            self.assertEqual(set(links), {"ResearchWorkbenchStart", "ResearchWorkbenchStop"})

            expected_target = os.path.join(_powershell_home(), "powershell.exe")
            expected_args = {
                "ResearchWorkbenchStart": '-NoProfile -ExecutionPolicy Bypass -File "' + str(START_SCRIPT) + '"',
                "ResearchWorkbenchStop": '-NoProfile -ExecutionPolicy Bypass -File "' + str(STOP_SCRIPT) + '"',
            }
            expected_workdir = str(PROJECT_ROOT)

            def canon(p):
                return os.path.normcase(os.path.realpath(p))

            for name in ("ResearchWorkbenchStart", "ResearchWorkbenchStop"):
                target, arguments, workdir = links[name]
                self.assertEqual(canon(target), canon(expected_target), "{0} target".format(name))
                self.assertEqual(arguments, expected_args[name], "{0} arguments".format(name))
                self.assertEqual(canon(workdir), canon(expected_workdir), "{0} working dir".format(name))

    def test_refuses_to_overwrite_unrelated_shortcut(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp)
            # Precreate a same-named powershell shortcut that is NOT ours.
            lnk = os.path.join(str(desktop), "ResearchWorkbenchStart.lnk")
            shell = win32com.client.Dispatch("WScript.Shell")
            sc = shell.CreateShortcut(lnk)
            sc.TargetPath = os.path.join(_powershell_home(), "powershell.exe")
            sc.Arguments = '-NoProfile -Command "Write-Output unrelated"'
            sc.WorkingDirectory = os.environ.get("WINDIR", r"C:\Windows")
            sc.Save()
            original_args = sc.Arguments

            result = _run_ps1(SHORTCUTS_SCRIPT, "-DesktopPath", str(desktop))
            self.assertNotEqual(result.returncode, 0, "creator should refuse an unrelated shortcut")
            self.assertIn("Refusing to overwrite existing unrelated shortcut", result.stdout + result.stderr)

            # The precreated link must be left untouched.
            after = _read_shortcut_links(desktop)["ResearchWorkbenchStart"]
            self.assertEqual(after[1], original_args, "unrelated shortcut arguments were overwritten")


@unittest.skipUnless(shutil.which("powershell.exe"), "powershell.exe not available")
class PowerShellParseTests(unittest.TestCase):
    def test_scripts_parse_cleanly(self):
        for path in (START_SCRIPT, STOP_SCRIPT, SHORTCUTS_SCRIPT):
            with self.subTest(script=path.name):
                code, out, err = _parse_with_powershell(path)
                self.assertEqual(code, 0, "parse failed for {0}: {1}".format(path.name, err))
                self.assertIn("PARSE_OK", out)


if __name__ == "__main__":
    unittest.main()
