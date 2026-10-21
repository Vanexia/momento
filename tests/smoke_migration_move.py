"""Folder migration must never leave a recording in two places.

``shutil.move`` falls back to copy-then-delete after ANY rename failure. When
another handle (the editor's preview player) held the file, it copied every
byte to the new folder, failed to delete the locked original, and left a
sidecar-less duplicate that a retry then skipped as "already there".

A cross-drive move is simulated by making the direct rename fail with the
Windows "different disk drive" error, exactly what drives the copy path. Open
Python file handles stand in for the preview lock (Python opens files without
delete sharing, like QMediaPlayer).
"""

from __future__ import annotations

import errno
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.core import storage_cleanup  # noqa: E402
from momento.core.storage_cleanup import MigrationWorker, move_file_safely  # noqa: E402

_real_rename = os.rename


def _cross_drive(old_root: Path, new_root: Path):
    """os.rename that refuses to cross from old_root into new_root."""

    def rename(src, dst):
        src_p, dst_p = Path(src), Path(dst)
        if old_root in src_p.parents and new_root in dst_p.parents:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return _real_rename(src, dst)

    # Patch the shared os module so shutil.move would see it too.
    return mock.patch("os.rename", rename)


class MoveFileSafelyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="mementos_move_")
        root = Path(self._dir.name)
        self.old = root / "old"
        self.new = root / "new"
        self.old.mkdir()
        self.new.mkdir()
        self.src = self.old / "game_2026.mkv"
        self.src.write_bytes(b"recording-bytes" * 1000)
        self.dst = self.new / "game_2026.mkv"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _leftovers(self) -> list[str]:
        return sorted(p.name for p in (*self.old.iterdir(), *self.new.iterdir())
                      if p.name.endswith((".moving", ".partial")))

    def test_same_volume_move(self) -> None:
        move_file_safely(self.src, self.dst)
        self.assertFalse(self.src.exists())
        self.assertEqual(self.dst.read_bytes(), b"recording-bytes" * 1000)

    def test_same_volume_locked_source_copies_nothing(self) -> None:
        with open(self.src, "rb"):
            with self.assertRaises(OSError):
                move_file_safely(self.src, self.dst)
        self.assertTrue(self.src.exists())
        self.assertFalse(self.dst.exists())

    def test_cross_drive_move(self) -> None:
        with _cross_drive(self.old, self.new):
            move_file_safely(self.src, self.dst)
        self.assertFalse(self.src.exists())
        self.assertEqual(self.dst.read_bytes(), b"recording-bytes" * 1000)
        self.assertEqual(self._leftovers(), [])

    def test_cross_drive_locked_source_copies_nothing(self) -> None:
        with _cross_drive(self.old, self.new), open(self.src, "rb"):
            with mock.patch.object(storage_cleanup.shutil, "copy2") as copy:
                with self.assertRaises(OSError):
                    move_file_safely(self.src, self.dst)
                copy.assert_not_called()
        self.assertTrue(self.src.exists())
        self.assertFalse(self.dst.exists())
        self.assertEqual(self._leftovers(), [])

    def test_cross_drive_copy_failure_rolls_back(self) -> None:
        with _cross_drive(self.old, self.new), mock.patch.object(
            storage_cleanup.shutil, "copy2", side_effect=OSError(errno.ENOSPC, "Disk full")
        ):
            with self.assertRaises(OSError):
                move_file_safely(self.src, self.dst)
        self.assertTrue(self.src.exists())
        self.assertFalse(self.dst.exists())
        self.assertEqual(self._leftovers(), [])

    def test_cross_drive_undeletable_original_rolls_back(self) -> None:
        staged = self.src.with_name(f".{self.src.name}.moving")
        real_unlink = Path.unlink

        def unlink(path_self, missing_ok=False):
            if path_self == staged:
                raise PermissionError(32, "in use")
            return real_unlink(path_self, missing_ok=missing_ok)

        with _cross_drive(self.old, self.new), mock.patch.object(Path, "unlink", unlink):
            with self.assertRaises(OSError):
                move_file_safely(self.src, self.dst)
        self.assertTrue(self.src.exists(), "original restored")
        self.assertFalse(self.dst.exists(), "no duplicate left behind")
        self.assertEqual(self._leftovers(), [])


class MigrationWorkerTests(unittest.TestCase):
    def test_previewed_recording_is_not_duplicated_across_drives(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mementos_migrate_") as folder:
            root = Path(folder)
            old, new = root / "old", root / "new"
            old.mkdir()
            new.mkdir()
            locked = old / "locked.mkv"
            free = old / "free.mkv"
            locked.write_bytes(b"L" * 4096)
            free.write_bytes(b"F" * 4096)
            (old / "locked.mkv.thumb.jpg").write_bytes(b"thumb")

            with _cross_drive(old, new), open(locked, "rb"):
                moved, failed = MigrationWorker(old, new).run()

            self.assertEqual((moved, failed), (1, 1))
            self.assertFalse((new / "locked.mkv").exists(), "no duplicate of the locked file")
            self.assertTrue(locked.exists())
            self.assertTrue((old / "locked.mkv.thumb.jpg").exists(), "sidecar stays with it")
            self.assertTrue((new / "free.mkv").exists())
            self.assertFalse(free.exists())


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
