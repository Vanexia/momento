"""Verify the thumbnail pool: submit jobs for every MP4, count successes."""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QCoreApplication

from momento.core.thumbnails import extract_async
from media_fixture import make_momento_mkv


def main() -> int:
    temp = tempfile.TemporaryDirectory(prefix="momento_thumbpool_")
    folder = Path(temp.name)
    recordings = [
        make_momento_mkv(folder / f"fixture-{index}.mkv", duration_seconds=1.0)
        for index in range(2)
    ]

    print(f"Submitting {len(recordings)} thumbnail jobs ...")

    app = QCoreApplication(sys.argv)
    results: list[tuple[str, str]] = []

    def on_done(path: str, thumb: str) -> None:
        results.append((path, thumb))
        tag = Path(thumb).name if thumb else "FAIL"
        print(f"  [{len(results)}/{len(recordings)}] {Path(path).name} -> {tag}")
        if len(results) == len(recordings):
            QCoreApplication.quit()

    for p in recordings:
        extract_async(p, on_done)

    # 60s safety timeout
    deadline = time.time() + 60
    while len(results) < len(recordings) and time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)

    succeeded = sum(1 for _, t in results if t)
    print(f"\nResults: {succeeded}/{len(recordings)} succeeded")
    complete = len(results) == len(recordings)
    temp.cleanup()
    return 0 if complete and succeeded == len(recordings) else 3


if __name__ == "__main__":
    sys.exit(main())
