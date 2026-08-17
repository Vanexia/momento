"""Deterministic regressions for capture and session safety boundaries.

Run:
    C:\\dev\\Momento\\.venv\\Scripts\\python.exe tests\\smoke_capture_safety.py
"""

from __future__ import annotations

import dataclasses
import errno
import logging
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

import momento.core.game_watcher as gw  # noqa: E402
import momento.core.encoder as encoder_mod  # noqa: E402
import momento.core.recorder as recorder_mod  # noqa: E402
import momento.core.session as sess  # noqa: E402
import momento.core.video_capture as capture  # noqa: E402
from momento.config import Config  # noqa: E402
from momento.core import encoders  # noqa: E402
from momento.core.encoder import InProcessEncoder  # noqa: E402
from momento.core.game_watcher import ActiveGame  # noqa: E402
from momento.core.recorder import RecordingFinalizeError  # noqa: E402


_results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    _results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} - {name}")


def test_explicit_empty_game_list_stays_empty() -> None:
    real_loader = gw._load_known_games
    gw._load_known_games = lambda: ["fallback.exe"]
    try:
        watcher = gw.GameWatcher(known_games=[])
        check("empty game list: bundled defaults are not restored", watcher._known == set())
    finally:
        gw._load_known_games = real_loader


class _FakeProcess:
    def __init__(self, pid: int, exe: Path, ppid: int) -> None:
        self.info = {
            "name": "ffmpeg.exe",
            "pid": pid,
            "exe": str(exe),
            "ppid": ppid,
        }
        self.killed = False

    def kill(self) -> None:
        self.killed = True


class _FakeParent:
    def __init__(self, running: bool) -> None:
        self._running = running

    def is_running(self) -> bool:
        return self._running


def test_ffmpeg_cleanup_requires_bundled_orphan() -> None:
    bundled = Path("C:/dev/Momento/resources/ffmpeg/ffmpeg.exe")
    unrelated_orphan = _FakeProcess(101, Path("C:/Tools/ffmpeg.exe"), 901)
    bundled_live = _FakeProcess(102, bundled, 902)
    bundled_orphan = _FakeProcess(103, bundled, 903)
    processes = [unrelated_orphan, bundled_live, bundled_orphan]

    real_ffmpeg_exe = sess.ffmpeg_exe
    real_iter = sess.psutil.process_iter
    real_pid_exists = sess.psutil.pid_exists
    real_process = sess.psutil.Process
    sess.ffmpeg_exe = lambda: bundled
    sess.psutil.process_iter = lambda _attrs: iter(processes)
    sess.psutil.pid_exists = lambda pid: pid == 902
    sess.psutil.Process = lambda pid: _FakeParent(running=pid == 902)
    try:
        killed = sess.kill_orphan_ffmpeg_processes()
    finally:
        sess.ffmpeg_exe = real_ffmpeg_exe
        sess.psutil.process_iter = real_iter
        sess.psutil.pid_exists = real_pid_exists
        sess.psutil.Process = real_process

    check("ffmpeg cleanup: kills one confirmed bundled orphan", killed == 1)
    check("ffmpeg cleanup: bundled orphan is killed", bundled_orphan.killed)
    check("ffmpeg cleanup: unrelated orphan is preserved", not unrelated_orphan.killed)
    check("ffmpeg cleanup: bundled child with live parent is preserved", not bundled_live.killed)


class _Frame:
    def __init__(self, pixels: np.ndarray) -> None:
        self.frame_buffer = pixels
        self.height, self.width = pixels.shape[:2]


class _Control:
    def stop(self) -> None:
        pass


def test_wgc_frame_snapshot_owns_its_pixels() -> None:
    pixels = np.zeros((6, 8, 4), dtype=np.uint8)
    pixels[..., 3] = 0xFF
    real_is_window = capture.is_window
    capture.is_window = lambda _hwnd: True
    try:
        streamer = capture.WindowVideoStreamer(hwnd=1, framerate=60)
    finally:
        capture.is_window = real_is_window
    streamer._frame_size = (8, 6)

    streamer._on_frame(_Frame(pixels), _Control())
    snapshot = streamer._latest_frame
    pixels[0, 0] = [10, 20, 30, 40]

    check("WGC snapshot: retained frame is a distinct array", snapshot is not pixels)
    check(
        "WGC snapshot: native-buffer mutation cannot change retained pixels",
        snapshot is not None and snapshot[0, 0].tolist() == [0, 0, 0, 255],
    )


