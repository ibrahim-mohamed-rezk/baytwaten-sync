"""Keep Arabic and emoji output from crashing on non-UTF-8 Windows consoles (e.g. Git Bash with cp1252)."""
import sys


def utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
