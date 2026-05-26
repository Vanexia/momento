"""Momento entry point: single-instance check, QApplication, tray + session wiring."""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

# Module-level logger so helpers like _migrate_legacy_clips and
# _seed_default_devices (which run before main() builds its local logger)
# have something to call. main() reuses this one.
logger = logging.getLogger("momento")

from momento.config import load_config, save_config
from momento.core.media_probe import find_broken_recordings, repair_async
from momento.core.session import SessionManager
from momento.core.storage_cleanup import enforce_storage_limit
from momento.ui.theme import apply_dark_theme
from momento.ui.tray import MomentoTray
from momento.ui.welcome import WelcomeDialog
from momento.util.format import format_bytes, free_bytes_for
from momento.util.hotkey import HotkeyError, HotkeyService
from momento.util.logging_setup import install_exception_hook, setup_logging
from momento.util.paths import config_path
from momento.util.resources import app_icon_path
from momento.util.single_instance import AlreadyRunningError, SingleInstance


_RECORDING_STAMP_RE = re.compile(r"_\d{4}-\d{2}-\d{2}_\d{6}")
_CLIP_DEFAULT_SUFFIX_RE = re.compile(r"_clip_\d+$", re.IGNORECASE)
_CLIP_SIDECAR_SUFFIXES = (".thumb.jpg", ".bookmarks.json")


def _looks_like_legacy_clip(path) -> bool:
    """True if ``path`` (in the recordings root) appears to be an export.

    Heuristic — only used during the one-shot migration:
      * Filename matches the default trim-worker suffix ``_clip_N``, OR
      * Filename lacks the recorder's ``_YYYY-MM-DD_HHMMSS`` stamp, which
        means it didn't come from the recording flow.
    Either pattern is a strong signal the file is a user-exported trim.
    """
    stem = path.stem
    if _CLIP_DEFAULT_SUFFIX_RE.search(stem):
        return True
    return _RECORDING_STAMP_RE.search(stem) is None


def _migrate_legacy_clips(folder) -> None:
    """Move clip-shaped files in ``folder`` into ``folder/clips/``.

    Moves the media file plus any matching sidecars (thumbnail / bookmarks).
    Idempotent: subsequent calls find nothing left to move.
    """
    folder = Path(folder)
    if not folder.is_dir():
        return
    clips_dir = folder / "clips"
    moved = 0
    try:
        entries = list(folder.iterdir())
    except OSError:
        return
    for p in entries:
        if not p.is_file():
            continue
        if p.suffix.lower() not in (".mp4", ".mkv"):
            continue
        if not _looks_like_legacy_clip(p):
            continue
        clips_dir.mkdir(parents=True, exist_ok=True)
        target = clips_dir / p.name
        if target.exists():
            logger.warning(
                "Skipped migrating %s — clips/%s already exists",
                p.name, p.name,
            )
            continue
        try:
            p.rename(target)
        except OSError as e:
            logger.warning("Could not migrate %s: %s", p.name, e)
            continue
        # Move sidecars next to it.
        for suffix in _CLIP_SIDECAR_SUFFIXES:
            sidecar = p.with_name(p.name + suffix)
            if sidecar.exists():
                try:
                    sidecar.rename(target.with_name(target.name + suffix))
                except OSError:
                    pass
        moved += 1
    if moved:
        logger.info("Migrated %d legacy clip(s) into %s", moved, clips_dir)


def _log_auto_repair_done(path_str: str, ok: bool, err: str) -> None:
    if ok:
        logger.info("Auto-repair finished: %s", path_str)
    else:
        logger.warning("Auto-repair failed for %s: %s", path_str, err[:200])


def _warn_if_low_disk(tray, config) -> None:
    """Show the warning toast at startup if the output drive is below the
    user's low-disk watermark.

    ``config.low_disk_warning_gb == 0`` disables the check. Failure to
    stat the drive is a non-event — Momento just won't warn.
    """
    threshold_gb = config.low_disk_warning_gb
    if threshold_gb <= 0:
        return
    free = free_bytes_for(Path(config.output_folder))
    if free is None or free >= threshold_gb * (1 << 30):
        return
    try:
        toast = tray._ensure_toast()
        toast.show_warning(
            "Low disk space",
            f"Only {format_bytes(free)} free on the recordings drive. "
            f"Recordings may run out of space during a game.",
        )
    except Exception:
        logger.exception("Could not show low-disk warning toast")


def _seed_default_devices(config) -> None:
    """Populate empty mic/system fields with the current Windows defaults.

    Importing ``soundcard`` is a little expensive (COM init) so we do it
    here rather than at module load. Either lookup failing is non-fatal —
    the user can always pick devices manually in Settings.
    """
    import soundcard as sc

    if not config.mic_device:
        try:
            mic = sc.default_microphone()
            if mic is not None:
                config.mic_device = str(mic.id)
        except Exception:
            pass
    if not config.system_audio_device:
        try:
            spk = sc.default_speaker()
            if spk is not None:
                config.system_audio_device = str(spk.id)
        except Exception:
            pass