class _ClosingCapture:
    def __init__(self, **_kwargs) -> None:
        self._events: dict[str, object] = {}

    def event(self, fn):
        self._events[fn.__name__] = fn
        return fn

    def start_free_threaded(self) -> None:
        pixels = np.zeros((6, 8, 4), dtype=np.uint8)
        self._events["on_frame_arrived"](_Frame(pixels), _Control())
        self._events["on_closed"]()


class _StaticCapture(_ClosingCapture):
    def start_free_threaded(self) -> None:
        first = np.zeros((7, 9, 4), dtype=np.uint8)
        first[..., 3] = 0xFF
        self._events["on_frame_arrived"](_Frame(first), _Control())
        second = first.copy()
        second[0, 0] = [1, 2, 3, 255]
        self._events["on_frame_arrived"](_Frame(second), _Control())
        second[0, 0] = [10, 20, 30, 40]


class _CaptureThreadControl:
    def __init__(self) -> None:
        self.finished = threading.Event()

    def wait(self) -> None:
        self.finished.wait(timeout=2.0)

    def stop(self) -> None:
        self.finished.set()


class _UnexpectedStopCapture(_ClosingCapture):
    last_kwargs: dict[str, object] = {}
    control: _CaptureThreadControl | None = None

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        type(self).last_kwargs = kwargs
        type(self).control = _CaptureThreadControl()

    def start_free_threaded(self) -> _CaptureThreadControl:
        pixels = np.zeros((6, 8, 4), dtype=np.uint8)
        pixels[..., 3] = 0xFF
        self._events["on_frame_arrived"](_Frame(pixels), _Control())
        assert type(self).control is not None
        return type(self).control


def test_wgc_static_startup_frame_is_retained() -> None:
    real_capture = capture.WindowsCapture
    real_is_window = capture.is_window
    real_settle_ms = capture._SIZE_SETTLE_MS
    capture.WindowsCapture = _StaticCapture
    capture.is_window = lambda _hwnd: True
    capture._SIZE_SETTLE_MS = 0
    try:
        streamer = capture.WindowVideoStreamer(hwnd=1, framerate=60)
        size = streamer.prepare()
    finally:
        capture.WindowsCapture = real_capture
        capture.is_window = real_is_window
        capture._SIZE_SETTLE_MS = real_settle_ms

    check("WGC static startup: even capture dimensions are locked", size == (8, 6))
    check(
        "WGC static startup: setup frame seeds the sender",
        streamer._latest_frame is not None and streamer._latest_frame.shape == (6, 8, 4),
    )
    check(
        "WGC static startup: newest same-size pixels own their storage",
        streamer._latest_frame is not None
        and streamer._latest_frame[0, 0].tolist() == [1, 2, 3, 255],
    )
    streamer.stop()


def test_wgc_close_during_settle_fails_startup() -> None:
    real_capture = capture.WindowsCapture
    real_is_window = capture.is_window
    capture.WindowsCapture = _ClosingCapture
    capture.is_window = lambda _hwnd: True
    try:
        streamer = capture.WindowVideoStreamer(hwnd=1, framerate=60)
        raised = False
        try:
            streamer.prepare()
        except RuntimeError:
            raised = True
    finally:
        capture.WindowsCapture = real_capture
        capture.is_window = real_is_window

    check("WGC startup: close during size settle raises", raised)
    check("WGC startup: closed capture does not lock a frame size", streamer._frame_size is None)


def test_wgc_close_after_prepare_cancels_recorder_publish() -> None:
    """A close in the prepare->publish gap must poison the pending start."""
    recorder = recorder_mod.Recorder()
    recorder._starting = True
    generation = recorder._audio_generation

    recorder._on_video_window_closed(generation)

    check(
        "WGC startup: a window close before publish records a terminal failure",
        recorder._encoder_failed is not None,
    )
    check(
        "WGC startup: close-before-publish cannot look like an external stop",
        not recorder._stop_requested,
    )


def test_stopped_wgc_refuses_to_start_sender() -> None:
    real_is_window = capture.is_window
    capture.is_window = lambda _hwnd: True
    try:
        streamer = capture.WindowVideoStreamer(hwnd=1, framerate=60)
    finally:
        capture.is_window = real_is_window
    streamer._frame_size = (8, 6)
    streamer._stop_event.set()
    raised = False
    try:
        streamer.start_sending(lambda *_args: None)
    except RuntimeError:
        raised = True
    check("WGC startup: a terminal capture cannot start frame delivery", raised)
    check("WGC startup: no sender thread is published after terminal close", not streamer._started)


