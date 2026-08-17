"""Targeted tests for the recording-pipeline failure paths hardened after the
Codex review. These exercise the *fix* paths the happy-path smoke tests can't:

  Bug 1: InProcessEncoder.stop() must NOT flush/close while a worker is still
         alive (concurrent libav use on the same stream/container corrupts the
         file). A hung worker -> skip finalize + mark fatal, no deadlock.
  Bug 2: A finalize failure must be fatal, not swallowed: Recorder.stop() raises
         RecordingFinalizeError (so the session never reports a clean save),
         and returns the path on a clean finalize.
  Bug 3: Audio stream start() must report device-open failures promptly so
         Recorder can degrade to video-only / single-audio recording instead
         of silently assuming the stream exists.

Run:
    C:\\dev\\Momento\\.venv\\Scripts\\python.exe tests\\smoke_recording_failures.py
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import av  # noqa: E402

import momento.core.encoder as encmod  # noqa: E402
import momento.core.mic_capture as micmod  # noqa: E402
import momento.core.recorder as recmod  # noqa: E402
from momento.core import encoders  # noqa: E402
from momento.core.encoder import InProcessEncoder  # noqa: E402
from momento.core.mic_capture import MicStreamer, list_mic_devices  # noqa: E402
from momento.core.recorder import (  # noqa: E402
    Recorder,
    RecordingFinalizeError,
    RecordingStartCancelled,
)

_results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    _results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} - {name}")


def _frame() -> np.ndarray:
    img = np.zeros((240, 320, 4), dtype=np.uint8)
    img[:, :, 3] = 255
    return img


def _make_encoder(path: Path) -> InProcessEncoder:
    codec = encoders.pick_encoder()
    return InProcessEncoder(
        output_path=path,
        video_width=320,
        video_height=240,
        video_framerate=30,
        video_codec=codec,
        video_options=encoders.quality_options_for(codec, "high", 12_000),
        encoder_pix_fmt=encoders.preferred_pix_fmt_for(codec),
    )


# --------------------------------------------------------------------- Bug 1
def test_hung_worker_skips_finalize(tmp: Path) -> None:
    """A worker stuck inside encode past the drain timeout must NOT be raced:
    stop() returns promptly (no deadlock) and marks fatal instead of flushing
    or closing the container concurrently."""
    encmod._DRAIN_TIMEOUT_S = 0.5  # speed the join timeout up for the test
    enc = _make_encoder(tmp / "hung.mkv")

    hung = threading.Event()
    release = threading.Event()

    def hanging_encode(item, stream):  # instance attr -> no self
        hung.set()
        release.wait(timeout=15.0)  # simulate a wedged hardware encoder

    enc._encode_one_video = hanging_encode  # type: ignore[assignment]
    enc.start()
    enc.submit_video(_frame(), 0.0)
    got_stuck = hung.wait(timeout=3.0)

    t0 = time.monotonic()
    enc.stop()
    elapsed = time.monotonic() - t0
    release.set()  # let the daemon worker unwind

    check("Bug1: worker actually got stuck inside encode", got_stuck)
    check("Bug1: stop() returns promptly, no deadlock", elapsed < 4.0)
    check("Bug1: fatal_error set when a worker is still alive", enc.fatal_error is not None)


# --------------------------------------------------------------------- Bug 2
class _FakeStats:
    def summary(self) -> str:
        return "fake-stats"


class _FakeEncoder:
    def __init__(self, fatal: Exception | None) -> None:
        self._fatal = fatal
        self.output_path = Path("unused")

    def stop(self) -> _FakeStats:
        return _FakeStats()

    @property
    def fatal_error(self) -> Exception | None:
        return self._fatal


class _FakeCapture:
    def stop(self) -> None:
        pass


def _recorder_with(fatal: Exception | None, mkv: Path) -> Recorder:
    rec = Recorder()
    rec._is_running = True
    rec._params = object()  # non-None; stop() only null-checks it
    rec._encoder = _FakeEncoder(fatal)  # type: ignore[assignment]
    rec._video = _FakeCapture()  # type: ignore[assignment]
    rec._loopback = _FakeCapture()  # type: ignore[assignment]
    rec._mic = _FakeCapture()  # type: ignore[assignment]
    rec._mkv_path = mkv
    return rec


def test_recorder_finalize_failure(tmp: Path) -> None:
    mkv = tmp / "dirty.mkv"
    mkv.write_bytes(b"\x1aE\xdf\xa3 pretend-mkv-with-clusters")  # file exists, recoverable
    rec = _recorder_with(RuntimeError("simulated close failure (disk full)"), mkv)
    raised = False
    try:
        rec.stop()
    except RecordingFinalizeError as e:
        raised = e.path == mkv
    check("Bug2: Recorder.stop() raises RecordingFinalizeError on a dirty finalize", raised)
    check("Bug2: the recoverable file is left on disk (not deleted)", mkv.exists())


def test_recorder_clean_finalize(tmp: Path) -> None:
    mkv = tmp / "clean.mkv"
    mkv.write_bytes(b"pretend-finalised")
    rec = _recorder_with(None, mkv)
    ret = rec.stop()
    check("Bug2: Recorder.stop() returns the path on a clean finalize", ret == mkv)


# --------------------------------------------------------------------- Bug 3
def test_mic_open_failure_raises(tmp: Path) -> None:
    """A device that enumerates but can't open must make start() RAISE, not
    return success while the capture thread silently dies."""
    mics = list_mic_devices()
    if not mics:
        check("Bug3: (skipped — no mic enumerated on this host)", True)
        return
    real_open = micmod.audio_devices.open_input_stream

    def boom(*_a, **_k):
        raise RuntimeError("simulated: device enumerated but won't open")

    micmod.audio_devices.open_input_stream = boom  # type: ignore[assignment]
    try:
        ms = MicStreamer(device_id_or_name=mics[0].id)
        raised = False
        try:
            ms.start(lambda *_a: True)
        except RuntimeError:
            raised = True
        check("Bug3: mic start() raises when the device fails to open", raised)
        check("Bug3: mic is not marked started after an open failure", not ms._started)
    finally:
        micmod.audio_devices.open_input_stream = real_open  # type: ignore[assignment]


# ------------------------------------------------------------- Phase 16.5
_startup_order: list[str] | None = None


class _FakeVideoStreamer:
    """Stands in for WGC so Recorder.start() can run headless."""

    last_instance = None

    def __init__(self, hwnd: int, framerate: int) -> None:
        self.on_window_closed = None
        self.on_capture_failed = None
        type(self).last_instance = self

    def prepare(self) -> tuple[int, int]:
        return (640, 480)

    def start_sending(self, sink, *, pts_clock=None) -> None:
        if _startup_order is not None:
            _startup_order.append("video")
        frame = np.zeros((480, 640, 4), dtype=np.uint8)
        frame[:, :, 3] = 255
        sink(frame, pts_clock() if pts_clock is not None else 0.0)

    def startup_frame(self) -> np.ndarray:
        frame = np.zeros((480, 640, 4), dtype=np.uint8)
        frame[:, :, 3] = 255
        return frame

    def stop(self) -> None:
        pass


class _BoomLoopback:
    """Enumerates fine, then fails to open — the post-16.2 raise path."""

    def __init__(self, device_id: str, sample_rate: int = 48000, channels: int = 2) -> None:
        pass

    def start(self, sink) -> None:
        raise RuntimeError("simulated: loopback device won't open")

    def stop(self) -> None:
        pass


class _EarlyDropLoopback:
    """Capture opens, then reports failure before Recorder publishes state."""

    def __init__(self, device_id: str, sample_rate: int = 48000, channels: int = 2) -> None:
        self.on_capture_failed = None

    def start(self, sink) -> None:
        assert self.on_capture_failed is not None
        self.on_capture_failed(RuntimeError("simulated early loopback drop"))

    def stop(self) -> None:
        pass


class _EarlyDropMic:
    def __init__(self, device_id_or_name: str, sample_rate: int = 48000, channels: int = 1) -> None:
        self.on_capture_failed = None

    def start(self, sink) -> None:
        assert self.on_capture_failed is not None
        self.on_capture_failed(RuntimeError("simulated early mic drop"))

    def stop(self) -> None:
        pass


class _OrderLoopback:
    def __init__(self, device_id: str, sample_rate: int = 48000, channels: int = 2) -> None:
        self.on_capture_failed = None

    def start(self, sink) -> None:
        assert _startup_order is not None
        _startup_order.append("system")
        sink(np.zeros((960, 2), dtype=np.float32), None)

    def stop(self) -> None:
        pass


class _OrderMic:
    def __init__(self, device_id_or_name: str, sample_rate: int = 48000, channels: int = 2) -> None:
        self.on_capture_failed = None

    def start(self, sink) -> None:
        assert _startup_order is not None
        _startup_order.append("mic")
        sink(np.zeros((960, 2), dtype=np.float32), None)

    def stop(self) -> None:
        pass


def test_audio_capture_starts_before_video_delivery(tmp: Path) -> None:
    global _startup_order
    real_video = recmod.WindowVideoStreamer
    real_loop = recmod.LoopbackStreamer
    real_mic = recmod.MicStreamer
    real_native_rate = recmod.audio_devices.native_sample_rate
    recmod.WindowVideoStreamer = _FakeVideoStreamer  # type: ignore[assignment]
    recmod.LoopbackStreamer = _OrderLoopback  # type: ignore[assignment]
    recmod.MicStreamer = _OrderMic  # type: ignore[assignment]
    recmod.audio_devices.native_sample_rate = lambda *_a, **_k: 48_000
    _startup_order = []
    try:
        rec = Recorder()
        rec.start(
            output_path=tmp / "startup-order.mkv",
            hwnd=1,
            mic_device="fake-mic",
            audio_device="fake-system",
        )
        order = list(_startup_order)
        rec.stop()
    finally:
        _startup_order = None
        recmod.WindowVideoStreamer = real_video  # type: ignore[assignment]
        recmod.LoopbackStreamer = real_loop  # type: ignore[assignment]
        recmod.MicStreamer = real_mic  # type: ignore[assignment]
        recmod.audio_devices.native_sample_rate = real_native_rate

    check("A/V startup: both audio legs start before video delivery", order == ["system", "mic", "video"])


def test_failed_audio_start_keeps_video_recording(tmp: Path) -> None:
    """Optional audio device failures must not abort the whole recording."""
    real_video = recmod.WindowVideoStreamer
    real_loop = recmod.LoopbackStreamer
    recmod.WindowVideoStreamer = _FakeVideoStreamer  # type: ignore[assignment]
    recmod.LoopbackStreamer = _BoomLoopback  # type: ignore[assignment]
    try:
        rec = Recorder()
        out = tmp / "stub_check.mkv"
        rec.start(
            output_path=out, hwnd=1,
            mic_device="fake-mic", audio_device="fake-sys",
        )
        check("optional-audio: start continues after audio-open failure", rec.is_recording)
        check("optional-audio: start claim released", not rec._starting)
        check("optional-audio: recorder reports mic_dropped", rec.mic_dropped)
        check("optional-audio: recorder reports sys_dropped", rec.sys_dropped)
        stopped = rec.stop()
        check("optional-audio: stop releases recorder", not rec.is_recording)
        check("optional-audio: stop returns the recording path", stopped == out)
    finally:
        recmod.WindowVideoStreamer = real_video  # type: ignore[assignment]
        recmod.LoopbackStreamer = real_loop  # type: ignore[assignment]


def test_audio_drop_during_publish_is_not_lost(tmp: Path) -> None:
    """A capture thread can die after start() confirms but before Recorder's
    final publish lock. The dropped state must win over the local streamer
    reference, and the owner notification must wait until session state exists.
    """
    real_video = recmod.WindowVideoStreamer
    real_loop = recmod.LoopbackStreamer
    real_mic = recmod.MicStreamer
    recmod.WindowVideoStreamer = _FakeVideoStreamer  # type: ignore[assignment]
    recmod.LoopbackStreamer = _EarlyDropLoopback  # type: ignore[assignment]
    recmod.MicStreamer = _EarlyDropMic  # type: ignore[assignment]
    try:
        rec = Recorder()
        notifications: list[str] = []
        rec.on_audio_dropped = notifications.append
        out = tmp / "early_audio_drop.mkv"
        rec.start(
            output_path=out,
            hwnd=1,
            mic_device="fake-mic",
            audio_device="fake-sys",
        )
        check("publish-gap audio drop keeps video recording", rec.is_recording)
        check("publish-gap audio drop reports both missing legs", rec.mic_dropped and rec.sys_dropped)
        check("publish-gap audio drop does not notify before session publish", notifications == [])
        rec.stop()
    finally:
        recmod.WindowVideoStreamer = real_video  # type: ignore[assignment]
        recmod.LoopbackStreamer = real_loop  # type: ignore[assignment]
        recmod.MicStreamer = real_mic  # type: ignore[assignment]


def test_audio_drop_emits_warning(tmp: Path) -> None:
    """A dropped audio leg on a live recording surfaces a FAILURE_AUDIO warning
    (so the user is never silently left without their mic / game sound)."""
    import momento.core.session as sessmod

    sm = sessmod.SessionManager.__new__(sessmod.SessionManager)
    emitted: list[tuple[str, str]] = []
    sm._on_failure = lambda reason, game, detail: emitted.append((reason, detail))

    class _Game:
        exe_name = "Wow.exe"

    sm._recorder = type("R", (), {"mic_dropped": True, "sys_dropped": False})()
    sm._warn_if_audio_dropped(_Game())
    check(
        "audio-warn: FAILURE_AUDIO emitted on a dropped leg",
        bool(emitted) and emitted[0][0] == sessmod.FAILURE_AUDIO,
    )
    check(
        "audio-warn: detail names the dropped device",
        bool(emitted) and "microphone" in emitted[0][1],
    )

    emitted.clear()
    sm._recorder = type("R", (), {"mic_dropped": False, "sys_dropped": False})()
    sm._warn_if_audio_dropped(_Game())
    check("audio-warn: silent when all configured audio is captured", not emitted)


def test_missing_audio_config_keeps_video_recording(tmp: Path) -> None:
    """Blank audio config should record video instead of skipping the game."""
    real_video = recmod.WindowVideoStreamer
    recmod.WindowVideoStreamer = _FakeVideoStreamer  # type: ignore[assignment]
    try:
        rec = Recorder()
        out = tmp / "video_only.mkv"
        rec.start(
            output_path=out, hwnd=1,
            mic_device="", audio_device="",
        )
        check("missing-audio: start continues with blank audio config", rec.is_recording)
        stopped = rec.stop()
        check("missing-audio: stop returns the recording path", stopped == out)
        check("missing-audio: file exists", out.exists())
        with av.open(str(out)) as inp:
            frames = sum(1 for _ in inp.decode(video=0))
        check("missing-audio: file decodes video", frames >= 1)
    finally:
        recmod.WindowVideoStreamer = real_video  # type: ignore[assignment]


def test_live_wgc_failure_reaches_recorder_owner(tmp: Path) -> None:
    real_video = recmod.WindowVideoStreamer
    recmod.WindowVideoStreamer = _FakeVideoStreamer  # type: ignore[assignment]
    try:
        rec = Recorder()
        failures: list[Exception] = []
        rec.on_video_capture_failed = failures.append
        out = tmp / "capture_failure.mkv"
        rec.start(output_path=out, hwnd=1, mic_device="", audio_device="")
        video = _FakeVideoStreamer.last_instance
        assert video is not None and video.on_capture_failed is not None
        video.on_capture_failed(RuntimeError("simulated WGC termination"))
        stopped = rec.stop()
    finally:
        recmod.WindowVideoStreamer = real_video  # type: ignore[assignment]

    check("WGC failure wiring: Recorder notifies its owner", len(failures) == 1)
    check("WGC failure wiring: the usable clip still finalizes", stopped == out)


def test_start_can_be_cancelled_while_building(tmp: Path) -> None:
    """A game/process exit during slow start should cancel the pending publish."""
    prepare_entered = threading.Event()
    release_prepare = threading.Event()
    stopped = threading.Event()

    class _SlowVideoStreamer:
        def __init__(self, hwnd: int, framerate: int) -> None:
            self.on_window_closed = None

        def prepare(self) -> tuple[int, int]:
            prepare_entered.set()
            release_prepare.wait(timeout=5.0)
            return (640, 480)

        def start_sending(self, sink, *, pts_clock=None) -> None:
            pass

        def stop(self) -> None:
            stopped.set()

    real_video = recmod.WindowVideoStreamer
    recmod.WindowVideoStreamer = _SlowVideoStreamer  # type: ignore[assignment]
    try:
        rec = Recorder()
        cancelled = {"ok": False, "err": None}

        def _start() -> None:
            try:
                rec.start(
                    output_path=tmp / "cancelled.mkv",
                    hwnd=1,
                    mic_device="fake-mic",
                    audio_device="fake-sys",
                )
            except RecordingStartCancelled:
                cancelled["ok"] = True
            except Exception as e:
                cancelled["err"] = e

        t = threading.Thread(target=_start, daemon=True)
        t.start()
        entered = prepare_entered.wait(timeout=3.0)
        busy_while_starting = rec.is_busy and not rec.is_recording
        stop_ret = rec.stop()
        release_prepare.set()
        t.join(timeout=3.0)

        check("start-cancel: prepare was in progress", entered)
        check("start-cancel: recorder reports busy before publish", busy_while_starting)
        check("start-cancel: stop during start returns no final path", stop_ret is None)
        check("start-cancel: start raises RecordingStartCancelled", cancelled["ok"])
        check("start-cancel: no unexpected start error", cancelled["err"] is None)
        check("start-cancel: video capture stopped", stopped.is_set())
        check("start-cancel: claim released", not rec.is_busy)
    finally:
        release_prepare.set()
        recmod.WindowVideoStreamer = real_video  # type: ignore[assignment]


def test_production_encoder_open_falls_back_to_next_backend(tmp: Path) -> None:
    """A backend that passes probing but fails in the real container must not
    lose the game session when another compatible backend can start."""
    real_video = recmod.WindowVideoStreamer
    real_encoder = recmod.InProcessEncoder
    real_pick = recmod.encoders.pick_encoder_for_recording
    attempts: list[str] = []

    class _CandidateEncoder:
        def __init__(self, *, output_path, video_codec, **_kwargs) -> None:
            self.output_path = Path(output_path)
            self.video_codec = video_codec
            self.on_fatal_error = None
            self.fatal_error = None

        def start(self, *, startup_video_frame=None) -> None:
            attempts.append(self.video_codec)
            if self.video_codec == encoders.AMF:
                raise RuntimeError("simulated AMF production-open failure")
            assert startup_video_frame is not None
            self.output_path.write_bytes(b"mock-finalized")

        def current_pts_seconds(self) -> float:
            return 0.0

        def submit_video(self, *_args) -> bool:
            return True

        def close_sys_audio(self) -> None:
            pass

        def close_mic_audio(self) -> None:
            pass

        def stop(self) -> _FakeStats:
            return _FakeStats()

    def fake_pick(*, excluded=(), **_kwargs) -> str:
        return encoders.LIBX264 if encoders.AMF in excluded else encoders.AMF

    recmod.WindowVideoStreamer = _FakeVideoStreamer  # type: ignore[assignment]
    recmod.InProcessEncoder = _CandidateEncoder  # type: ignore[assignment]
    recmod.encoders.pick_encoder_for_recording = fake_pick  # type: ignore[assignment]
    try:
        rec = Recorder()
        out = tmp / "encoder-fallback.mkv"
        rec.start(output_path=out, hwnd=1, mic_device="", audio_device="")
        selected = getattr(rec._encoder, "video_codec", None)
        stopped = rec.stop()
    finally:
        recmod.WindowVideoStreamer = real_video  # type: ignore[assignment]
        recmod.InProcessEncoder = real_encoder  # type: ignore[assignment]
        recmod.encoders.pick_encoder_for_recording = real_pick  # type: ignore[assignment]

    check("encoder startup fallback: AMF is attempted first", attempts[:1] == [encoders.AMF])
    check("encoder startup fallback: software backend is attempted next", attempts == [encoders.AMF, encoders.LIBX264])
    check("encoder startup fallback: working backend is published", selected == encoders.LIBX264)
    check("encoder startup fallback: recording still finalizes", stopped == out)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="momento_failtest_") as d:
        tmp = Path(d)
        for fn in (
            test_hung_worker_skips_finalize,
            test_recorder_finalize_failure,
            test_recorder_clean_finalize,
            test_mic_open_failure_raises,
            test_audio_capture_starts_before_video_delivery,
            test_failed_audio_start_keeps_video_recording,
            test_audio_drop_during_publish_is_not_lost,
            test_audio_drop_emits_warning,
            test_missing_audio_config_keeps_video_recording,
            test_live_wgc_failure_reaches_recorder_owner,
            test_start_can_be_cancelled_while_building,
            test_production_encoder_open_falls_back_to_next_backend,
        ):
            try:
                fn(tmp)
            except Exception as e:  # a test that errors is itself a failure
                check(f"{fn.__name__} raised unexpectedly: {e!r}", False)

    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
