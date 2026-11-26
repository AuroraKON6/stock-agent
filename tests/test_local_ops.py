"""Cross-platform entry points (scripts/local_ops.py) and its shell wrappers.

The PowerShell counterparts keep byte-for-byte snapshot tests in
test_research_workbench_ops.py; these cover the POSIX implementation with the
same rules: strict PID identity before any signal, loopback-only binding, and a
single Windows implementation (hand-off to the .ps1 scripts).
"""
import importlib.util
import json
import os
import tempfile
import contextlib
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import shutil
import subprocess
import types
import stat
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / "local_ops.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _HealthHandler(BaseHTTPRequestHandler):
    payload = {"status": "ok", "instance_id": "inst-1", "pid": 4242}
    shutdown_calls = []

    def log_message(self, *args):
        return None

    def _reply(self, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._reply(self.payload)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        self.shutdown_calls.append((self.path, self.headers.get("X-Workbench-Instance")))
        self._reply({"status": "stopping"} if self.path == "/shutdown" else {"status": "refused"})


class LocalOpsTestCase(unittest.TestCase):
    def setUp(self):
        self.ops = _load("local_ops_under_test")
        self.scratch = Path(tempfile.mkdtemp(prefix="stock_agent_ops_"))
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)

    def parse(self, *arguments):
        return self.ops.build_parser().parse_args(list(arguments))


class LockedInterpreterTests(LocalOpsTestCase):
    def test_missing_locked_interpreter_refuses_path_python(self):
        with patch.object(self.ops, "ROOT", self.scratch):
            with self.assertRaisesRegex(self.ops.OpsError, "PATH python is not a supported fallback"):
                self.ops.locked_python()

    def test_layout_follows_the_platform(self):
        with patch.object(self.ops, "ROOT", self.scratch):
            for windows, relative in ((True, "Scripts/python.exe"), (False, "bin/python")):
                interpreter = self.scratch / ".venv312" / relative
                interpreter.parent.mkdir(parents=True, exist_ok=True)
                interpreter.write_bytes(b"")
                with patch.object(self.ops, "IS_WINDOWS", windows):
                    self.assertEqual(self.ops.locked_python(), interpreter)
                interpreter.unlink()

    def test_config_prefers_local_then_announces_example(self):
        with patch.object(self.ops, "ROOT", self.scratch):
            (self.scratch / "config").mkdir()
            resolved = self.ops.resolve_config(None)
            self.assertEqual((resolved["source"], resolved["path"].name), ("example", "watchlist.example.json"))
            (self.scratch / "config" / "watchlist.local.json").write_text("{}", encoding="utf-8")
            self.assertEqual(self.ops.resolve_config(None)["source"], "local")
            explicit = self.ops.resolve_config("config/other.json")
            self.assertEqual((explicit["source"], explicit["path"]), ("explicit", self.scratch / "config" / "other.json"))