def test_wgc_native_stop_is_reported_and_uses_milliseconds() -> None:
    real_capture = capture.WindowsCapture
    real_is_window = capture.is_window
    real_settle_ms = capture._SIZE_SETTLE_MS
    capture.WindowsCapture = _UnexpectedStopCapture
    capture.is_window = lambda _hwnd: True
    capture._SIZE_SETTLE_MS = 0
    failures: list[Exception] = []
    try:
        streamer = capture.WindowVideoStreamer(hwnd=1, framerate=60)
        streamer.on_capture_failed = failures.append
        streamer.prepare()
        control = _UnexpectedStopCapture.control
        assert control is not None
        control.finished.set()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not failures:
            time.sleep(0.01)
    finally:
        capture.WindowsCapture = real_capture
        capture.is_window = real_is_window
        capture._SIZE_SETTLE_MS = real_settle_ms

    check(
        "WGC interval: windows-capture receives milliseconds",
        _UnexpectedStopCapture.last_kwargs.get("minimum_update_interval") == 16,
    )
    check("WGC recovery: unexpected native stop reaches the owner", len(failures) == 1)
    check("WGC recovery: unexpected native stop halts frame delivery", streamer._stop_event.is_set())
    streamer.stop()


def test_wgc_interval_tracks_requested_high_framerate() -> None:
    real_capture = capture.WindowsCapture
    real_is_window = capture.is_window
    real_settle_ms = capture._SIZE_SETTLE_MS
    capture.WindowsCapture = _UnexpectedStopCapture
    capture.is_window = lambda _hwnd: True
    capture._SIZE_SETTLE_MS = 0
    try:
        streamer = capture.WindowVideoStreamer(hwnd=1, framerate=240)
        streamer.prepare()
        interval = _UnexpectedStopCapture.last_kwargs.get("minimum_update_interval")
        streamer.stop()
    finally:
        capture.WindowsCapture = real_capture
        capture.is_window = real_is_window
        capture._SIZE_SETTLE_MS = real_settle_ms

    check("WGC high FPS: 240 fps requests a 4 ms update interval", interval == 4)


def test_wgc_intentional_stop_is_silent() -> None:
    real_capture = capture.WindowsCapture
    real_is_window = capture.is_window
    real_settle_ms = capture._SIZE_SETTLE_MS
    capture.WindowsCapture = _UnexpectedStopCapture
    capture.is_window = lambda _hwnd: True
    capture._SIZE_SETTLE_MS = 0
    failures: list[Exception] = []
    try:
        streamer = capture.WindowVideoStreamer(hwnd=1, framerate=60)
        streamer.on_capture_failed = failures.append
        streamer.prepare()
        streamer.stop()
        time.sleep(0.02)
    finally:
        capture.WindowsCapture = real_capture
        capture.is_window = real_is_window
        capture._SIZE_SETTLE_MS = real_settle_ms

    check("WGC recovery: intentional stop does not report a failure", failures == [])


def test_sender_deadline_skips_late_slots() -> None:
    interval = 1.0 / 60.0
    previous_deadline = 10.0

    next_deadline = capture._next_sender_deadline(
        previous_deadline,
        interval,
        now=previous_deadline + interval + 0.001,
    )

    check(
        "video pacing: a late wake advances to a future frame slot",
        next_deadline > previous_deadline + interval + 0.001,
    )
    check(
        "video pacing: a late wake skips exactly the elapsed slot",
        abs(next_deadline - (previous_deadline + 2 * interval)) < 1e-9,
    )
    exact_boundary = previous_deadline + 3 * interval
    boundary_deadline = capture._next_sender_deadline(
        previous_deadline,
        interval,
        now=exact_boundary,
    )
    check(
        "video pacing: an exact elapsed-slot boundary still advances",
        boundary_deadline > exact_boundary,
    )
    advanced, missed, lateness = capture._advance_sender_deadline(
        previous_deadline,
        interval,
        now=previous_deadline + 10.5 * interval,
    )
    check("video pacing: every elapsed slot is counted", missed == 10)
    check("video pacing: counted deadline remains in the future", advanced > previous_deadline + 10.5 * interval)
    check("video pacing: lateness is measured from the first missed deadline", lateness > 9 * interval)


