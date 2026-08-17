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


def is_window(hwnd: int) -> bool:
    if _user32 is None or not hwnd:
        return False
    return bool(_user32.IsWindow(hwnd))


# --- window placement (maximized-vs-normal state + restore rect) ----------
# WINDOWPLACEMENT.showCmd values we care about.
_SW_SHOWNORMAL = 1
_SW_SHOWMINIMIZED = 2
_SW_SHOWMAXIMIZED = 3


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT),
    ]


def _bind_placement_api() -> bool:
    """Set argtypes for the placement calls. HWND is a 64-bit pointer; with no
    argtypes ctypes defaults each arg to C ``int`` and would truncate it."""
    if _user32 is None:
        return False
    _user32.GetWindowPlacement.argtypes = [
        wintypes.HWND, ctypes.POINTER(_WINDOWPLACEMENT)
    ]
    _user32.GetWindowPlacement.restype = wintypes.BOOL
    _user32.SetWindowPlacement.argtypes = [
        wintypes.HWND, ctypes.POINTER(_WINDOWPLACEMENT)
    ]
    _user32.SetWindowPlacement.restype = wintypes.BOOL
    return True


def save_window_placement(hwnd: int):
    """Capture a window's full placement as an opaque token for
    :func:`restore_window_placement`.

    The token records BOTH whether the window is maximized and its normal
    restore rect. Round-tripping through Windows' own placement mechanism is
    what makes a *maximized* window come back maximized after a fullscreen
    excursion — Qt drops the Maximized bit the moment fullscreen is applied on
    Windows, so its window-state machine can't recompute the target on exit.

    Returns None on non-Windows or failure.
    """
    if not _bind_placement_api() or not hwnd:
        return None
    wp = _WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(_WINDOWPLACEMENT)
    if not _user32.GetWindowPlacement(wintypes.HWND(hwnd), ctypes.byref(wp)):
        return None
    return wp


def restore_window_placement(hwnd: int, token) -> bool:
    """Put a window back exactly as :func:`save_window_placement` captured it —
    maximized → maximized, normal → its saved rect — in one atomic OS call.

    Returns False on non-Windows, a missing token, or failure, so the caller
    can fall back to a best-effort Qt state restore. A saved *minimized* state
    is promoted to normal so exiting fullscreen never drops the window to the
    taskbar.
    """
    if not _bind_placement_api() or not hwnd or token is None:
        return False
    wp = token
    wp.length = ctypes.sizeof(_WINDOWPLACEMENT)
    if wp.showCmd == _SW_SHOWMINIMIZED:
        wp.showCmd = _SW_SHOWNORMAL
    return bool(_user32.SetWindowPlacement(wintypes.HWND(hwnd), ctypes.byref(wp)))


# DwmSetWindowAttribute: DWMWA_TRANSITIONS_FORCEDISABLED = 3 (BOOL; TRUE means
# the window's animations are force-disabled).
_DWMWA_TRANSITIONS_FORCEDISABLED = 3


def set_window_transitions_enabled(hwnd: int, enabled: bool) -> None:
    """Toggle DWM's minimize/maximize/restore animation for a single window.

    Disabling it makes window-state changes snap instantly instead of playing
    the Windows scale animation. We disable it across the fullscreen-exit
    transition so the window doesn't visibly *flash* through the intermediate
    normal size on its way back to maximized — Qt has to step
    full -> normal -> maximized to rebuild the title-bar frame, and that
    stepping is what the animation makes visible. Best-effort; never raises.
    """
    if sys.platform != "win32" or not hwnd:
        return
    try:
        value = wintypes.BOOL(not enabled)  # TRUE => transitions force-disabled
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            wintypes.DWORD(_DWMWA_TRANSITIONS_FORCEDISABLED),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
    except (OSError, AttributeError):
        pass


# --- borderless fullscreen (no Qt window-state stepping -> no exit flash) ---
# Qt's fullscreen<->maximized transition steps the window down to its NORMAL
# size for a frame on the way back; DWM composites that frame = the visible
# exit flash. Driving the SAME HWND straight between maximized and
# monitor-bounds via Win32 never touches the normal size, so there's nothing to
# flash — and capture/screen-share keeps following because the HWND is stable.
_GWL_STYLE = -16
_WS_CAPTION = 0x00C00000
_WS_THICKFRAME = 0x00040000
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_FRAMECHANGED = 0x0020
_SWP_NOOWNERZORDER = 0x0200
_HWND_TOP = 0