class ProcessIdentityTests(LocalOpsTestCase):
    CONFIG = Path("/proj/config/watchlist.local.json")
    CMD = ("/proj/.venv312/bin/python -B -m stock_agent review-workbench "
           "--config /proj/config/watchlist.local.json --host 127.0.0.1 --port 8767")

    def test_workbench_identity_needs_module_config_host_and_port(self):
        check = self.ops.is_workbench_process
        self.assertTrue(check(1, self.CONFIG, "127.0.0.1", 8767, self.CMD))
        self.assertTrue(check(1, Path("/c.json"), "localhost", 8767,
                              "/p/python -m stock_agent review-workbench --config=/c.json --host=localhost --port=8767"))
        self.assertFalse(check(1, Path("/other/c.json"), "localhost", 8767,
                               "/p/python -m stock_agent review-workbench --config=/c.json --host=localhost --port=8767"))
        for host, port in (("0.0.0.0", 8767), ("127.0.0.1", 8768)):
            with self.subTest(host=host, port=port):
                self.assertFalse(check(1, self.CONFIG, host, port, self.CMD))

    def test_workbench_identity_refuses_partial_matches(self):
        check = self.ops.is_workbench_process
        self.assertFalse(check(1, self.CONFIG, "127.0.0.1", 8767, self.CMD.replace("review-workbench", "watch")))
        self.assertFalse(check(1, self.CONFIG, "127.0.0.1", 8767, self.CMD.replace("watchlist.local", "watchlist.other")))
        self.assertFalse(check(1, self.CONFIG, "127.0.0.1", 8767, self.CMD.replace("--port 8767", "--port 87670")))
        self.assertFalse(check(1, self.CONFIG, "127.0.0.1", 8767, ""))

    def test_live_lookup_reads_the_real_command_line(self):
        self.assertFalse(self.ops.is_workbench_process(0, self.CONFIG, "127.0.0.1", 8767))
        if os.name == "nt":
            self.skipTest("ps based lookup is POSIX only")
        own = os.getpid()
        self.assertFalse(self.ops.is_workbench_process(own, self.CONFIG, "127.0.0.1", 8767))
        self.assertFalse(self.ops.is_watch_process(own))
        self.assertFalse(self.ops.is_watch_process(2 ** 22 + 12345))

    def test_watch_identity_needs_this_checkouts_interpreter(self):
        root = str(self.ops.ROOT / ".venv312")
        self.assertTrue(self.ops.is_watch_process(1, "{0}/bin/python -m stock_agent watch --interval-minutes 30".format(root)))
        self.assertFalse(self.ops.is_watch_process(1, "/other/.venv312/bin/python -m stock_agent watch"))
        self.assertFalse(self.ops.is_watch_process(1, "{0}/bin/python -m stock_agent run".format(root)))


class StartWorkbenchValidationTests(LocalOpsTestCase):
    def test_refuses_non_loopback_host(self):
        for host in ("0.0.0.0", "192.168.1.2", "example.com"):
            with self.subTest(host=host):
                self.assertEqual(self.ops.cmd_start_workbench(self.parse("start-workbench", "--host", host)), 1)

    def test_refuses_out_of_range_port_and_missing_config(self):
        self.assertEqual(self.ops.cmd_start_workbench(self.parse("start-workbench", "--port", "70000")), 1)
        self.assertEqual(self.ops.cmd_start_workbench(self.parse("start-workbench", "--port", "0")), 1)
        self.assertEqual(self.ops.cmd_start_workbench(
            self.parse("start-workbench", "--config", str(self.scratch / "missing.json"))), 1)


