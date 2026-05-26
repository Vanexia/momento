"""Pop the recording-started / recording-saved toasts so you can eyeball them.

By default this honors your saved config (Settings → Notifications) — i.e. if
you've unticked "Recording saved", the second toast won't appear. That makes
it a faithful preview of what the real app does. Pass --force to override and
always show both, for pure visual testing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QSize, QTimer, QUrl
from PyQt6.QtGui import QIcon
from PyQt6.QtMultimedia import QSoundEffect
from PyQt6.QtWidgets import QApplication

from momento.config import load_config
from momento.ui.theme import apply_dark_theme
from momento.ui.toast import RecordingToast
from momento.util.resources import app_icon_path, bookmark_sound_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force",
        action="store_true",
        help="Always show both toasts, ignoring the saved Settings flags.",
    )
    args = parser.parse_args()

    app = QApplication(sys.argv)
    apply_dark_theme(app)

    cfg = load_config()
    icon_p = app_icon_path()
    if icon_p is not None:
        # QIcon → pixmap(128) so we pick a high-res sub-image from the .ico
        # rather than QPixmap()'s default first-sub-image behaviour.
        app.setWindowIcon(QIcon(str(icon_p)))

    toast = RecordingToast()
    if icon_p is not None:
        toast.set_app_icon(QIcon(str(icon_p)).pixmap(QSize(128, 128)))

    show_started = args.force or cfg.show_recording_started_toast
    show_saved = args.force or cfg.show_recording_saved_toast
    # The bookmark toast always shows when the user hits the hotkey during
    # a recording — there's no opt-out, since the alternative is silent
    # feedback. Always preview it.
    show_bookmark = True

    print(f"Config: started={cfg.show_recording_started_toast}, "
          f"saved={cfg.show_recording_saved_toast}  (force={args.force})")
    print(f"Will show: started={show_started}, saved={show_saved}, bookmark={show_bookmark}")

    delay = 0
    if show_started:
        QTimer.singleShot(0, lambda: toast.show_recording("Elden Ring", duration_ms=3500))
        delay += 4000

    if show_bookmark:
        # Preload the chime so it plays in lock-step with the toast.
        chime = QSoundEffect()
        wav = bookmark_sound_path()
        if wav is not None:
            chime.setSource(QUrl.fromLocalFile(str(wav)))
            chime.setVolume(0.5)

        def fire_bookmark() -> None:
            toast.show_bookmark("Elden Ring", 83.0, duration_ms=2500)
            chime.play()

        QTimer.singleShot(delay, fire_bookmark)
        delay += 3000

    if show_saved:
        QTimer.singleShot(delay, lambda: toast.show_idle("Elden Ring", duration_ms=3500))
        delay += 4000

    if not show_started and not show_saved and not show_bookmark:
        print("All toasts disabled — nothing to show. Use --force to override.")
        return 0

    QTimer.singleShot(delay + 500, app.quit)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
