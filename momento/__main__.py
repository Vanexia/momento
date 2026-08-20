"""Momento entry point: single-instance check, QApplication, tray + session wiring."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from momento import __version__
from momento.config import load_config, save_config
from momento.core.media_probe import (
    cleanup_stale_repair_temps,
    find_broken_recordings,
    repair_async,
)
from momento.core.session import SessionManager
from momento.core.storage_cleanup import enforce_storage_limit
from momento.ui.theme import apply_dark_theme
from momento.ui.tray import MomentoTray
from momento.ui.welcome import WelcomeDialog
from momento.updater.attempts import UpdateAttemptStore
from momento.updater.cache import UpdateCache
from momento.updater.client import UpdateClient
from momento.updater.runtime import UpdateRuntime, updated_attempt_token
from momento.updater.service import UpdateService
from momento.util.format import format_bytes, free_bytes_for
from momento.util.hotkey import HotkeyError, HotkeyService
from momento.util.logging_setup import install_exception_hook, setup_logging
from momento.util.paths import update_cache_dir
from momento.util.resources import app_icon_path, update_public_key_path
from momento.util.single_instance import AlreadyRunningError, SingleInstance

# Module-level logger so startup helpers that run before ``main()`` builds its
# local state have somewhere to report failures. ``main()`` reuses this one.
logger = logging.getLogger("momento")


def _log_auto_repair_done(path_str: str, ok: bool, err: str) -> None:
    if ok:
        logger.info("Auto-repair finished")
    else:
        logger.warning("Auto-repair failed: %s", err[:200])


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


def _migrate_legacy_audio_devices(config) -> None:
    """Rewrite soundcard-era WASAPI endpoint ids ("{0.0.1...}.{guid}") in the
    saved config to PortAudio friendly names, so the Settings dropdowns show the
    right device and a user's selection survives the backend swap. Runs every
    launch but is a no-op once the values are names. Non-fatal.
    """
    from momento.core import audio_devices

    changed = False
    for field in ("mic_device", "system_audio_device"):
        val = getattr(config, field, "") or ""
        if audio_devices.looks_like_endpoint_id(val):
            name = audio_devices.friendly_name_for_endpoint_id(val)
            if name:
                setattr(config, field, name)
                changed = True
                logger.info("Migrated legacy %s endpoint to its friendly name", field)
    if changed:
        try:
            save_config(config)
        except Exception:
            logger.exception("Could not persist migrated audio device ids")


def _seed_default_devices(config) -> None:
    """Populate empty mic/system fields with openable Windows devices.

    Devices are persisted by friendly name (PortAudio/WASAPI). Either lookup
    failing is non-fatal — the user can always pick devices manually in
    Settings.
    """
    from momento.core import audio_devices

    if config.mic_device and config.system_audio_device:
        return
    try:
        with audio_devices.pyaudio_session() as p:
            if not config.mic_device:
                config.mic_device = _first_openable_mic_name(p)
            if not config.system_audio_device:
                config.system_audio_device = _first_openable_loopback_name(p)
    except Exception:
        logger.exception("Could not seed first-run audio devices")


def _first_openable_mic_name(p) -> str:
    from momento.core import audio_devices

    devices = audio_devices.list_input_device_names(p)
    devices.sort(key=lambda item: (not item[1], item[0].casefold()))
    for name, _is_default in devices:
        if audio_devices.probe_open(p, name, loopback=False):
            return name
        logger.warning("Skipping non-openable first-run microphone default")
    return ""


def _first_openable_loopback_name(p) -> str:
    from momento.core import audio_devices

    # list_output_device_names already returns the default endpoint first.
    for name, _ in audio_devices.list_output_device_names(p):
        if audio_devices.probe_open(p, name, loopback=True):
            return name
        logger.warning("Skipping non-openable first-run system-audio default")
    return ""


def _monitoring_allowed_on_launch(
    config, *, is_first_run: bool, setup_accepted: bool
) -> bool:
    """Start monitoring only after onboarding has been explicitly accepted."""
    return bool(
        config.start_monitoring_on_launch
        and (not is_first_run or setup_accepted)
    )


def _set_app_user_model_id() -> None:
    """Give the process an explicit AppUserModelID so Windows treats Momento as
    its own application on the taskbar — correct icon (the window icon, not the
    host pythonw/python interpreter's), correct grouping, and proper pinning.
    Without this a Python-hosted launch shows the generic Python taskbar icon.
    No-op on non-Windows."""
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Momento.Desktop"
        )
    except Exception:  # pragma: no cover — cosmetic taskbar hint only
        pass


def main() -> int:
    _set_app_user_model_id()
    setup_logging()
    install_exception_hook()

    updated_token = updated_attempt_token(sys.argv[1:])
    try:
        instance = SingleInstance()
        instance.acquire(updated_token=updated_token)
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
        app.setApplicationVersion(__version__)
        app.setQuitOnLastWindowClosed(False)
        apply_dark_theme(app)

        icon_path = app_icon_path()
        if icon_path is not None:
            app.setWindowIcon(QIcon(str(icon_path)))

        if not QSystemTrayIcon.isSystemTrayAvailable():
            QMessageBox.critical(None, "Momento", "System tray is not available on this platform.")
            return 2

        update_runtime = None
        try:
            update_public_key = update_public_key_path().read_bytes()
            update_cache = UpdateCache(update_cache_dir(), update_public_key)
            update_runtime = UpdateRuntime(
                current_version=__version__,
                single_instance=instance,
                cache=update_cache,
                attempts=UpdateAttemptStore(update_cache_dir()),
            )
        except Exception as exc:
            logger.warning("Updater initialization failed (%s)", type(exc).__name__)

        # An explicit marker lets setup safely resume when its window was
        # closed before Finish. Older configs migrate as already complete.
        config = load_config()
        is_first_run = not config.setup_complete
        _migrate_legacy_audio_devices(config)
        if is_first_run:
            # Seed real Windows defaults, but keep setup incomplete until the
            # user successfully finishes the wizard.
            try:
                _seed_default_devices(config)
                save_config(config)
                logger.info(
                    "First-run defaults seeded: mic=%s system=%s",
                    bool(config.mic_device), bool(config.system_audio_device),
                )
            except Exception:
                logger.exception("Could not seed first-run device defaults")
        logger.info(
            "Loaded config: mic=%s system=%s output_configured=%s first_run=%s",
            bool(config.mic_device), bool(config.system_audio_device),
            bool(config.output_folder), is_first_run,
        )

        session = SessionManager(config)
        tray = MomentoTray(session, config)
        session.set_status_callback(tray.on_session_status)
        session.set_failure_callback(tray.on_session_failure)
        session.set_bookmark_callback(tray.on_bookmark_added)
        session.set_recording_finished_callback(tray.on_recording_finished)
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

        update_service = None
        if update_runtime is not None:
            update_service = UpdateService(
                current_version=__version__,
                session=session,
                client=UpdateClient(cache=update_runtime.cache),
                can_install=tray.is_update_install_ready,
                confirm_install=tray._confirm_update_install,
                launch_installer=update_runtime.launch,
                quit_callback=app.quit,
                parent=tray,
            )
            tray.set_update_service(update_service)

        setup_accepted = False
        if is_first_run:
            welcome = WelcomeDialog(config)
            welcome.settings_saved.connect(tray._apply_new_config)
            setup_accepted = welcome.exec() == WelcomeDialog.DialogCode.Accepted
            if setup_accepted:
                config = tray._config
                QTimer.singleShot(0, tray._on_open_editor)

        if _monitoring_allowed_on_launch(
            config, is_first_run=is_first_run, setup_accepted=setup_accepted
        ):
            session.start()
            logger.info("Momento started; tray is live")
        else:
            logger.info(
                "Momento started with game monitoring paused",
            )

        # Recovery pass for recordings left in a broken state by a previous
        # crash (process killed before encoder.stop() finalised the segment
        # header). Synchronous scan (~50ms per file), async repairs. First
        # sweep any orphaned repair temps from a repair that died mid-flight
        # — this runs before the scan queues fresh repairs, so it can only
        # touch stale files, never a live one.
        try:
            swept = cleanup_stale_repair_temps(config.output_folder)
            if swept:
                logger.info("Removed %d orphaned repair temp(s) at startup", swept)
        except Exception:
            logger.exception("Stale repair-temp sweep raised")
        try:
            broken = find_broken_recordings(config.output_folder)
            if broken:
                logger.warning(
                    "Found %d unfinalised recording(s); auto-repairing in background",
                    len(broken),
                )
                for p in broken:
                    logger.info("Auto-repair queued")
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

        if update_service is not None:
            if update_runtime is not None and updated_token is not None:
                update_runtime.schedule_startup_confirmation(
                    updated_token,
                    schedule=QTimer.singleShot,
                    on_complete=lambda _confirmed: (
                        update_service.start_automatic_check()
                    ),
                )
            else:
                QTimer.singleShot(0, update_service.start_automatic_check)

        # Dev/test affordance: `python -m momento --show` opens the editor on
        # launch instead of waiting for a tray click. No effect on normal runs.
        if "--show" in sys.argv:
            QTimer.singleShot(900, tray._on_open_editor)

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