def test_sender_pacing_health_reports_hitches() -> None:
    real_is_window = capture.is_window
    capture.is_window = lambda _hwnd: True
    try:
        streamer = capture.WindowVideoStreamer(hwnd=1, framerate=60)
    finally:
        capture.is_window = real_is_window

    streamer._frames_submitted = 9_950
    streamer._scheduler_slots_missed = 50
    streamer._scheduler_late_events = 41
    streamer._scheduler_max_lateness_s = 0.15

    check("video pacing health: scheduled slot total includes misses", streamer.scheduled_slots == 10_000)
    check("video pacing health: miss rate is reported separately", abs(streamer.scheduler_miss_rate - 0.005) < 1e-9)
    check("video pacing health: a 100ms hitch marks pacing degraded", streamer.pacing_degraded)


def test_watcher_stop_timeout_keeps_single_thread() -> None:
    watcher = gw.GameWatcher(known_games=[], poll_interval=0.01)
    entered = threading.Event()
    release = threading.Event()
    thread_ids: list[int] = []

    def blocking_tick() -> None:
        thread_ids.append(threading.get_ident())
        entered.set()
        release.wait(timeout=5.0)

    watcher._tick = blocking_tick
    watcher.start()
    first_thread = watcher._thread
    entered.wait(timeout=2.0)
    watcher.stop()
    watcher.start()
    second_thread = watcher._thread

    check("watcher timeout: original watcher remains owned", second_thread is first_thread)
    check("watcher timeout: restart does not create a second thread", len(set(thread_ids)) == 1)

    watcher._stop_event.set()
    release.set()
    if first_thread is not None:
        first_thread.join(timeout=2.0)
    if second_thread is not None and second_thread is not first_thread:
        second_thread.join(timeout=2.0)


class _SessionWatcher:
    def __init__(self) -> None:
        self.is_running = True
        self.on_game_start = None
        self.on_game_stop = None
        self.released: list[ActiveGame] = []

    def update_fullscreen_skip(self, _exes) -> None:
        pass

    def release_active_for_retry(self, game: ActiveGame, retry_after_s=0.0) -> None:
        self.released.append(game)


class _SessionRecorder:
    def __init__(self) -> None:
        self.start_calls: list[dict] = []
        self._recording = False
        self.on_window_closed = None
        self.on_audio_dropped = None
        self.on_encoder_failed = None
        self.on_video_capture_failed = None
        self.mic_dropped = False
        self.sys_dropped = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def is_busy(self) -> bool:
        return self._recording

    def start(self, **kwargs) -> None:
        self.start_calls.append(kwargs)
        self._recording = True

    def stop(self):
        self._recording = False
        return None

    def current_position(self):
        return None


def _session(tmp: Path) -> tuple[sess.SessionManager, _SessionRecorder]:
    config = dataclasses.replace(Config(), output_folder=tmp)
    recorder = _SessionRecorder()
    manager = sess.SessionManager(config, watcher=_SessionWatcher(), recorder=recorder)
    return manager, recorder


def test_session_does_not_start_during_finalization() -> None:
    with tempfile.TemporaryDirectory(prefix="momento_finalize_guard_") as folder:
        manager, recorder = _session(Path(folder))
        game = ActiveGame("game.exe", 101, None, 1.0)
        manager._finalizing = True

        manager._start_recording(game, hwnd=4242)

        check("session finalizing: recorder start is not called", recorder.start_calls == [])
        check("session finalizing: no unpublished recorder remains live", not recorder.is_recording)


def test_second_game_is_deferred_while_first_start_is_pending() -> None:
    with tempfile.TemporaryDirectory(prefix="momento_deferred_start_") as folder:
        manager, recorder = _session(Path(folder))
        game_a = ActiveGame("game-a.exe", 201, None, 1.0)
        game_b = ActiveGame("game-b.exe", 202, None, 2.0)
        first_wait_entered = threading.Event()
        release_first_wait = threading.Event()

        real_wait = sess.wait_for_window
        real_alive = sess._game_still_running

        def fake_wait(pid: int, timeout=0.0, poll_interval=0.25):
            if pid == game_a.pid:
                first_wait_entered.set()
                release_first_wait.wait(timeout=3.0)
                return None
            return 4242

        sess.wait_for_window = fake_wait
        sess._game_still_running = lambda game: game.pid == game_b.pid
        try:
            manager._on_game_start(game_a)
            entered = first_wait_entered.wait(timeout=2.0)
            manager._on_game_start(game_b)
            release_first_wait.set()

            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not recorder.start_calls:
                time.sleep(0.01)
        finally:
            release_first_wait.set()
            sess.wait_for_window = real_wait
            sess._game_still_running = real_alive

        check("deferred start: first game's wait was active", entered)
        check("deferred start: second game eventually starts", len(recorder.start_calls) == 1)
        check(
            "deferred start: second game's window is recorded",
            bool(recorder.start_calls) and recorder.start_calls[0].get("hwnd") == 4242,
        )


