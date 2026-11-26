"""Exercise the guard callback without installing an irreversible test-wide hook."""
import importlib.util
import os
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
        self.module = module
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

    @unittest.skipUnless(Path("/dev/fd").exists(), "directory fds are a POSIX facility")
    def test_relative_name_is_anchored_to_its_dir_fd(self):
        # POSIX shutil.rmtree passes bare names relative to a directory fd; a
        # bare resolve() would read "probe" against the cwd, i.e. the checkout.
        with tempfile.TemporaryDirectory() as outside:
            fd = os.open(outside, os.O_RDONLY)
            try:
                self.audit("os.rmdir", ("probe", fd))
                self.audit("os.rename", ("probe", "moved", fd, fd))
            finally:
                os.close(fd)
        fd = os.open(self.root, os.O_RDONLY)
        try:
            for event, args in [("os.remove", ("probe", fd)), ("os.rmdir", ("probe", fd)),
                                ("os.rename", ("probe", "moved", fd, fd))]:
                with self.subTest(event=event), self.assertRaises(PermissionError):
                    self.audit(event, args)
        finally:
            os.close(fd)

    def test_unverifiable_dir_fd_fails_closed(self):
        with self.assertRaises(PermissionError):
            self.audit("os.remove", ("probe", 10 ** 9))

    def test_truncate_length_is_not_mistaken_for_a_dir_fd(self):
        # os.truncate audits (path_or_fd, length): a RELATIVE path is resolved
        # against the cwd, and the length must never be read as a directory fd.
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        with patch.object(self.module, "_fd_directory") as lookup:
            for value in ("data/probe", os.fsencode("data/probe"), self.root / "data" / "probe"):
                with self.subTest(value=value), self.assertRaises(PermissionError):
                    self.audit("os.truncate", (value, 7))
            # fd taking form: an integer names a descriptor, not a workspace path
            self.audit("os.truncate", (12345, 7))
            lookup.assert_not_called()

    def test_truncate_relative_name_outside_workspace_is_allowed(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as outside:
            os.chdir(outside)
            try:
                with patch.object(self.module, "_fd_directory") as lookup:
                    self.audit("os.truncate", ("probe", 7))
                    lookup.assert_not_called()
            finally:
                # Before the directory is removed: Windows refuses to delete
                # the current directory, so it must be left first.
                os.chdir(cwd)

    def test_rename_resolves_source_and_destination_fds_separately(self):
        # os.rename audits (src, dst, src_dir_fd, dst_dir_fd). Each side is
        # anchored to its own descriptor; the destination fd is not the
        # source's.
        directories = {}

        def fake_fd_directory(dir_fd):
            return directories[dir_fd]

        with tempfile.TemporaryDirectory() as outside, patch.object(
                self.module, "_fd_directory", side_effect=fake_fd_directory):
            directories.update({10: self.root, 20: Path(outside)})
            with self.assertRaises(PermissionError):
                # source outside, destination inside: the write into the checkout is denied
                self.audit("os.rename", ("probe", "probe", 20, 10))
            with self.assertRaises(PermissionError):
                # source inside, destination outside: the removal from the checkout is denied
                self.audit("os.rename", ("probe", "probe", 10, 20))
            self.audit("os.rename", ("probe", "moved", 20, 20))
            self.audit("os.rename", (str(Path(outside) / "probe"), str(Path(outside) / "moved"), 10, 10))
            self.audit("os.rename", (str(Path(outside) / "probe"), str(Path(outside) / "moved")))
            with self.assertRaises(PermissionError):
                self.audit("os.rename", (str(Path(outside) / "probe"), str(self.root / "moved")))
            # os.symlink: the single dir_fd applies to the link being created
            with self.assertRaises(PermissionError):
                self.audit("os.symlink", ("target", "link", 10))
            self.audit("os.symlink", ("target", "link", 20))
            # the target text is never resolved, not even inside the checkout
            self.audit("os.symlink", (str(self.root / "elsewhere"), "link", 20))
            self.audit("os.symlink", (str(self.root / "elsewhere"), str(Path(outside) / "link")))
            # os.link shares rename's signature
            with self.assertRaises(PermissionError):
                self.audit("os.link", ("probe", "probe", 20, 10))
            self.audit("os.link", ("probe", "probe", 20, 20))

    def test_fd_taking_call_is_not_mistaken_for_a_dir_fd(self):
        # os.truncate audits (fd, length): the length must not be used as a base.
        self.audit("os.truncate", (12345, 7))
