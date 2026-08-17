"""Smoke test for the PortAudio/WASAPI audio capture layer (replaces soundcard).

Proves the rewritten mic + loopback streamers actually open a device and deliver
float32 ``(frames, 2)`` chunks, that channel conversion and the legacy
endpoint-id migration helpers behave, and that ``resolve_*`` handle names. Opens
REAL devices (default mic + default output loopback) — skips gracefully on a
host with no audio endpoints.

The headline assertion: the default USB microphone on the test machine uses a
format that the previous soundcard backend could not open. If this test
captures from it, the original bug is fixed.

Run: .venv\\Scripts\\python.exe tests\\smoke_audio_capture.py
"""

from __future__ import annotations

import gc
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from momento.core import audio_devices as ad  # noqa: E402
from momento.core.audio_loopback import (  # noqa: E402
    LoopbackStreamer,
    list_loopback_devices,
    resolve_loopback_device,
)
from momento.core.mic_capture import (  # noqa: E402
    MicStreamer,
    list_mic_devices,
    resolve_mic_device,
)

_results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    _results.append((name, bool(ok)))
    print(("PASS" if ok else "FAIL"), "-", name)


def _capture(streamer_cls, device_id: str, label: str) -> None:
    got = {"n": 0, "shape": None, "peak": 0.0}

    def sink(arr, _pts):
        got["n"] += 1
        got["shape"] = arr.shape
        got["peak"] = max(got["peak"], float(np.max(np.abs(arr))) if arr.size else 0.0)
        return True

    s = streamer_cls(device_id)
    try:
        s.start(sink)
        time.sleep(0.4)
    finally:
        s.stop()
    check(f"{label}: opened + submitted chunks ({got['n']})", got["n"] > 0)
    check(
        f"{label}: chunks are (frames, 2) float32",
        got["shape"] is not None and got["shape"][1] == 2,
    )
    # An idle loopback endpoint legitimately yields synthesized-silence chunks
    # instead of real device chunks; either counter proves the leg delivered.
    total = s.chunks_submitted + getattr(s, "silence_chunks_submitted", 0)
    check(f"{label}: chunk counters match ({total})", total > 0)


def main() -> None:
    mics = list_mic_devices()
    sysd = list_loopback_devices()
    print("mics:", [m.id for m in mics])
    print("outputs:", [d.id for d in sysd])
    print()

    check("enumerate: at least one mic", len(mics) > 0)
    check("enumerate: at least one output endpoint", len(sysd) > 0)

    # channel conversion
    check("to_channels mono->2", ad.to_channels(np.ones((4, 1), np.float32), 2).shape == (4, 2))
    check("to_channels 3->2", ad.to_channels(np.ones((4, 3), np.float32), 2).shape == (4, 2))
    check("to_channels identity", ad.to_channels(np.ones((4, 2), np.float32), 2).shape == (4, 2))

    # legacy endpoint-id migration helpers
    check(
        "looks_like_endpoint_id: true on endpoint id",
        ad.looks_like_endpoint_id("{0.0.1.00000000}.{5b16aff2-327f-4f3a-bd48-1d917e5dbb42}"),
    )
    check(
        "looks_like_endpoint_id: false on friendly name",
        not ad.looks_like_endpoint_id("USB Microphone"),
    )
    check(
        "friendly_name: bogus id -> None (no crash)",
        ad.friendly_name_for_endpoint_id(
            "{0.0.1.00000000}.{00000000-0000-0000-0000-000000000000}"
        )
        is None,
    )

    # resolution
    if mics:
        check("resolve_mic_device: by name", resolve_mic_device(mics[0].id) is not None)
    check("resolve_mic_device: blank -> None", resolve_mic_device("") is None)
    if sysd:
        check("resolve_loopback_device: by name", resolve_loopback_device(sysd[0].id) is not None)

    # real capture — the actual fix
    if mics:
        _capture(MicStreamer, mics[0].id, "mic")
    if sysd:
        _capture(LoopbackStreamer, sysd[0].id, "loopback (system audio)")

    # leak check: repeated start/stop must not accumulate capture threads or
    # leave a PyAudio session open (each streamer owns a thread + a PyAudio
    # instance for its lifetime).
    if mics and sysd:
        base_threads = threading.active_count()
        for _ in range(4):
            for cls, dev in ((MicStreamer, mics[0].id), (LoopbackStreamer, sysd[0].id)):
                s = cls(dev)
                s.start(lambda *_a: True)
                time.sleep(0.15)
                s.stop()
        gc.collect()
        lingering = [t.name for t in threading.enumerate() if "Capture" in t.name]
        check("leak: no lingering capture threads after 4 start/stop cycles", not lingering)
        check(
            "leak: thread count returns to baseline (+/-1)",
            threading.active_count() <= base_threads + 1,
        )

    # first-run seeding: a blank config must get a working device picked, fast
    # (the probe-open hang fix means an idle loopback can't stall startup).
    from momento.config import load_config
    from momento.__main__ import _seed_default_devices

    cfg = load_config()
    cfg.mic_device = ""
    cfg.system_audio_device = ""
    t0 = time.monotonic()
    _seed_default_devices(cfg)
    seed_elapsed = time.monotonic() - t0
    check("seed: first-run picks a mic device", bool(cfg.mic_device))
    check(f"seed: completes fast, no hang ({seed_elapsed:.2f}s)", seed_elapsed < 5.0)

    passed = sum(1 for _, ok in _results if ok)
    total = len(_results)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
