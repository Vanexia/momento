"""Single-instance enforcement via an exclusive lock on a file under %APPDATA%.

The lock is held by the process for its lifetime — if the process crashes, the
OS releases the lock automatically. The lock file itself is best-effort
deleted on clean exit.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from momento.util.paths import appdata_dir

logger = logging.getLogger(__name__)


class AlreadyRunningError(RuntimeError):
    """Raised when another Momento instance is already holding the lock."""


class SingleInstance:
    """Context manager that acquires an exclusive lock on a file in APPDATA.

    Usage::

        try:
            with SingleInstance():
                run_app()
        except AlreadyRunningError:
            print("Momento is already running.")
            sys.exit(1)
    """

    def __init__(self, lock_path: Path | None = None) -> None:
        self._lock_path = lock_path or (appdata_dir() / "momento.lock")
        self._fh = None  # type: ignore[assignment]

    def __enter__(self) -> "SingleInstance":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def acquire(self) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self._lock_path, "wb+")

        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as e:
                fh.close()
                raise AlreadyRunningError(
                    f"Another Momento instance is already running (lock: {self._lock_path})"
                ) from e
        else:
            # Best-effort on non-Windows for dev convenience
            import fcntl  # type: ignore[import-not-found]

            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                fh.close()
                raise AlreadyRunningError(
                    f"Another Momento instance is already running (lock: {self._lock_path})"
                ) from e

        # Record PID for visibility (not used programmatically — the lock is the
        # source of truth).
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()).encode("ascii"))
        fh.flush()
        self._fh = fh
        logger.info("Acquired single-instance lock at %s (pid=%d)", self._lock_path, os.getpid())

    def release(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            fh.close()
        except Exception:
            logger.exception("Error closing single-instance lock handle")
        try:
            self._lock_path.unlink()
        except OSError:
            pass
        logger.info("Released single-instance lock")
