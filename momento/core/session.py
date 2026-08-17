"""Glue layer that turns game-start/stop events into recording start/stop calls.

Also takes care of one bit of crash recovery: scanning for orphaned ffmpeg.exe
processes that our app spawned in a previous (crashed) run and killing them
before we start a new recording session.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psutil

from momento.config import Config
from momento.core.bookmarks import BookmarkStore
from momento.core.encoder import is_output_write_error
from momento.core.game_watcher import ActiveGame, GameWatcher, _game_still_running
from momento.core.recorder import Recorder, RecordingFinalizeError, RecordingStartCancelled
from momento.core.video_capture import wait_for_window
from momento.util.ffmpeg_path import ffmpeg_exe
from momento.util.format import format_bytes, free_bytes_for
from momento.util.screen import primary_refresh_rate

logger = logging.getLogger(__name__)

# Status pushed to UI listeners. Strings keep coupling loose.
SessionStatusCallback = Callable[[str, "ActiveGame | None"], None]

STATUS_IDLE = "idle"
STATUS_RECORDING = "recording"

# Failure reasons the tray can surface as a warning toast.
FAILURE_NO_WINDOW = "no_window"
FAILURE_OUTPUT_FOLDER = "output_folder"
FAILURE_GENERIC = "generic"
# A recording that ran but didn't finalise cleanly (flush/close error, hung
# worker). Distinct from the start failures above: a file exists and is
# recoverable, so the tray phrases it as "may be incomplete", not "couldn't
# record", and must cancel the would-be "saved" toast.
FAILURE_FINALIZE = "finalize"
# A recording that started fine but couldn't capture a configured audio device
# (mic and/or system audio). The recording continues without it; this warns the
# user so audio they expected (their voice, game sound) is never silently lost.
FAILURE_AUDIO = "audio"
# Hardware encode was unavailable and the recording is using the CPU floor.
FAILURE_SOFTWARE_ENCODER = "software_encoder"
# WGC ended while the captured HWND remained valid. The current MKV can close
# cleanly, but the recording was interrupted and the game needs a fresh session.
FAILURE_VIDEO_CAPTURE = "video_capture"
FAILURE_VIDEO_PERFORMANCE = "video_performance"
FAILURE_LOW_DISK = "low_disk"

SessionFailureCallback = Callable[[str, "ActiveGame", str], None]
# (reason, game, detail-message-for-the-toast-subtitle)

# Fires when the bookmark hotkey lands a fresh bookmark (after dedup).
# (game, elapsed_seconds)
SessionBookmarkCallback = Callable[["ActiveGame", float], None]
SessionRecordingFinishedCallback = Callable[[Path], None]

# Window-discovery budget for a freshly-launched game. The wait runs on its
# own thread and keeps retrying while the game process is alive, so the cap
# only needs to cover the pathological cold start (patch-day shader
# compilation, addon scans — WoW has taken >10s in the wild). Sliced so
# pause / quit / process-exit abort the wait promptly.
_WINDOW_WAIT_TOTAL_S = 180.0
_WINDOW_WAIT_SLICE_S = 5.0
_START_RETRY_COOLDOWN_S = 15.0
_WINDOW_RECREATE_RETRY_COOLDOWN_S = 1.0
_DISK_GUARD_INTERVAL_S = 10.0
_HARD_MIN_FREE_BYTES = 1 * 1024 ** 3
_STARTER_JOIN_TIMEOUT_S = 12.0


@dataclass(slots=True)
class UpdateQuiescence:
    """Lease that blocks new recordings while an update handoff is prepared."""

    _session: "SessionManager"
    _resume_monitoring: bool
    _committed: bool = False
    _released: bool = False

    def commit(self) -> None:
        self._committed = True

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._committed:
            return
        self._session._release_update_quiescence(
            resume_monitoring=self._resume_monitoring
        )

    def __enter__(self) -> "UpdateQuiescence":
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


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
        # A game the user toggled Auto-record OFF must not be recorded by the
        # fullscreen fallback either (it only leaves the known set otherwise).
        self._watcher.update_fullscreen_skip(config.disabled_games)
        # Finalise promptly when the game WINDOW closes, rather than waiting
        # for the watcher to notice the process exit (some games linger ~30s).
        self._recorder.on_window_closed = self._on_recording_window_closed
        self._recorder.on_audio_dropped = self._on_audio_dropped_mid_recording
        self._recorder.on_encoder_failed = self._on_encoder_failed_mid_recording
        self._recorder.on_video_capture_failed = self._on_video_capture_failed_mid_recording
        self._recorder.on_video_degraded = self._on_video_degraded_mid_recording

        self._on_status_change = on_status_change
        self._on_failure = on_failure
        self._on_bookmark: SessionBookmarkCallback | None = None
        self._on_recording_finished: SessionRecordingFinishedCallback | None = None
        self._lock = threading.RLock()
        # Read the primary monitor's refresh rate once on construction —
        # SessionManager is built on the Qt thread (from __main__.py) so
        # this is the safe place to call primary_refresh_rate(). The
        # watcher fires _on_game_start on a background thread where we
        # can't talk to Qt; we use this cached value instead.
        self._detected_refresh_rate: int = primary_refresh_rate(default=60)
        logger.info("Detected primary monitor refresh rate: %d Hz", self._detected_refresh_rate)
        self._current_output: Path | None = None
        self._pending_output: Path | None = None
        self._current_game: ActiveGame | None = None
        self._bookmarks: BookmarkStore | None = None
        self._start_pending = False
        self._starter_thread: threading.Thread | None = None
        self._pending_game: ActiveGame | None = None
        self._deferred_game: ActiveGame | None = None
        self._finalizing = False
        self._update_quiescing = False
        self._disk_guard_stop: threading.Event | None = None

    # ------------------------------------------------------------------ API
    @property
    def is_recording(self) -> bool:
        return self._recorder.is_recording

    @property
    def current_game(self) -> ActiveGame | None:
        with self._lock:
            return self._current_game

    @property
    def current_output(self) -> Path | None:
        with self._lock:
            return self._current_output or self._pending_output

    def start(self) -> None:
        """Start the session loop. Kills orphan ffmpeg processes first."""
        killed = kill_orphan_ffmpeg_processes()
        if killed:
            logger.warning("Killed %d orphan ffmpeg.exe process(es) from prior runs", killed)
        self._watcher.start()
        self._emit_status(STATUS_IDLE, None)

    def pause_monitoring(self) -> bool:
        """Stop the watcher without affecting an in-flight recording.

        Used by the tray's "Pause monitoring" menu item — keeps Momento
        running in the tray but prevents new auto-recordings from starting.
        """
        watcher_stopped = False
        try:
            stop_result = self._watcher.stop()
            watcher_stopped = (
                not self._watcher.is_running
                if stop_result is None
                else bool(stop_result)
            )
        except Exception:
            logger.exception("Error pausing watcher")
        cancel_start = getattr(self._recorder, "cancel_start", None)
        if callable(cancel_start):
            try:
                cancel_start()
            except Exception:
                logger.exception("Error cancelling pending recorder start")
        self._join_starter(context="pause")
        with self._lock:
            start_pending = self._start_pending
        return (
            watcher_stopped
            and not self._watcher.is_running
            and not start_pending
        )

    def resume_monitoring(self) -> None:
        """Re-start the watcher after :meth:`pause_monitoring`."""
        try:
            self._watcher.start()
        except Exception:
            logger.exception("Error resuming watcher")

    def stop_current_recording(self) -> None:
        """Manually stop the in-flight recording (if any). Watcher keeps
        running so the next game launch can start a fresh one."""
        if not self._recorder_busy():
            return
        self._finalize(self.current_game, source="manual stop")

    @property
    def is_monitoring(self) -> bool:
        return self._watcher.is_running

    def wait_initial_scan(self, timeout: float | None = None) -> bool:
        wait = getattr(self._watcher, "wait_initial_scan", None)
        return bool(wait(timeout)) if callable(wait) else True

    def acquire_update_quiescence(self) -> UpdateQuiescence | None:
        """Atomically stop new recording work only when already fully idle."""
        with self._lock:
            if (
                self._update_quiescing
                or self._start_pending
                or self._finalizing
                or self._recorder_busy()
            ):
                return None
            self._update_quiescing = True
            self._deferred_game = None
            was_monitoring = bool(self._watcher.is_running)

        try:
            stop_result = self._watcher.stop()
            watcher_stopped = (
                not self._watcher.is_running
                if stop_result is None
                else bool(stop_result)
            )
        except Exception:
            logger.exception("Could not stop game monitoring for update")
            self._release_update_quiescence(resume_monitoring=was_monitoring)
            return None

        if not watcher_stopped:
            logger.warning(
                "Update handoff denied because game monitoring did not stop in time"
            )
            self._release_update_quiescence(resume_monitoring=was_monitoring)
            return None

        with self._lock:
            idle = (
                not self._watcher.is_running
                and not self._start_pending
                and not self._finalizing
                and not self._recorder_busy()
            )
        if not idle:
            logger.warning("Update handoff lost the recording-idle race")
            self._release_update_quiescence(resume_monitoring=was_monitoring)
            return None
        return UpdateQuiescence(self, was_monitoring)

    def _release_update_quiescence(self, *, resume_monitoring: bool) -> None:
        with self._lock:
            self._update_quiescing = False
        if resume_monitoring:
            self.resume_monitoring()

    def shutdown(self) -> None:
        """Stop the watcher, then any in-flight recording. Idempotent."""
        try:
            self._watcher.stop()
        except Exception:
            logger.exception("Error stopping watcher")

        self._stop_disk_guard()

        finalised = False
        if self._recorder_busy():
            logger.info("Shutdown: stopping in-flight recording")
            try:
                self._finalize(self.current_game, source="shutdown")
                finalised = True
            except Exception:
                logger.exception("Error stopping recorder during shutdown")

        self._join_starter(context="shutdown")

        if not finalised:
            with self._lock:
                self._current_game = None
                self._current_output = None
                self._pending_output = None
                self._bookmarks = None
            self._emit_status(STATUS_IDLE, None)

    def reload_config(self, config: Config) -> None:
        """Apply config changes. Already-running recording keeps its settings."""
        self.config = config
        self._watcher.update_known_games(_active_known_games(config))
        self._watcher.set_record_any_fullscreen(config.record_any_fullscreen)
        self._watcher.update_fullscreen_skip(config.disabled_games)

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

    def set_recording_finished_callback(
        self, cb: SessionRecordingFinishedCallback | None
    ) -> None:
        """Install a callback for a recording file that reached disk."""
        self._on_recording_finished = cb

    def add_bookmark(self) -> bool:
        """Record a bookmark at the current recording position.

        Returns True if added, False if no active recording or if the
        timestamp was deduped against a near-twin (< 0.5s).
        """
        elapsed = self._recorder.current_position()
        if elapsed is None:
            return False
        with self._lock:
            store = self._bookmarks
            game = self._current_game
        if store is None:
            return False
        added = store.add(elapsed)
        if added:
            logger.info("Bookmark @ %.2fs", elapsed)
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
        # Audio is optional for auto-recording. Missing or broken devices are
        # handled inside Recorder.start() by closing that encoder input and
        # keeping the game capture alive; never skip a recording just because
        # Windows renamed, unplugged, or glitched an audio endpoint.

        with self._lock:
            if self._update_quiescing:
                logger.info("Ignoring game start (%s): update handoff is active", game.exe_name)
                return
            if self._start_pending:
                if self._pending_game != game:
                    self._deferred_game = game
                    logger.info(
                        "Deferring game start (%s): another start is pending",
                        game.exe_name,
                    )
                else:
                    logger.info("Ignoring duplicate pending start (%s)", game.exe_name)
                return
            if self._finalizing:
                self._deferred_game = game
                logger.info(
                    "Deferring game start (%s): finalisation is in progress",
                    game.exe_name,
                )
                return
            if self._recorder_busy():
                logger.info("Ignoring game start (%s): already recording", game.exe_name)
                return
            self._start_pending = True
            self._pending_game = game

        # Window discovery + recorder start run on their own thread. The
        # watcher must keep ticking while we wait (game-exit detection,
        # pause/quit responsiveness), and the wait itself must be allowed to
        # take MINUTES: a cold start on a patch day (shader compilation,
        # addon scans) can hold the window back well past any short timeout.
        # The old single 10s wait here was terminal — the watcher pins the
        # game as active until its process exits, so one slow launch meant a
        # "couldn't find a window" toast and a whole session never recorded.
        starter = threading.Thread(
            target=self._wait_for_window_and_start,
            args=(game,),
            name="RecordingStarter",
            daemon=True,
        )
        with self._lock:
            self._starter_thread = starter
        starter.start()

    def _wait_for_window_and_start(self, game: ActiveGame) -> None:
        """Keep looking for the game's main window while its process is alive,
        then start recording. Runs on the RecordingStarter thread.

        Aborts silently when the game exits first (nothing recordable — the
        watcher's process-exit path handles state) or when monitoring is
        paused/stopped mid-wait (covers tray Pause and app shutdown). The
        failure toast fires only when the cap expires with the game still
        running and windowless.
        """
        try:
            deadline = time.monotonic() + _WINDOW_WAIT_TOTAL_S
            waited = 0.0
            while True:
                if self._recorder_busy():
                    logger.info(
                        "Window wait for %s abandoned: another recording is active",
                        game.exe_name,
                    )
                    return
                if not self._watcher.is_running:
                    logger.info(
                        "Window wait for %s abandoned: monitoring stopped", game.exe_name
                    )
                    return
                hwnd = wait_for_window(game.pid, timeout=_WINDOW_WAIT_SLICE_S)
                # Re-check AFTER the (up-to-5s, non-cancellable) wait slice: the
                # user may have paused monitoring or quit while it was blocking.
                # Without this, a window appearing in that final slice would
                # start a recording the user just paused to prevent.
                if not self._watcher.is_running:
                    logger.info(
                        "Window wait for %s abandoned: monitoring stopped during wait",
                        game.exe_name,
                    )
                    return
                if hwnd is not None:
                    # wait_for_window is keyed by PID. Windows may recycle that
                    # PID during a long cold-start wait, so verify the detected
                    # process identity before accepting the returned HWND.
                    if not _game_still_running(game):
                        logger.info(
                            "Window wait for %s abandoned: detected process exited "
                            "or its PID was reused (pid=%d)",
                            game.exe_name,
                            game.pid,
                        )
                        return
                    break
                waited += _WINDOW_WAIT_SLICE_S
                if not _game_still_running(game):
                    logger.info(
                        "Window wait for %s abandoned: process exited before showing "
                        "a window (pid=%d)", game.exe_name, game.pid,
                    )
                    return
                if time.monotonic() >= deadline:
                    self._emit_failure(
                        FAILURE_NO_WINDOW, game,
                        f"Couldn't find a window for {game.exe_name} within "
                        f"{int(_WINDOW_WAIT_TOTAL_S)} seconds.",
                    )
                    self._release_game_for_retry(game)
                    return
                logger.info(
                    "Still waiting for a window for %s (pid=%d, ~%.0fs elapsed)",
                    game.exe_name, game.pid, waited,
                )
            self._start_recording(game, hwnd)
        finally:
            deferred: ActiveGame | None = None
            with self._lock:
                self._start_pending = False
                self._pending_game = None
                if self._starter_thread is threading.current_thread():
                    self._starter_thread = None
                deferred = self._take_deferred_start_if_idle_locked()
            if deferred is not None:
                self._on_game_start(deferred)

    def _start_recording(self, game: ActiveGame, hwnd: int) -> None:
        """Spin up the recorder for ``game``'s window + publish session state."""
        with self._lock:
            if self._update_quiescing:
                logger.info("Ignoring recorder start for %s: update handoff is active", game.exe_name)
                return
            if self._finalizing:
                self._deferred_game = game
                logger.info(
                    "Deferring recorder start for %s until finalisation completes",
                    game.exe_name,
                )
                return
        c = self.config
        slug = _slugify_game(game.exe_name)
        output_path = _build_output_path(c.output_folder, slug)
        framerate = self._detected_refresh_rate if c.framerate_auto else c.framerate
        with self._lock:
            self._pending_output = output_path
        try:
            self._recorder.start(
                output_path=output_path,
                hwnd=hwnd,
                mic_device=c.mic_device,
                audio_device=c.system_audio_device,
                mic_volume_pct=c.mic_volume_pct,
                audio_volume_pct=c.system_volume_pct,
                framerate=framerate,
                audio_offset_ms=c.audio_offset_ms,
                game_slug=slug,
                target_resolution=c.target_resolution,
                quality_preset=c.quality_preset,
                custom_bitrate_kbps=c.custom_bitrate_kbps,
            )
        except RecordingStartCancelled:
            logger.info("Recording start for %s was cancelled before publish", game.exe_name)
            self._clear_pending_output(output_path)
            with self._lock:
                if self._finalizing:
                    self._deferred_game = game
            return
        except Exception as e:
            msg = str(e) or "Unknown error."
            lowered = msg.casefold()
            output_failure = (
                is_output_write_error(e)
                or "writable" in lowered
                or "output folder" in lowered
            )
            reason = FAILURE_OUTPUT_FOLDER if output_failure else FAILURE_GENERIC
            if output_failure:
                logger.error("Recorder output start failed for %s: %s", game.exe_name, msg)
            else:
                logger.exception("Failed to start recorder for %s", game.exe_name)
            self._clear_pending_output(output_path)
            self._emit_failure(reason, game, msg)
            if not output_failure:
                self._release_game_for_retry(game)
            return

        if not _game_still_running(game):
            logger.info(
                "Game %s exited while recording was starting; finalising immediately",
                game.exe_name,
            )
            with self._lock:
                self._current_game = game
                self._current_output = output_path
                self._pending_output = None
                self._bookmarks = BookmarkStore(output_path)
            self._finalize(game, source="process exit during start")
            return
        stop_unpublished = False
        published = False
        with self._lock:
            if self._finalizing or not self._recorder.is_recording:
                logger.info(
                    "Recording for %s stopped before session state was published",
                    game.exe_name,
                )
                stop_unpublished = self._recorder.is_recording
                self._pending_output = None
                if self._finalizing:
                    self._deferred_game = game
            else:
                self._current_game = game
                self._current_output = output_path
                self._pending_output = None
                self._bookmarks = BookmarkStore(output_path)
                self._emit_status(STATUS_RECORDING, game)
                published = True
        if stop_unpublished:
            try:
                self._recorder.stop()
            except Exception:
                logger.exception("Error stopping unpublished recording for %s", game.exe_name)
            return
        if not published:
            return
        # Recording is live; warn (outside the lock) if a configured audio
        # device couldn't be captured so the loss is never silent.
        self._warn_if_audio_dropped(game)
        self._warn_if_software_encoder(game)
        self._start_disk_guard(game, output_path)

    def _warn_if_audio_dropped(self, game: ActiveGame) -> None:
        rec = self._recorder
        dropped = []
        if getattr(rec, "mic_dropped", False):
            dropped.append("microphone")
        if getattr(rec, "sys_dropped", False):
            dropped.append("system audio")
        if not dropped:
            return
        detail = (
            f"Couldn't capture your {' and '.join(dropped)}. The recording is "
            "running without it; pick a working device in Settings > Audio."
        )
        self._emit_failure(FAILURE_AUDIO, game, detail)

    def _on_audio_dropped_mid_recording(self, name: str) -> None:
        """A configured audio device died part-way through a recording (e.g.
        unplugged). Warn — the rest of the clip has no `name`, but the loss must
        never be silent. Fired from the capture thread; _emit_failure marshals
        to the GUI thread via the failure signal."""
        # Snapshot the game under the lock (this runs on the capture thread,
        # racing the finalize path that nulls _current_game under the same lock)
        # so the warning is attributed to the right game, not a torn read.
        with self._lock:
            game = self._current_game
        detail = (
            f"Your {name} stopped during recording — the rest of this clip has "
            f"no {name}. Check the device in Settings > Audio."
        )
        self._emit_failure(FAILURE_AUDIO, game, detail)

    def _warn_if_software_encoder(self, game: ActiveGame) -> None:
        if getattr(self._recorder, "active_video_codec", None) != "libx264":
            return
        self._emit_failure(
            FAILURE_SOFTWARE_ENCODER,
            game,
            "Hardware encoding was unavailable, so this recording is using the CPU. "
            "Lower the resolution or frame rate if the game stutters or frames drop.",
        )

    def _on_encoder_failed_mid_recording(self, exc: Exception) -> None:
        """Finalize promptly when a live encoder worker exits unexpectedly."""
        with self._lock:
            game = self._current_game
            already_finalizing = self._finalizing
        if already_finalizing or not self._recorder_busy():
            return
        logger.error(
            "Encoder failed during recording for %s: %s",
            getattr(game, "exe_name", "?"),
            str(exc).strip() or repr(exc),
        )
        output_failure = is_output_write_error(exc)
        if output_failure:
            self._emit_failure(
                FAILURE_OUTPUT_FOLDER,
                game,
                "Recording stopped because Momento could no longer write to the "
                "output drive. Check its free space and connection; this clip may "
                "need Repair.",
            )
        threading.Thread(
            target=self._finalize,
            args=(game,),
            kwargs={"source": "output failure" if output_failure else "encoder failure"},
            name="EncoderFailureFinalize",
            daemon=True,
        ).start()

    def _on_video_degraded_mid_recording(self, drop_rate: float) -> None:
        with self._lock:
            game = self._current_game
        if game is None:
            return
        self._emit_failure(
            FAILURE_VIDEO_PERFORMANCE,
            game,
            f"Momento is dropping about {max(0.0, drop_rate) * 100:.1f}% of video "
            "frames. Lower the recording resolution, frame rate, or quality if "
            "this continues.",
        )

    def _on_video_capture_failed_mid_recording(self, exc: Exception) -> None:
        """Save the current clip and retry when a live WGC session disappears."""
        with self._lock:
            game = self._current_game
            already_finalizing = self._finalizing
        if already_finalizing or not self._recorder_busy():
            return
        logger.error(
            "Video capture failed during recording for %s: %s",
            getattr(game, "exe_name", "?"),
            str(exc).strip() or repr(exc),
        )
        self._emit_failure(
            FAILURE_VIDEO_CAPTURE,
            game,
            "Video capture stopped unexpectedly. Momento saved this clip and will "
            "retry while the game remains open.",
        )
        threading.Thread(
            target=self._finalize,
            args=(game,),
            kwargs={"source": "capture failure"},
            name="CaptureFailureFinalize",
            daemon=True,
        ).start()

    def _on_game_stop(self, game: ActiveGame) -> None:
        # Process death (from the watcher). Route through the shared finaliser
        # — which may be a no-op if the window-closed path already wrapped up.
        self._finalize(game, source="process exit")

    def _on_recording_window_closed(self) -> None:
        """The captured game WINDOW was destroyed (recorder/WGC callback).

        Fires the instant the user closes the game — well before the process
        exits for games that linger (WoW ~30s). Finalise now instead of
        waiting for the watcher. Runs on the WGC capture thread, so hand the
        actual stop() (which tears WGC down) off to a separate thread.

        Finalisation releases the still-live process after a short cooldown.
        Normal exits simply disappear during the next window wait; games that
        recreate their HWND are captured again instead of losing the session.
        """
        with self._lock:
            game = self._current_game
        threading.Thread(
            target=self._finalize,
            args=(game,),
            kwargs={"source": "window closed"},
            name="WindowClosedFinalize",
            daemon=True,
        ).start()

    def _finalize(self, game: ActiveGame | None, *, source: str) -> None:
        """Stop + finalise the in-flight recording and reset to idle.

        Safe to call from multiple paths concurrently (window-close vs
        process-exit): one caller claims finalisation, and any concurrent
        caller exits without emitting an early idle/saved transition while the
        winner is still flushing the encoder.
        """
        with self._lock:
            if self._finalizing:
                logger.info("Ignoring %s finalise request: finalise already in progress", source)
                return
            busy = self._recorder_busy()
            state_game = self._current_game
            if game is None:
                game = state_game
            if not busy and state_game is None:
                return
            self._finalizing = True

        self._stop_disk_guard()

        name = game.exe_name if game is not None else "?"
        final: Path | None = None
        finalize_error: RecordingFinalizeError | None = None
        try:
            try:
                final = self._recorder.stop()
            except RecordingFinalizeError as e:
                # Recording ran but didn't finalise cleanly. Only the caller
                # that actually owned the stop reaches here (stop() is
                # lock-guarded; the loser gets None), so this is authoritative.
                finalize_error = e
                logger.error("Recording finalize failed (%s) for %s: %s", source, name, e)
            except Exception:
                logger.exception("Error stopping recorder (%s) for %s", source, name)
            with self._lock:
                self._current_game = None
                self._current_output = None
                self._pending_output = None
                self._bookmarks = None
            # Emit the dirty-finalize warning BEFORE the idle status. The tray
            # derives the "Recording saved" toast from the recording->idle
            # transition; emitting the failure first lets it cancel that toast so
            # we never claim a clean save for an incomplete file.
            #
            # Fire even when `game` is None: if the window closed during the
            # brief publish gap (recorder running but _current_game not yet set),
            # we still owned a real stop() that finalised dirty — the early
            # `not busy and state_game is None` guard already filtered no-op
            # finalises. Without this, that narrow window silently swallowed the
            # warning and a clean "saved" toast claimed an incomplete file. The
            # tray renders a None game as "Momento".
            if finalize_error is not None and source != "output failure":
                self._emit_failure(
                    FAILURE_FINALIZE, game,
                    "The recording may be incomplete. Momento will try to repair it "
                    "automatically; if it won't play, right-click it and choose Repair.",
                )
            finished = final or (finalize_error.path if finalize_error is not None else None)
            if finished is not None:
                self._emit_recording_finished(finished)
            if final is not None:
                logger.info("Recording for %s finalised (%s)", name, source)
            self._emit_status(STATUS_IDLE, None)
            if final is not None:
                try:
                    from momento.core.storage_cleanup import enforce_storage_limit
                    enforce_storage_limit(self.config.output_folder, self.config.max_storage_gb)
                except Exception:
                    logger.exception("Storage cleanup after recording stop failed")
        finally:
            deferred: ActiveGame | None = None
            with self._lock:
                self._finalizing = False
                deferred = self._take_deferred_start_if_idle_locked()
            if game is not None:
                if source in {"encoder failure", "capture failure"}:
                    self._release_game_for_retry(game)
                elif source == "window closed":
                    self._release_game_for_retry(
                        game, retry_after_s=_WINDOW_RECREATE_RETRY_COOLDOWN_S
                    )
            if deferred is not None:
                self._on_game_start(deferred)

    def _take_deferred_start_if_idle_locked(self) -> ActiveGame | None:
        """Claim a deferred watcher event once no start/finalise owns the recorder."""
        if (
            self._deferred_game is None
            or self._update_quiescing
            or self._start_pending
            or self._finalizing
            or self._recorder_busy()
        ):
            return None
        game = self._deferred_game
        self._deferred_game = None
        return game

    def _emit_failure(self, reason: str, game: ActiveGame, detail: str) -> None:
        # Always log — the toast is transient and respects mute settings, so
        # without this line a failure can leave zero trace in momento.log
        # (which is exactly what made the 2026-06-10 no-window incident
        # diagnosable only from the user's screenshot).
        logger.warning(
            "Recording failure (%s) for %s: %s",
            reason, getattr(game, "exe_name", "?"), detail,
        )
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

    def _emit_recording_finished(self, path: Path) -> None:
        cb = self._on_recording_finished
        if cb is None:
            return
        try:
            cb(path)
        except Exception:
            logger.exception("Recording-finished callback raised")

    def _recorder_busy(self) -> bool:
        busy = getattr(self._recorder, "is_busy", None)
        if busy is not None:
            if callable(busy):
                return bool(busy())
            return bool(busy)
        return bool(self._recorder.is_recording)

    def _clear_pending_output(self, output_path: Path) -> None:
        with self._lock:
            if self._pending_output == output_path:
                self._pending_output = None

    def _join_starter(self, *, context: str) -> None:
        with self._lock:
            starter = self._starter_thread
        if starter is None or starter is threading.current_thread():
            return
        starter.join(timeout=_STARTER_JOIN_TIMEOUT_S)
        if starter.is_alive():
            logger.error(
                "Recording startup did not unwind within %.0fs during %s",
                _STARTER_JOIN_TIMEOUT_S,
                context,
            )

    def _start_disk_guard(self, game: ActiveGame, output_path: Path) -> None:
        """Stop a live recording before its destination volume is exhausted."""
        self._stop_disk_guard()
        stop_event = threading.Event()
        with self._lock:
            self._disk_guard_stop = stop_event
        threading.Thread(
            target=self._disk_guard_worker,
            args=(game, Path(output_path), stop_event),
            name="RecordingDiskGuard",
            daemon=True,
        ).start()

    def _stop_disk_guard(self) -> None:
        with self._lock:
            stop_event = self._disk_guard_stop
            self._disk_guard_stop = None
        if stop_event is not None:
            stop_event.set()

    def _disk_guard_worker(
        self,
        game: ActiveGame,
        output_path: Path,
        stop_event: threading.Event,
    ) -> None:
        configured_gb = max(0, int(self.config.low_disk_warning_gb))
        threshold = max(
            _HARD_MIN_FREE_BYTES,
            configured_gb * 1024 ** 3 if configured_gb else 0,
        )
        while not stop_event.is_set():
            free = free_bytes_for(output_path.parent)
            if free is not None and free <= threshold:
                with self._lock:
                    owns_guard = self._disk_guard_stop is stop_event
                    owns_recording = self._current_game == game
                if owns_guard and owns_recording and self._recorder_busy():
                    self._emit_failure(
                        FAILURE_LOW_DISK,
                        game,
                        f"Recording stopped with {format_bytes(free)} free so the "
                        "drive does not fill completely. Free space or choose a "
                        "different folder before recording again.",
                    )
                    self._finalize(game, source="low disk")
                return
            if stop_event.wait(_DISK_GUARD_INTERVAL_S):
                return

    def _release_game_for_retry(
        self,
        game: ActiveGame,
        *,
        retry_after_s: float = _START_RETRY_COOLDOWN_S,
    ) -> None:
        release = getattr(self._watcher, "release_active_for_retry", None)
        if release is None:
            return
        try:
            release(game, retry_after_s=retry_after_s)
        except TypeError:
            release(game)
        except Exception:
            logger.exception("Could not release active game for retry")


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
    """Kill abandoned instances of Momento's bundled ``ffmpeg.exe`` only."""
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

            parent_gone = False
            ppid = proc.info.get("ppid")
            if not ppid:
                parent_gone = True
            elif not psutil.pid_exists(int(ppid)):
                parent_gone = True
            else:
                try:
                    parent_gone = not psutil.Process(int(ppid)).is_running()
                except psutil.NoSuchProcess:
                    parent_gone = True
                except psutil.AccessDenied:
                    # AccessDenied means the parent may still be alive. Only a
                    # confirmed missing process is safe to classify as orphaned.
                    parent_gone = False

            if is_our_bundled and parent_gone:
                logger.info(
                    "Killing abandoned Momento ffmpeg pid=%d exe=%s",
                    proc.info["pid"], exe_path_str,
                )
                proc.kill()
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return killed
