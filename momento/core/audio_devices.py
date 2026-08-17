"""Shared PortAudio (WASAPI) audio-device layer.

Replaces the ``soundcard`` library. ``soundcard``'s Windows backend hard-asserted
that every capture device exposes a 32-bit-float ``WAVEFORMATEXTENSIBLE`` mix
format and crashed with a *bare* ``AssertionError`` on the equally-valid
``WAVE_FORMAT_IEEE_FLOAT`` representation, which many pro interfaces, USB
microphones, and virtual cables report. The user
saw that as "Mic test stopped:" with no detail, and recordings silently dropped
the mic. PortAudio negotiates the device format through WASAPI, so *any* device
Windows exposes can be captured, and WASAPI loopback gives us system audio off
any output endpoint.

Sample rate: PortAudio's WASAPI *shared-mode* path does NOT resample — a stream
must be opened at the endpoint's native mix-format rate or the open fails
(``Invalid sample rate``, -9997). So we open at the device's ``defaultSampleRate``
and let the encoder's filter graph aresample every input to 48 kHz (the encoder
already accepts per-input ``mic_sample_rate`` / ``sys_sample_rate``). Pinning
48 kHz would silently drop audio from every 44.1 kHz mic/headset/DAC.

PyAudio identifies devices by an unstable integer index, so we persist devices
by their friendly *name* (for example, ``"USB Microphone"``).
Configs written by the old soundcard build stored WASAPI endpoint IDs
(``"{0.0.1.00000000}.{guid}"``); :func:`friendly_name_for_endpoint_id` maps
those to names so a saved selection survives the upgrade.

Everything here is Windows/WASAPI-specific, matching the rest of Momento.
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading

import numpy as np
import pyaudiowpatch as pyaudio

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_RATE = 48000
DEFAULT_CHANNELS = 2

# PyAudio names a loopback capture of an output endpoint "<endpoint> [Loopback]".
_LOOPBACK_SUFFIX = " [Loopback]"

# Serialises PortAudio API calls that touch global/host-API state — Pa_Initialize
# / Pa_Terminate, device enumeration, and Pa_OpenStream. (Pa_ReadStream on an
# already-open stream is NOT held under this lock — concurrent reads on distinct
# streams are PortAudio's supported steady state.) This makes it safe for the GUI
# to enumerate devices while a capture thread opens or holds one.
_pa_lock = threading.Lock()


@contextlib.contextmanager
def pyaudio_session():
    """Yield a PyAudio instance and guarantee ``terminate()``.

    PyAudio refcounts Pa_Initialize, so it is safe to have several live at once
    (the GUI enumerating while capture threads stream).
    """
    with _pa_lock:
        p = pyaudio.PyAudio()
    try:
        yield p
    finally:
        with _pa_lock:
            with contextlib.suppress(Exception):
                p.terminate()


def _wasapi_info(p):
    return p.get_host_api_info_by_type(pyaudio.paWASAPI)


# --------------------------------------------------------------- enumeration
def list_input_device_names(p) -> list[tuple[str, bool]]:
    """Real WASAPI capture devices as ``(name, is_default)``, default first.

    Loopback shadow devices are excluded — output endpoints are surfaced
    separately by :func:`list_output_device_names` and looped back internally.
    """
    with _pa_lock:
        info = _wasapi_info(p)
        hi = info["index"]
        default_idx = info.get("defaultInputDevice", -1)
        out: list[tuple[str, bool]] = []
        seen: set[str] = set()
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["hostApi"] != hi or d["maxInputChannels"] <= 0:
                continue
            name = d["name"]
            if name.endswith(_LOOPBACK_SUFFIX) or name in seen:
                continue
            seen.add(name)
            out.append((name, i == default_idx))
    out.sort(key=lambda t: (not t[1], t[0].lower()))
    return out


def list_output_device_names(p) -> list[tuple[str, bool]]:
    """Real WASAPI output endpoints as ``(name, is_default)``, default first.

    These are what the user picks as the system-audio source; we open their
    loopback under the hood.
    """
    with _pa_lock:
        info = _wasapi_info(p)
        hi = info["index"]
        default_idx = info.get("defaultOutputDevice", -1)
        out: list[tuple[str, bool]] = []
        seen: set[str] = set()
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["hostApi"] != hi or d["maxOutputChannels"] <= 0:
                continue
            name = d["name"]
            if name.endswith(_LOOPBACK_SUFFIX) or name in seen:
                continue
            seen.add(name)
            out.append((name, i == default_idx))
    out.sort(key=lambda t: (not t[1], t[0].lower()))
    return out


def _find_capture_device(p, name: str, *, loopback: bool) -> tuple[int, int, float] | None:
    """``(index, max_input_channels, default_sample_rate)`` for a capturable
    WASAPI device, or None. For ``loopback`` we look for the
    ``"<name> [Loopback]"`` shadow device that captures the output endpoint.

    Caller must hold ``_pa_lock``.
    """
    info = _wasapi_info(p)
    hi = info["index"]
    target = name + _LOOPBACK_SUFFIX if loopback else name
    for i in range(p.get_device_count()):
        d = p.get_device_info_by_index(i)
        if d["hostApi"] != hi or d["maxInputChannels"] <= 0:
            continue
        if d["name"] == target:
            return i, int(d["maxInputChannels"]), float(d["defaultSampleRate"])
    return None


# -------------------------------------------------------------------- opening
def open_input_stream(
    p,
    name: str,
    sample_rate: int,
    channels: int,
    frames_per_buffer: int,
    *,
    loopback: bool = False,
):
    """Open a capture stream; return ``(stream, native_channels, rate)``.

    Opens float32 at ``sample_rate`` — or, when ``sample_rate`` is falsy, at the
    device's native ``defaultSampleRate`` (PortAudio shared mode can't resample,
    so the rate must match the endpoint). Falls back through fewer channels so a
    mono mic still opens; the caller up/down-mixes to the encoder's channel count
    via :func:`to_channels`.
    """
    with _pa_lock:
        found = _find_capture_device(p, name, loopback=loopback)
        if found is None:
            what = "loopback for" if loopback else "device"
            raise ValueError(f"Audio {what} not found: {name!r}")
        index, max_in, default_rate = found
        rate = int(sample_rate) if sample_rate else int(round(default_rate))

        # Try the requested channel count, then the device max, then mono.
        candidates: list[int] = []
        for c in (min(channels, max_in), max_in, 1):
            c = max(1, int(c))
            if c not in candidates:
                candidates.append(c)

        last_err: Exception | None = None
        for open_ch in candidates:
            try:
                stream = p.open(
                    format=pyaudio.paFloat32,
                    channels=open_ch,
                    rate=rate,
                    input=True,
                    input_device_index=index,
                    frames_per_buffer=int(frames_per_buffer),
                )
                return stream, open_ch, rate
            except Exception as e:  # noqa: BLE001 — try the next channel layout
                last_err = e
                logger.info(
                    "Open %r at %d ch / %d Hz failed (%s); trying next layout",
                    name, open_ch, rate, _format_error(e),
                )
        assert last_err is not None
        raise last_err


def open_output_stream(p, name: str, sample_rate: int, channels: int, frames_per_buffer: int):
    """Open a playback stream on a named WASAPI output endpoint (mic monitor).

    Returns ``(stream, channels, rate)``. ``sample_rate`` falsy -> device native.
    """
    with _pa_lock:
        info = _wasapi_info(p)
        hi = info["index"]
        index = None
        max_out = 2
        default_rate = float(DEFAULT_SAMPLE_RATE)
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["hostApi"] == hi and d["maxOutputChannels"] > 0 and d["name"] == name:
                index = i
                max_out = int(d["maxOutputChannels"])
                default_rate = float(d["defaultSampleRate"])
                break
        if index is None:
            raise ValueError(f"Output device not found: {name!r}")
        open_ch = max(1, min(channels, max_out))
        rate = int(sample_rate) if sample_rate else int(round(default_rate))
        stream = p.open(
            format=pyaudio.paFloat32,
            channels=open_ch,
            rate=rate,
            output=True,
            output_device_index=index,
            frames_per_buffer=int(frames_per_buffer),
        )
        return stream, open_ch, rate


def native_sample_rate(name_or_id: str, *, loopback: bool) -> int | None:
    """The device's native mix-format rate, resolved the same way the streamer
    resolves it (so the encoder is told the exact rate the stream opens at).
    None if the device can't be found.
    """
    if loopback:
        from momento.core.audio_loopback import resolve_loopback_device

        dev = resolve_loopback_device(name_or_id)
    else:
        from momento.core.mic_capture import resolve_mic_device

        dev = resolve_mic_device(name_or_id)
    if dev is None:
        return None
    try:
        with pyaudio_session() as p:
            with _pa_lock:
                found = _find_capture_device(p, dev.id, loopback=loopback)
    except Exception:
        logger.exception("native_sample_rate failed for %r", name_or_id)
        return None
    return int(round(found[2])) if found else None


def default_output_name(p) -> str | None:
    with _pa_lock:
        info = _wasapi_info(p)
        idx = info.get("defaultOutputDevice", -1)
        if idx is None or idx < 0:
            return None
        try:
            return p.get_device_info_by_index(idx)["name"]
        except Exception:
            return None


# ------------------------------------------------------------ channel mixing
def to_channels(arr: np.ndarray, target: int) -> np.ndarray:
    """Up/down-mix a float32 ``(frames, src)`` block to ``target`` channels."""
    if arr.ndim == 1:
        arr = arr.reshape(-1, 1)
    src = arr.shape[1]
    if src == target:
        return arr
    if src == 1:
        return np.repeat(arr, target, axis=1)
    if target == 1:
        return arr.mean(axis=1, keepdims=True).astype(np.float32, copy=False)
    if target == 2 and src > 2:
        # Windows/WASAPI uses the standard speaker order. Preserve centre
        # dialogue and surround effects instead of truncating everything after
        # FL/FR. Normalising each output row keeps full-scale input bounded.
        weights = np.zeros((2, src), dtype=np.float32)
        weights[0, 0] = 1.0  # front left
        weights[1, 1] = 1.0  # front right
        if src == 3:  # FL, FR, FC
            weights[:, 2] = 0.707
        elif src == 4:  # FL, FR, back left, back right
            weights[0, 2] = 0.707
            weights[1, 3] = 0.707
        elif src == 5:  # FL, FR, FC, back left, back right
            weights[:, 2] = 0.707
            weights[0, 3] = 0.707
            weights[1, 4] = 0.707
        else:  # 5.1 / 6.1 / 7.1 and any extra channels
            weights[:, 2] = 0.707  # centre
            weights[:, 3] = 0.5    # LFE
            if src == 7:  # back centre, side left, side right
                weights[:, 4] = 0.5
                weights[0, 5] = 0.707
                weights[1, 6] = 0.707
            else:
                weights[0, 4] = 0.707  # back left
                weights[1, 5] = 0.707  # back right
                if src >= 8:
                    weights[0, 6] = 0.707  # side left
                    weights[1, 7] = 0.707  # side right
                if src > 8:
                    weights[:, 8:] = 0.25
        weights /= weights.sum(axis=1, keepdims=True)
        return np.ascontiguousarray(arr.astype(np.float32, copy=False) @ weights.T)
    if src > target:
        return np.ascontiguousarray(arr[:, :target])
    reps = (target + src - 1) // src
    return np.ascontiguousarray(np.tile(arr, (1, reps))[:, :target])


# ------------------------------------------------------ probe (first-run seed)
def probe_open(p, name: str, *, loopback: bool) -> bool:
    """True if a capture stream on ``name`` opens at its native rate.

    Used by first-run seeding to skip enumerated-but-unopenable endpoints. We
    deliberately do NOT read a frame: an idle/cold WASAPI loopback endpoint
    delivers no first frame, so a blocking read here would hang app startup
    (seeding runs synchronously before the tray appears). A successful open is a
    sufficient openable signal — PortAudio has already negotiated the format.
    """
    try:
        stream, _ch, _rate = open_input_stream(
            p, name, 0, DEFAULT_CHANNELS, 480, loopback=loopback
        )
    except Exception:
        return False
    with contextlib.suppress(Exception):
        stream.stop_stream()
    with contextlib.suppress(Exception):
        stream.close()
    return True


# ----------------------------------------------- legacy endpoint-id migration
# Old soundcard configs stored WASAPI endpoint IDs: "{0.0.<flow>.<...>}.{guid}"
# where flow 1 = capture, 0 = render (WASAPI only ever uses 0 or 1). We rebuild
# the device's full friendly name from the MMDevices registry ("<endpoint desc>
# (<adapter>)"), which matches the name PortAudio reports, so a saved selection
# migrates to the new name scheme.
_ENDPOINT_RE = re.compile(r"^\{0\.0\.([01])\.[0-9a-fA-F]+\}\.(\{[0-9a-fA-F\-]+\})$")
_PK_ENDPOINT_DESC = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
_PK_ADAPTER_NAME = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"


def looks_like_endpoint_id(value: str | None) -> bool:
    return bool(value) and bool(_ENDPOINT_RE.match(value))


def friendly_name_for_endpoint_id(endpoint_id: str | None) -> str | None:
    """Map an old soundcard WASAPI endpoint id to its PortAudio friendly name."""
    if not endpoint_id:
        return None
    m = _ENDPOINT_RE.match(endpoint_id)
    if not m:
        return None
    flow = "Capture" if m.group(1) == "1" else "Render"  # regex guarantees 0/1
    guid = m.group(2)
    import winreg

    base = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio"
        rf"\{flow}\{guid}\Properties"
    )
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as key:
            desc = _reg_str(key, _PK_ENDPOINT_DESC)
            adapter = _reg_str(key, _PK_ADAPTER_NAME)
    except OSError:
        return None
    if desc and adapter:
        return f"{desc} ({adapter})"
    return desc or None


def _reg_str(key, value_name: str) -> str | None:
    import winreg

    try:
        v, _ = winreg.QueryValueEx(key, value_name)
    except OSError:
        return None
    return v if isinstance(v, str) and v.strip() else None


def _format_error(exc: Exception) -> str:
    text = str(exc).strip()
    return text or repr(exc)
