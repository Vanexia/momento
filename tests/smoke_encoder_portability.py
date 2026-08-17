"""Hardware-vendor portability contracts for live H.264 selection."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import av  # noqa: E402
import numpy as np  # noqa: E402

from momento.core import encoders  # noqa: E402
from momento.core.encoder import InProcessEncoder, _VideoItem  # noqa: E402


checks = 0
failures = 0


def check(label: str, condition: bool) -> None:
    global checks, failures
    checks += 1
    if condition:
        print(f"PASS - {label}")
    else:
        failures += 1
        print(f"FAIL - {label}")


def test_release_libav_contains_every_fallback() -> None:
    expected = {
        encoders.NVENC,
        encoders.AMF,
        encoders.QSV,
        encoders.MEDIA_FOUNDATION,
        encoders.LIBX264,
    }
    check(
        "release libav: every configured H.264 backend is compiled in",
        expected <= av.codec.codecs_available,
    )


def test_amd_is_selected_with_actual_recording_parameters() -> None:
    calls: list[tuple[str, int, int, int, str, int]] = []
    original = encoders._probe_one

    def fake_probe(
        name: str,
        *,
        width: int = 320,
        height: int = 240,
        framerate: int = 30,
        preset: str = "high",
        custom_bitrate_kbps: int = 12_000,
    ) -> encoders._ProbeResult:
        calls.append((name, width, height, framerate, preset, custom_bitrate_kbps))
        return encoders._ProbeResult(name, name == encoders.AMF, None)

    encoders._probe_one = fake_probe
    try:
        selected = encoders.pick_encoder_for_recording(
            width=2560,
            height=1440,
            framerate=60,
            preset="custom",
            custom_bitrate_kbps=16_000,
        )
    finally:
        encoders._probe_one = original

    check("AMD mock: AMF is selected after NVENC is unavailable", selected == encoders.AMF)
    check(
        "AMD mock: probe uses the real 1440p60 Custom profile",
        calls == [
            (encoders.NVENC, 2560, 1440, 60, "custom", 16_000),
            (encoders.AMF, 2560, 1440, 60, "custom", 16_000),
        ],
    )


def test_failed_amd_probe_reaches_software_floor() -> None:
    calls: list[str] = []
    original = encoders._probe_one

    def fake_probe(name: str, **_kwargs: object) -> encoders._ProbeResult:
        calls.append(name)
        error = None if name == encoders.LIBX264 else "mock hardware unavailable"
        return encoders._ProbeResult(name, name == encoders.LIBX264, error)

    encoders._probe_one = fake_probe
    try:
        selected = encoders.pick_encoder_for_recording(
            width=3840,
            height=2160,
            framerate=60,
            preset="custom",
            custom_bitrate_kbps=16_000,
        )
    finally:
        encoders._probe_one = original

    check("AMD mock: hardware failure falls back to libx264", selected == encoders.LIBX264)
    check("AMD mock: every backend is attempted in priority order", calls == list(encoders._PRIORITY))


def test_amd_options_match_the_bundled_encoder_contract() -> None:
    custom = encoders.quality_options_for(encoders.AMF, "custom", 16_000)
    quality = encoders.quality_options_for(encoders.AMF, "high", 16_000)
    check(
        "AMD options: public default uses 16 Mbit CBR",
        custom == {
            "usage": "transcoding",
            "quality": "balanced",
            "rc": "cbr",
            "b": "16000k",
        },
    )
    check(
        "AMD options: quality mode supplies all frame QPs",
        quality.get("rc") == "cqp"
        and {quality.get("qp_i"), quality.get("qp_p"), quality.get("qp_b")} == {"19"},
    )
    check("AMD options: host-frame input uses a supported format", encoders.preferred_pix_fmt_for(encoders.AMF) == "yuv420p")


def test_live_driver_failure_demotes_amd_until_restart() -> None:
    original_probe = encoders._probe_one
    original_disabled = dict(encoders._runtime_disabled)

    def fake_probe(name: str, **_kwargs: object) -> encoders._ProbeResult:
        available = name in {encoders.AMF, encoders.MEDIA_FOUNDATION, encoders.LIBX264}
        return encoders._ProbeResult(name, available, None if available else "mock unavailable")

    encoders._probe_one = fake_probe
    encoders._runtime_disabled.clear()
    try:
        first = encoders.pick_encoder_for_recording(
            width=2560,
            height=1440,
            framerate=60,
            preset="custom",
            custom_bitrate_kbps=16_000,
        )
        encoders.disable_for_process(encoders.AMF, "mock driver reset")
        retry = encoders.pick_encoder_for_recording(
            width=2560,
            height=1440,
            framerate=60,
            preset="custom",
            custom_bitrate_kbps=16_000,
        )
    finally:
        encoders._probe_one = original_probe
        encoders._runtime_disabled.clear()
        encoders._runtime_disabled.update(original_disabled)

    check("AMD recovery: AMF is preferred while healthy", first == encoders.AMF)
    check("AMD recovery: a failed AMF session retries on MF", retry == encoders.MEDIA_FOUNDATION)


def test_qsv_downscale_uses_its_required_pixel_format() -> None:
    captured: list[av.VideoFrame] = []

    class _Stream:
        def encode(self, frame: av.VideoFrame):
            captured.append(frame)
            return ()

    with tempfile.TemporaryDirectory(prefix="momento_qsv_format_") as folder:
        encoder = InProcessEncoder(
            output_path=Path(folder) / "qsv.mkv",
            video_width=16,
            video_height=12,
            video_framerate=60,
            target_width=8,
            target_height=6,
            video_codec=encoders.QSV,
            video_options={},
            encoder_pix_fmt="nv12",
        )
        pixels = np.zeros((12, 16, 4), dtype=np.uint8)
        encoder._encode_one_video(_VideoItem(pixels, 0), _Stream())

    check(
        "Intel portability: downscale preserves QSV's required nv12 format",
        len(captured) == 1 and captured[0].format.name == "nv12",
    )


def main() -> int:
    test_release_libav_contains_every_fallback()
    test_amd_is_selected_with_actual_recording_parameters()
    test_failed_amd_probe_reaches_software_floor()
    test_amd_options_match_the_bundled_encoder_contract()
    test_live_driver_failure_demotes_amd_until_restart()
    test_qsv_downscale_uses_its_required_pixel_format()
    print(f"\n{checks - failures}/{checks} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