def main() -> int:
    setup_logging()
    install_exception_hook()

    try:
        instance = SingleInstance()
        instance.acquire()
    except AlreadyRunningError:
        # Setting the app-level icon BEFORE constructing the dialog gives
        # the title bar Momento's icon instead of Qt's default.
        app = QApplication.instance() or QApplication(sys.argv)
        icon_path = app_icon_path()
        if icon_path is not None:
            app.setWindowIcon(QIcon(str(icon_path)))
        QMessageBox.information(
            None, "Momento", "Momento is already running (check the system tray)."
        )
        return 1

    try:
        app = QApplication(sys.argv)
        app.setApplicationName("Momento")
        app.setQuitOnLastWindowClosed(False)
        apply_dark_theme(app)

        icon_path = app_icon_path()
        if icon_path is not None:
            app.setWindowIcon(QIcon(str(icon_path)))

        if not QSystemTrayIcon.isSystemTrayAvailable():
            QMessageBox.critical(None, "Momento", "System tray is not available on this platform.")
            return 2

        # Loads %APPDATA%/Momento/config.json (or defaults if missing).
        is_first_run = not config_path().exists()
        config = load_config()
        if is_first_run:
            # Pre-pick the system default mic + speaker so recording works
            # out-of-the-box even if the user dismisses the welcome dialog
            # without opening Settings. Saves the config so this only runs
            # once. Errors are non-fatal (worst case: same empty fields as
            # before, "Couldn't record" toast on first game launch).
            try:
                _seed_default_devices(config)
                save_config(config)
                logger.info(
                    "First-run defaults seeded: mic=%r system=%r",
                    config.mic_device, config.system_audio_device,
                )
            except Exception:
                logger.exception("Could not seed first-run device defaults")
        logger.info(
            "Loaded config: mic=%r system=%r output=%s first_run=%s",
            config.mic_device, config.system_audio_device, config.output_folder,
            is_first_run,
        )

        session = SessionManager(config)
        tray = MomentoTray(session, config)
        session.set_status_callback(tray.on_session_status)
        session.set_failure_callback(tray.on_session_failure)
        session.set_bookmark_callback(tray.on_bookmark_added)
        tray.on_session_status("idle", None)
        tray.show()

        # Global bookmark hotkey (default F8, configurable in Settings).
        hotkey_service = HotkeyService(app)
        hotkey_service.set_callback(session.add_bookmark)
        try:
            hotkey_service.set_hotkey(config.bookmark_hotkey)
        except HotkeyError as e:
            logger.warning("Bookmark hotkey unavailable (%s); continuing without it", e)
        tray.set_hotkey_service(hotkey_service)

        if config.start_monitoring_on_launch:
            session.start()
            logger.info("Momento started; tray is live")
        else:
            logger.info(
                "Momento started in paused state — monitoring won't begin "
                "until the user resumes it from the tray menu",
            )

        # One-shot migration: move legacy exported clips out of the root
        # recordings folder and into clips/. Runs every startup but is
        # idempotent (only moves files that match the clip pattern AND
        # are still in the root).
        try:
            _migrate_legacy_clips(config.output_folder)
        except Exception:
            logger.exception("Clip migration raised")

        # Recovery pass for recordings left in a broken state by a previous
        # crash (process killed before encoder.stop() finalised the segment
        # header). Synchronous scan (~50ms per file), async repairs.
        try:
            broken = find_broken_recordings(config.output_folder)
            if broken:
                logger.warning(
                    "Found %d unfinalised recording(s); auto-repairing in background",
                    len(broken),
                )
                for p in broken:
                    logger.info("Auto-repair queued: %s", p.name)
                    repair_async(p, _log_auto_repair_done)
        except Exception:
            logger.exception("Crash-recovery scan raised")

        # Storage hygiene — trim old recordings if the user has set a quota,
        # and surface a warning toast if the output drive is low on space.
        try:
            removed = enforce_storage_limit(config.output_folder, config.max_storage_gb)
            if removed:
                logger.info(
                    "Storage cleanup at startup removed %d old recording(s)",
                    removed,
                )
        except Exception:
            logger.exception("Startup storage cleanup raised")
        try:
            _warn_if_low_disk(tray, config)
        except Exception:
            logger.exception("Low-disk warning check raised")

        # First-run nudge so a brand-new user knows to open Settings before
        # launching a game. Saving Settings creates config.json which prevents
        # this from firing on subsequent launches; returning users can
        # re-open the wizard from the editor's File menu.
        if is_first_run:
            welcome = WelcomeDialog(config)
            welcome.settings_saved.connect(tray._apply_new_config)
            welcome.exec()

        try:
            rc = app.exec()
        finally:
            logger.info("Shutting down session ...")
            hotkey_service.shutdown()
            session.shutdown()
        return rc
    finally:
        instance.release()


if __name__ == "__main__":
    sys.exit(main())
