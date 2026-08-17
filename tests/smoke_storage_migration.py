"""Focused regressions for output-folder migration safety."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QThread, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialog  # noqa: E402

import momento.core.storage_cleanup as storage_mod  # noqa: E402
import momento.core.recording_safety as safety_mod  # noqa: E402
import momento.ui.settings_dialog as settings_mod  # noqa: E402
from momento.config import Config  # noqa: E402
from momento.core.storage_cleanup import MigrationWorker  # noqa: E402
from momento.ui.settings_dialog import SettingsPanel  # noqa: E402
from media_fixture import make_momento_mkv  # noqa: E402


_app = QApplication.instance() or QApplication(sys.argv[:1])
_results: list[tuple[str, bool]] = []
_OWNERSHIP_SUFFIX = ".momento.json"


def check(name: str, ok: bool) -> None:
    _results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} - {name}")


def _ownership_sidecar(path: Path) -> Path:
    return path.with_name(path.name + _OWNERSHIP_SUFFIX)


def _make_tagged_sparse_recording(path: Path, size_bytes: int) -> bool:
    try:
        make_momento_mkv(
            path,
            duration_seconds=0.2,
            fps=5,
            width=32,
            height=32,
            with_audio=False,
            game_slug="storage-test",
        )
    except Exception:
        return False
    with path.open("r+b") as fh:
        fh.truncate(size_bytes)
    return True


def test_sidecar_failure_is_reported(tmp: Path) -> None:
    old = tmp / "sidecar-old"
    new = tmp / "sidecar-new"
    old.mkdir()
    media = old / "game_2026-01-01_120000.mkv"
    sidecar = old / f"{media.name}.bookmarks.json"
    owner = _ownership_sidecar(media)
    media.write_bytes(b"media")
    sidecar.write_text("[]", encoding="utf-8")
    marker_created = safety_mod.mark_recording_owned(media)
    check("migration fixture: source marker is valid", marker_created)
    if not marker_created:
        return

    real_move = storage_mod.shutil.move

    def fail_sidecar(src, dst, *args, **kwargs):
        if Path(src) == sidecar:
            raise OSError("simulated sidecar move failure")
        return real_move(src, dst, *args, **kwargs)

    storage_mod.shutil.move = fail_sidecar
    try:
        moved, failed = MigrationWorker(old, new).run()
    finally:
        storage_mod.shutil.move = real_move

    check("media move is still counted when its sidecar fails", moved == 1)
    check("sidecar failure increments the failed count", failed == 1)
    check(
        "failed sidecar remains recoverable at the source",
        (new / media.name).exists() and sidecar.exists(),
    )
    check(
        "ownership sidecar follows its recording during migration",
        _ownership_sidecar(new / media.name).exists()
        and safety_mod.has_valid_ownership_marker(new / media.name)
        and not owner.exists(),
    )


def test_quota_deletes_only_momento_owned_recordings(tmp: Path) -> None:
    folder = tmp / "quota"
    folder.mkdir()
    mib = 1024 * 1024

    unrelated_mkv = folder / "family-holiday.mkv"
    unrelated_mp4 = folder / "downloaded-film.mp4"
    with unrelated_mkv.open("wb") as fh:
        fh.truncate(700 * mib)
    with unrelated_mp4.open("wb") as fh:
        fh.truncate(700 * mib)
    # A stale marker must not transfer ownership to replacement content.
    _ownership_sidecar(unrelated_mkv).write_text(
        '{"schema":1,"owner":"Momento","size":1,"mtime_ns":1}',
        encoding="utf-8",
    )

    owned_old = folder / "wow_2026-01-01_120000.mkv"
    owned_new = folder / "wow_2026-01-01_130000.mkv"
    made_old = _make_tagged_sparse_recording(owned_old, 650 * mib)
    made_new = _make_tagged_sparse_recording(owned_new, 650 * mib)
    check("quota fixture: tagged Momento recordings were generated", made_old and made_new)
    if not (made_old and made_new):
        return

    old_thumb = owned_old.with_name(owned_old.name + ".thumb.jpg")
    old_bookmarks = owned_old.with_name(owned_old.name + ".bookmarks.json")
    old_thumb.write_bytes(b"thumb")
    old_bookmarks.write_text("[]", encoding="utf-8")
    now = time.time()
    for age, path in (
        (500, unrelated_mkv),
        (400, unrelated_mp4),
        (300, owned_old),
        (200, owned_new),
    ):
        os.utime(path, (now - age, now - age))

    deleted = storage_mod.enforce_storage_limit(folder, max_gb=1)

    check("quota safety: unrelated MKV is never deleted", unrelated_mkv.exists())
    check("quota safety: unrelated MP4 is never deleted", unrelated_mp4.exists())
    check(
        "quota safety: stale ownership marker cannot claim unrelated media",
        unrelated_mkv.exists(),
    )
    check(
        "quota safety: only the oldest owned recording is evicted",
        deleted == 1 and not owned_old.exists() and owned_new.exists(),
    )
    check(
        "quota safety: evicted recording sidecars are removed",
        not old_thumb.exists()
        and not old_bookmarks.exists()
        and not _ownership_sidecar(owned_old).exists(),
    )
    check(
        "quota safety: embedded-tag fallback creates a durable marker",
        _ownership_sidecar(owned_new).exists(),
    )


def test_replacement_media_cannot_inherit_ownership(tmp: Path) -> None:
    media = tmp / "replace-me.mkv"
    media.write_bytes(b"A" * 8192)
    check(
        "replacement fixture: original receives an ownership marker",
        safety_mod.mark_recording_owned(media),
    )
    original = media.stat()
    replacement = tmp / "replacement.mkv"
    replacement.write_bytes(b"B" * original.st_size)
    os.utime(
        replacement,
        ns=(replacement.stat().st_atime_ns, original.st_mtime_ns),
    )
    os.replace(replacement, media)
    replaced = media.stat()
    check(
        "replacement fixture: size and timestamp were preserved",
        replaced.st_size == original.st_size
        and replaced.st_mtime_ns == original.st_mtime_ns,
    )
    check(
        "replacement media cannot inherit the prior file's ownership",
        not safety_mod.has_valid_ownership_marker(media),
    )


def test_rejected_progress_waits_for_worker(tmp: Path) -> None:
    old = tmp / "thread-old"
    new = tmp / "thread-new"
    old.mkdir()
    source = old / "game_2026-01-01_120000.mkv"
    source.write_bytes(b"media")

    class SlowMigrationWorker:
        finished = False

        def __init__(self, _old: Path, _new: Path) -> None:
            pass

        def collect_media_pairs(self):
            return [(source, new / source.name)]

        def run(self, *, pairs, progress_callback):
            del pairs
            progress_callback(0, 1, source.name)
            time.sleep(0.2)
            type(self).finished = True
            progress_callback(1, 1, "")
            return 1, 0

    class TrackingThread(QThread):
        instances: list[QThread] = []

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            type(self).instances.append(self)

    real_worker = settings_mod.MigrationWorker
    real_thread = settings_mod.QThread
    settings_mod.MigrationWorker = SlowMigrationWorker
    settings_mod.QThread = TrackingThread
    panel = SettingsPanel(Config(output_folder=old))

    # Bypass reject()/closeEvent() to model application teardown unexpectedly
    # ending the nested modal loop. The method must still drain its worker.
    def force_reject() -> None:
        modal = QApplication.activeModalWidget()
        if modal is not None:
            QDialog.done(modal, int(QDialog.DialogCode.Rejected))

    QTimer.singleShot(25, force_reject)
    started = time.monotonic()
    try:
        result = panel._run_migration_with_progress(old, new)
        elapsed = time.monotonic() - started
        check("unexpected rejection waits for migration completion", elapsed >= 0.15)
        check("migration result survives unexpected rejection", result == (1, 0))
        check(
            "no migration QThread is running when the method returns",
            bool(TrackingThread.instances)
            and all(not thread.isRunning() for thread in TrackingThread.instances),
        )
    finally:
        for thread in TrackingThread.instances:
            if thread.isRunning():
                thread.quit()
                thread.wait(1000)
        settings_mod.MigrationWorker = real_worker
        settings_mod.QThread = real_thread
        panel.close()
        _app.processEvents()


def test_progress_dialog_refuses_user_close(tmp: Path) -> None:
    old = tmp / "close-old"
    new = tmp / "close-new"
    old.mkdir()
    source = old / "game_2026-01-01_120000.mkv"
    source.write_bytes(b"media")

    class SlowMigrationWorker:
        def __init__(self, _old: Path, _new: Path) -> None:
            pass

        def collect_media_pairs(self):
            return [(source, new / source.name)]

        def run(self, *, pairs, progress_callback):
            del pairs
            progress_callback(0, 1, source.name)
            time.sleep(0.15)
            progress_callback(1, 1, "")
            return 1, 0

    real_worker = settings_mod.MigrationWorker
    settings_mod.MigrationWorker = SlowMigrationWorker
    panel = SettingsPanel(Config(output_folder=old))
    still_visible_after_close: list[bool] = []

    def try_close() -> None:
        modal = QApplication.activeModalWidget()
        if modal is not None:
            modal.close()
            still_visible_after_close.append(modal.isVisible())

    QTimer.singleShot(25, try_close)
    try:
        result = panel._run_migration_with_progress(old, new)
        check(
            "migration progress ignores a user close attempt",
            still_visible_after_close == [True],
        )
        check("non-closable migration still reports its result", result == (1, 0))
    finally:
        settings_mod.MigrationWorker = real_worker
        panel.close()
        _app.processEvents()


def test_dead_sync_wrapper_is_removed() -> None:
    check(
        "dead migrate_to_folder wrapper is absent",
        not hasattr(storage_mod, "migrate_to_folder"),
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="momento_storage_migration_") as d:
        tmp = Path(d)
        test_sidecar_failure_is_reported(tmp)
        test_quota_deletes_only_momento_owned_recordings(tmp)
        test_replacement_media_cannot_inherit_ownership(tmp)
        test_rejected_progress_waits_for_worker(tmp)
        test_progress_dialog_refuses_user_close(tmp)
        test_dead_sync_wrapper_is_removed()

    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
