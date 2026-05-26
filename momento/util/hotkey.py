"""Global hotkey service via Win32 RegisterHotKey + WM_HOTKEY.

A single instance owns the registration. Update the binding by calling
:meth:`HotkeyService.set_hotkey` with a string like ``"F8"`` or
``"Ctrl+Shift+B"``. The callback fires on the Qt main thread because
``QAbstractNativeEventFilter`` dispatches there.

Why not the ``keyboard`` package? It works but is overkill and has install
quirks; Win32's RegisterHotKey is built in and only needs ctypes.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Callable
from ctypes import wintypes

from PyQt6.QtCore import QAbstractNativeEventFilter, QObject

logger = logging.getLogger(__name__)

# ---- Win32 constants ------------------------------------------------------
_WM_HOTKEY = 0x0312
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008
_MOD_NOREPEAT = 0x4000

# Virtual-key codes we care about (extend as needed).
_VK_MAP: dict[str, int] = {
    **{f"F{i}": 0x70 + (i - 1) for i in range(1, 25)},
    **{c: ord(c) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
    **{c: ord(c) for c in "0123456789"},
    "SPACE": 0x20,
    "TAB": 0x09,
    "ENTER": 0x0D,
    "ESC": 0x1B,
    "ESCAPE": 0x1B,
    "HOME": 0x24,
    "END": 0x23,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
    "INSERT": 0x2D,
    "DELETE": 0x2E,
    "UP": 0x26,
    "DOWN": 0x28,
    "LEFT": 0x25,
    "RIGHT": 0x27,
}

_MOD_TOKEN_MAP: dict[str, int] = {
    "CTRL": _MOD_CONTROL,
    "CONTROL": _MOD_CONTROL,
    "ALT": _MOD_ALT,
    "SHIFT": _MOD_SHIFT,
    "WIN": _MOD_WIN,
    "META": _MOD_WIN,
}


class HotkeyError(ValueError):
    """Raised when a hotkey string can't be parsed or registered."""


def parse_hotkey(spec: str) -> tuple[int, int]:
    """Parse ``"Ctrl+Shift+F8"`` into (modifiers, vk). MOD_NOREPEAT always set."""
    if not spec or not spec.strip():
        raise HotkeyError("Empty hotkey")
    tokens = [t.strip() for t in spec.split("+") if t.strip()]
    if not tokens:
        raise HotkeyError(f"Unparseable hotkey: {spec!r}")
    *mod_tokens, key_token = tokens
    mods = _MOD_NOREPEAT
    for m in mod_tokens:
        mod = _MOD_TOKEN_MAP.get(m.upper())
        if mod is None:
            raise HotkeyError(f"Unknown modifier: {m!r}")
        mods |= mod
    vk = _VK_MAP.get(key_token.upper())
    if vk is None:
        raise HotkeyError(f"Unknown key: {key_token!r}")
    return mods, vk


class _WinHotkeyFilter(QAbstractNativeEventFilter):
    """Watches WM_HOTKEY on the Qt thread message pump and fires callbacks."""

    def __init__(self) -> None:
        super().__init__()
        # id -> callback. Multiple ids supported so adding a second binding
        # in the future is straightforward.
        self._callbacks: dict[int, Callable[[], None]] = {}

    def register(self, id_: int, callback: Callable[[], None]) -> None:
        self._callbacks[id_] = callback

    def unregister(self, id_: int) -> None:
        self._callbacks.pop(id_, None)

    def nativeEventFilter(self, event_type, message):  # noqa: N802 (Qt API)
        if event_type != b"windows_generic_MSG":
            return False, 0
        try:
            msg = wintypes.MSG.from_address(int(message))
        except (TypeError, ValueError):
            return False, 0
        if msg.message == _WM_HOTKEY:
            cb = self._callbacks.get(int(msg.wParam))
            if cb is not None:
                try:
                    cb()
                except Exception:
                    logger.exception("Hotkey callback raised")
        return False, 0


class HotkeyService(QObject):
    """Single-binding global hotkey. Reconfigurable at runtime.

    Usage:
        svc = HotkeyService(app)
        svc.set_callback(lambda: print("hot!"))
        svc.set_hotkey("F8")
        ...
        svc.shutdown()
    """

    _HOTKEY_ID = 0x4D4D  # 'MM'

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._callback: Callable[[], None] | None = None
        self._current_spec: str | None = None
        self._registered = False

        if sys.platform != "win32":
            logger.warning("HotkeyService: non-Windows platform; will be a no-op")
            self._user32 = None
            self._filter = None
            return

        self._user32 = ctypes.windll.user32
        self._user32.RegisterHotKey.argtypes = [
            wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT
        ]
        self._user32.RegisterHotKey.restype = wintypes.BOOL
        self._user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user32.UnregisterHotKey.restype = wintypes.BOOL

        from PyQt6.QtCore import QCoreApplication

        self._filter = _WinHotkeyFilter()
        app = QCoreApplication.instance()
        if app is None:
            raise RuntimeError("HotkeyService requires a QCoreApplication to exist")
        app.installNativeEventFilter(self._filter)

    # ---------------------------------------------------------- API
    def set_callback(self, callback: Callable[[], None] | None) -> None:
        self._callback = callback
        if self._filter is not None:
            if callback is None:
                self._filter.unregister(self._HOTKEY_ID)
            else:
                self._filter.register(self._HOTKEY_ID, callback)

    def set_hotkey(self, spec: str) -> None:
        """Replace the current binding. Raises HotkeyError on a bad spec."""
        mods, vk = parse_hotkey(spec)
        if self._user32 is None:
            self._current_spec = spec
            return
        self._unregister()
        ok = self._user32.RegisterHotKey(None, self._HOTKEY_ID, mods, vk)
        if not ok:
            err = ctypes.GetLastError()
            raise HotkeyError(
                f"RegisterHotKey failed for {spec!r} (errno {err}). "
                f"Another app may already own this combo."
            )
        self._registered = True
        self._current_spec = spec
        logger.info("Hotkey bound: %s", spec)

    def current_hotkey(self) -> str | None:
        return self._current_spec

    def shutdown(self) -> None:
        self._unregister()
        if self._filter is not None:
            from PyQt6.QtCore import QCoreApplication

            app = QCoreApplication.instance()
            if app is not None:
                app.removeNativeEventFilter(self._filter)
            self._filter = None

    # ------------------------------------------------------- internals
    def _unregister(self) -> None:
        if self._user32 is not None and self._registered:
            try:
                self._user32.UnregisterHotKey(None, self._HOTKEY_ID)
            except Exception:
                pass
            self._registered = False
