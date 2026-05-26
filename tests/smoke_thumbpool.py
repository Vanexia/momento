"""Verify the thumbnail pool: submit jobs for every MP4, count successes."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QCoreApplication

from momento.core.thumbnails import extract_async, thumb_path_for


def main() -> int:
    folder = Path("C:/dev/Momento/recordings")
    mp4s = [
        p for p in folder.iterdir()
        if p.suffix.lower() == ".mp4" and not p.name.endswith(".thumb.jpg")
    ]
    if not mp4s:
        print("No recordings to test")
        return 2

    # Clear any existing thumbs so we exercise the extract path for all.
    for p in mp4s:
        thumb_path_for(p).unlink(missing_ok=True)

    print(f"Submitting {len(mp4s)} thumbnail jobs ...")

    app = QCoreApplication(sys.argv)
    results: list[tuple[str, str]] = []

    def on_done(path: str, thumb: str) -> None:
        results.append((path, thumb))
        tag = Path(thumb).name if thumb else "FAIL"
        print(f"  [{len(results)}/{len(mp4s)}] {Path(path).name} -> {tag}")
        if len(results) == len(mp4s):
            QCoreApplication.quit()

    for p in mp4s:
        extract_async(p, on_done)

    # 60s safety timeout
    deadline = time.time() + 60
    while len(results) < len(mp4s) and time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)

    succeeded = sum(1 for _, t in results if t)
    print(f"\nResults: {succeeded}/{len(mp4s)} succeeded")
    return 0 if succeeded == len(mp4s) else 3


if __name__ == "__main__":
    sys.exit(main())
