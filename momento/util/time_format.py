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
