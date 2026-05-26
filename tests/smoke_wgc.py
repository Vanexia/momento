"""Verify windows_capture can grab frames from notepad."""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

user32 = ctypes.windll.user32


def find_main_window(pid: int) -> int | None:
    """Return the HWND of the largest visible top-level window owned by pid."""
    hwnds: list[tuple[int, int]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd: int, _lparam: int) -> bool:
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value == pid and user32.IsWindowVisible(hwnd):
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            area = (rect.right - rect.left) * (rect.bottom - rect.top)
            if area > 1000:
                hwnds.append((hwnd, area))
        return True

    user32.EnumWindows(cb, 0)
    if not hwnds:
        return None
    hwnds.sort(key=lambda x: -x[1])
    return hwnds[0][0]


def main() -> int:
    import psutil
    import subprocess
    from windows_capture import Frame, InternalCaptureControl, WindowsCapture

    print("Spawning notepad ...")
    proc = subprocess.Popen(["notepad.exe"], creationflags=subprocess.CREATE_NO_WINDOW)
    time.sleep(1.5)  # let notepad create its window

    # The launcher's PID may not own the visible window (Windows 11 Notepad
    # spawns a child Notepad.exe). Search this PID first, then children.
    hwnd = find_main_window(proc.pid)
    if hwnd is None:
        try:
            for child in psutil.Process(proc.pid).children(recursive=True):
                hwnd = find_main_window(child.pid)
                if hwnd:
                    break
        except psutil.NoSuchProcess:
            pass
    if hwnd is None:
        # Fall back: look for any visible Notepad.exe window on the system
        for p in psutil.process_iter(["name", "pid"]):
            try:
                if (p.info.get("name") or "").lower() == "notepad.exe":
                    hwnd = find_main_window(p.info["pid"])
                    if hwnd:
                        break
            except psutil.NoSuchProcess:
                pass
    if hwnd is None:
        print("FAIL: could not locate notepad HWND")
        proc.kill()
        return 2

    print(f"Capturing HWND={hwnd}")

    frames_seen = 0
    first_size: tuple[int, int] | None = None
    out_png = Path("C:/dev/Momento/recordings/wgc_smoke.png").resolve()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    stop_evt = threading.Event()

    capture = WindowsCapture(
        cursor_capture=False,
        draw_border=False,
        window_hwnd=hwnd,
    )

    @capture.event
    def on_frame_arrived(frame: Frame, control: InternalCaptureControl) -> None:
        nonlocal frames_seen, first_size
        frames_seen += 1
        if first_size is None:
            first_size = (frame.width, frame.height)
            print(f"  first frame: {frame.width}x{frame.height} buffer.shape={frame.frame_buffer.shape}")
            try:
                frame.save_as_image(str(out_png))
                print(f"  saved sample frame to {out_png}")
            except Exception as e:
                print(f"  could not save sample: {e}")
        if frames_seen >= 30 or stop_evt.is_set():
            try:
                control.stop()
            except Exception:
                pass

    @capture.event
    def on_closed() -> None:
        stop_evt.set()
        print("  capture closed")

    capture.start_free_threaded()

    # Run for up to 3 seconds, then stop and clean up.
    for _ in range(30):
        if stop_evt.is_set():
            break
        time.sleep(0.1)
    stop_evt.set()
    time.sleep(0.3)

    try:
        psutil.Process(hwnd_to_pid(hwnd)).kill()
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass

    print(f"Frames seen: {frames_seen}")
    return 0 if frames_seen > 0 else 3


def hwnd_to_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


if __name__ == "__main__":
    sys.exit(main())
