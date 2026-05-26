"""Single time formatter shared by the preview, timeline, and editor.

``H:MM:SS`` for any value above an hour (or when ``force_hours=True``),
``M:SS`` otherwise.
"""

from __future__ import annotations


def fmt_time(seconds: float, *, force_hours: bool = False) -> str:
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h or force_hours:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def parse_time(text: str) -> float | None:
    """Parse "S", "M:SS", or "H:MM:SS" into seconds. Returns None on bad input."""
    if text is None:
        return None
    parts = text.strip().split(":")
    if not (1 <= len(parts) <= 3):
        return None
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if any(n < 0 for n in nums):
        return None
    if len(parts) == 1:
        return nums[0]
    if len(parts) == 2:
        m, s = nums
        return m * 60 + s
    h, m, s = nums
    return h * 3600 + m * 60 + s
