"""A settings file that can't be used must never be overwritten at launch.

Before this, any load fallback returned a bare Config(setup_complete=False);
startup treated that as a first run and immediately saved defaults over the
user's config.json. A transient read lock (antivirus/OneDrive at sign-in) did
so without even keeping a backup, and a non-UTF-8 hand edit crashed startup.
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import momento.__main__ as app_main  # noqa: E402
import momento.config as config_mod  # noqa: E402

GOOD = {
    "setup_complete": True,
    "output_folder": "D:\\Recordings\\Mementos",
    "framerate": 60,
    "mic_volume_pct": 150,
    "known_games": ["Wow.exe"],
}


class StartupConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="mementos_config_load_")
        self.folder = Path(self._dir.name)
        self.path = self.folder / "config.json"
        patches = [
            mock.patch.object(config_mod, "config_path", lambda: self.path),
            # Startup seeds real Windows audio devices; keep tests device-free.
            mock.patch.object(app_main, "_seed_default_devices", lambda cfg: None),
            mock.patch.object(app_main, "_migrate_legacy_audio_devices", lambda cfg: None),
            mock.patch("time.sleep", lambda _s: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _backups(self) -> list[Path]:
        return sorted(self.folder.glob("config.json.broken-*"))

    def _assert_untouched_returning_user(self, original: bytes, problem: str) -> None:
        loaded, first_run = app_main._load_startup_config()
        self.assertEqual(loaded.problem, problem)
        self.assertFalse(first_run, "a broken existing config is not a first run")
        self.assertTrue(loaded.config.setup_complete)
        self.assertEqual(self.path.read_bytes(), original, "config.json was overwritten")

    def test_invalid_json_is_kept_and_backed_up(self) -> None:
        original = b'{"output_folder": "D:\\\\Mementos", "framerate": 60,'
        self.path.write_bytes(original)
        self._assert_untouched_returning_user(original, "invalid_json")
        self.assertEqual([p.read_bytes() for p in self._backups()], [original])

    def test_non_object_json_is_kept_and_backed_up(self) -> None:
        original = b'["not", "a", "config"]'
        self.path.write_bytes(original)
        self._assert_untouched_returning_user(original, "not_an_object")
        self.assertEqual(len(self._backups()), 1)

    def test_persistent_read_lock_never_overwrites(self) -> None:
        original = json.dumps(GOOD).encode()
        self.path.write_bytes(original)
        real_read = Path.read_bytes
        calls = {"n": 0}

        def locked(path_self):
            if path_self == self.path:
                calls["n"] += 1
                raise PermissionError(32, "The process cannot access the file")
            return real_read(path_self)

        with mock.patch.object(Path, "read_bytes", locked):
            loaded, first_run = app_main._load_startup_config()
        self.assertEqual(loaded.problem, "unreadable")
        self.assertFalse(first_run, "an unreadable existing config is not a first run")
        self.assertTrue(loaded.config.setup_complete)
        self.assertEqual(self.path.read_bytes(), original, "config.json was overwritten")
        self.assertEqual(calls["n"], config_mod._READ_ATTEMPTS)

    def test_transient_read_lock_is_retried(self) -> None:
        self.path.write_text(json.dumps(GOOD), encoding="utf-8")
        real_read = Path.read_bytes
        calls = {"n": 0}

        def flaky(path_self):
            if path_self == self.path and calls["n"] < 2:
                calls["n"] += 1
                raise PermissionError(32, "locked by another process")
            return real_read(path_self)

        with mock.patch.object(Path, "read_bytes", flaky):
            loaded, first_run = app_main._load_startup_config()
        self.assertIsNone(loaded.problem)
        self.assertFalse(first_run)
        self.assertEqual(loaded.config.mic_volume_pct, 150)

    def test_utf8_bom_is_accepted(self) -> None:
        self.path.write_bytes(b"\xef\xbb\xbf" + json.dumps(GOOD).encode())
        loaded, _ = app_main._load_startup_config()
        self.assertIsNone(loaded.problem)
        self.assertEqual(loaded.config.framerate, 60)

    def test_ansi_hand_edit_is_accepted_instead_of_crashing(self) -> None:
        data = dict(GOOD, output_folder="D:\\Vidéos\\Mementos")
        self.path.write_bytes(json.dumps(data, ensure_ascii=False).encode("cp1252"))
        loaded, _ = app_main._load_startup_config()
        self.assertIsNone(loaded.problem)
        self.assertEqual(str(loaded.config.output_folder), "D:\\Vidéos\\Mementos")

    def test_overflowing_number_only_resets_that_field(self) -> None:
        self.path.write_text(
            '{"setup_complete": true, "framerate": 1e400, "mic_volume_pct": 150}',
            encoding="utf-8",
        )
        loaded, _ = app_main._load_startup_config()
        self.assertIsNone(loaded.problem)
        self.assertEqual(loaded.config.mic_volume_pct, 150)
        self.assertEqual(loaded.config.framerate, config_mod.Config().framerate)
        self.assertEqual(self._backups(), [])

    def test_genuine_first_run_still_seeds_and_saves(self) -> None:
        loaded, first_run = app_main._load_startup_config()
        self.assertIsNone(loaded.problem)
        self.assertTrue(first_run)
        self.assertTrue(self.path.exists(), "first run should persist seeded defaults")
        self.assertFalse(json.loads(self.path.read_text("utf-8"))["setup_complete"])

    def test_unfinished_setup_resumes(self) -> None:
        self.path.write_text(json.dumps(dict(GOOD, setup_complete=False)), encoding="utf-8")
        _, first_run = app_main._load_startup_config()
        self.assertTrue(first_run)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
