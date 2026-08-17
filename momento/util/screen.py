"""Screen / display helpers backed by Qt's QGuiApplication.

Kept separate from the ctypes layer in windows_api.py because this needs the
Qt event loop to be initialised (QGuiApplication.instance() must exist),
whereas windows_api is pure Win32 + ctypes.
"""

from __future__ import annotations

from PyQt6.QtGui import QGuiApplication


def primary_refresh_rate(default: int = 60) -> int:
    """Return the primary monitor's refresh rate as an int in [24, 240].

    Falls back to ``default`` if Qt isn't up yet or reports an unusable
    value (some virtual displays return 0). The clamp matches the
    framerate range Momento's recorder advertises.

    Should be called from the Qt main thread once a QGuiApplication
    exists — typically right after the app is constructed in __main__.
    """
    app = QGuiApplication.instance()
    if app is None:
        return default
    screen = app.primaryScreen()
    if screen is None:
        return default
    rate = screen.refreshRate()
    if not rate or rate <= 0:
        return default
    return max(24, min(240, int(round(rate))))
