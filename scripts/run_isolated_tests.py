"""Offline unittest entry point; deny writes to the real checkout before discovery.

This is an accidental-write guard, not a sandbox for hostile native code.
Subprocesses are denied because Python audit hooks do not propagate to children.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import urlsplit
from urllib.request import url2pathname

ROOT = Path(__file__).resolve().parents[1]

# Position of the dir_fd argument in each audited call that accepts one
# (os.truncate takes an fd, not a path, so it has none).
_PATH_DIR_FD = {"os.remove": 1, "os.rmdir": 1, "os.mkdir": 2, "os.chmod": 2, "os.utime": 3}


def _arg(args, index):
    return args[index] if len(args) > index else None


def _fd_directory(dir_fd):
    """Directory a fd refers to, or None when the platform cannot tell.

    Linux exposes it through /proc; macOS /dev/fd cannot report directory fds
    (EINVAL) and needs F_GETPATH instead."""
    try:
        import fcntl  # POSIX only
        path = fcntl.fcntl(dir_fd, fcntl.F_GETPATH, bytes(1024))
        return os.fsdecode(path.split(b"\0", 1)[0])
    except (ImportError, AttributeError, OSError):
        pass
    try:
        return os.readlink("/proc/self/fd/{0}".format(dir_fd))
    except OSError:
        return None


def install_guard(root: Path) -> None:
    root = root.resolve()

    def resolve(value, dir_fd=None):
        """Physical path of an audited name.

        POSIX ``shutil.rmtree`` walks with directory fds and passes bare names
        relative to ``dir_fd``; resolving those against the cwd (as a bare
        ``Path.resolve`` would) misattributes them, so anchor to the fd's
        directory and fail closed when that directory cannot be learned."""
        if not isinstance(value, (str, bytes, os.PathLike)):
            return None
        path = Path(os.fsdecode(value))
        if path.is_absolute() or not isinstance(dir_fd, int) or dir_fd < 0:
            return path.resolve()
        base = _fd_directory(dir_fd)
        if base is None:
            raise PermissionError(
                "test isolation: unverifiable dir_fd path denied: " + str(path))
        return (Path(base) / path).resolve()

    def protect(value, dir_fd=None):
        path = resolve(value, dir_fd)
        if path is not None and (path == root or root in path.parents):
            raise PermissionError("test isolation: workspace write denied: " + str(path))

    def audit(event, args):
        if event == "open":
            mode, flags = args[1], args[2]
            if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            ):
                protect(args[0])
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime", "os.truncate"}:
            protect(args[0], _arg(args, _PATH_DIR_FD.get(event, -1)))
        elif event in {"os.rename", "os.link", "os.symlink"}:
            protect(args[0], _arg(args, 2))
            protect(args[1], _arg(args, 3))
        elif event == "sqlite3.connect" and args[0] != ":memory:":
            if str(args[0]).startswith("file:"):
                uri = urlsplit(str(args[0]))
                if uri.netloc or not uri.path.startswith("/"):
                    raise PermissionError("test isolation: non-local SQLite URI denied")
                protect(url2pathname(uri.path))
            else:
                protect(args[0])
        elif event == "socket.connect":
            address = args[1]
            if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
                raise PermissionError("test isolation: external connection denied")
        elif event in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"}:
            raise PermissionError("test isolation: unguarded child process denied")

    sys.addaudithook(audit)


def use_physical_tempdir() -> str:
    """Point ``tempfile`` at the physical temp directory and return it.

    macOS aliases /tmp, /var and /etc to /private/*; the evidence vault and
    run-manifest checks reject any symlinked ancestor by design, so a temp
    root named through an alias would refuse every fixture under it. The checks
    stay strict -- only the test scratch location moves to the real path."""
    named = os.path.abspath(tempfile.gettempdir())
    physical = os.path.realpath(named)
    if physical != named:
        os.makedirs(physical, exist_ok=True)
        tempfile.tempdir = physical
    return physical


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", default="test*.py")
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    use_physical_tempdir()
    sys.path.insert(0, str(ROOT))
    install_guard(ROOT)
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern=args.pattern)
    return 0 if unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
