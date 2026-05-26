"""Headless QMediaPlayer probe: does Windows Media Foundation play our MKVs?

Loads ``recordings/smoke_recorder.mkv`` (produced by tests/smoke_recorder.py)
into a QMediaPlayer, waits up to 5s for duration to populate, prints the
result. Exits 0 if duration > 0 and no error fired.

If this fails, we need to either remux MKV->MP4 on demand for preview, or
ship a different player (libvlc-python).
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import QCoreApplication, QTimer, QUrl
from PyQt6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer
from PyQt6.QtWidgets import QApplication

MKV = Path(__file__).resolve().parents[1] / "recordings" / "smoke_recorder.mkv"


def main() -> int:
    if not MKV.exists():
        print(f"FAIL: {MKV} not found — run smoke_recorder.py first")
        return 1

    app = QApplication.instance() or QApplication(sys.argv)
    player = QMediaPlayer()
    # Attach an audio output so the player actually decodes the audio track.
    audio = QAudioOutput(QMediaDevices.defaultAudioOutput())
    player.setAudioOutput(audio)

    state = {"duration_ms": -1, "error_msg": None, "media_status": None}

    def on_duration(ms: int) -> None:
        state["duration_ms"] = ms
        print(f"  durationChanged: {ms} ms")
        if ms > 0:
            QCoreApplication.quit()

    def on_error(err: QMediaPlayer.Error, msg: str) -> None:
        if err == QMediaPlayer.Error.NoError:
            return
        state["error_msg"] = f"{err.name}: {msg or player.errorString()}"
        print(f"  ERROR: {state['error_msg']}")
        QCoreApplication.quit()

    def on_status(status: QMediaPlayer.MediaStatus) -> None:
        state["media_status"] = status.name
        print(f"  mediaStatus: {status.name}")
        # InvalidMedia = format/codec rejected
        if status == QMediaPlayer.MediaStatus.InvalidMedia:
            QCoreApplication.quit()

    player.durationChanged.connect(on_duration)
    player.errorOccurred.connect(on_error)
    player.mediaStatusChanged.connect(on_status)

    player.setSource(QUrl.fromLocalFile(str(MKV)))
    print(f"Loaded: {MKV}")
    QTimer.singleShot(5000, QCoreApplication.quit)  # 5s budget
    app.exec()

    print()
    print(f"duration_ms = {state['duration_ms']}")
    print(f"error_msg   = {state['error_msg']}")
    print(f"last_status = {state['media_status']}")
    if state["error_msg"] is not None:
        print("FAIL: QMediaPlayer rejected the MKV")
        return 2
    if state["duration_ms"] <= 0:
        print("FAIL: never saw a non-zero duration (probably no demuxer)")
        return 3
    print("PASS: QMediaPlayer loaded MKV with non-zero duration")
    return 0


if __name__ == "__main__":
    sys.exit(main())