def _bare_encoder(path: Path) -> InProcessEncoder:
    codec = encoders.pick_encoder()
    return InProcessEncoder(
        output_path=path,
        video_width=8,
        video_height=6,
        video_framerate=30,
        video_codec=codec,
        video_options=encoders.quality_options_for(codec, "high", 12_000),
        encoder_pix_fmt=encoders.preferred_pix_fmt_for(codec),
    )


def test_encoder_worker_failure_closes_submission_gate() -> None:
    with tempfile.TemporaryDirectory(prefix="momento_encoder_failure_") as folder:
        encoder = _bare_encoder(Path(folder) / "worker-failure.mkv")
        failed = threading.Event()
        failure: list[Exception] = []

        def crash(_item, _stream) -> None:
            raise RuntimeError("simulated video worker failure")

        encoder._video_stream = object()
        encoder._container = object()
        encoder._started = True
        encoder._encode_one_video = crash
        encoder.on_fatal_error = lambda exc: (failure.append(exc), failed.set())
        encoder._video_thread = threading.Thread(target=encoder._video_worker, daemon=True)
        encoder._video_thread.start()

        frame = np.zeros((6, 8, 4), dtype=np.uint8)
        first_accepted = encoder.submit_video(frame, 0.0)
        notified = failed.wait(timeout=2.0)
        accepted_after_failure = encoder.submit_video(frame, 0.1)
        encoder._video_thread.join(timeout=2.0)
        encoder._started = False

        check("encoder failure: first frame reaches the worker", first_accepted)
        check("encoder failure: fatal callback fires promptly", notified and bool(failure))
        check("encoder failure: later submissions are rejected", not accepted_after_failure)
        check("encoder failure: failed worker exits", not encoder._video_thread.is_alive())


def test_repeated_audio_processing_errors_become_fatal() -> None:
    with tempfile.TemporaryDirectory(prefix="momento_audio_errors_") as folder:
        encoder = _bare_encoder(Path(folder) / "audio-errors.mkv")
        raised: Exception | None = None
        for attempt in range(encoder_mod._AUDIO_ERROR_FATAL_COUNT):
            try:
                encoder._note_audio_processing_error(
                    RuntimeError(f"simulated audio error {attempt + 1}"),
                    stage="encode/mux",
                )
            except Exception as exc:
                raised = exc
                break

        check(
            "audio errors: isolated failures are tolerated before the threshold",
            encoder._audio_consecutive_errors == encoder_mod._AUDIO_ERROR_FATAL_COUNT,
        )
        check(
            "audio errors: persistent encode/mux failure becomes fatal",
            raised is not None and "audio" in str(raised).lower(),
        )


def test_successful_audio_frame_resets_error_streak() -> None:
    with tempfile.TemporaryDirectory(prefix="momento_audio_recovery_") as folder:
        encoder = _bare_encoder(Path(folder) / "audio-recovery.mkv")
        encoder._audio_consecutive_errors = encoder_mod._AUDIO_ERROR_FATAL_COUNT - 1

        class Stream:
            @staticmethod
            def encode(_frame):
                return ()

        encoder._encode_and_mux_audio(SimpleNamespace(pts=1), Stream())
        check(
            "audio errors: a healthy encoded frame resets the consecutive streak",
            encoder._audio_consecutive_errors == 0,
        )


def test_encoder_rejects_duplicate_video_pts_slots() -> None:
    with tempfile.TemporaryDirectory(prefix="momento_video_pts_") as folder:
        encoder = _bare_encoder(Path(folder) / "video-pts.mkv")
        encoder._started = True
        frame = np.zeros((6, 8, 4), dtype=np.uint8)

        first = encoder.submit_video(frame, 0.0)
        duplicate = encoder.submit_video(frame, 0.001)
        next_slot = encoder.submit_video(frame, 1.0 / 30.0)
        encoder._started = False

        check("video PTS: first frame is accepted", first)
        check("video PTS: same frame-rate slot is rejected", not duplicate)
        check("video PTS: next frame-rate slot is accepted", next_slot)
        check("video PTS: malformed duplicate is reported as frame loss", (
            encoder._stats.video_frames_submitted == 3
            and encoder._stats.video_frames_dropped == 1
        ))
        queued_pts = [encoder._video_q.get_nowait().pts for _ in range(2)]
        check("video PTS: only requested unique slots are queued", queued_pts == [0, 1])


