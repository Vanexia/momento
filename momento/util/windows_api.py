"""Thin ctypes helpers around user32 — HWND lookup, window geometry.

Avoids a pywin32 dependency. Windows-only.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

if sys.platform == "win32":
    _user32 = ctypes.windll.user32

    # Per-monitor v2 DPI awareness for the whole process so GetWindowRect /
    # GetClientRect return *physical* pixels (matching WGC frame sizes).
    # Without this, ffmpeg's rawvideo input is sized wrong on HiDPI systems.
    # DPI_AWARENESS_CONTEXT is a HANDLE (void*), not an int, so the sentinel
    # values must be wrapped in c_void_p — passing a bare -4 yields error 87.
    def _set_dpi_aware() -> None:
        try:
            _user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            _user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
            ok = _user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            )
            if ok:
                return
        except (AttributeError, OSError):
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
            return
        except (AttributeError, OSError):
            pass
        try:
            _user32.SetProcessDPIAware()  # system DPI fallback
        except (AttributeError, OSError):
            pass

    _set_dpi_aware()
else:  # pragma: no cover — module is windows-only at runtime
    _user32 = None


def logical_drives() -> list[Path]:
    """Return roots of every mounted drive — ``[Path('C:/'), Path('D:/'), …]``.

    Empty on non-Windows or when the kernel call fails. Uses the
    bitmask form so we never poke a non-existent drive letter and
    trigger a "no disk in drive A:" UI prompt.
    """
    if sys.platform != "win32":
        return []
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except (OSError, AttributeError):
        return []
    return [
        Path(f"{chr(ord('A') + i)}:/")
        for i in range(26)
        if mask & (1 << i)
    ]


def find_main_hwnd_for_pid(pid: int) -> int | None:
    """Return the HWND of the largest visible top-level window owned by ``pid``.

    Returns None if no suitable window exists yet (caller can retry — many games
    take a few seconds to create their main window after launch).
    """
    if _user32 is None:
        return None
    hits: list[tuple[int, int]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd: int, _lparam: int) -> bool:
        owner_pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        if owner_pid.value != pid:
            return True
        if not _user32.IsWindowVisible(hwnd):
            return True
        rect = wintypes.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(rect))
        area = (rect.right - rect.left) * (rect.bottom - rect.top)
        if area < 1000:  # filter out tooltip/shadow windows
            return True
        hits.append((hwnd, area))
        return True

    _user32.EnumWindows(_cb, 0)
    if not hits:
        return None
    hits.sort(key=lambda x: -x[1])
    return hits[0][0]


def find_main_hwnd_for_pid_with_children(pid: int) -> int | None:
    """Try the pid first, then any child processes.

    Windows 11 Notepad (and a few games) launch a launcher exe that spawns a
    child process which owns the actual UI window.
    """
    hwnd = find_main_hwnd_for_pid(pid)
    if hwnd is not None:
        return hwnd
    try:
        import psutil

        for child in psutil.Process(pid).children(recursive=True):
            hwnd = find_main_hwnd_for_pid(child.pid)
            if hwnd is not None:
                return hwnd
    except Exception:
        pass
    return None


def get_window_size(hwnd: int) -> tuple[int, int] | None:
    """Return (width, height) of the window's bounding rect, or None on error."""
    if _user32 is None or not hwnd:
        return None
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    return (w, h)


def is_window(hwnd: int) -> bool:
    if _user32 is None or not hwnd:
        return False
    return bool(_user32.IsWindow(hwnd))


def foreground_fullscreen_pid() -> int | None:
    """If the current foreground window covers an entire monitor, return its PID.

    Used as a fallback game-detection mode: any unknown app that goes fullscreen
    on the user's primary display is treated as a "game". Common edge cases —
    YouTube/Netflix in F11 in a browser, video players in fullscreen — would
    also match; this is opt-in.
    """
    if _user32 is None:
        return None
    hwnd = _user32.GetForegroundWindow()
    if not hwnd or not _user32.IsWindowVisible(hwnd):
        return None
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None

    # MONITOR_DEFAULTTONEAREST = 2
    mon = _user32.MonitorFromWindow(hwnd, 2)
    if not mon:
        return None

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    if not _user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        return None

    mon_w = mi.rcMonitor.right - mi.rcMonitor.left
    mon_h = mi.rcMonitor.bottom - mi.rcMonitor.top
    # A "fullscreen" window covers >=99% of the monitor in both dimensions.
    # 99% (not 100%) tolerates borderless windows with a one-px chrome edge.
    if w < mon_w * 0.99 or h < mon_h * 0.99:
        return None
    # And it should sit at the monitor's origin within a few pixels.
    if abs(rect.left - mi.rcMonitor.left) > 5 or abs(rect.top - mi.rcMonitor.top) > 5:
        return None

    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value) or None
