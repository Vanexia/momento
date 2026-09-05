"""Exercise library mutations against real Windows file locks and collisions."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QMessageBox  # noqa: E402

import momento.ui.editor as editor  # noqa: E402
from momento.core.recording_safety import begin_file_activity  # noqa: E402


class _Host:
    _is_file_busy = editor.EditorWindow._is_file_busy

    def __init__(self) -> None:
        self.status = ""
        self._status = SimpleNamespace(
            setText=lambda text: setattr(self, "status", text)
        )
        self._list = SimpleNamespace(remove_path=lambda _path: None)
        self._current_selection = None
        self.preview = SimpleNamespace(load=lambda _path: None)
        self._game_slug_cache = {}
        self._duration_cache = {}
        self._duration_hint_cache = {}
        self.exports = []
        self._launch_trim = lambda *args: self.exports.append(args)
        self.timeline = SimpleNamespace(start_seconds=0.0, end_seconds=2.0)

    def refresh(self) -> None:
        pass


class LibraryFileFailures(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="momento-file-failures-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.host = _Host()
        self.messages = []
        for method in ("warning", "critical"):
            mocking = patch.object(
                editor.QMessageBox,
                method,
                side_effect=lambda *args: self.messages.append(args[2]),
            )
            mocking.start()
            self.addCleanup(mocking.stop)
        question = patch.object(
            editor.QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
        )
        question.start()
        self.addCleanup(question.stop)
        naming = patch.object(
            editor.QInputDialog, "getText", return_value=("renamed", True)
        )
        naming.start()
        self.addCleanup(naming.stop)

    def fixture(self, name: str = "source.mkv") -> tuple[Path, ...]:
        media = self.root / name
        paths = (
            media,
            *(
                media.with_name(media.name + suffix)
                for suffix in (".thumb.jpg", ".bookmarks.json", ".momento.json")
            ),
        )
        for i, path in enumerate(paths):
            path.write_bytes(f"fixture-{i}".encode())
        return paths

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
    def test_failed_video_delete_preserves_all_related_files(self) -> None:
        paths = self.fixture()
        # An ordinary reader denies delete sharing on Windows, like a player
        # still holding the video. Sidecars remain writable.
        with paths[0].open("rb"):
            editor.EditorWindow._on_delete_requested(self.host, [paths[0]])
        self.assertTrue(paths[0].exists(), "The locked recording must survive")
        for i, path in enumerate(paths):
            self.assertTrue(path.exists(), f"Failed deletion removed {path.suffix}")
            self.assertEqual(path.read_bytes(), f"fixture-{i}".encode())
        self.assertTrue(self.messages)

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
    def test_bulk_delete_reports_the_file_actually_removed(self) -> None:
        locked = self.fixture("locked.mkv")
        removable = self.fixture("removable.mkv")
        with locked[0].open("rb"):
            editor.EditorWindow._on_delete_requested(
                self.host, [locked[0], removable[0]]
            )
        self.assertFalse(removable[0].exists())
        self.assertIn("removable.mkv", self.host.status)
        self.assertNotIn("locked.mkv", self.host.status)

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
    def test_sidecar_cleanup_error_still_counts_deleted_video(self) -> None:
        paths = self.fixture()
        with paths[2].open("rb"):
            editor.EditorWindow._on_delete_requested(self.host, [paths[0]])
        self.assertFalse(paths[0].exists())
        self.assertTrue(paths[2].exists())
        self.assertIn("source.mkv", self.host.status)
        self.assertTrue(self.messages)

    def test_rename_refuses_sidecar_collision_before_moving_video(self) -> None:
        paths = self.fixture()
        target_thumb = self.root / "renamed.mkv.thumb.jpg"
        target_thumb.write_bytes(b"unrelated thumbnail")
        editor.EditorWindow._on_rename_requested(self.host, paths[0])
        self.assertTrue(
            all(path.exists() for path in paths),
            "A collision split the recording from its sidecars",
        )
        self.assertFalse((self.root / "renamed.mkv").exists())
        self.assertEqual(target_thumb.read_bytes(), b"unrelated thumbnail")
        self.assertTrue(self.messages)

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
    def test_rename_restores_original_names_after_late_lock_failure(self) -> None:
        paths = self.fixture()
        with paths[-1].open("rb"):
            editor.EditorWindow._on_rename_requested(self.host, paths[0])
        self.assertTrue(
            all(path.exists() for path in paths),
            "Rename left a partially moved recording",
        )
        self.assertFalse(any(self.root.glob("renamed*")))
        self.assertTrue(self.messages)

    def test_rename_does_not_inherit_orphan_bookmarks(self) -> None:
        media = self.root / "source.mkv"
        media.write_bytes(b"video with no bookmarks")
        orphan = self.root / "renamed.mkv.bookmarks.json"
        orphan.write_bytes(b"[1.25]")
        editor.EditorWindow._on_rename_requested(self.host, media)
        self.assertTrue(media.exists())
        self.assertFalse((self.root / "renamed.mkv").exists())
        self.assertEqual(orphan.read_bytes(), b"[1.25]")

    def test_delete_rechecks_activity_after_confirmation(self) -> None:
        paths = self.fixture()
        activity = []

        def confirm(*_args):
            activity.append(begin_file_activity(paths[0]))
            return QMessageBox.StandardButton.Yes

        try:
            with patch.object(editor.QMessageBox, "question", side_effect=confirm):
                editor.EditorWindow._on_delete_requested(self.host, [paths[0]])
            self.assertTrue(
                all(path.exists() for path in paths),
                "A newly busy recording was deleted",
            )
        finally:
            for lease in activity:
                lease.release()

    def test_rename_rechecks_activity_after_name_prompt(self) -> None:
        paths = self.fixture()
        activity = []

        def name(*_args):
            activity.append(begin_file_activity(paths[0]))
            return "renamed", True

        try:
            with patch.object(editor.QInputDialog, "getText", side_effect=name):
                editor.EditorWindow._on_rename_requested(self.host, paths[0])
            self.assertTrue(
                all(path.exists() for path in paths),
                "A newly busy recording was renamed",
            )
        finally:
            for lease in activity:
                lease.release()

    def test_unavailable_clip_folder_is_reported_without_escaping_ui_slot(self) -> None:
        media = self.fixture()[0]
        self.host._current_selection = media
        (self.root / "clips").write_bytes(b"a file occupies the folder name")
        editor.EditorWindow._on_export_clicked(self.host)
        self.assertEqual(self.host.exports, [])
        self.assertTrue(
            self.messages, "An unavailable output folder needs a visible error"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
