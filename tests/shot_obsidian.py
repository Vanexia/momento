"""Dev screenshot harness for the Obsidian redesign.

Builds the real EditorWindow with a lightweight fake session (so the status
strip renders), selects a recording (so the player + timeline populate),
optionally injects fake highlight/bookmark times, and grabs the main and/or
settings views to PNGs.

    python tests/shot_obsidian.py build/shot.png            # main window
    python tests/shot_obsidian.py build/shot.png settings   # settings screen
    python tests/shot_obsidian.py build/shot.png recording  # recording state
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from momento.config import load_config
from momento.core.game_watcher import ActiveGame
from momento.core.session import STATUS_RECORDING
from momento.ui.editor import EditorWindow
from momento.ui.theme import apply_dark_theme


class _FakeRecorder:
    def current_position(self):
        return 271.0  # 4:31 — matches the design playhead


class _FakeSession:
    """Just enough surface for StatusPanel + editor wiring."""

    def __init__(self, monitoring: bool = True) -> None:
        self._recorder = _FakeRecorder()
        self._monitoring = monitoring
        self._current_game = None

    @property
    def is_monitoring(self) -> bool:
        return self._monitoring

    @property
    def is_recording(self) -> bool:
        return False

    @property
    def current_game(self):
        return self._current_game


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("build/shot.png")
    view = sys.argv[2] if len(sys.argv) > 2 else "main"
    out.parent.mkdir(parents=True, exist_ok=True)

    app = QApplication([])
    apply_dark_theme(app)
    cfg = load_config()

    session = _FakeSession(monitoring=True)
    ed = EditorWindow(cfg, session=session)
    # Dev tool: never let the harness persist its own window geometry over the
    # user's real saved state (app.quit() → aboutToQuit → _save_window_state).
    ed._save_window_state = lambda: None
    ed.resize(1380, 860)
    ed.show()

    def setup_and_shoot() -> None:
        # Populate the player + timeline from the first recording.
        try:
            ed._list.select_first()
            sel = ed._list.selected_path()
            if sel is not None:
                ed.preview.load(sel)
                # Fake highlight/bookmark markers (the design's "Auto-detected 6")
                dur = 48 * 60 + 22  # 48:22 sample
                marks = [d * dur for d in (0.067, 0.241, 0.412, 0.569, 0.706, 0.848, 0.941)]
                ed.timeline.set_duration(dur)
                ed.timeline.set_bookmarks(marks)
                ed.timeline.set_clip_range(dur * 0.555, dur * 0.61)
                ed.preview.set_highlights(marks)
                ed.preview.set_clip_meta(
                    "Mythic Raid — Council Kill", "World of Warcraft"
                )
                ed.preview._pager.set_index(4, len(marks))
        except Exception as exc:  # pragma: no cover (dev tool)
            print("setup warning:", exc)

        if view == "recording" and ed._status_panel is not None:
            ed._status_panel.set_status(
                STATUS_RECORDING, ActiveGame(exe_name="Wow.exe", pid=1, exe_path=None)
            )
        if view == "settings":
            ed.show_settings("Audio")

        QTimer.singleShot(600, _grab)

    def _grab() -> None:
        pix = ed.grab()
        pix.save(str(out))
        print(f"saved {out}  ({pix.width()}x{pix.height()})")
        app.quit()

    QTimer.singleShot(1500, setup_and_shoot)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
