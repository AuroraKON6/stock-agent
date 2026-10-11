from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.python_environment import (
    build_python_environment_audit,
    write_python_environment_audit,
)


NOW = datetime(2026, 9, 2, 9, 0, 0, tzinfo=timezone(timedelta(hours=8)))


class PythonEnvironmentAuditTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self._write(
            "requirements.lock.txt",
            "alpha==1.0 \\\n+    --hash=sha256:" + "a" * 64 + "\n"
            "beta-package==2.0 \\\n+    --hash=sha256:" + "b" * 64 + "\n",
        )
        self._write(".venv311/Scripts/python.exe", "")
        self._write(".venv312/Scripts/python.exe", "")
        self._write("stock_agent/example.py", "VALUE = 1\n")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write(self, relative: str, value: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    def _runner(self, *, mismatch: bool = False, tests_ok: bool = True):
        def runner(command, cwd, env, timeout):
            executable = str(command[0])
            version = "3.11.15" if "311" in executable else "3.12.13"
            if len(command) >= 3 and command[1] == "-c" and "importlib.metadata" in command[2]:
                packages = [["alpha", "9.9" if mismatch else "1.0"], ["beta_package", "2.0"]]
                return 0, json.dumps({
                    "version": version,
                    "implementation": "CPython",
                    "executable": executable,
                    "packages": packages,
                }) + "\n", "", 0.1
            if len(command) >= 3 and command[1] == "-c" and "akshare" in command[2]:
                return 0, '{"akshare":"1","pysnowball":"1","pyyaml":"1"}\n', "", 0.1
            if "unittest" in command:
                if tests_ok:
                    return 0, "", "Ran 751 tests in 10.0s\n\nOK (skipped=2)\n", 10.0
                return 1, "", "Ran 751 tests in 10.0s\n\nFAILED (failures=1)\n", 10.0
            return 1, "", "unsupported", 0.0

        return runner

    def _build(self, **kwargs):
        with project_path_override(lambda value: self.root / str(value)):
            return build_python_environment_audit(
                kwargs.pop("python_paths", None),
                run_tests=kwargs.pop("run_tests", True),
                now=NOW,
                command_runner=kwargs.pop("command_runner", self._runner()),
                **kwargs,
            )

    def test_two_locked_modern_environments_pass(self):
        audit = self._build()
        self.assertEqual(audit["status"], "PASS")
        self.assertEqual(audit["locked_package_count"], 2)
        self.assertEqual([item["minor"] for item in audit["environments"]], ["3.11", "3.12"])
        self.assertTrue(all(item["lock_match"] for item in audit["environments"]))
        self.assertTrue(all(item["test_suite"]["test_count"] == 751 for item in audit["environments"]))
        self.assertEqual(len(audit["release_candidate_fingerprint"]), 64)
        self.assertTrue(audit["release_candidate_stable"])
        self.assertFalse(audit["operation_contract"]["test_output_recorded"])

    def test_tests_must_be_explicitly_run_for_pass(self):
        audit = self._build(run_tests=False)
        self.assertEqual(audit["status"], "BLOCKED")
        self.assertTrue(all(item["test_suite"]["status"] == "NOT_RUN" for item in audit["environments"]))

    def test_lock_version_mismatch_blocks(self):
        audit = self._build(command_runner=self._runner(mismatch=True))
        self.assertEqual(audit["status"], "BLOCKED")
        self.assertFalse(audit["environments"][0]["lock_match"])
        self.assertIn("expected 1.0, installed 9.9", audit["environments"][0]["version_mismatches"][0])

    def test_failed_test_suite_blocks(self):
        audit = self._build(command_runner=self._runner(tests_ok=False))
        self.assertEqual(audit["status"], "BLOCKED")
        self.assertTrue(all(item["test_suite"]["status"] == "FAIL" for item in audit["environments"]))

    def test_missing_required_minor_blocks(self):
        audit = self._build(python_paths=[".venv311/Scripts/python.exe"])
        self.assertEqual(audit["status"], "BLOCKED")
        self.assertEqual(audit["missing_minors"], ["3.12"])

    def test_source_change_during_test_blocks_evidence(self):
        base_runner = self._runner()
        changed = {"done": False}

        def runner(command, cwd, env, timeout):
            result = base_runner(command, cwd, env, timeout)
            if "unittest" in command and not changed["done"]:
                self._write("stock_agent/example.py", "VALUE = 2\n")
                changed["done"] = True
            return result

        audit = self._build(command_runner=runner)
        self.assertEqual(audit["status"], "BLOCKED")
        self.assertFalse(audit["release_candidate_stable"])
        self.assertIn("Release candidate changed during Python environment audit", audit["blockers"])

    def test_write_creates_release_control_without_raw_output(self):
        with project_path_override(lambda value: self.root / str(value)):
            output = write_python_environment_audit(
                run_tests=True,
                now=NOW,
                command_runner=self._runner(),
            )
        json_path = Path(output["python_environment_json_path"])
        md_path = Path(output["python_environment_path"])
        self.assertTrue(json_path.is_file())
        self.assertTrue(md_path.is_file())
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "PASS")
        self.assertNotIn("Ran 751 tests", json_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
