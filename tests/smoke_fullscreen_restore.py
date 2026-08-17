"""Regression test for the maximized -> fullscreen -> exit round-trip.

The bug (CLAUDE.md "ACTIVE UNRESOLVED ISSUE"): exiting in-window fullscreen
did not return a *maximized* editor window to maximized — Qt drops the
WindowMaximized bit once fullscreen is applied on Windows, so every Qt-only
exit landed the window at the default size. The fix captures the pre-fullscreen
WINDOWPLACEMENT (Win32) on enter and restores it on exit.

IMPORTANT: this MUST run on the real Windows platform (a genuinely visible
window). Offscreen/headless Qt *keeps* the Maximized bit and gives false
passes — see CLAUDE.md. The window is shown for real and goes fullscreen
briefly, then the test reports and quits.

    C:\\dev\\Momento\\.venv\\Scripts\\python.exe tests\\smoke_fullscreen_restore.py

State is read in deferred callbacks (QTimer) because window state settles
asynchronously on Windows — a synchronous read after setWindowState reports
the pre-WM state and misleads.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from momento.config import load_config  # noqa: E402
from momento.ui.editor import EditorWindow  # noqa: E402
from momento.ui.theme import apply_dark_theme  # noqa: E402

_SETTLE_MS = 700


def _geom(win) -> str:
    r = win.geometry()
    return f"{r.width()}x{r.height()}+{r.x()}+{r.y()}"


def main() -> int:
    app = QApplication(sys.argv)
    if app.platformName() == "offscreen":
        print("FAIL: running offscreen — window state would be a false pass.")
        return 2
    apply_dark_theme(app)

    cfg = load_config()
    # session=None: skip the live status panel, but every other chrome panel
    # (left pane min-width 300, bottom panel min-height 170) is real, so this
    # exercises the actual exit path including showing chrome back.
    ed = EditorWindow(cfg, session=None)
    # show() first so the editor's one-shot _apply_first_show_geometry (Phase 17:
    # restores the saved window state / monitor) runs and settles. THEN maximize
    # explicitly in step_maximize — otherwise the saved-state restore overrides a
    # showMaximized() issued before the first show and the round-trip would start
    # from the saved (possibly non-maximized) geometry instead of maximized.
    ed.show()

    results: dict[str, object] = {"ok": False}

    def step_maximize() -> None:
        ed.showMaximized()
        QTimer.singleShot(_SETTLE_MS, step_record_before)

    def step_record_before() -> None:
        results["before_max"] = ed.isMaximized()
        results["before_geom"] = _geom(ed)
        print(f"[1] after showMaximized: isMaximized={results['before_max']} geom={results['before_geom']}")
        ed.preview.toggle_fullscreen()
        QTimer.singleShot(_SETTLE_MS, step_record_fullscreen)

    def step_record_fullscreen() -> None:
        # Fullscreen is driven via Win32 borderless (not Qt's window state), so
        # isFullScreen() is unreliable — assert the window actually covers the
        # FULL monitor (incl. the taskbar area) instead.
        scr = ed.screen().geometry() if ed.screen() is not None else None
        r = ed.geometry()
        results["fs_covers_screen"] = bool(
            scr is not None
            and r.width() == scr.width()
            and r.height() == scr.height()
            and r.x() == scr.x()
            and r.y() == scr.y()
        )
        print(f"[2] after enter fullscreen: covers_screen={results['fs_covers_screen']} geom={_geom(ed)}")
        ed.preview.toggle_fullscreen()
        QTimer.singleShot(_SETTLE_MS, step_record_after)

    def step_record_after() -> None:
        results["after_max"] = ed.isMaximized()
        results["after_geom"] = _geom(ed)
        print(
            f"[3] after exit fullscreen: isMaximized={results['after_max']} "
            f"geom={results['after_geom']}"
        )
        _report()
        app.quit()

    def _report() -> None:
        ok = (
            results.get("before_max") is True
            and results.get("fs_covers_screen") is True
            and results.get("after_max") is True
            and results.get("after_geom") == results.get("before_geom")
        )
        print("-" * 60)
        if ok:
            print("PASS: maximized -> fullscreen -> exit returned to maximized "
                  f"at the same geometry ({results.get('after_geom')}).")
        else:
            print("FAIL: did not round-trip. Expected before_max=True, "
                  "fs_covers_screen=True, after_max=True, geom unchanged.")
            print(f"      got: {results}")
        results["ok"] = ok

    QTimer.singleShot(_SETTLE_MS, step_maximize)
    # Hard safety net in case a transition wedges.
    QTimer.singleShot(_SETTLE_MS * 9, app.quit)
    app.exec()
    return 0 if results["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
