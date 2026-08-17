"""Regression test for the audio-capture stall/stop crash class (2026-07-02).

The bug: WASAPI loopback delivers NOTHING while the endpoint's render engine is
idle (device-dependent - some USB audio devices power down while onboard audio keeps
sending silence). The capture thread then sat blocked in Pa_ReadStream forever
(0 chunks all recording), stop()'s join timed out, and its "force-close the
stream to unblock the read" hardening closed the PortAudio stream from another
thread — undefined behaviour that killed the whole process with a native access
violation (no traceback; the app "randomly closed" and the recording was left
unfinalised).

The fix: poll-based reads (never block → the loop always sees stop()), the loop
is the stream's ONLY owner (stop() never touches it), and idle loopback gaps are
synthesized as real-time silence so the encoder's mix graph keeps flowing.

Everything here uses fake PortAudio streams — headless + CI-safe, no devices.

Run: .venv\\Scripts\\python.exe tests\\smoke_audio_stall.py
"""

from __future__ import annotations

import contextlib
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from momento.core import audio_devices  # noqa: E402
from momento.core import audio_loopback  # noqa: E402
from momento.core import mic_capture  # noqa: E402

_results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    _results.append((name, bool(ok)))
    print(("PASS" if ok else "FAIL"), "-", name)


_CHUNK = 960
_RATE = 48000