def test_encoder_failure_uses_session_finalize_path() -> None:
    with tempfile.TemporaryDirectory(prefix="momento_encoder_session_") as folder:
        tmp = Path(folder)
        manager, recorder = _session(tmp)
        game = ActiveGame("game.exe", 301, None, 3.0)
        output = tmp / "game_2026-01-01_000000.mkv"
        output.write_bytes(b"recoverable")
        failures: list[str] = []
        finalized = threading.Event()

        def fail_stop():
            recorder._recording = False
            finalized.set()
            raise RecordingFinalizeError(output, "simulated worker failure")

        recorder.stop = fail_stop
        recorder._recording = True
        manager.set_failure_callback(
            lambda reason, _game, _detail: failures.append(reason)
        )
        with manager._lock:
            manager._current_game = game
            manager._current_output = output

        hook = recorder.on_encoder_failed
        if hook is not None:
            hook(RuntimeError("simulated worker failure"))
        finished_promptly = finalized.wait(timeout=2.0)
        release_deadline = time.monotonic() + 2.0
        while time.monotonic() < release_deadline and not manager._watcher.released:
            time.sleep(0.01)

        check("encoder failure path: Recorder hook is installed", hook is not None)
        check("encoder failure path: live recording finalizes promptly", finished_promptly)
        check(
            "encoder failure path: existing incomplete-recording warning is used",
            failures == [sess.FAILURE_FINALIZE],
        )
        check(
            "encoder failure path: still-running game is released for retry",
            manager._watcher.released == [game],
        )


def test_output_write_failure_is_not_treated_as_a_gpu_failure() -> None:
    with tempfile.TemporaryDirectory(prefix="momento_output_failure_") as folder:
        manager, recorder = _session(Path(folder))
        game = ActiveGame("game.exe", 305, None, 3.0)
        failures: list[tuple[str, str]] = []
        finalized = threading.Event()

        def stop_after_disk_failure():
            recorder._recording = False
            finalized.set()
            return None

        recorder.stop = stop_after_disk_failure
        recorder._recording = True
        manager.set_failure_callback(
            lambda reason, _game, detail: failures.append((reason, detail))
        )
        with manager._lock:
            manager._current_game = game

        hook = recorder.on_encoder_failed
        if hook is not None:
            hook(OSError(errno.ENOSPC, "simulated disk full"))
        finished = finalized.wait(timeout=2.0)
        time.sleep(0.05)

        check("output failure: live recording finalizes promptly", finished)
        check(
            "output failure: user sees an output-space warning",
            len(failures) == 1
            and failures[0][0] == sess.FAILURE_OUTPUT_FOLDER
            and "space" in failures[0][1].lower(),
        )
        check(
            "output failure: healthy video backend is not retried",
            manager._watcher.released == [],
        )


def test_output_write_failure_does_not_demote_codec() -> None:
    import momento.core.recorder as recorder_mod

    recorder = recorder_mod.Recorder()
    recorder._is_running = True
    disabled: list[str] = []
    real_disable = recorder_mod.encoders.disable_for_process
    recorder_mod.encoders.disable_for_process = lambda codec, _exc: disabled.append(codec)
    try:
        recorder._handle_encoder_failure(
            recorder._audio_generation,
            OSError(errno.ENOSPC, "simulated disk full"),
            encoders.NVENC,
        )
    finally:
        recorder_mod.encoders.disable_for_process = real_disable

    check("output failure: NVENC remains eligible after disk-full", disabled == [])


def test_startup_output_failure_is_not_an_encoder_fallback() -> None:
    disk_full = OSError(errno.ENOSPC, "simulated startup disk full")
    check(
        "startup output failure: disk-full is never retried as another GPU backend",
        not recorder_mod._should_retry_encoder_candidate(disk_full),
    )
    check(
        "startup encoder failure: a driver/open error remains eligible for fallback",
        recorder_mod._should_retry_encoder_candidate(RuntimeError("mock driver failure")),
    )


