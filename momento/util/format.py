"""Shared display + filesystem formatters.

Sits next to ``time_format`` — same role for byte sizes and disk-space
lookups.
"""

from __future__ import annotations

import shutil
from pathlib import Path


_BYTE_UNITS: tuple[tuple[str, int], ...] = (
    ("TB", 1 << 40),
    ("GB", 1 << 30),
    ("MB", 1 << 20),
    ("KB", 1 << 10),
)


def format_bytes(n: int) -> str:
    """Compact human-readable size, e.g. ``"1.2 TB"`` / ``"47.0 GB"`` / ``"12 B"``."""
    n = max(0, int(n))
    for unit, threshold in _BYTE_UNITS:
        if n >= threshold:
            return f"{n / threshold:.1f} {unit}"
    return f"{n} B"


def free_bytes_for(path: Path) -> int | None:
    """Free bytes on the volume containing ``path``.

    Walks up via ``disk_usage`` failures so a not-yet-created output
    folder still resolves to its drive. Avoids ``Path.exists`` probing —
    each existence check on a UNC ancestor can stall on SMB, so we let
    the operation itself fail and step up at most a few levels.
    """
    probe = path
    for _ in range(8):
        try:
            return shutil.disk_usage(probe).free
        except OSError:
            if probe.parent == probe:
                return None
            probe = probe.parent
    return None