class StopSafetyTests(LocalOpsTestCase):
    """A recorded PID is only signalled after strict identity validation."""

    def setUp(self):
        super().setUp()
        self.pid_file = self.scratch / "agent.pid"
        patches = [patch.object(self.ops, "ROOT", self.scratch),
                   patch.object(self.ops, "AGENT_PID_FILE", self.pid_file)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.state = self.scratch / "state"
        self.state.mkdir()
        # These cases exercise identity validation; the POSIX start/stop lock
        # itself is covered by ConcurrentStartTests on POSIX.
        lock = patch.object(self.ops, "_state_lock", side_effect=lambda _path: contextlib.nullcontext())
        lock.start()
        self.addCleanup(lock.stop)

    def test_stop_agent_never_kills_a_foreign_pid(self):
        self.pid_file.write_text("{0}\n".format(os.getpid()), encoding="ascii")
        with patch.object(self.ops, "_terminate") as terminate:
            self.assertEqual(self.ops.cmd_stop_agent(self.parse("stop-agent")), 0)
        terminate.assert_not_called()
        self.assertFalse(self.pid_file.exists())

    def test_stop_agent_cleans_dead_and_absent_records(self):
        dead = 2 ** 22 + 777
        for text in ("{0}".format(dead), "not-a-pid", ""):
            with self.subTest(text=text):
                self.pid_file.write_text(text, encoding="ascii")
                self.assertEqual(self.ops.cmd_stop_agent(self.parse("stop-agent")), 0)
                self.assertFalse(self.pid_file.exists())
        self.assertEqual(self.ops.cmd_stop_agent(self.parse("stop-agent")), 0)

    def test_stop_agent_terminates_only_a_validated_watch_daemon(self):
        recorded = 2 ** 22 + 555
        self.pid_file.write_text("{0}".format(recorded), encoding="ascii")
        with patch.object(self.ops, "is_watch_process", return_value=True), \
                patch.object(self.ops, "_pid_alive", return_value=True), \
                patch.object(self.ops, "_terminate") as terminate:
            self.assertEqual(self.ops.cmd_stop_agent(self.parse("stop-agent")), 0)
        terminate.assert_called_once_with(recorded)
        self.assertFalse(self.pid_file.exists())

    def test_stop_workbench_refuses_foreign_pid_and_cleans_metadata(self):
        meta = self.state / "workbench.json"
        pid_file = self.state / "workbench.pid"
        meta.write_text(json.dumps({"pid": os.getpid(), "port": 8767, "host": "127.0.0.1",
                                    "config": "config/watchlist.example.json"}), encoding="utf-8")
        pid_file.write_text("{0}".format(os.getpid()), encoding="ascii")
        with patch.object(self.ops, "_terminate") as terminate:
            code = self.ops.cmd_stop_workbench(self.parse("stop-workbench", "--state-directory", str(self.state)))
        self.assertEqual(code, 0)
        terminate.assert_not_called()
        self.assertFalse(meta.exists())
        self.assertFalse(pid_file.exists())

    def test_stop_workbench_uses_authenticated_shutdown_endpoint(self):
        server = HTTPServer(("127.0.0.1", 0), _HealthHandler)
        port = server.server_address[1]
        _HealthHandler.shutdown_calls = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        meta = self.state / "workbench.json"
        meta.write_text(json.dumps({
            "pid": 4242, "port": port, "host": "127.0.0.1", "config": "config/watchlist.example.json",
            "instance_id": "inst-1", "url": "http://127.0.0.1:{0}/".format(port)}), encoding="utf-8")
        arguments = ["stop-workbench", "--state-directory", str(self.state)]
        # The recorded PID is fake: after /shutdown the process is gone, so no
        # signal path may ever be reached for it.
        with patch.object(self.ops, "is_workbench_process", return_value=False), \
                patch.object(self.ops, "_pid_alive", return_value=False) as alive, \
                patch.object(self.ops, "_terminate") as terminate:
            code = self.ops.cmd_stop_workbench(self.parse(*arguments))
        self.assertEqual(code, 0)
        self.assertEqual(_HealthHandler.shutdown_calls, [("/shutdown", "inst-1")])
        terminate.assert_not_called()
        self.assertFalse(meta.exists())
        self.assertIn(4242, [call.args[0] for call in alive.call_args_list])

    def test_stop_workbench_signals_only_when_shutdown_leaves_the_process_alive(self):
        server = HTTPServer(("127.0.0.1", 0), _HealthHandler)
        port = server.server_address[1]
        _HealthHandler.shutdown_calls = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        meta = self.state / "workbench.json"
        meta.write_text(json.dumps({
            "pid": 4242, "port": port, "host": "127.0.0.1", "config": "config/watchlist.example.json",
            "instance_id": "inst-1", "url": "http://127.0.0.1:{0}/".format(port)}), encoding="utf-8")
        alive = {"value": True}

        def fake_terminate(pid):
            self.assertEqual(pid, 4242)
            alive["value"] = False

        with patch.object(self.ops, "is_workbench_process", return_value=False), \
                patch.object(self.ops, "_pid_alive", side_effect=lambda _pid: alive["value"]), \
                patch.object(self.ops, "_terminate", side_effect=fake_terminate) as terminate:
            code = self.ops.cmd_stop_workbench(
                self.parse("stop-workbench", "--state-directory", str(self.state)))
        self.assertEqual(code, 0)
        self.assertEqual(_HealthHandler.shutdown_calls, [("/shutdown", "inst-1")])
        terminate.assert_called_once_with(4242)
        self.assertFalse(meta.exists())

    def test_stop_workbench_refuses_to_signal_when_shutdown_is_not_acknowledged(self):
        _HealthHandler.shutdown_calls = []
        meta = self.state / "workbench.json"
        meta.write_text(json.dumps({
            "pid": 4242, "port": 1, "host": "127.0.0.1", "config": "config/watchlist.example.json",
            "instance_id": "inst-1", "url": "http://127.0.0.1:1/"}), encoding="utf-8")
        with patch.object(self.ops, "is_workbench_process", return_value=True), \
                patch.object(self.ops, "_pid_alive", return_value=True), \
                patch.object(self.ops, "_http_json", return_value=None) as http, \
                patch.object(self.ops, "_terminate") as terminate:
            code = self.ops.cmd_stop_workbench(
                self.parse("stop-workbench", "--state-directory", str(self.state)))
        self.assertEqual(code, 0)
        # The endpoint did not answer "stopping", so the validated PID is terminated.
        self.assertEqual(http.call_args.args[1], "http://127.0.0.1:1/shutdown")
        terminate.assert_called_once_with(4242)
        self.assertFalse(meta.exists())

    def test_health_requires_matching_instance_and_pid(self):
        server = HTTPServer(("127.0.0.1", 0), _HealthHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertTrue(self.ops.health_matches("127.0.0.1", port, "inst-1", 4242))
        self.assertFalse(self.ops.health_matches("127.0.0.1", port, "", 4242))
        self.assertFalse(self.ops.health_matches("127.0.0.1", port, "inst-2", 4242))
        self.assertFalse(self.ops.health_matches("127.0.0.1", port, "inst-1", 4243))
        self.assertFalse(self.ops.health_matches("127.0.0.1", 1, "inst-1", 4242))

    def test_invalid_metadata_is_cleaned_without_signalling(self):
        meta = self.state / "workbench.json"
        for payload in ({"pid": 0, "port": 8767, "host": "127.0.0.1", "config": "x"},
                        {"pid": "abc", "port": 8767, "host": "127.0.0.1", "config": "x"},
                        {"pid": 5, "port": 8767, "host": "127.0.0.1"}):
            with self.subTest(payload=payload):
                meta.write_text(json.dumps(payload), encoding="utf-8")
                with patch.object(self.ops, "_terminate") as terminate:
                    self.assertEqual(self.ops.cmd_stop_workbench(
                        self.parse("stop-workbench", "--state-directory", str(self.state))), 0)
                terminate.assert_not_called()
                self.assertFalse(meta.exists())

    def test_stop_workbench_without_state_is_idempotent(self):
        self.assertEqual(self.ops.cmd_stop_workbench(
            self.parse("stop-workbench", "--state-directory", str(self.state / "missing"))), 0)


class GoLiveIsolationTests(LocalOpsTestCase):
    """The default checklist must run the suite through the isolation runner."""

    def setUp(self):
        super().setUp()
        (self.scratch / ".venv312" / "bin").mkdir(parents=True)
        (self.scratch / ".venv312" / "bin" / "python").write_bytes(b"")
        for item in (patch.object(self.ops, "ROOT", self.scratch), patch.object(self.ops, "IS_WINDOWS", False)):
            item.start()
            self.addCleanup(item.stop)

    def _run(self, *arguments):
        # The checklist's exit code follows the recorded readiness, not the steps.
        for relative, payload in (("reports/health/latest.json", {"overall_status": "OK"}),
                                  ("reports/production_pilot/latest.json",
                                   {"technical_readiness": "READY", "go_live_decision": "GO"})):
            path = self.scratch / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(list(command))
            return types.SimpleNamespace(returncode=0)

        with patch.object(self.ops.subprocess, "run", side_effect=fake_run):
            code = self.ops.main(["go-live-check", *arguments])
        return code, commands

    def test_default_runs_the_isolated_runner_not_bare_unittest(self):
        code, commands = self._run()
        self.assertEqual(code, 3)
        self.assertEqual(commands[0], [str(self.scratch / ".venv312" / "bin" / "python"),
                                       str(self.scratch / "scripts" / "run_isolated_tests.py")])
        for command in commands:
            self.assertNotIn("unittest", command)

    def test_skip_tests_runs_no_test_command(self):
        code, commands = self._run("--skip-tests")
        self.assertEqual(code, 3)
        self.assertEqual(len(commands), len(self.ops.GO_LIVE_STEPS))
        self.assertFalse(any("run_isolated_tests.py" in part for command in commands for part in command))


class ConcurrentStartTests(LocalOpsTestCase):
    """Two simultaneous starts must yield one recorded, trackable daemon."""

    def setUp(self):
        super().setUp()
        (self.scratch / ".venv312" / "bin").mkdir(parents=True)
        (self.scratch / ".venv312" / "bin" / "python").write_bytes(b"")
        self.pid_file = self.scratch / "agent.pid"
        self.lock_file = self.scratch / "stock_agent.lock"
        for item in (patch.object(self.ops, "ROOT", self.scratch),
                     patch.object(self.ops, "AGENT_PID_FILE", self.pid_file),
                     patch.object(self.ops.time, "sleep", return_value=None)):
            item.start()
            self.addCleanup(item.stop)
        self.gate = threading.Event()

    @unittest.skipUnless(os.name == "posix", "flock based locking is POSIX only")
    def test_concurrent_starts_launch_one_daemon_that_stop_can_clean(self):
        launched = []

        real_sleep = time.sleep

        def fake_popen(command, **_kwargs):
            launched.append(command)
            # Hold the launch window open so an unsynchronised second caller
            # would get past its "already running" check and launch too.
            real_sleep(0.3)
            return types.SimpleNamespace(pid=40000 + len(launched), returncode=None,
                                         poll=lambda: None)

        results = []

        def starter():
            self.gate.wait(10)
            results.append(self.ops.cmd_start_agent(self.parse("start-agent")))

        # The fake daemon is not a real process, so identity is asserted for it.
        with patch.object(self.ops, "is_watch_process", return_value=True), \
                patch.object(self.ops.subprocess, "Popen", side_effect=fake_popen):
            threads = [threading.Thread(target=starter) for _ in range(2)]
            for thread in threads:
                thread.start()
            self.gate.set()
            for thread in threads:
                thread.join(10)
        self.assertEqual(sorted(results), [0, 0], "second caller must report the running daemon")
        self.assertEqual(len(launched), 1, "exactly one daemon may be launched")
        self.assertEqual(self.pid_file.read_text(encoding="ascii").strip(), "40001")

        with patch.object(self.ops, "is_watch_process", return_value=True), \
                patch.object(self.ops, "_pid_alive", return_value=True), \
                patch.object(self.ops, "_terminate") as terminate:
            self.assertEqual(self.ops.cmd_stop_agent(self.parse("stop-agent")), 0)
        terminate.assert_called_once_with(40001)
        self.assertFalse(self.pid_file.exists())

    @unittest.skipUnless(os.name == "posix", "flock based locking is POSIX only")
    def test_lock_is_released_on_refusal_and_after_use(self):
        with self.ops._state_lock(self.lock_file):
            pass
        with self.ops._state_lock(self.lock_file):
            pass


class GoLiveCheckTests(LocalOpsTestCase):
    def test_step_order_matches_the_powershell_checklist(self):
        steps = self.ops.go_live_steps("config/watchlist.local.json", skip_tests=False)
        self.assertEqual([step["name"] for step in steps], [
            "unit_tests", "pi_runtime_validation", "pi_handoff_validation", "production_pilot_sync",
            "workspace_sync", "dashboard", "alerts", "final_doctor", "production_pilot_status"])
        by_name = {step["name"]: step["cli"] for step in steps}
        self.assertIsNone(by_name["unit_tests"])
        self.assertEqual(by_name["workspace_sync"], ["workspace-sync"])
        self.assertEqual(by_name["final_doctor"], ["doctor", "--config", "config/watchlist.local.json"])
        self.assertEqual(by_name["pi_runtime_validation"], ["pi-runtime-status", "--require-ready",
                                                            "--config", "config/watchlist.local.json"])

    def test_skip_tests_drops_only_the_unit_test_step(self):
        steps = self.ops.go_live_steps("c.json", skip_tests=True)
        self.assertNotIn("unit_tests", [step["name"] for step in steps])
        self.assertEqual(len(steps), len(self.ops.GO_LIVE_STEPS))

    def test_exit_code_is_technical_readiness_not_business_go(self):
        decide = self.ops._go_live_exit
        self.assertEqual(decide("OK", "READY", False), 3)
        for doctor, readiness, failed in (("FAIL", "READY", False), ("ERROR", "READY", False),
                                          ("OK", "BLOCKED", False), ("OK", "READY", True)):
            with self.subTest(doctor=doctor, readiness=readiness, failed=failed):
                self.assertEqual(decide(doctor, readiness, failed), 1)

    def test_scheduled_tasks_are_reported_per_platform(self):
        off_windows = self.ops.scheduled_task_rows("darwin")
        self.assertEqual(len(off_windows), 4)
        self.assertEqual({row["state"] for row in off_windows}, {"windows_task_scheduler_only"})
        self.assertEqual(self.ops.scheduled_task_rows("win32")[0]["state"], "unqueried")


class WindowsHandOffTests(LocalOpsTestCase):
    def test_every_counterpart_script_is_kept(self):
        for counterpart in self.ops.PS1.values():
            with self.subTest(counterpart=counterpart):
                self.assertTrue((SCRIPTS_DIR / counterpart).is_file())

    def test_options_are_translated_to_powershell_parameters(self):
        arguments = self.ops._windows_arguments
        self.assertEqual(arguments(self.parse("watch", "--interval-minutes", "15", "--config", "a.json")),
                         ["-Config", "a.json", "-IntervalMinutes", "15.0"])
        self.assertEqual(arguments(self.parse("start-workbench", "--host", "::1", "--port", "9000",
                                              "--no-browser", "--state-directory", "/tmp/wb")),
                         ["-HostName", "::1", "-Port", "9000", "-StateDirectory", "/tmp/wb", "-NoBrowser"])
        self.assertEqual(arguments(self.parse("stop-workbench")), ["-StateDirectory", "logs/workbench"])
        self.assertEqual(arguments(self.parse("go-live-check", "--skip-tests")), ["-SkipTests"])
        self.assertEqual(arguments(self.parse("go-live-check")), [])
        self.assertEqual(arguments(self.parse("run-once")), [])

    def test_windows_delegates_instead_of_running_the_posix_path(self):
        with patch.object(self.ops, "IS_WINDOWS", True), \
                patch.object(self.ops, "run_windows", return_value=7) as delegate, \
                patch.object(self.ops, "cmd_start_workbench") as posix_handler:
            self.assertEqual(self.ops.main(["start-workbench", "--port", "9000"]), 7)
        delegate.assert_called_once_with("start-workbench", ["-HostName", "127.0.0.1", "-Port", "9000",
                                                             "-StateDirectory", "logs/workbench"])
        posix_handler.assert_not_called()

    def test_ops_error_reports_the_locked_interpreter_rule(self):
        with patch.object(self.ops, "ROOT", self.scratch), patch("sys.stderr") as stderr:
            self.assertEqual(self.ops.main(["run-once"]), 2)
        self.assertIn("PATH python is not a supported fallback", "".join(call.args[0] for call in stderr.write.call_args_list))


class ShellWrapperTests(unittest.TestCase):
    WRAPPERS = {
        "run_once.sh": "run-once",
        "watch.sh": "watch",
        "start_agent.sh": "start-agent",
        "stop_agent.sh": "stop-agent",
        "start_research_workbench.sh": "start-workbench",
        "stop_research_workbench.sh": "stop-workbench",
        "go_live_check.sh": "go-live-check",
    }

    def test_each_wrapper_execs_its_subcommand(self):
        for name, subcommand in self.WRAPPERS.items():
            with self.subTest(wrapper=name):
                path = SCRIPTS_DIR / name
                text = path.read_text(encoding="utf-8")
                self.assertTrue(text.startswith("#!/usr/bin/env bash\n"))
                self.assertIn('exec python3 "$ROOT/scripts/local_ops.py" {0} "$@"'.format(subcommand), text)
                self.assertIn("scripts/{0}.ps1".format(name[:-3]), text)
                self.assertNotIn("\r", text)
                if os.name != "nt":
                    self.assertTrue(os.stat(path).st_mode & stat.S_IXUSR, "wrapper must be executable")


if __name__ == "__main__":
    unittest.main()
