"""Offline unittest entry point; deny writes to the real checkout before discovery.

This is an accidental-write guard, not a sandbox for hostile native code.
Subprocesses are denied because Python audit hooks do not propagate to children.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import unittest
from urllib.parse import urlsplit
from urllib.request import url2pathname

ROOT = Path(__file__).resolve().parents[1]


def install_guard(root: Path) -> None:
    root = root.resolve()

    def protect(value):
        if isinstance(value, (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(value)).resolve()
            if path == root or root in path.parents:
                raise PermissionError("test isolation: workspace write denied: " + str(path))

    def audit(event, args):
        if event == "open":
            mode, flags = args[1], args[2]
            if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            ):
                protect(args[0])
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime", "os.truncate"}:
            protect(args[0])
        elif event in {"os.rename", "os.link", "os.symlink"}:
            protect(args[0])
            protect(args[1])
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", default="test*.py")
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT))
    install_guard(ROOT)
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern=args.pattern)
    return 0 if unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
