"""Glue layer that turns game-start/stop events into recording start/stop calls.

Also takes care of one bit of crash recovery: scanning for orphaned ffmpeg.exe
processes that our app spawned in a previous (crashed) run and killing them
before we start a new recording session.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import psutil

from momento.config import Config
from momento.core.bookmarks import BookmarkStore
from momento.core.game_watcher import ActiveGame, GameWatcher
from momento.core.recorder import Recorder
from momento.core.video_capture import wait_for_window
from momento.util.ffmpeg_path import ffmpeg_exe
from momento.util.screen import primary_refresh_rate

logger = logging.getLogger(__name__)

# Status pushed to UI listeners. Strings keep coupling loose.
SessionStatusCallback = Callable[[str, "ActiveGame | None"], None]

STATUS_IDLE = "idle"
STATUS_RECORDING = "recording"

# Failure reasons the tray can surface as a warning toast.
FAILURE_NO_MIC = "no_mic"
FAILURE_NO_SYSTEM_AUDIO = "no_system_audio"
FAILURE_NO_DEVICES = "no_devices"   # both missing
FAILURE_NO_WINDOW = "no_window"
FAILURE_OUTPUT_FOLDER = "output_folder"
FAILURE_GENERIC = "generic"

SessionFailureCallback = Callable[[str, "ActiveGame", str], None]
# (reason, game, detail-message-for-the-toast-subtitle)

# Fires when the bookmark hotkey lands a fresh bookmark (after dedup).
# (game, elapsed_seconds)
SessionBookmarkCallback = Callable[["ActiveGame", float], None]


class SessionManager:
    """Owns the watcher + recorder pair and the config they share.

    The status callback is invoked from the watcher thread; it is intended for
    UI updates (tray label / icon) and should hand off via Qt signals.
    """

    def __init__(
        self,
        config: Config,
        watcher: GameWatcher | None = None,
        recorder: Recorder | None = None,
        on_status_change: SessionStatusCallback | None = None,
        on_failure: SessionFailureCallback | None = None,
    ) -> None:
        self.config = config
        self._recorder = recorder or Recorder()
        self._watcher = watcher or GameWatcher(
            known_games=_active_known_games(config),
            on_game_start=self._on_game_start,
            on_game_stop=self._on_game_stop,
            record_any_fullscreen=config.record_any_fullscreen,
        )
        # Reset callbacks on the watcher in case an external one was supplied.
        self._watcher.on_game_start = self._on_game_start
        self._watcher.on_game_stop = self._on_game_stop

        self._on_status_change = on_status_change
        self._on_failure = on_failure
        self._on_bookmark: SessionBookmarkCallback | None = None
        self._lock = threading.Lock()
        # Read the primary monitor's refresh rate once on construction —
        # SessionManager is built on the Qt thread (from __main__.py) so
        # this is the safe place to call primary_refresh_rate(). The
        # watcher fires _on_game_start on a background thread where we
        # can't talk to Qt; we use this cached value instead.
        self._detected_refresh_rate: int = primary_refresh_rate(default=60)
        logger.info("Detected primary monitor refresh rate: %d Hz", self._detected_refresh_rate)
        self._current_output: Path | None = None
        self._current_game: ActiveGame | None = None
        self._bookmarks: BookmarkStore | None = None

    # ------------------------------------------------------------------ API
    @property
    def is_recording(self) -> bool:
        return self._recorder.is_recording

    @property
    def current_game(self) -> ActiveGame | None:
        return self._current_game

    @property
    def current_output(self) -> Path | None:
        return self._current_output

    def start(self) -> None:
        """Start the session loop. Kills orphan ffmpeg processes first."""
        killed = kill_orphan_ffmpeg_processes()
        if killed:
            logger.warning("Killed %d orphan ffmpeg.exe process(es) from prior runs", killed)
        self._watcher.start()
        self._emit_status(STATUS_IDLE, None)

    def pause_monitoring(self) -> None:
        """Stop the watcher without affecting an in-flight recording.

        Used by the tray's "Pause monitoring" menu item — keeps Momento
        running in the tray but prevents new auto-recordings from starting.
        """
        try:
            self._watcher.stop()
        except Exception:
            logger.exception("Error pausing watcher")

    def resume_monitoring(self) -> None:
        """Re-start the watcher after :meth:`pause_monitoring`."""
        try:
            self._watcher.start()
        except Exception:
            logger.exception("Error resuming watcher")

    def stop_current_recording(self) -> None:
        """Manually stop the in-flight recording (if any). Watcher keeps
        running so the next game launch can start a fresh one."""
        if not self._recorder.is_recording:
            return
        game = self._current_game
        # Reuse the same path the watcher uses when a game exits.
        self._on_game_stop(game) if game is not None else None

    @property
    def is_monitoring(self) -> bool:
        return self._watcher.is_running

    def shutdown(self) -> None:
        """Stop the watcher, then any in-flight recording. Idempotent."""
        try:
            self._watcher.stop()
        except Exception:
            logger.exception("Error stopping watcher")

        if self._recorder.is_recording:
            logger.info("Shutdown: stopping in-flight recording")
            try:
                self._recorder.stop()
            except Exception:
                logger.exception("Error stopping recorder during shutdown")

        with self._lock:
            self._current_game = None
            self._current_output = None
            self._bookmarks = None
        self._emit_status(STATUS_IDLE, None)

    def reload_config(self, config: Config) -> None:
        """Apply config changes. Already-running recording keeps its settings."""
        self.config = config
        self._watcher.update_known_games(_active_known_games(config))
        self._watcher.set_record_any_fullscreen(config.record_any_fullscreen)

    def set_status_callback(self, cb: SessionStatusCallback | None) -> None:
        """Install (or replace) the status-change callback. Safe to call any time."""
        self._on_status_change = cb

    def set_failure_callback(self, cb: SessionFailureCallback | None) -> None:
        """Install (or replace) the recording-can't-start callback."""
        self._on_failure = cb

    def set_bookmark_callback(self, cb: SessionBookmarkCallback | None) -> None:
        """Install (or replace) the bookmark-added callback.

        Fires after a successful, non-deduped bookmark add. Used by the tray
        to surface a toast — the hotkey is otherwise silent, so the user
        needs visual confirmation that the press landed.
        """
        self._on_bookmark = cb

    def add_bookmark(self) -> bool:
        """Record a bookmark at the current recording position.

        Returns True if added, False if no active recording or if the
        timestamp was deduped against a near-twin (< 0.5s).
        """
        elapsed = self._recorder.current_position()
        if elapsed is None:
            return False
        store = self._bookmarks
        if store is None:
            return False
        added = store.add(elapsed)
        if added:
            logger.info("Bookmark @ %.2fs in %s", elapsed, store.recording_path.name)
            game = self._current_game
            cb = self._on_bookmark
            if game is not None and cb is not None:
                try:
                    cb(game, elapsed)
                except Exception:
                    logger.exception("Bookmark callback raised")
        return added

    # --------------------------------------------------------------- events
    def _on_game_start(self, game: ActiveGame) -> None:
        # Don't double-start (defence in depth — watcher enforces this too).
        if self._recorder.is_recording:
            logger.info("Ignoring game start (%s): already recording", game.exe_name)
            return

        c = self.config
        # Device-not-configured is the most common reason a user hits this.
        # Toast them so they know to open Settings — silent log lines are useless.
        if not c.mic_device and not c.system_audio_device:
            self._emit_failure(
                FAILURE_NO_DEVICES, game,
                "Mic and system audio aren't configured. Open Settings → Audio.",
            )
            return
        if not c.mic_device:
            self._emit_failure(
                FAILURE_NO_MIC, game,
                "No microphone configured. Open Settings → Audio to pick one.",
            )
            return
        if not c.system_audio_device:
            self._emit_failure(
                FAILURE_NO_SYSTEM_AUDIO, game,
                "No system-audio output configured. Open Settings → Audio.",
            )
            return

        # Find the game's main window — psutil sees the process before its
        # window exists, so retry briefly. Many games show a splash/launcher
        # first, but we want the largest visible top-level window of the pid
        # (or any child pid).
        hwnd = wait_for_window(game.pid, timeout=10.0)
        if hwnd is None:
            self._emit_failure(
                FAILURE_NO_WINDOW, game,
                f"Couldn't find a window for {game.exe_name} within 10 seconds.",
            )
            return

        slug = _slugify_game(game.exe_name)
        output_path = _build_output_path(c.output_folder, slug)
        framerate = self._detected_refresh_rate if c.framerate_auto else c.framerate
        try:
            self._recorder.start(
                output_path=output_path,
                hwnd=hwnd,
                mic_device=c.mic_device,
                audio_device=c.system_audio_device,
                mic_volume_pct=c.mic_volume_pct,
                audio_volume_pct=c.system_volume_pct,
                resolution=None,  # native window size; user can scale via config
                framerate=framerate,
                audio_offset_ms=c.audio_offset_ms,
                game_slug=slug,
                target_resolution=c.target_resolution,
                quality_preset=c.quality_preset,
                custom_bitrate_kbps=c.custom_bitrate_kbps,
            )
        except RuntimeError as e:
            # _is_writable / mkdir errors come up here.
            msg = str(e)
            reason = (
                FAILURE_OUTPUT_FOLDER
                if "writable" in msg or "Output folder" in msg
                else FAILURE_GENERIC
            )
            logger.error("Recorder.start failed for %s: %s", game.exe_name, msg)
            self._emit_failure(reason, game, msg)
            return
        except Exception as e:
            logger.exception("Failed to start recorder for %s", game.exe_name)
            self._emit_failure(FAILURE_GENERIC, game, str(e) or "Unknown error.")
            return

        with self._lock:
            self._current_game = game
            self._current_output = output_path
            self._bookmarks = BookmarkStore(output_path)
        self._emit_status(STATUS_RECORDING, game)

    def _on_game_stop(self, game: ActiveGame) -> None:
        # Always sync state to idle, even if the recorder isn't running —
        # otherwise a failed _on_game_start (mic missing, output folder gone,
        # WGC declined, …) leaves the session thinking it owns a recording
        # that never began, blocking the next game from triggering one.
        if self._recorder.is_recording:
            try:
                final = self._recorder.stop()
                logger.info("Recording for %s finalised at %s", game.exe_name, final)
            except Exception:
                logger.exception("Error stopping recorder after %s exited", game.exe_name)
        else:
            logger.debug(
                "Game %s exited; recorder wasn't running (start probably failed earlier)",
                game.exe_name,
            )
        with self._lock:
            self._current_game = None
            self._current_output = None
            self._bookmarks = None
        self._emit_status(STATUS_IDLE, None)
        try:
            from momento.core.storage_cleanup import enforce_storage_limit
            enforce_storage_limit(self.config.output_folder, self.config.max_storage_gb)
        except Exception:
            logger.exception("Storage cleanup after recording stop failed")

    def _emit_failure(self, reason: str, game: ActiveGame, detail: str) -> None:
        cb = self._on_failure
        if cb is None:
            return
        try:
            cb(reason, game, detail)
        except Exception:
            logger.exception("Failure callback raised")

    def _emit_status(self, status: str, game: ActiveGame | None) -> None:
        cb = self._on_status_change
        if cb is None:
            return
        try:
            cb(status, game)
        except Exception:
            logger.exception("Status callback raised")


