"""Logging + uncaught-exception capture.

stderr + a 2 MB x 5 rotating file in %APPDATA%/Momento/logs/momento.log.
``install_exception_hook()`` routes any uncaught exception through the same
logger so we have a traceback on disk before the app dies (especially
important once Momento runs as a frozen .exe with no console).
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import traceback
from pathlib import Path
from types import TracebackType

from momento.util.paths import logs_dir

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_logger = logging.getLogger("momento.crash")

_QUOTED_WINDOWS_PATH_RE = re.compile(
    r'''(?P<quote>["'])(?:[a-z]:[\\/]+|\\\\+)[^"'\r\n]+(?P=quote)''',
    re.IGNORECASE,
)
_WINDOWS_PATH_RE = re.compile(
    r"(?<![a-z0-9])(?:[a-z]:[\\/]+|\\\\+)[^\r\n]+",
    re.IGNORECASE,
)
_QUOTED_PRIVATE_FILENAME_RE = re.compile(
    r'''(?P<quote>["'])[^"'\\/\r\n]+\.(?:mkv|mp4|json|log|jpe?g|png)(?P=quote)''',
    re.IGNORECASE,
)
_PRIVATE_FILENAME_RE = re.compile(
    r"(?P<prefix>^|[=,(;\s])[^\\/:*?\"'<>|\r\n]*?"
    r"\.(?:mkv|mp4|json|log|jpe?g|png)(?![a-z0-9_.-])",
    re.IGNORECASE,
)
_WASAPI_ENDPOINT_RE = re.compile(
    r"\{[0-9.]+\}\.\{[0-9a-f-]+\}",
    re.IGNORECASE,
)


def redact_log_text(value: str) -> str:
    """Remove share-sensitive paths, filenames, and endpoint identifiers."""
    redacted = _QUOTED_WINDOWS_PATH_RE.sub('"<path>"', str(value))
    redacted = _WINDOWS_PATH_RE.sub("<path>", redacted)
    redacted = _QUOTED_PRIVATE_FILENAME_RE.sub('"<file>"', redacted)
    redacted = _PRIVATE_FILENAME_RE.sub(
        lambda match: f"{match.group('prefix')}<file>", redacted
    )
    return _WASAPI_ENDPOINT_RE.sub("<audio-endpoint>", redacted)


class PrivacyFormatter(logging.Formatter):
    """Redact the complete rendered record, including exception tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_log_text(super().format(record))


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    # Reset handlers so re-running tests doesn't double-log.
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = PrivacyFormatter(_FORMAT)

    stderr_h = logging.StreamHandler(sys.stderr)
    stderr_h.setFormatter(formatter)
    root.addHandler(stderr_h)

    # Try the canonical %APPDATA%/Momento/logs/ first, then a couple of safe
    # fallbacks. A frozen build has no stderr console, so losing the file
    # handler entirely would mean crash logs go nowhere.
    candidates = []
    try:
        candidates.append(logs_dir() / "momento.log")
    except OSError:
        pass
    import tempfile
    candidates.append(Path(tempfile.gettempdir()) / "momento.log")

    for log_file in candidates:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_h = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            file_h.setFormatter(formatter)
            root.addHandler(file_h)
            if log_file != candidates[0]:
                # We had to fall back — make a noisy note about it.
                logging.warning("Using the temporary fallback log location")
            break
        except OSError:
            continue
    else:
        logging.error("Could not open ANY log file destination — logs are stderr-only.")


def install_exception_hook() -> None:
    """Route uncaught exceptions through the logger before the app dies.

    Without this, a frozen Momento.exe with `console=False` would die
    silently — no console, no traceback, nothing to debug. With it, the
    crash gets a full stacktrace in ``%APPDATA%/Momento/logs/momento.log``.
    """
    prev_hook = sys.excepthook

    def _hook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            # Let Ctrl-C behave normally during interactive runs.
            prev_hook(exc_type, exc, tb)
            return
        _logger.critical(
            "Unhandled exception:\n%s",
            "".join(traceback.format_exception(exc_type, exc, tb)),
        )
        # Still call the previous hook (e.g. PyQt's default) so the user
        # might see a Windows error dialog instead of a silent vanish.
        try:
            prev_hook(exc_type, exc, tb)
        except Exception:
            pass

    sys.excepthook = _hook

    # Threads created via threading.Thread don't go through sys.excepthook;
    # threading.excepthook is the equivalent (Python 3.8+).
    import threading

    def _thread_hook(args: threading.ExceptHookArgs) -> None:  # type: ignore[name-defined]
        if issubclass(args.exc_type, KeyboardInterrupt):
            return
        _logger.critical(
            "Unhandled exception in thread %s:\n%s",
            args.thread.name if args.thread else "?",
            "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
        )

    threading.excepthook = _thread_hook
