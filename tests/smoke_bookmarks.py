"""Bookmark persistence must survive interrupted and concurrent saves."""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import momento.core.bookmarks as bookmark_mod  # noqa: E402
from momento.core.bookmarks import (  # noqa: E402
    BookmarkStore,
    load_bookmarks,
    save_bookmarks,
    sidecar_path_for,
)


_results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    _results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} - {name}")


def test_interrupted_write_keeps_previous_sidecar(tmp: Path) -> None:
    recording = tmp / "recording.mkv"
    save_bookmarks(recording, [1.0, 2.0])
    sidecar = sidecar_path_for(recording)
    original = sidecar.read_text(encoding="utf-8")
    real_write_text = Path.write_text

    def interrupted(self: Path, *_args, **_kwargs):
        real_write_text(self, "{partial", encoding="utf-8")
        raise OSError("simulated interrupted write")

    Path.write_text = interrupted  # type: ignore[method-assign]
    try:
        raised = False
        try:
            save_bookmarks(recording, [1.0, 2.0, 3.0])
        except OSError:
            raised = True
    finally:
        Path.write_text = real_write_text  # type: ignore[method-assign]

    check("interrupted bookmark save reports failure", raised)
    check("interrupted bookmark save preserves prior JSON", sidecar.read_text(encoding="utf-8") == original)
    check("preserved bookmark sidecar remains readable", load_bookmarks(recording) == [1.0, 2.0])
    check("failed atomic save cleans its temporary file", not sidecar.with_name(sidecar.name + ".tmp").exists())


def test_concurrent_adds_cannot_overwrite_newer_snapshot(tmp: Path) -> None:
    store = BookmarkStore(tmp / "concurrent.mkv")
    real_save = bookmark_mod.save_bookmarks
    first_entered = threading.Event()
    release_first = threading.Event()
    writes: list[list[float]] = []

    def controlled_save(_path, values: list[float]) -> None:
        if values == [1.0]:
            first_entered.set()
            release_first.wait(timeout=2.0)
        writes.append(list(values))

    bookmark_mod.save_bookmarks = controlled_save
    try:
        first = threading.Thread(target=store.add, args=(1.0,))
        second = threading.Thread(target=store.add, args=(2.0,))
        first.start()
        first_entered.wait(timeout=1.0)
        second.start()
        time.sleep(0.05)
        release_first.set()
        first.join(timeout=2.0)
        second.join(timeout=2.0)
    finally:
        release_first.set()
        bookmark_mod.save_bookmarks = real_save

    check("concurrent bookmark writers both finish", not first.is_alive() and not second.is_alive())
    check("newest bookmark snapshot is written last", bool(writes) and writes[-1] == [1.0, 2.0])
    check("in-memory bookmark order remains correct", store.snapshot() == [1.0, 2.0])


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="momento_bookmarks_") as d:
        tmp = Path(d)
        test_interrupted_write_keeps_previous_sidecar(tmp)
        test_concurrent_adds_cannot_overwrite_newer_snapshot(tmp)

    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
