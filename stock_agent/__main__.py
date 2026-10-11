import sys

from .cli import main


def _force_utf8_console() -> None:
    """Make CLI output readable on Windows legacy code pages.

    The default cp936 console mangles Chinese report text (handoff doc 7.15).
    Reconfigure both streams to UTF-8 with replacement so no code page can
    crash a run; pipes and redirected files get the same treatment.
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


if __name__ == "__main__":
    _force_utf8_console()
    main()
