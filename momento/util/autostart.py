"""Manage the HKCU Run-key entry that auto-launches Momento on login."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_VALUE = "Momento"


def autostart_command() -> str:
    """Build the command line Windows runs at login.

    Preference order:
      1. Running from a PyInstaller bundle (sys.frozen) — use sys.executable.
      2. Dev mode but a bundled exe exists at dist/Momento/Momento.exe — prefer
         that. Avoids the foot-gun where toggling autostart in a dev session
         overwrites the registry with the pythonw path and the user gets the
         wrong (slower, console-flashing, dev-state) version on next login.
      3. Dev mode, no bundle present — fall back to pythonw -m momento.
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'

    # momento/util/autostart.py -> parents[2] is the repo root.
    project_root = Path(__file__).resolve().parents[2]
    bundled_exe = project_root / "dist" / "Momento" / "Momento.exe"
    if bundled_exe.is_file():
        return f'"{bundled_exe}"'

    # Dev: prefer pythonw.exe so no console window flashes.
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    interpreter = pyw if pyw.exists() else py
    return f'"{interpreter}" -m momento'


def is_autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY, 0, winreg.KEY_READ) as k:
            value, _ = winreg.QueryValueEx(k, REG_VALUE)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError:
        logger.exception("Could not read autostart registry value")
        return False


def set_autostart(enabled: bool) -> None:
    """Add or remove the HKCU Run key. No-op on non-Windows."""
    if sys.platform != "win32":
        return
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REG_KEY, 0, winreg.KEY_SET_VALUE
        ) as k:
            if enabled:
                cmd = autostart_command()
                winreg.SetValueEx(k, REG_VALUE, 0, winreg.REG_SZ, cmd)
                logger.info("Autostart enabled: %s", cmd)
            else:
                try:
                    winreg.DeleteValue(k, REG_VALUE)
                    logger.info("Autostart disabled")
                except FileNotFoundError:
                    pass
    except OSError:
        logger.exception("Failed to update autostart registry")
        raise