def test_session_classifies_startup_disk_full_as_output_failure() -> None:
    with tempfile.TemporaryDirectory(prefix="momento_start_disk_full_") as folder:
        manager, recorder = _session(Path(folder))
        game = ActiveGame("game.exe", 307, None, 3.0)
        failures: list[tuple[str, str]] = []

        def fail_start(**_kwargs) -> None:
            raise OSError(errno.ENOSPC, "simulated startup disk full")

        recorder.start = fail_start
        manager.set_failure_callback(
            lambda reason, _game, detail: failures.append((reason, detail))
        )
        manager._start_recording(game, hwnd=4242)

        check(
            "startup output failure: Session shows the output-folder warning",
            len(failures) == 1 and failures[0][0] == sess.FAILURE_OUTPUT_FOLDER,
        )
        check(
            "startup output failure: the game is not put into a GPU retry loop",
            manager._watcher.released == [],
        )


def test_low_disk_guard_stops_before_the_drive_fills() -> None:
    with tempfile.TemporaryDirectory(prefix="momento_disk_guard_") as folder:
        tmp = Path(folder)
        manager, recorder = _session(tmp)
        manager.config = dataclasses.replace(
            manager.config, low_disk_warning_gb=5
        )
        game = ActiveGame("game.exe", 306, None, 3.0)
        failures: list[tuple[str, str]] = []
        recorder._recording = True
        with manager._lock:
            manager._current_game = game
            manager._current_output = tmp / "recording.mkv"
        manager.set_failure_callback(
            lambda reason, _game, detail: failures.append((reason, detail))
        )

        start_guard = getattr(manager, "_start_disk_guard", None)
        check("low disk guard: SessionManager exposes the active guard", callable(start_guard))
        if callable(start_guard):
            real_free = sess.free_bytes_for
            real_interval = sess._DISK_GUARD_INTERVAL_S
            sess.free_bytes_for = lambda _path: 4 * 1024**3
            sess._DISK_GUARD_INTERVAL_S = 0.01
            try:
                start_guard(game, tmp / "recording.mkv")
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and recorder.is_recording:
                    time.sleep(0.01)
            finally:
                sess.free_bytes_for = real_free
                sess._DISK_GUARD_INTERVAL_S = real_interval

            check("low disk guard: recording is stopped", not recorder.is_recording)
            check(
                "low disk guard: user sees why recording stopped",
                len(failures) == 1
                and failures[0][0] == sess.FAILURE_LOW_DISK,
            )
            check("low disk guard: no automatic retry loop", manager._watcher.released == [])


def test_capture_failure_warns_saves_and_retries() -> None:
    with tempfile.TemporaryDirectory(prefix="momento_capture_session_") as folder:
        tmp = Path(folder)
        manager, recorder = _session(tmp)
        game = ActiveGame("game.exe", 303, None, 3.0)
        failures: list[tuple[str, str]] = []
        finalized = threading.Event()

        def stop_cleanly():
            recorder._recording = False
            finalized.set()
            return tmp / "capture-interrupted.mkv"

        recorder.stop = stop_cleanly
        recorder._recording = True
        manager.set_failure_callback(
            lambda reason, _game, detail: failures.append((reason, detail))
        )
        with manager._lock:
            manager._current_game = game

        hook = recorder.on_video_capture_failed
        if hook is not None:
            hook(RuntimeError("simulated graphics-driver reset"))
        finished_promptly = finalized.wait(timeout=2.0)
        release_deadline = time.monotonic() + 2.0
        while time.monotonic() < release_deadline and not manager._watcher.released:
            time.sleep(0.01)

        check("capture failure path: Recorder hook is installed", hook is not None)
        check("capture failure path: live recording finalizes promptly", finished_promptly)
        check(
            "capture failure path: user sees an interruption warning",
            len(failures) == 1
            and failures[0][0] == sess.FAILURE_VIDEO_CAPTURE
            and "retry" in failures[0][1],
        )
        check(
            "capture failure path: still-running game is released for retry",
            manager._watcher.released == [game],
        )


def test_software_encoder_fallback_is_visible() -> None:
    with tempfile.TemporaryDirectory(prefix="momento_software_encoder_") as folder:
        manager, recorder = _session(Path(folder))
        game = ActiveGame("game.exe", 302, None, 3.0)
        failures: list[tuple[str, str]] = []
        recorder.active_video_codec = "libx264"
        manager.set_failure_callback(
            lambda reason, _game, detail: failures.append((reason, detail))
        )

        manager._warn_if_software_encoder(game)

        check(
            "software fallback: warning identifies CPU encoding",
            failures == [
                (
                    sess.FAILURE_SOFTWARE_ENCODER,
                    "Hardware encoding was unavailable, so this recording is using the CPU. "
                    "Lower the resolution or frame rate if the game stutters or frames drop.",
                )
            ],
        )


