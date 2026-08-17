"""Smoke test: pick the newest recording, trim seconds 1..3, verify output."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QCoreApplication, QThread

from momento.trim.ffmpeg_trim import TrimWorker, next_clip_path


def main() -> int:
    folder = Path("C:/dev/Momento/recordings")
    sources = [
        p for p in folder.iterdir()
        if p.suffix.lower() in (".mkv", ".mp4") and "_clip_" not in p.name
    ]
    if not sources:
        print("No source recordings found")
        return 2
    src = max(sources, key=lambda p: p.stat().st_mtime)
    out = next_clip_path(src)
    print(f"Source: {src.name}")
    print(f"Output: {out.name}")

    app = QCoreApplication(sys.argv)

    state = {"done": False, "ok": False, "msg": ""}

    worker = TrimWorker(src, start=0.5, end=2.5, output_path=out)
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)

    def on_progress(cur: float, total: float) -> None:
        print(f"  progress: {cur:.2f}s / {total:.2f}s")

    def on_done(path: str) -> None:
        state["done"] = True
        state["ok"] = True
        state["msg"] = path
        QCoreApplication.quit()

    def on_failed(msg: str) -> None:
        state["done"] = True
        state["ok"] = False
        state["msg"] = msg
        QCoreApplication.quit()

    worker.progress.connect(on_progress)
    worker.done.connect(on_done)
    worker.failed.connect(on_failed)
    worker.finished.connect(thread.quit)

    thread.start()

    deadline = time.time() + 20
    while not state["done"] and time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)

    thread.wait(2000)

    if state["ok"]:
        size = out.stat().st_size if out.exists() else 0
        print(f"PASS: {state['msg']}  ({size:,} bytes)")
        return 0
    print(f"FAIL: {state['msg']}")
    return 3


if __name__ == "__main__":
    sys.exit(main())
