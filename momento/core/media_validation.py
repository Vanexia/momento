"""Structural validation for media produced by destructive/offline operations."""

from __future__ import annotations

import json
import math
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from momento.util.ffmpeg_path import ffprobe_exe

_CREATION = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


@dataclass(frozen=True)
class MediaSummary:
    formats: frozenset[str]
    duration: float | None
    streams: tuple[tuple[str, str], ...]
    game_tag: str


def probe_media_summary(path: Path | str, *, timeout: float = 20.0) -> MediaSummary:
    """Read the container, duration, A/V codecs, and Momento tag with ffprobe."""
    media = Path(path)
    try:
        result = subprocess.run(
            [
                str(ffprobe_exe()),
                "-v",
                "error",
                "-show_entries",
                "format=format_name,duration:format_tags=MOMENTO_GAME:stream=codec_type,codec_name",
                "-of",
                "json",
                str(media),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_CREATION,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"ffprobe could not inspect {media.name}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[-300:] or f"exit code {result.returncode}"
        raise ValueError(f"ffprobe rejected {media.name}: {detail}")
    try:
        payload = json.loads(result.stdout or "{}")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"ffprobe returned invalid metadata for {media.name}") from exc

    format_data = payload.get("format")
    if not isinstance(format_data, dict):
        raise ValueError(f"ffprobe found no container metadata for {media.name}")
    formats = frozenset(
        part.strip().casefold()
        for part in str(format_data.get("format_name") or "").split(",")
        if part.strip()
    )
    duration: float | None = None
    raw_duration = format_data.get("duration")
    try:
        parsed_duration = float(raw_duration)
        if math.isfinite(parsed_duration) and parsed_duration > 0:
            duration = parsed_duration
    except (TypeError, ValueError):
        pass

    streams: list[tuple[str, str]] = []
    for stream in payload.get("streams") or ():
        if not isinstance(stream, dict):
            continue
        stream_type = str(stream.get("codec_type") or "").casefold()
        codec = str(stream.get("codec_name") or "").casefold()
        if stream_type and codec:
            streams.append((stream_type, codec))
    tags = format_data.get("tags")
    game_tag = ""
    if isinstance(tags, dict):
        for key, value in tags.items():
            if str(key).casefold() == "momento_game":
                game_tag = str(value or "").strip()
                break
    return MediaSummary(formats, duration, tuple(streams), game_tag)


def validate_repair_candidate(source: Path, candidate: Path) -> str | None:
    """Return an error when a repaired MKV is unsafe to replace its source."""
    try:
        repaired = probe_media_summary(candidate)
    except ValueError as exc:
        return str(exc)
    error = _validate_common(repaired, expected_format="matroska")
    if error:
        return error

    try:
        original = probe_media_summary(source)
    except ValueError:
        original = None
    if original is not None:
        missing = _missing_av_streams(original, repaired)
        if missing:
            return f"Repaired file lost source stream(s): {missing}"
        if original.game_tag and repaired.game_tag != original.game_tag:
            return "Repaired file lost its Momento game metadata"
    return None


def validate_trim_candidate(
    source: Path,
    candidate: Path,
    *,
    expected_duration: float,
) -> str | None:
    """Return an error when an exported MP4 is incomplete or unreadable."""
    try:
        exported = probe_media_summary(candidate)
    except ValueError as exc:
        return str(exc)
    error = _validate_common(exported, expected_format="mp4")
    if error:
        return error
    if (
        exported.duration is not None
        and expected_duration > 0
        and exported.duration > expected_duration + max(5.0, expected_duration * 0.25)
    ):
        return "Exported clip duration is far longer than the selected range"

    try:
        original = probe_media_summary(source)
    except ValueError:
        original = None
    if original is not None:
        missing = _missing_av_streams(original, exported)
        if missing:
            return f"Exported clip lost source stream(s): {missing}"
        if original.game_tag and exported.game_tag != original.game_tag:
            return "Exported clip lost its Momento game metadata"
    return None


def _validate_common(summary: MediaSummary, *, expected_format: str) -> str | None:
    if expected_format == "mp4":
        if not ({"mov", "mp4"} & summary.formats):
            return "Output is not an MP4 container"
    elif expected_format not in summary.formats:
        return "Output is not a Matroska container"
    if summary.duration is None:
        return "Output has no readable positive duration"
    if ("video", "h264") not in summary.streams:
        return "Output has no readable H.264 video stream"
    return None


def _missing_av_streams(source: MediaSummary, candidate: MediaSummary) -> str:
    expected = Counter(stream for stream in source.streams if stream[0] in {"video", "audio"})
    actual = Counter(stream for stream in candidate.streams if stream[0] in {"video", "audio"})
    missing = expected - actual
    return ", ".join(
        f"{stream_type}/{codec} x{count}"
        for (stream_type, codec), count in sorted(missing.items())
    )
