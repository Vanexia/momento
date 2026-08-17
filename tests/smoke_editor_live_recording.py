"""The recordings browser must never expose the MKV currently being written."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from momento.ui.editor import _list_recordings  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="momento_editor_live_") as d:
        folder = Path(d).resolve()
        active = folder / "Wow_2026-01-01_120000.mkv"
        finished = folder / "Wow_2026-01-01_110000.mkv"
        active.write_bytes(b"live")
        finished.write_bytes(b"finished")

        while_recording = _list_recordings(folder, exclude_paths={active})
        after_finish = _list_recordings(folder)
        hidden_while_live = active.resolve() not in while_recording
        finished_visible = finished.resolve() in while_recording
        visible_after_finish = active.resolve() in after_finish

        print(f"{'PASS' if hidden_while_live else 'FAIL'} - active recording hidden")
        print(f"{'PASS' if finished_visible else 'FAIL'} - finished recording visible")
        print(f"{'PASS' if visible_after_finish else 'FAIL'} - file appears after finalise")

        return 0 if hidden_while_live and finished_visible and visible_after_finish else 1


if __name__ == "__main__":
    raise SystemExit(main())
