"""Per-window video capture via Windows Graphics Capture API.

WGC delivers the game's window (not the desktop) as BGRA frames on a
background thread. We stash the latest frame under a lock, and a separate
clock-driven sender thread submits the latest BGRA buffer to the encoder
at exactly the configured framerate. This produces a CFR stream regardless
of whether WGC fires more or less often than the encoder rate.

Sequence:

    1. prepare() starts WGC and blocks until the FIRST frame arrives — the
       frame size is ground-truth (Win11 shadow regions mean GetWindowRect
       and the actual captured frame disagree, so we wait and learn).
    2. The first frame's size is locked, rounded DOWN to even dimensions
       (4:2:0 subsampling needs even W/H).
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

# ``windows-capture`` defines this value in milliseconds. Without it, WGC only
# delivers frames on screen change, so static UI scenes can produce no frames.
_MIN_UPDATE_INTERVAL_60FPS_MS = 16

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
_SCHEDULER_MISS_WARN_RATE = 0.01
_SCHEDULER_HITCH_WARN_S = 0.1


# Submission callback signature: (bgra_array, pts_seconds) -> bool (queued?).
FrameSink = Callable[[np.ndarray, float | None], bool]
PtsClock = Callable[[], float]


def _advance_sender_deadline(
    previous: float,
    interval: float,
    *,
    now: float,
) -> tuple[float, int, float]:
    """Advance to a future deadline and report elapsed frame slots."""
    deadline = previous + interval
    first_deadline = deadline
    missed = 0
    if deadline <= now:
        missed = int((now - deadline) // interval) + 1
        deadline += missed * interval
    if deadline <= now:
        deadline += interval
        missed += 1
    lateness = max(0.0, now - first_deadline) if missed else 0.0
    return deadline, missed, lateness


def _next_sender_deadline(previous: float, interval: float, *, now: float) -> float:
    """Return the next future frame deadline, skipping every elapsed slot."""
    return _advance_sender_deadline(previous, interval, now=now)[0]


def _owned_frame_at_size(buffer: np.ndarray, width: int, height: int) -> np.ndarray:
    """Copy a BGRA frame into the encoder's fixed dimensions."""
    raw_h, raw_w = buffer.shape[:2]
    if raw_w < width or raw_h < height:
        copy_h = min(raw_h, height)
        copy_w = min(raw_w, width)
        snapshot = np.zeros((height, width, 4), dtype=buffer.dtype)
        snapshot[:copy_h, :copy_w] = buffer[:copy_h, :copy_w]
        snapshot[..., 3] = 0xFF
        return snapshot
    return np.array(buffer[:height, :width], copy=True, order="C")


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
        self._capture_control: object | None = None
        self._capture_monitor_thread: threading.Thread | None = None
        self._sender_thread: threading.Thread | None = None
        self._pts_clock: PtsClock | None = None

        self._stop_event = threading.Event()
        self._first_frame_event = threading.Event()
        self._started = False

        self._frame_size: tuple[int, int] | None = None
        # Latest captured frame as a contiguous BGRA ndarray, locked under
        # _frame_lock for read/write. The capture writes a new array; the
        # sender reads the reference and submits without copying.
        self._latest_frame: np.ndarray | None = None
        self._settling_frame: np.ndarray | None = None
        self._frame_lock = threading.Lock()
        # Latest raw (uncropped) WGC frame dimensions, sampled by prepare()
        # to detect when the window's size has stopped changing. Updated on
        # every frame, read by prepare()'s settling loop.
        self._latest_raw_size: tuple[int, int] | None = None
        self._frames_submitted = 0
        self._scheduler_slots_missed = 0
        self._scheduler_late_events = 0
        self._scheduler_max_lateness_s = 0.0
        # Fired (on the WGC thread) when the captured window is destroyed —
        # i.e. the game closed. This happens the instant the window goes,
        # well before the process exits for games that linger on shutdown
        # (WoW can take ~30s). Lets the recorder finalise promptly instead of
        # waiting for the game-watcher to notice process death. Set by the
        # owner before prepare(); only meaningful for a live recording.
        self.on_window_closed: Callable[[], None] | None = None
        # A live WGC session can end while the HWND remains valid (for example
        # after a graphics-driver reset). Surface that separately so the owner
        # can finalise and retry instead of repeating the last frame forever.
        self.on_capture_failed: Callable[[Exception], None] | None = None
        self._intentional_stop = False
        self._terminal_callback_lock = threading.Lock()
        self._terminal_callback_sent = False

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

    @property
    def scheduled_slots(self) -> int:
        return self._frames_submitted + self._scheduler_slots_missed

    @property
    def scheduler_slots_missed(self) -> int:
        return self._scheduler_slots_missed

    @property
    def scheduler_late_events(self) -> int:
        return self._scheduler_late_events

    @property
    def scheduler_max_lateness_ms(self) -> float:
        return self._scheduler_max_lateness_s * 1000.0

    @property
    def scheduler_miss_rate(self) -> float:
        return (
            self._scheduler_slots_missed / self.scheduled_slots
            if self.scheduled_slots
            else 0.0
        )

    @property
    def pacing_degraded(self) -> bool:
        return (
            self.scheduler_miss_rate >= _SCHEDULER_MISS_WARN_RATE
            or self._scheduler_max_lateness_s >= _SCHEDULER_HITCH_WARN_S
        )

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
        self._intentional_stop = False
        with self._terminal_callback_lock:
            self._terminal_callback_sent = False
        self._latest_raw_size = None
        with self._frame_lock:
            self._latest_frame = None
            self._settling_frame = None

        try:
            self._capture = WindowsCapture(
                cursor_capture=self._capture_cursor,
                draw_border=False,
                window_hwnd=self._hwnd,
                minimum_update_interval=_MIN_UPDATE_INTERVAL_60FPS_MS,
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
            logger.info("WGC capture closed")
            self._notify_capture_ended(
                RuntimeError("Windows Graphics Capture closed unexpectedly")
            )

        self._capture_control = cap.start_free_threaded()
        if self._capture_control is not None and hasattr(self._capture_control, "wait"):
            control = self._capture_control
            self._capture_monitor_thread = threading.Thread(
                target=self._monitor_capture_control,
                args=(control,),
                name="WGCCaptureMonitor",
                daemon=True,
            )
            self._capture_monitor_thread.start()

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

        if self._stop_event.is_set():
            self._teardown()
            raise RuntimeError("WGC closed while capture size was settling")

        # Lock the locked-and-even size now.
        raw_w, raw_h = last_size
        even_w = raw_w - (raw_w & 1)
        even_h = raw_h - (raw_h & 1)
        self._frame_size = (even_w, even_h)
        with self._frame_lock:
            settling_frame = self._settling_frame
            self._settling_frame = None
        if settling_frame is not None:
            seeded_frame = _owned_frame_at_size(settling_frame, even_w, even_h)
            with self._frame_lock:
                if self._latest_frame is None:
                    self._latest_frame = seeded_frame
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

    def start_sending(
        self,
        sink: FrameSink,
        *,
        pts_clock: PtsClock | None = None,
    ) -> None:
        """Begin the clock-driven sender thread that pushes frames to ``sink``."""
        if self._started:
            raise RuntimeError("WindowVideoStreamer already sending")
        if self._frame_size is None:
            raise RuntimeError("prepare() must succeed before start_sending()")
        self._sink = sink
        self._pts_clock = pts_clock
        self._frames_submitted = 0
        self._scheduler_slots_missed = 0
        self._scheduler_late_events = 0
        self._scheduler_max_lateness_s = 0.0
        self._sender_thread = threading.Thread(
            target=self._sender_loop, name="VideoSender", daemon=True
        )
        self._sender_thread.start()
        self._started = True

    def startup_frame(self) -> np.ndarray:
        """Return an owned prepared frame for synchronous encoder priming."""
        if self._frame_size is None:
            raise RuntimeError("prepare() must succeed before requesting a startup frame")
        with self._frame_lock:
            frame = self._latest_frame
            if frame is None:
                raise RuntimeError("WGC has no prepared startup frame")
            return np.array(frame, copy=True, order="C")

    def stop(self, timeout: float = 3.0) -> None:
        # Treat stop as the canonical teardown for both prepared and
        # actively-sending states.
        self._teardown(timeout=timeout)
        self._started = False

    # ---------------------------------------------------------- internals
    def _teardown(self, timeout: float = 3.0) -> None:
        # Mark this as an intentional stop so the WGC on_closed event (which
        # fires as we release the capture) doesn't re-trigger on_window_closed.
        self._intentional_stop = True
        self._stop_event.set()
        self._first_frame_event.set()
        control = self._capture_control
        if control is not None:
            try:
                control.stop()
            except Exception:
                logger.debug("WGC capture control was already stopped", exc_info=True)
        # CaptureControl.wait() can take seconds to return after stop() on a
        # real WGC session. The monitor is observational and daemonized, so it
        # must never hold up audio shutdown or extend the recording timeline.
        self._capture_control = None
        self._capture_monitor_thread = None
        self._capture = None
        if self._sender_thread is not None and self._sender_thread.is_alive():
            self._sender_thread.join(timeout=timeout)
        self._sender_thread = None
        self._started = False
        log = logger.warning if self.pacing_degraded else logger.info
        log(
            "WindowVideoStreamer stopped: submitted=%d; scheduler misses=%d/%d "
            "(%.2f%%); late events=%d; max lateness=%.1fms",
            self._frames_submitted,
            self._scheduler_slots_missed,
            self.scheduled_slots,
            self.scheduler_miss_rate * 100.0,
            self._scheduler_late_events,
            self.scheduler_max_lateness_ms,
        )

    def _monitor_capture_control(self, control: object) -> None:
        """Notice native WGC termination even when no close callback arrives."""
        try:
            control.wait()
        except Exception as exc:
            self._notify_capture_ended(
                RuntimeError(f"Windows Graphics Capture control failed: {exc}")
            )
            return
        self._notify_capture_ended(
            RuntimeError("Windows Graphics Capture thread stopped unexpectedly")
        )

    def _notify_capture_ended(self, failure: Exception) -> None:
        """Classify one unexpected native capture end and notify the owner."""
        if self._intentional_stop or self._stop_event.is_set():
            return
        with self._terminal_callback_lock:
            if self._terminal_callback_sent:
                return
            self._terminal_callback_sent = True

        window_vanished = not is_window(self._hwnd)
        self._stop_event.set()
        self._first_frame_event.set()
        if window_vanished:
            callback = self.on_window_closed
            callback_name = "on_window_closed"
            args: tuple[object, ...] = ()
        else:
            logger.error("WGC ended while HWND %d was still valid: %s", self._hwnd, failure)
            callback = self.on_capture_failed
            callback_name = "on_capture_failed"
            args = (failure,)
        if callback is not None:
            try:
                callback(*args)
            except Exception:
                logger.exception("%s callback raised", callback_name)

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

        if self._frame_size is None:
            # Preserve the newest owned startup frame. A static window may never
            # repaint after prepare() locks its dimensions; in that case this
            # becomes the sender's initial held frame.
            settling_frame = np.array(frame.frame_buffer, copy=True, order="C")
            with self._frame_lock:
                if self._frame_size is None:
                    self._settling_frame = settling_frame
                else:
                    width, height = self._frame_size
                    self._latest_frame = _owned_frame_at_size(
                        settling_frame, width, height
                    )
            self._first_frame_event.set()
            return

        w_locked, h_locked = self._frame_size
        snapshot = _owned_frame_at_size(frame.frame_buffer, w_locked, h_locked)

        with self._frame_lock:
            self._latest_frame = snapshot
        self._first_frame_event.set()

    def _sender_loop(self) -> None:
        """Submit the latest captured frame to the encoder sink at framerate.

        When the caller supplies ``pts_clock``, scheduled frame deadlines are
        mapped onto that shared A/V clock. Scheduler jitter therefore changes
        wake time without creating duplicate timestamps or stretching video.
        """
        interval = self._frame_interval
        deadline_origin = time.perf_counter()
        next_tick = deadline_origin
        pts_origin = self._pts_clock() if self._pts_clock is not None else None
        sink = self._sink
        if sink is None:
            return

        while not self._stop_event.is_set():
            with self._frame_lock:
                frame = self._latest_frame
            if frame is not None:
                try:
                    pts = (
                        None
                        if pts_origin is None
                        else pts_origin + (next_tick - deadline_origin)
                    )
                    sink(frame, pts)
                    self._frames_submitted += 1
                except Exception:
                    logger.exception("Video sink raised; ending sender loop")
                    return

            now = time.perf_counter()
            next_tick, missed, lateness = _advance_sender_deadline(
                next_tick,
                interval,
                now=now,
            )
            if missed:
                self._scheduler_slots_missed += missed
                self._scheduler_late_events += 1
                self._scheduler_max_lateness_s = max(
                    self._scheduler_max_lateness_s,
                    lateness,
                )
            if self._stop_event.wait(timeout=next_tick - now):
                return


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


__all__: Iterable[str] = ("WindowVideoStreamer", "FrameSink", "PtsClock", "wait_for_window")