def _bind_frame_api() -> bool:
    """Set 64-bit-safe signatures. GWL_STYLE is a LONG_PTR; with no argtypes
    ctypes would truncate the value/return to 32 bits."""
    if _user32 is None:
        return False
    _user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    _user32.SetWindowPos.restype = wintypes.BOOL
    _user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    return True


def _monitor_bounds(hwnd: int):
    """Full monitor rect (rcMonitor, includes the taskbar area) for hwnd's
    monitor — so the borderless window covers the screen exactly like true
    fullscreen. Returns (x, y, w, h) or None."""
    mon = _user32.MonitorFromWindow(wintypes.HWND(hwnd), 2)  # MONITOR_DEFAULTTONEAREST
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
    r = mi.rcMonitor
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def enter_borderless_fullscreen(hwnd: int):
    """Strip the window frame and resize it to fill its monitor, keeping the
    SAME HWND. Returns the saved GWL_STYLE (int) that
    :func:`exit_borderless_fullscreen` needs, or None on non-Windows / failure
    (the caller then falls back to Qt fullscreen)."""
    if not _bind_frame_api() or not hwnd:
        return None
    try:
        bounds = _monitor_bounds(hwnd)
        if bounds is None:
            return None
        style = _user32.GetWindowLongPtrW(wintypes.HWND(hwnd), _GWL_STYLE)
        _user32.SetWindowLongPtrW(
            wintypes.HWND(hwnd), _GWL_STYLE, style & ~(_WS_CAPTION | _WS_THICKFRAME)
        )
        mx, my, mw, mh = bounds
        _user32.SetWindowPos(
            wintypes.HWND(hwnd), wintypes.HWND(_HWND_TOP),
            mx, my, mw, mh, _SWP_FRAMECHANGED | _SWP_NOOWNERZORDER,
        )
        return int(style)
    except (OSError, AttributeError):
        return None


def exit_borderless_fullscreen(hwnd: int, saved_style, placement_token) -> bool:
    """Restore the frame style saved on enter and put the window back to
    maximized-or-normal via SetWindowPlacement — directly, with NO intermediate
    normal-size frame. Returns False on non-Windows / missing data / failure."""
    if not _bind_frame_api() or not hwnd or saved_style is None:
        return False
    try:
        _user32.SetWindowLongPtrW(wintypes.HWND(hwnd), _GWL_STYLE, int(saved_style))
        # Apply the restored frame (recomputes the non-client area) without yet
        # moving/sizing; SetWindowPlacement then sets the final geom + state.
        _user32.SetWindowPos(
            wintypes.HWND(hwnd), wintypes.HWND(_HWND_TOP), 0, 0, 0, 0,
            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_FRAMECHANGED | _SWP_NOOWNERZORDER,
        )
        return restore_window_placement(hwnd, placement_token)
    except (OSError, AttributeError):
        return False


def force_foreground_window(hwnd: int) -> None:
    """Bring ``hwnd`` to the front AND give it input focus, defeating Windows'
    foreground lock.

    A plain SetForegroundWindow / Qt activateWindow() is silently downgraded
    to a taskbar-flash when the calling process isn't the foreground one —
    which is exactly the case after a system-tray click (the shell owns the
    foreground). The reliable workaround is to momentarily attach our input
    queue to the current foreground thread's, which makes Windows treat the
    SetForegroundWindow call as coming from the active app. Best-effort; never
    raises.
    """
    if _user32 is None or not hwnd:
        return
    try:
        kernel32 = ctypes.windll.kernel32
        SW_RESTORE = 9
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, SW_RESTORE)

        fg = _user32.GetForegroundWindow()
        if fg == hwnd:
            return

        our_tid = kernel32.GetCurrentThreadId()
        fg_tid = 0
        if fg:
            pid = wintypes.DWORD()
            fg_tid = _user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))

        attached = False
        if fg_tid and fg_tid != our_tid:
            attached = bool(_user32.AttachThreadInput(our_tid, fg_tid, True))
        try:
            _user32.BringWindowToTop(hwnd)
            _user32.SetForegroundWindow(hwnd)
            _user32.SetActiveWindow(hwnd)
            _user32.SetFocus(hwnd)
        finally:
            if attached:
                _user32.AttachThreadInput(our_tid, fg_tid, False)
    except (OSError, AttributeError):
        pass


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