class FakeStream:
    """Stands in for a PyAudio blocking-mode input stream.

    mode="idle": never has data (the powered-down-device case).
    mode="data": delivers one chunk every 20 ms (a healthy device).
    mode="dead": never has data and reports inactive (device lost).
    """

    def __init__(self, mode: str, channels: int = 2) -> None:
        self.mode = mode
        self.channels = channels
        self.closed = False
        self.close_thread_ident: int | None = None
        self.reads = 0
        self._next_data = time.monotonic()

    def get_read_available(self) -> int:
        if self.mode != "data":
            return 0
        return _CHUNK if time.monotonic() >= self._next_data else 0

    def read(self, n: int, exception_on_overflow: bool = True) -> bytes:
        assert self.mode == "data", "read() must never be called without available data"
        self.reads += 1
        self._next_data += _CHUNK / _RATE
        return np.full((n * self.channels,), 0.25, dtype=np.float32).tobytes()

    def is_active(self) -> bool:
        return self.mode != "dead" and not self.closed

    def stop_stream(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        self.close_thread_ident = threading.get_ident()


class _Patched:
    """Route both streamers' device layer at a FakeStream."""

    def __init__(self, stream: FakeStream) -> None:
        self.stream = stream

    def __enter__(self):
        self._orig_session = audio_devices.pyaudio_session
        self._orig_open = audio_devices.open_input_stream
        self._orig_resolve_loop = audio_loopback.resolve_loopback_device
        self._orig_resolve_mic = mic_capture.resolve_mic_device

        @contextlib.contextmanager
        def fake_session():
            yield None

        def fake_open(_p, _name, _sr, _ch, _frames, *, loopback=False):
            return self.stream, self.stream.channels, _RATE

        audio_devices.pyaudio_session = fake_session
        audio_devices.open_input_stream = fake_open
        audio_loopback.resolve_loopback_device = lambda k: audio_loopback.LoopbackDevice(
            name=k, id=k
        )
        mic_capture.resolve_mic_device = lambda k: mic_capture.MicDevice(name=k, id=k)
        return self

    def __exit__(self, *exc):
        audio_devices.pyaudio_session = self._orig_session
        audio_devices.open_input_stream = self._orig_open
        audio_loopback.resolve_loopback_device = self._orig_resolve_loop
        mic_capture.resolve_mic_device = self._orig_resolve_mic
        return False


def _run(streamer, run_s: float):
    chunks: list[np.ndarray] = []
    streamer.start(lambda arr, _pts: chunks.append(arr))
    time.sleep(run_s)
    t0 = time.monotonic()
    streamer.stop()
    return chunks, time.monotonic() - t0


def main() -> None:
    # ---- loopback on an idle endpoint (the crash scenario) ----
    fake = FakeStream("idle")
    s = audio_loopback.LoopbackStreamer("Fake Speakers")
    with _Patched(fake):
        chunks, stop_took = _run(s, 0.5)
    n_expected = 0.5 / (_CHUNK / _RATE)  # ~25 chunks in 0.5s at real-time pace
    check("loopback idle: stop() returns promptly (no wedge)", stop_took < 1.0)
    check(
        f"loopback idle: silence synthesized at ~real-time pace ({len(chunks)})",
        0.5 * n_expected <= len(chunks) <= 1.5 * n_expected,
    )
    check(
        "loopback idle: synthesized chunks are silent (frames, 2) float32",
        all(
            a.shape == (_CHUNK, 2) and a.dtype == np.float32 and not a.any() for a in chunks
        ),
    )
    check("loopback idle: counters split real vs synthetic", s.chunks_submitted == 0
          and s.silence_chunks_submitted == len(chunks))
    check("loopback idle: stream closed by capture thread, NOT stop()'s thread",
          fake.closed and fake.close_thread_ident != threading.get_ident())
    check("loopback idle: read() never called while starved", fake.reads == 0)
    check("loopback idle: no lingering capture thread",
          not any("LoopbackCapture" in t.name for t in threading.enumerate()))

    # ---- loopback with real data flowing ----
    fake = FakeStream("data")
    s = audio_loopback.LoopbackStreamer("Fake Speakers")
    with _Patched(fake):
        chunks, stop_took = _run(s, 0.5)
    check(f"loopback data: real chunks delivered ({s.chunks_submitted})",
          s.chunks_submitted >= 0.5 * n_expected)
    check("loopback data: no spurious silence while data flows",
          s.silence_chunks_submitted <= 2)
    check("loopback data: stop() prompt", stop_took < 1.0)

    # ---- loopback device death mid-capture ----
    fake = FakeStream("dead")
    s = audio_loopback.LoopbackStreamer("Fake Speakers")
    failed = threading.Event()
    with _Patched(fake):
        s.on_capture_failed = lambda _e: failed.set()
        s.start(lambda *_a: True)
        got_failure = failed.wait(timeout=2.0)
        s.stop()
    check("loopback dead device: on_capture_failed fires", got_failure)

    # ---- mic on a stalled device: responsive stop, NO synthesis ----
    fake = FakeStream("idle")
    m = mic_capture.MicStreamer("Fake Mic")
    with _Patched(fake):
        chunks, stop_took = _run(m, 0.4)
    check("mic stall: stop() returns promptly (no wedge)", stop_took < 1.0)
    check("mic stall: no synthesized chunks (quiet mic is a fault, not silence)",
          len(chunks) == 0 and m.chunks_submitted == 0)
    check("mic stall: stream closed by capture thread, NOT stop()'s thread",
          fake.closed and fake.close_thread_ident != threading.get_ident())

    # ---- mic with data ----
    fake = FakeStream("data")
    m = mic_capture.MicStreamer("Fake Mic")
    with _Patched(fake):
        chunks, stop_took = _run(m, 0.4)
    check(f"mic data: chunks delivered ({m.chunks_submitted})", m.chunks_submitted > 0)
    check("mic data: chunk shape (frames, 2) float32",
          all(a.shape == (_CHUNK, 2) and a.dtype == np.float32 for a in chunks))

    # ---- mic device death ----
    fake = FakeStream("dead")
    m = mic_capture.MicStreamer("Fake Mic")
    failed = threading.Event()
    with _Patched(fake):
        m.on_capture_failed = lambda _e: failed.set()
        m.start(lambda *_a: True)
        got_failure = failed.wait(timeout=2.0)
        m.stop()
    check("mic dead device: on_capture_failed fires", got_failure)

    passed = sum(1 for _, ok in _results if ok)
    total = len(_results)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
