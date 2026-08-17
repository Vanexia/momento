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
import momento.ui.settings_dialog as settings_mod  # noqa: E402
from momento.config import Config  # noqa: E402
from momento.core.storage_cleanup import MigrationWorker  # noqa: E402
from momento.ui.settings_dialog import SettingsPanel  # noqa: E402


_app = QApplication.instance() or QApplication(sys.argv[:1])
_results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    _results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} - {name}")


def test_sidecar_failure_is_reported(tmp: Path) -> None:
    old = tmp / "sidecar-old"
    new = tmp / "sidecar-new"
    old.mkdir()
    media = old / "game_2026-01-01_120000.mkv"
    sidecar = old / f"{media.name}.bookmarks.json"
    media.write_bytes(b"media")
    sidecar.write_text("[]", encoding="utf-8")

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
        test_rejected_progress_waits_for_worker(tmp)
        test_progress_dialog_refuses_user_close(tmp)
        test_dead_sync_wrapper_is_removed()

    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