class _MessageHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_sustained_video_drops_are_reported_as_degraded() -> None:
    with tempfile.TemporaryDirectory(prefix="momento_drop_health_") as folder:
        encoder = _bare_encoder(Path(folder) / "degraded.mkv")
        encoder._stats.video_frames_submitted = 300
        encoder._stats.video_frames_dropped = 30
        encoder._video_drop_window.extend([False] * 270 + [True] * 30)
        handler = _MessageHandler()
        encoder_mod.logger.addHandler(handler)
        try:
            encoder._maybe_warn_video_drops()
        finally:
            encoder_mod.logger.removeHandler(handler)

        encoder._started = True
        accepted_after_warning = encoder.submit_video(
            np.zeros((6, 8, 4), dtype=np.uint8), 0.0
        )
        encoder._started = False
        summary = encoder._stats.summary()

        check(
            "video health: sustained 10% loss emits a warning",
            any("degraded" in message.lower() and "10.0%" in message for message in handler.messages),
        )
        check("video health: final stats are marked degraded", encoder._stats.video_health_degraded)
        check("video health: summary cannot read as healthy", "DEGRADED" in summary)
        check("video health: warning does not stop the recording", accepted_after_warning)
        check("video health: warning is not a fatal encoder error", encoder.fatal_error is None)


def test_sustained_video_drops_reach_the_session_owner() -> None:
    with tempfile.TemporaryDirectory(prefix="momento_drop_warning_") as folder:
        manager, recorder = _session(Path(folder))
        game = ActiveGame("game.exe", 304, None, 3.0)
        failures: list[tuple[str, str]] = []
        recorder._recording = True
        with manager._lock:
            manager._current_game = game
        manager.set_failure_callback(
            lambda reason, _game, detail: failures.append((reason, detail))
        )

        hook = getattr(recorder, "on_video_degraded", None)
        if hook is not None:
            hook(0.125)

        check("video health warning: Recorder hook is installed", hook is not None)
        check(
            "video health warning: user sees a performance warning",
            len(failures) == 1
            and failures[0][0] == getattr(sess, "FAILURE_VIDEO_PERFORMANCE", None)
            and "12.5%" in failures[0][1],
        )


def main() -> int:
    for fn in (
        test_explicit_empty_game_list_stays_empty,
        test_ffmpeg_cleanup_requires_bundled_orphan,
        test_wgc_frame_snapshot_owns_its_pixels,
        test_wgc_static_startup_frame_is_retained,
        test_wgc_close_during_settle_fails_startup,
        test_wgc_close_after_prepare_cancels_recorder_publish,
        test_stopped_wgc_refuses_to_start_sender,
        test_wgc_native_stop_is_reported_and_uses_milliseconds,
        test_wgc_interval_tracks_requested_high_framerate,
        test_wgc_intentional_stop_is_silent,
        test_sender_deadline_skips_late_slots,
        test_sender_pacing_health_reports_hitches,
        test_watcher_stop_timeout_keeps_single_thread,
        test_session_does_not_start_during_finalization,
        test_second_game_is_deferred_while_first_start_is_pending,
        test_encoder_worker_failure_closes_submission_gate,
        test_repeated_audio_processing_errors_become_fatal,
        test_successful_audio_frame_resets_error_streak,
        test_encoder_rejects_duplicate_video_pts_slots,
        test_encoder_failure_uses_session_finalize_path,
        test_output_write_failure_is_not_treated_as_a_gpu_failure,
        test_output_write_failure_does_not_demote_codec,
        test_startup_output_failure_is_not_an_encoder_fallback,
        test_session_classifies_startup_disk_full_as_output_failure,
        test_low_disk_guard_stops_before_the_drive_fills,
        test_capture_failure_warns_saves_and_retries,
        test_software_encoder_fallback_is_visible,
        test_sustained_video_drops_are_reported_as_degraded,
        test_sustained_video_drops_reach_the_session_owner,
    ):
        try:
            fn()
        except Exception as exc:
            check(f"{fn.__name__} raised unexpectedly: {exc!r}", False)

    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
