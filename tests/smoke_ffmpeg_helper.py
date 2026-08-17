"""End-to-end checks for Momento's minimal FFmpeg/ffprobe helper pair."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import av  # noqa: E402
from PyQt6.QtGui import QImage  # noqa: E402

import momento.core.media_probe as media_probe  # noqa: E402
import momento.core.media_validation as media_validation  # noqa: E402
import momento.core.recording_safety as recording_safety  # noqa: E402
import momento.core.thumbnails as thumbnails  # noqa: E402
import momento.trim.ffmpeg_trim as ffmpeg_trim  # noqa: E402
from media_fixture import make_momento_mkv  # noqa: E402


def main(helper_dir: Path) -> int:
    helper_dir = Path(helper_dir).resolve()
    ffmpeg = helper_dir / "ffmpeg.exe"
    ffprobe = helper_dir / "ffprobe.exe"
    results: list[tuple[str, bool]] = []

    def check(label: str, condition: bool) -> None:
        results.append((label, bool(condition)))
        print(f"{'PASS' if condition else 'FAIL'} - {label}")

    media_probe.ffmpeg_exe = lambda: ffmpeg
    media_probe.ffprobe_exe = lambda: ffprobe
    media_validation.ffprobe_exe = lambda: ffprobe
    thumbnails.ffmpeg_exe = lambda: ffmpeg
    thumbnails.ffprobe_exe = lambda: ffprobe
    ffmpeg_trim.ffmpeg_exe = lambda: ffmpeg

    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "build-ffmpeg-helper.yml"
    ).read_text(encoding="utf-8")
    check(
        "workflow: publication requires every reviewed binary/archive hash",
        all(
            digest in workflow
            for digest in (
                "A53993C4FBFBC3FA9ED201AE03502F053182699B3580C7523DC66D176D0371FC",
                "DD7364CD03D86CB5F91FD028174CB6D5F1B2F3BA2606095676E0596B216A4D4D",
                "BB8E4FC7A4E8E3BB5EA4F509BFA49E01BAD1932F8CD1E4399D145D90C080F0B5",
            )
        ),
    )

    with tempfile.TemporaryDirectory(prefix="momento_helper_") as temp:
        root = Path(temp) / "space & apostrophe's" / "\u6e2c\u8a66"
        root.mkdir(parents=True)
        source = make_momento_mkv(root / "source.mkv", game_slug="helper-fixture")

        duration = media_probe.DurationProbe(source)._fast_probe()
        metadata_result: list[tuple[str, float, str]] = []
        metadata = media_probe.MetadataProbe(source)
        metadata.signals.done.connect(
            lambda path, seconds, slug: metadata_result.append((path, seconds, slug))
        )
        metadata.run()
        check("probe: final MKV duration is readable", 2.8 <= duration <= 3.2)
        check(
            "probe: embedded Momento game metadata is readable",
            bool(metadata_result)
            and metadata_result[0][1] > 0
            and metadata_result[0][2] == "helper-fixture",
        )

        thumbnail = thumbnails._extract(source)
        image = QImage(str(thumbnail)) if thumbnail else QImage()
        check(
            "thumbnail: H.264 MKV produces a valid 320px JPEG",
            thumbnail is not None
            and thumbnail.stat().st_size > 0
            and not image.isNull()
            and image.width() == 320
            and image.height() % 2 == 0,
        )

        clip = root / "clips" / "clip.mp4"
        trim_results: list[str] = []
        trim_failures: list[str] = []
        trim = ffmpeg_trim.TrimWorker(source, 0.25, 2.5, clip)
        trim.done.connect(trim_results.append)
        trim.failed.connect(trim_failures.append)
        trim.run()
        check(
            "trim: MKV exports atomically to MP4",
            trim_results == [str(clip)]
            and not trim_failures
            and clip.stat().st_size > 4096,
        )

        if clip.is_file():
            container = av.open(str(clip))
            try:
                codecs = {
                    (stream.type, stream.codec_context.name)
                    for stream in container.streams
                }
                tag = container.metadata.get("MOMENTO_GAME")
            finally:
                container.close()
            payload = clip.read_bytes()
            check(
                "trim: H.264/AAC and Momento metadata survive stream copy",
                {("video", "h264"), ("audio", "aac")} <= codecs
                and tag == "helper-fixture",
            )
            check(
                "trim: MP4 is fast-started",
                0 <= payload.find(b"moov") < payload.find(b"mdat"),
            )

            retrim = root / "clips" / "retrim.mp4"
            worker = ffmpeg_trim.TrimWorker(clip, 0.0, 1.0, retrim)
            retrim_done: list[str] = []
            worker.done.connect(retrim_done.append)
            worker.run()
            check(
                "trim: an exported MP4 can be trimmed again",
                retrim_done == [str(retrim)] and retrim.stat().st_size > 4096,
            )

        broken = make_momento_mkv(
            root / "broken.mkv",
            game_slug="repair-fixture",
            live=True,
        )
        os.utime(
            broken,
            (broken.stat().st_atime - 120, broken.stat().st_mtime - 120),
        )
        check(
            "repair: interrupted fixture has durable Momento ownership",
            recording_safety.mark_recording_owned(broken),
        )
        check(
            "repair: live MKV is detected as missing duration",
            broken
            in media_probe.find_broken_recordings(
                root,
                min_age_seconds=0,
                min_size_bytes=4096,
            ),
        )
        repair_results: list[tuple[str, bool, str]] = []
        repair = media_probe.RepairJob(broken)
        repair.signals.done.connect(
            lambda path, ok, error: repair_results.append((path, ok, error))
        )
        repair.run()
        repaired_duration = media_probe.DurationProbe(broken)._fast_probe()
        repaired = av.open(str(broken))
        try:
            repair_tag = repaired.metadata.get("MOMENTO_GAME")
            repair_codecs = {stream.codec_context.name for stream in repaired.streams}
        finally:
            repaired.close()
        check(
            "repair: remux restores duration without changing codecs or metadata",
            repair_results == [(str(broken.resolve()), True, "")]
            and repaired_duration > 0
            and repair_tag == "repair-fixture"
            and {"h264", "aac"} <= repair_codecs,
        )

        video_only = make_momento_mkv(root / "video-only.mkv", with_audio=False)
        video_thumb = thumbnails._extract(video_only)
        check(
            "thumbnail: video-only recording remains supported",
            video_thumb is not None and video_thumb.stat().st_size > 0,
        )

        leftovers = [
            path
            for path in root.rglob("*")
            if path.is_file()
            and (path.name.endswith(".partial") or ".repairing." in path.name)
        ]
        check("lifecycle: no partial trim or repair files remain", not leftovers)

    passed = sum(ok for _, ok in results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("helper_dir", type=Path)
    raise SystemExit(main(parser.parse_args().helper_dir))
