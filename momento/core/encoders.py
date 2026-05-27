"""Video encoder backend selection.

Probes the bundled libav at startup for which H.264 encoders the host
machine can actually open (not just which encoders libav was compiled
with — every Momento build ships with all of them). Picks the
highest-priority working encoder for this hardware and exposes a
quality-preset → options mapping that's correct for that backend.

Priority order (best perf/quality first; software last):

    NVIDIA NVENC    (NVIDIA GPUs, dedicated)
    AMD AMF         (AMD GPUs / APUs, since GCN 1.0)
    Intel QSV       (Intel iGPU and Arc dGPUs)
    Windows MF      (generic Media Foundation hardware path — fallback
                     when the vendor-specific one is unavailable but a
                     hardware encoder still exists, e.g. Apollo Lake or
                     niche driver setups)
    libx264         (pure-software CPU encode — works on ANY machine but
                     uses substantially more CPU than the hardware paths)

Detection is cached per-process — probing each candidate takes ~50-200ms
and we only need to do it once.

Quality presets map to per-backend option dictionaries via
:func:`quality_options_for`. Each backend has its own option vocabulary
(``cq`` on NVENC vs ``global_quality`` on QSV vs ``crf`` on libx264);
this module hides the differences from the rest of the recorder.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

import av
import av.codec

logger = logging.getLogger(__name__)


# Encoder identifiers — match the libav encoder names so the values can
# be passed directly to ``InProcessEncoder(video_codec=...)`` and
# ``container.add_stream(name)``.
NVENC = "h264_nvenc"
AMF = "h264_amf"
QSV = "h264_qsv"
MEDIA_FOUNDATION = "h264_mf"
LIBX264 = "libx264"


# Probe priority. First entry that the host can actually open wins.
# libx264 is always at the end as the guaranteed-works fallback.
_PRIORITY: tuple[str, ...] = (NVENC, AMF, QSV, MEDIA_FOUNDATION, LIBX264)


# Human-readable labels for the Settings UI + log lines. Kept terse so
# they fit in the Capture tab subtitle row.
_DISPLAY_NAMES: dict[str, str] = {
    NVENC: "NVIDIA NVENC (hardware)",
    AMF: "AMD AMF (hardware)",
    QSV: "Intel QuickSync (hardware)",
    MEDIA_FOUNDATION: "Windows Media Foundation (hardware)",
    LIBX264: "libx264 (software / CPU)",
}


# Pixel format the encoder actually accepts. QSV is the picky one — its
# libav build advertises ``nv12 qsv`` only. The rest accept yuv420p (the
# universal H.264 player-compatibility format). Routing the wrong pix_fmt
# either fails at codec_context.open() or — worse — silently inserts a
# software swscale before the hardware encoder, defeating the whole point.
_PREFERRED_PIX_FMT: dict[str, str] = {
    NVENC: "yuv420p",
    AMF: "yuv420p",
    QSV: "nv12",
    MEDIA_FOUNDATION: "yuv420p",
    LIBX264: "yuv420p",
}


def display_name_for(encoder: str) -> str:
    """Friendly label for ``encoder``. Returns the raw name if unknown."""
    return _DISPLAY_NAMES.get(encoder, encoder)


def preferred_pix_fmt_for(encoder: str) -> str:
    """Pixel format the encoder accepts cleanly. Defaults to yuv420p."""
    return _PREFERRED_PIX_FMT.get(encoder, "yuv420p")


# ---------- Detection ----------------------------------------------------

@dataclass(frozen=True)
class _ProbeResult:
    encoder: str
    ok: bool
    error: Optional[str]


_probe_cache: Optional[list[_ProbeResult]] = None


def _probe_one(name: str) -> _ProbeResult:
    """Try to open a CodecContext for ``name`` with the SAME options the
    real recorder will use. Returns success + any error.

    Probing with default options used to mask bugs where the encoder
    opened bare but rejected our quality options at real-record time
    (e.g. an older AMF driver missing `qp_b`). We now apply the "high"
    preset's options dict — the most demanding hardware path — so a
    probe pass guarantees that ``Recorder.start()`` for the same backend
    will also open.

    Pix_fmt comes from :func:`preferred_pix_fmt_for`. QSV in particular
    needs ``nv12``, not ``yuv420p`` — passing the wrong format here
    rejects the probe with ``Invalid argument`` and the backend never
    becomes available even though it would work fine at real-record.
    """
    ctx = None
    try:
        ctx = av.codec.CodecContext.create(name, "w")
        ctx.width = 320
        ctx.height = 240
        ctx.pix_fmt = preferred_pix_fmt_for(name)
        ctx.time_base = Fraction(1, 30)
        ctx.framerate = Fraction(30)
        # Apply the same options the real recorder will set, so a backend
        # whose options don't open is correctly reported as unavailable.
        # custom_bitrate_kbps is irrelevant under "high" (CRF-style) — we
        # pass a placeholder.
        ctx.options = quality_options_for(name, "high", 12000)
        ctx.open()
        return _ProbeResult(encoder=name, ok=True, error=None)
    except Exception as exc:  # noqa: BLE001 — third-party can throw anything
        return _ProbeResult(
            encoder=name, ok=False, error=f"{type(exc).__name__}: {exc}"
        )
    finally:
        # Drop the local ref immediately so libav can release the
        # underlying AVCodecContext (and the NVENC/AMF/QSV session it
        # holds) on the next GC cycle. PyAV 14 dropped explicit close().
        ctx = None  # noqa: F841 — intentional release


def detect_available() -> list[str]:
    """Return the list of encoder names that actually work on this host.

    Order follows :data:`_PRIORITY`. Result is cached at module level —
    safe to call repeatedly.

    Every probe is logged at INFO (whether it succeeded or failed) so a
    user reporting "why isn't my GPU picked?" can read momento.log and
    see exactly which backend failed and what error libav returned —
    no need to raise log levels and reproduce.
    """
    global _probe_cache
    if _probe_cache is None:
        _probe_cache = [_probe_one(name) for name in _PRIORITY]
        for r in _probe_cache:
            logger.info(
                "Encoder probe: %s = %s%s",
                r.encoder, "available" if r.ok else "unavailable",
                f" ({r.error})" if r.error else "",
            )
    return [r.encoder for r in _probe_cache if r.ok]


def pick_encoder(preferred: Optional[str] = None) -> str:
    """Return the encoder to use for the next recording.

    If ``preferred`` is given and the host actually supports it, use it
    (lets the user pin a backend from Settings later). Otherwise pick
    the highest-priority working backend. libx264 is the guaranteed
    floor — if even that fails the system genuinely can't encode video
    and we'd rather raise loudly than silently misbehave.
    """
    available = detect_available()
    if not available:
        raise RuntimeError(
            "No H.264 encoder is available on this machine — neither "
            "hardware (NVENC/AMF/QSV/MF) nor software (libx264) opened. "
            "Recording cannot start."
        )
    if preferred and preferred in available:
        return preferred
    return available[0]


# ---------- Quality preset → encoder options -----------------------------

# Constant-quality "quality factor" per preset. Each encoder interprets
# the number through its own option name, but the relative scale is
# similar across libx264 (CRF), NVENC (CQ), and QSV (ICQ / global_quality):
# 18-23 visually transparent, 23-28 mid, 28+ visibly compressed.
_QUALITY_FACTOR: dict[str, int] = {
    "low": 28,
    "medium": 23,
    "high": 19,
}


def quality_options_for(
    encoder: str, preset: str, custom_bitrate_kbps: int
) -> dict[str, str]:
    """Return the options dict for ``encoder`` at the given quality preset.

    ``preset`` is one of ``low`` / ``medium`` / ``high`` / ``custom``.
    For ``custom`` the dict uses constant-bitrate mode with
    ``custom_bitrate_kbps``; for the named presets each backend's
    constant-quality mode is used with a CRF/CQ-equivalent value.
    """
    if encoder == NVENC:
        return _nvenc_options(preset, custom_bitrate_kbps)
    if encoder == AMF:
        return _amf_options(preset, custom_bitrate_kbps)
    if encoder == QSV:
        return _qsv_options(preset, custom_bitrate_kbps)
    if encoder == MEDIA_FOUNDATION:
        return _mf_options(preset, custom_bitrate_kbps)
    if encoder == LIBX264:
        return _libx264_options(preset, custom_bitrate_kbps)
    # Unknown encoder — return empty dict and let libav use its defaults.
    logger.warning("No quality options known for encoder %s; using defaults", encoder)
    return {}


def _nvenc_options(preset: str, kbps: int) -> dict[str, str]:
    """NVIDIA NVENC.

    ``preset=p4`` is the balanced quality/speed point in NVENC's
    p1 (fastest) → p7 (slowest, best quality) scale. ``tune=hq``
    biases for HD recording. Spatial + temporal AQ improve perceptual
    quality at the same bitrate at near-zero cost on NVENC.
    """
    base = {
        "preset": "p4",
        "tune": "hq",
        "spatial-aq": "1",
        "temporal-aq": "1",
    }
    if preset == "custom":
        base["rc"] = "cbr"
        base["b"] = f"{max(1000, int(kbps))}k"
        return base
    cq = _QUALITY_FACTOR.get(preset, _QUALITY_FACTOR["high"])
    base["rc"] = "vbr"
    base["cq"] = str(cq)
    base["b"] = "0"
    return base


def _amf_options(preset: str, kbps: int) -> dict[str, str]:
    """AMD AMF.

    AMF's option names diverge meaningfully from NVENC:
    - ``rc=cqp`` is the closest analog to NVENC's CQ mode; ``qp_i / qp_p / qp_b``
      set the constant quality per frame type.
    - ``usage=transcoding`` is the catch-all for non-low-latency capture
      (vs ``ultralowlatency`` for streaming).
    - ``quality`` (``speed`` / ``balanced`` / ``quality``) is the
      perf/quality knob analogous to NVENC's ``preset``.
    """
    speed_for: dict[str, str] = {
        "low": "speed",
        "medium": "balanced",
        "high": "quality",
    }
    base = {
        "usage": "transcoding",
        "quality": speed_for.get(preset, "balanced"),
    }
    if preset == "custom":
        base["rc"] = "cbr"
        base["b"] = f"{max(1000, int(kbps))}k"
        return base
    qp = _QUALITY_FACTOR.get(preset, _QUALITY_FACTOR["high"])
    qp_s = str(qp)
    base["rc"] = "cqp"
    base["qp_i"] = qp_s
    base["qp_p"] = qp_s
    base["qp_b"] = qp_s
    return base


def _qsv_options(preset: str, kbps: int) -> dict[str, str]:
    """Intel QuickSync (QSV).

    Uses Intel's ICQ (Intelligent Constant Quality) via ``global_quality``
    for the named presets. ``preset=medium`` is the perf/quality middle
    ground; faster presets help on older iGPUs at the cost of file size.
    """
    speed_for: dict[str, str] = {
        "low": "faster",
        "medium": "medium",
        "high": "slow",
    }
    base = {
        "preset": speed_for.get(preset, "medium"),
        "look_ahead": "0",  # disable lookahead — adds latency for capture
    }
    if preset == "custom":
        base["b"] = f"{max(1000, int(kbps))}k"
        return base
    q = _QUALITY_FACTOR.get(preset, _QUALITY_FACTOR["high"])
    base["global_quality"] = str(q)
    return base


def _mf_options(preset: str, kbps: int) -> dict[str, str]:
    """Windows Media Foundation generic hardware encoder.

    Notably limited compared to vendor-specific paths — only a handful
    of options are exposed. The quality knob in libav's h264_mf binding
    is ``quality`` (0-100, higher = better), NOT ``quality_vs_speed``
    despite what some Microsoft docs call the underlying MF property.
    ``ffmpeg -h encoder=h264_mf`` confirms: ``rate_control``, ``scenario``,
    ``quality``, ``hw_encoding`` — no ``quality_vs_speed``.
    """
    quality_for: dict[str, int] = {
        "low": 30,
        "medium": 60,
        "high": 85,
    }
    base: dict[str, str] = {}
    if preset == "custom":
        base["rate_control"] = "cbr"
        base["b"] = f"{max(1000, int(kbps))}k"
        return base
    # Quality-VBR mode: 'quality' takes effect under rate_control=quality.
    base["rate_control"] = "quality"
    base["quality"] = str(quality_for.get(preset, 60))
    return base


def _libx264_options(preset: str, kbps: int) -> dict[str, str]:
    """Pure-software CPU encode.

    ``preset=ultrafast`` is essentially mandatory for live capture — even
    ``superfast`` on a fast desktop CPU struggles to encode 1080p60
    without dropping frames. ``tune=zerolatency`` disables B-frames and
    other look-ahead tricks that add latency. Quality at this preset is
    notably worse than hardware encoders at the same bitrate, but it's
    the only path that works on a machine with no usable hardware
    encoder at all.
    """
    base = {
        "preset": "ultrafast",
        "tune": "zerolatency",
    }
    if preset == "custom":
        base["b"] = f"{max(1000, int(kbps))}k"
        return base
    crf = _QUALITY_FACTOR.get(preset, _QUALITY_FACTOR["high"])
    base["crf"] = str(crf)
    return base
