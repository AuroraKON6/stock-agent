"""Exercise the guard callback without installing an irreversible test-wide hook."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class IsolationTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "isolated_runner", Path(__file__).resolve().parents[1] / "scripts/run_isolated_tests.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.root = Path(__file__).resolve().parents[1]
        with patch.object(module.sys, "addaudithook") as hook:
            module.install_guard(self.root)
        self.audit = hook.call_args.args[0]

    def test_writes_and_database_blocked(self):
        for event, args in [
            ("open", (str(self.root / "data/probe"), "w", 0)),
            ("sqlite3.connect", (str(self.root / "data/probe.sqlite"),)),
            ("os.rename", (str(Path(tempfile.gettempdir()) / "probe"), str(self.root / "probe"))),
        ]:
            with self.subTest(event=event), self.assertRaises(PermissionError):
                self.audit(event, args)

    def test_temp_write_and_loopback_allowed(self):
        self.audit("open", (str(Path(tempfile.gettempdir()) / "probe"), "w", 0))
        self.audit("socket.connect", (None, ("127.0.0.1", 12345)))

    def test_external_and_child_denied(self):
        for event, args in [("socket.connect", (None, ("example.com", 443))),
                            ("subprocess.Popen", ("python",))]:
            with self.subTest(event=event), self.assertRaises(PermissionError):
                self.audit(event, args)

    def test_sqlite_uri_cannot_escape_guard(self):
        with self.assertRaises(PermissionError):
            self.audit("sqlite3.connect", ((self.root / "data/encoded name.sqlite").as_uri() + "?mode=ro",))
        self.audit("sqlite3.connect", ((Path(tempfile.gettempdir()) / "encoded name.sqlite").as_uri() + "?mode=ro",))
        with self.assertRaises(PermissionError):
            self.audit("sqlite3.connect", ("file://other-host/database.sqlite?mode=ro",))