# ----------------------------------------------------------------- helpers
_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _active_known_games(config: Config) -> list[str]:
    """Filter ``config.known_games`` down to the exes the watcher should match.

    Entries the user has toggled off in Settings live in ``config.disabled_games``
    — they stay in ``known_games`` so the UI can show them as known-but-paused,
    but the watcher must never trigger on them.
    """
    if not config.disabled_games:
        return list(config.known_games)
    disabled = {g.lower() for g in config.disabled_games}
    return [g for g in config.known_games if g.lower() not in disabled]


def _slugify_game(exe_name: str) -> str:
    """Strip the .exe and sanitize for a filename."""
    stem = exe_name[:-4] if exe_name.lower().endswith(".exe") else exe_name
    cleaned = _INVALID_FS_CHARS.sub("_", stem).strip().rstrip(".")
    return cleaned or "game"


def _build_output_path(folder: Path, slug: str) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    # MKV matches OBS's default: cluster-based / crash-safe, and we never
    # remux on the recording path. Trim export emits MP4 separately.
    return Path(folder) / f"{slug}_{stamp}.mkv"


def kill_orphan_ffmpeg_processes() -> int:
    """Kill ffmpeg.exe processes that almost certainly belong to a prior crash.

    "Orphan" heuristic: process basename == ffmpeg.exe AND (
        the executable resolves to our bundled ffmpeg.exe
        OR the parent process no longer exists
    ).

    We're conservative on purpose — the user may run other ffmpeg.exe instances
    for unrelated work, and we must not kill those.
    """
    our_ffmpeg = None
    try:
        our_ffmpeg = ffmpeg_exe().resolve()
    except FileNotFoundError:
        pass

    killed = 0
    for proc in psutil.process_iter(["name", "pid", "exe", "ppid"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name != "ffmpeg.exe":
                continue

            exe_path_str = proc.info.get("exe")
            is_our_bundled = False
            if exe_path_str and our_ffmpeg is not None:
                try:
                    is_our_bundled = Path(exe_path_str).resolve() == our_ffmpeg
                except OSError:
                    is_our_bundled = False

            parent_alive = False
            ppid = proc.info.get("ppid")
            if ppid:
                try:
                    parent_alive = psutil.pid_exists(int(ppid)) and psutil.Process(int(ppid)).is_running()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    parent_alive = False

            if is_our_bundled or not parent_alive:
                logger.info(
                    "Killing orphan ffmpeg pid=%d exe=%s parent_alive=%s bundled=%s",
                    proc.info["pid"], exe_path_str, parent_alive, is_our_bundled,
                )
                proc.kill()
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return killed
