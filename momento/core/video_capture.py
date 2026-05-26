"""Per-window video capture via Windows Graphics Capture API.

WGC delivers the game's window (not the desktop) as BGRA frames on a
background thread. We stash the latest frame under a lock, and a separate
clock-driven sender thread submits the latest BGRA buffer to the encoder
at exactly the configured framerate. This produces a CFR stream regardless
of whether WGC fires more or less often than the encoder rate.

Sequence:

    1. start() starts WGC and blocks until the FIRST frame arrives — the
       frame size is ground-truth (Win11 shadow regions mean GetWindowRect
       and the actual captured frame disagree, so we wait and learn).
    2. The first frame's size is locked, rounded DOWN to even dimensions
       (yuv420p subsampling needs even W/H).
    3. The sender thread starts, ticking at 1/framerate, submitting the
       latest BGRA frame to the encoder.
    4. stop() halts WGC and the sender thread.

Compared to the original TCP+ffmpeg path:
  * No subprocess, no TCP, no sendall back-pressure.
  * No cv2.cvtColor in the WGC callback (the encoder accepts BGRA and
    converts on-encode — same total CPU but in the encoder's worker thread
    instead of the WGC thread).
  * Submission is non-blocking: a slow encoder drops frames at the
    encoder's bounded queue, never stalls capture.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable

import numpy as np
from windows_capture import Frame, InternalCaptureControl, WindowsCapture

from momento.util.windows_api import is_window

logger = logging.getLogger(__name__)

# 100 ns ticks — WGC's time unit. 166666 ticks = 16.666 ms = 60 fps minimum.
# Without this, WGC only delivers frames on screen change, so static UI scenes
# (loading screens, menus, idle Notepad) produce no frames at all.
_MIN_UPDATE_INTERVAL_60FPS = 166666

_FIRST_FRAME_TIMEOUT = 5.0
# Many games (FFXIV, Path of Exile, etc.) launch in one window mode and switch
# to another in the first ~second — typically windowed → borderless fullscreen.
# If we lock the encoder dimensions to the FIRST frame we see, the rest of the
# recording is cropped to that initial size. To avoid this, prepare() observes
# the size for ``_SIZE_SETTLE_MS`` of "no change" before locking, capped at
# ``_SIZE_SETTLE_MAX_S`` total wait time so a game that resizes endlessly
# doesn't block the recorder forever.
_SIZE_SETTLE_MS = 500
_SIZE_SETTLE_MAX_S = 3.0


# Submission callback signature: (bgra_array, pts_seconds) -> bool (queued?).
FrameSink = Callable[[np.ndarray, float], bool]


class WindowVideoStreamer:
    """Captures one HWND via WGC and submits BGRA frames to an encoder sink.

    Frame pacing is driven by a Python clock at ``framerate`` fps, NOT by WGC.
    WGC fires its callback whenever the window content changes (or every 16.67
    ms via the minimum_update_interval cap); we just update a held "latest
    frame". A sender thread reads the latest frame each clock tick and calls
    the sink — duplicating during static moments, fresh during motion. The
    encoder gets a steady CFR stream and produces correct output timing.
    """

    def __init__(
        self,
        hwnd: int,
        *,
        framerate: int = 60,
        capture_cursor: bool = True,
    ) -> None:
        if not is_window(hwnd):
            raise ValueError(f"HWND {hwnd} is not a valid window")
        self._hwnd = hwnd
        self._sink: FrameSink | None = None
        self._capture_cursor = capture_cursor
        self._framerate = max(1, int(framerate))
        self._frame_interval = 1.0 / self._framerate

        self._capture: WindowsCapture | None = None
        self._sender_thread: threading.Thread | None = None

        self._stop_event = threading.Event()
        self._first_frame_event = threading.Event()
        self._started = False

        self._frame_size: tuple[int, int] | None = None
        # Latest captured frame as a contiguous BGRA ndarray, locked under
        # _frame_lock for read/write. The capture writes a new array; the
        # sender reads the reference and submits without copying.
        self._latest_frame: np.ndarray | None = None
        self._frame_lock = threading.Lock()
        # Latest raw (uncropped) WGC frame dimensions, sampled by prepare()
        # to detect when the window's size has stopped changing. Updated on
        # every frame, read by prepare()'s settling loop.
        self._latest_raw_size: tuple[int, int] | None = None
        self._frames_submitted = 0
        self._t0_monotonic: float | None = None

    # ---------------------------------------------------------- public API
    @property
    def frame_size(self) -> tuple[int, int]:
        if self._frame_size is None:
            raise RuntimeError("frame_size unknown — start() has not completed")
        return self._frame_size

    @property
    def width(self) -> int:
        return self.frame_size[0]

    @property
    def height(self) -> int:
        return self.frame_size[1]

    @property
    def frames_submitted(self) -> int:
        return self._frames_submitted

    def prepare(self) -> tuple[int, int]:
        """Start WGC and block until the first frame arrives.

        Returns the locked (width, height). Does NOT start the sender thread
        yet — call :meth:`start_sending` once the encoder is built and the
        sink is available.

        Raises TimeoutError if WGC doesn't produce a frame within
        ``_FIRST_FRAME_TIMEOUT`` seconds (usually means the window vanished or
        WGC isn't supported for that surface type).
        """
        if self._started:
            raise RuntimeError("WindowVideoStreamer already started")

        self._stop_event.clear()
        self._first_frame_event.clear()

        try:
            self._capture = WindowsCapture(
                cursor_capture=self._capture_cursor,
                draw_border=False,
                window_hwnd=self._hwnd,
                minimum_update_interval=_MIN_UPDATE_INTERVAL_60FPS,
                dirty_region=False,
            )
        except TypeError:
            # Older windows-capture signature without the optional kwargs.
            self._capture = WindowsCapture(
                cursor_capture=self._capture_cursor,
                draw_border=False,
                window_hwnd=self._hwnd,
            )

        cap = self._capture

        @cap.event
        def on_frame_arrived(frame: Frame, control: InternalCaptureControl) -> None:
            try:
                self._on_frame(frame, control)
            except Exception:
                logger.exception("WGC frame handler raised")

        @cap.event
        def on_closed() -> None:
            logger.info("WGC capture closed (window gone?)")
            self._stop_event.set()
            self._first_frame_event.set()  # unblock start()

        cap.start_free_threaded()

        if not self._first_frame_event.wait(timeout=_FIRST_FRAME_TIMEOUT):
            self._teardown()
            raise TimeoutError(
                f"WGC produced no frames within {_FIRST_FRAME_TIMEOUT:.1f}s for HWND {self._hwnd}"
            )
        if self._latest_raw_size is None:
            self._teardown()
            raise RuntimeError("WGC closed before first frame arrived")

        # Settle: poll the latest raw size until it's been stable for
        # _SIZE_SETTLE_MS, or until we've waited _SIZE_SETTLE_MAX_S total.
        # Catches the windowed→borderless transition that games trigger
        # within the first second of running.
        deadline = time.monotonic() + _SIZE_SETTLE_MAX_S
        last_size = self._latest_raw_size
        last_change_at = time.monotonic()
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                break
            cur = self._latest_raw_size
            if cur is None:
                time.sleep(0.05)
                continue
            if cur != last_size:
                logger.info(
                    "WGC window resized during settle: %dx%d -> %dx%d",
                    last_size[0], last_size[1], cur[0], cur[1],
                )
                last_size = cur
                last_change_at = time.monotonic()
            elif (time.monotonic() - last_change_at) * 1000 >= _SIZE_SETTLE_MS:
                break
            time.sleep(0.05)

        # Lock the locked-and-even size now.
        raw_w, raw_h = last_size
        even_w = raw_w - (raw_w & 1)
        even_h = raw_h - (raw_h & 1)
        self._frame_size = (even_w, even_h)
        if (even_w, even_h) != (raw_w, raw_h):
            logger.info(
                "Locked capture size: %dx%d (cropped from %dx%d for yuv420p)",
                even_w, even_h, raw_w, raw_h,
            )
        else:
            logger.info("Locked capture size: %dx%d", even_w, even_h)

        logger.info(
            "WindowVideoStreamer prepared: hwnd=%d size=%dx%d framerate=%d",
            self._hwnd, self.width, self.height, self._framerate,
        )
        return self._frame_size

    def start_sending(self, sink: FrameSink) -> None:
        """Begin the clock-driven sender thread that pushes frames to ``sink``."""
        if self._started:
            raise RuntimeError("WindowVideoStreamer already sending")
        if self._frame_size is None:
            raise RuntimeError("prepare() must succeed before start_sending()")
        self._sink = sink
        self._t0_monotonic = time.monotonic()
        self._sender_thread = threading.Thread(
            target=self._sender_loop, name="VideoSender", daemon=True
        )
        self._sender_thread.start()
        self._started = True

    def stop(self, timeout: float = 3.0) -> None:
        # Treat stop as the canonical teardown for both prepared and
        # actively-sending states.
        self._teardown(timeout=timeout)
        self._started = False

    # ---------------------------------------------------------- internals
    def _teardown(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        self._first_frame_event.set()
        self._capture = None
        if self._sender_thread is not None and self._sender_thread.is_alive():
            self._sender_thread.join(timeout=timeout)
        self._sender_thread = None
        self._started = False
        logger.info("WindowVideoStreamer stopped (submitted %d frames)", self._frames_submitted)

    def _on_frame(self, frame: Frame, control: InternalCaptureControl) -> None:
        """WGC callback — stash the latest frame. Sender thread submits."""
        if self._stop_event.is_set():
            try:
                control.stop()
            except Exception:
                pass
            return

        raw_w, raw_h = frame.width, frame.height
        self._latest_raw_size = (raw_w, raw_h)
        self._first_frame_event.set()

        if self._frame_size is None:
            # prepare() hasn't locked yet — settling loop is still running.
            # Frames captured during this window are intentionally dropped:
            # the resolution they belong to may not match what we end up
            # encoding at, and the buffer at this stage isn't shaped right
            # to feed the encoder anyway.
            return

        w_locked, h_locked = self._frame_size
        if raw_w < w_locked or raw_h < h_locked:
            # Window shrank below locked size mid-recording — produce a
            # padded frame so the stream stays valid instead of dropping.
            buf = frame.frame_buffer
            # Pad to locked dims with black (BGRA: zeroes; alpha 0xff for
            # opacity in case the encoder uses it as a hint).
            padded = np.zeros((h_locked, w_locked, 4), dtype=buf.dtype)
            padded[:raw_h, :raw_w] = buf
            padded[..., 3] = 0xFF
            snapshot = padded
        else:
            buf = frame.frame_buffer
            if (raw_w, raw_h) != (w_locked, h_locked):
                buf = buf[:h_locked, :w_locked]
            # np.ascontiguousarray copies — necessary because
            # frame.frame_buffer is a view onto WGC's internal staging
            # memory that the next frame will overwrite.
            snapshot = np.ascontiguousarray(buf)

        with self._frame_lock:
            self._latest_frame = snapshot

    def _sender_loop(self) -> None:
        """Submit the latest captured frame to the encoder sink at framerate.

        We pass pts_seconds=None to the sink so the encoder stamps each
        submission with its own wallclock reference — a single shared t0
        across video + mic + system audio is the only way to keep the
        three streams aligned in the output container.
        """
        interval = self._frame_interval
        next_tick = time.perf_counter()
        sink = self._sink
        if sink is None:
            return

        while not self._stop_event.is_set():
            with self._frame_lock:
                frame = self._latest_frame
            if frame is not None:
                try:
                    sink(frame, None)
                    self._frames_submitted += 1
                except Exception:
                    logger.exception("Video sink raised; ending sender loop")
                    return

            next_tick += interval
            sleep_for = next_tick - time.perf_counter()
            if sleep_for > 0:
                if self._stop_event.wait(timeout=sleep_for):
                    return
            else:
                # Falling behind. Skip the deficit instead of accumulating lag.
                next_tick = time.perf_counter()


def wait_for_window(pid: int, timeout: float = 10.0, poll_interval: float = 0.25) -> int | None:
    """Wait for ``pid`` (or any of its children) to create a main window.

    Useful right after a game launch — psutil sees the process before its
    window exists. Returns the HWND or None on timeout.
    """
    from momento.util.windows_api import find_main_hwnd_for_pid_with_children

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hwnd = find_main_hwnd_for_pid_with_children(pid)
        if hwnd is not None:
            return hwnd
        time.sleep(poll_interval)
    return None


__all__: Iterable[str] = ("WindowVideoStreamer", "FrameSink", "wait_for_window")
