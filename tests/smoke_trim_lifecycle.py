r"""Focused regressions for trim output and editor file-lifecycle safety.

Run:
    C:\dev\Momento\.venv\Scripts\python.exe tests\smoke_trim_lifecycle.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import momento.trim.ffmpeg_trim as ffmpeg_trim  # noqa: E402
import momento.ui.editor as editor_module  # noqa: E402
from momento.trim.ffmpeg_trim import TrimWorker  # noqa: E402
from momento.ui.editor import EditorWindow, _list_recordings  # noqa: E402
from momento.util.ffmpeg_path import ffmpeg_exe  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv[:1])
_results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    _results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} - {name}")


class _FakeProcess:
    def __init__(self, args, *, returncode: int, payload: bytes, on_read=None) -> None:
        self.target = Path(args[-1])
        self.target.write_bytes(payload)
        self.returncode = returncode
        self._on_read = on_read
        self._terminated = False
        self.stderr = self
        self._iterated = False

    def __iter__(self):
        return self

    def __next__(self) -> str:
        if self._iterated:
            raise StopIteration
        self._iterated = True
        if self._on_read is not None:
            self._on_read()
        return "frame=1 time=00:00:01.00\n"

    def poll(self):
        return self.returncode if self._terminated else None

    def terminate(self) -> None:
        self._terminated = True
        self.returncode = -15

    def wait(self) -> int:
        return self.returncode


def _run_worker_with_fake_process(
    tmp: Path, *, returncode: int, cancel_during_read: bool = False
) -> tuple[Path, Path | None, list[str], list[str]]:
    src = tmp / "source.mkv"
    src.write_bytes(b"source")
    output = tmp / "clips" / "result.mp4"
    outputs: list[str] = []
    failures: list[str] = []
    launched_targets: list[Path] = []
    worker = TrimWorker(src, 0.0, 2.0, output)
    real_popen = ffmpeg_trim.subprocess.Popen

    def fake_popen(args, **_kwargs):
        proc = _FakeProcess(
            args,
            returncode=returncode,
            payload=b"x" * 8192,
            on_read=worker.cancel if cancel_during_read else None,
        )
        launched_targets.append(proc.target)
        return proc

    ffmpeg_trim.subprocess.Popen = fake_popen
    try:
        worker.done.connect(outputs.append)
        worker.failed.connect(failures.append)
        worker.run()
    finally:
        ffmpeg_trim.subprocess.Popen = real_popen

    target = launched_targets[0] if launched_targets else None
    return output, target, outputs, failures


def test_trim_uses_atomic_temporary_output(tmp: Path) -> None:
    success = tmp / "success"
    success.mkdir()
    output, target, done, failed = _run_worker_with_fake_process(
        success, returncode=0
    )
    check("trim-success: ffmpeg writes to a sibling non-library temporary", target is not None and target != output and target.parent == output.parent and target.suffix == ".partial")
    check("trim-success: final output appears only after success", output.exists() and done == [str(output)] and not failed)
    check("trim-success: temporary output is consumed", target is not None and not target.exists())

    failure = tmp / "failure"
    failure.mkdir()
    output, target, done, failed = _run_worker_with_fake_process(
        failure, returncode=1
    )
    check("trim-failure: partial final output is absent", not output.exists() and not done and bool(failed))
    check("trim-failure: temporary output is cleaned", target is not None and not target.exists())

    cancelled = tmp / "cancelled"
    cancelled.mkdir()
    output, target, done, failed = _run_worker_with_fake_process(
        cancelled, returncode=0, cancel_during_read=True
    )
    check("trim-cancel: partial final output is absent", not output.exists() and not done and failed == ["Cancelled"])
    check("trim-cancel: temporary output is cleaned", target is not None and not target.exists())


def test_real_ffmpeg_publishes_completed_clip(tmp: Path) -> None:
    source = tmp / "source.mkv"
    output = tmp / "clips" / "real-result.mp4"
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    generated = subprocess.run(
        [
            str(ffmpeg_exe()),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=30:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        capture_output=True,
        creationflags=creationflags,
        timeout=60,
    )
    check("real-trim: synthetic source was generated", generated.returncode == 0)
    if generated.returncode != 0:
        return

    done: list[str] = []
    failed: list[str] = []
    worker = TrimWorker(source, 0.25, 2.5, output)
    worker.done.connect(done.append)
    worker.failed.connect(failed.append)
    worker.run()

    check(
        "real-trim: bundled ffmpeg published a valid final MP4",
        done == [str(output)] and not failed and output.stat().st_size >= 4096,
    )
    check(
        "real-trim: no temporary export remains",
        not output.with_name(f".{output.name}.partial").exists(),
    )


def test_partial_trim_is_not_library_media(tmp: Path) -> None:
    clips = tmp / "clips"
    clips.mkdir()
    partial = clips / ".result.mp4.partial"
    partial.write_bytes(b"partial")
    check(
        "trim-partial: a work file is never listed as library media",
        partial not in _list_recordings(tmp),
    )


class _Status:
    def __init__(self) -> None:
        self.text = ""

    def setText(self, text: str) -> None:
        self.text = text


class _List:
    def remove_path(self, _path: Path) -> None:
        raise AssertionError("busy path must not be removed from the list")


class _Dialogs:
    StandardButton = QMessageBox.StandardButton
    question_calls = 0

    @classmethod
    def question(cls, *_args, **_kwargs):
        cls.question_calls += 1
        return cls.StandardButton.Cancel

    @staticmethod
    def warning(*_args, **_kwargs) -> None:
        pass

    @staticmethod
    def critical(*_args, **_kwargs) -> None:
        pass


class _Inputs:
    get_calls = 0

    @classmethod
    def getText(cls, *_args, **_kwargs):
        cls.get_calls += 1
        return "", False


class _CloseEvent:
    def __init__(self) -> None:
        self.ignored = False

    def ignore(self) -> None:
        self.ignored = True


def _editor_harness(trimmed: set[str]):
    busy_method = getattr(EditorWindow, "_is_file_busy", None)

    class Harness:
        if busy_method is not None:
            _is_file_busy = busy_method

        def refresh(self) -> None:
            raise AssertionError("busy file operation must return before refresh")

    host = Harness()
    host._trimming_paths = set(trimmed)
    host._status = _Status()
    host._list = _List()
    host._current_selection = None
    host.preview = SimpleNamespace(load=lambda _path: None)
    host._game_slug_cache = {}
    host._duration_cache = {}
    host._duration_hint_cache = {}
    return host


def test_delete_and_rename_refuse_busy_files(tmp: Path) -> None:
    source = tmp / "busy.mkv"
    source.write_bytes(b"busy")
    path_key = str(source.resolve())
    real_message_box = editor_module.QMessageBox
    real_input_dialog = editor_module.QInputDialog
    real_is_repairing = editor_module.is_repairing
    editor_module.QMessageBox = _Dialogs
    editor_module.QInputDialog = _Inputs
    try:
        check("busy-state: editor exposes a per-path trim busy check", hasattr(EditorWindow, "_is_file_busy"))

        _Dialogs.question_calls = 0
        trim_delete = _editor_harness({path_key})
        try:
            EditorWindow._on_delete_requested(trim_delete, [source])
            refused = _Dialogs.question_calls == 0 and source.exists()
        except Exception:
            refused = False
        check("busy-delete: deleting a trimming file is refused before confirmation", refused)

        _Dialogs.question_calls = 0
        editor_module.is_repairing = lambda path: Path(path).resolve() == source.resolve()
        repair_delete = _editor_harness(set())
        try:
            EditorWindow._on_delete_requested(repair_delete, [source])
            refused = _Dialogs.question_calls == 0 and source.exists()
        except Exception:
            refused = False
        check("busy-delete: deleting a repairing file is refused before confirmation", refused)

        _Inputs.get_calls = 0
        editor_module.is_repairing = lambda _path: False
        trim_rename = _editor_harness({path_key})
        try:
            EditorWindow._on_rename_requested(trim_rename, source)
            refused = _Inputs.get_calls == 0 and source.exists()
        except Exception:
            refused = False
        check("busy-rename: renaming a trimming file is refused before prompt", refused)

        _Inputs.get_calls = 0
        editor_module.is_repairing = lambda path: Path(path).resolve() == source.resolve()
        repair_rename = _editor_harness(set())
        try:
            EditorWindow._on_rename_requested(repair_rename, source)
            refused = _Inputs.get_calls == 0 and source.exists()
        except Exception:
            refused = False
        check("busy-rename: renaming a repairing file is refused before prompt", refused)
    finally:
        editor_module.QMessageBox = real_message_box
        editor_module.QInputDialog = real_input_dialog
        editor_module.is_repairing = real_is_repairing


def test_close_to_tray_keeps_trim_running(_tmp: Path) -> None:
    cancel_calls: list[bool] = []

    class Preview:
        @staticmethod
        def is_fullscreen() -> bool:
            return False

    host = SimpleNamespace(
        preview=Preview(),
        _config=SimpleNamespace(close_to_tray=True),
        _settings_panel=None,
        _save_window_state=lambda: None,
        _park_preview_for_tray=lambda: None,
        _release_preview_if_parked=lambda: None,
        hide=lambda: None,
        setWindowOpacity=lambda _opacity: None,
        _trim_worker=SimpleNamespace(cancel=lambda: cancel_calls.append(True)),
    )
    event = _CloseEvent()
    EditorWindow.closeEvent(host, event)
    check("tray-close: the close event is converted to a tray hide", event.ignored)
    check("tray-close: an active trim is not cancelled", not cancel_calls)


def test_quit_cancels_and_drains_active_trim(tmp: Path) -> None:
    """Exercise the real QThread teardown in a child process.

    Keeping it out-of-process turns a Qt abort into an ordinary failed check
    instead of taking down the rest of this smoke suite.
    """
    child = tmp / "quit_trim_child.py"
    child.write_text(
        """
import os
import sys
import threading
import time
from pathlib import Path
from types import MethodType

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"C:\\dev\\Momento")

from PyQt6.QtCore import QThread, QTimer
from PyQt6.QtWidgets import QApplication
import momento.trim.ffmpeg_trim as trim_module
from momento.trim.ffmpeg_trim import TrimWorker
from momento.ui.editor import EditorWindow

root = Path(sys.argv[1])
src = root / "source.mkv"
src.write_bytes(b"source")
out = root / "clip.mp4"
started = threading.Event()
released = threading.Event()

class Stderr:
    def __iter__(self):
        started.set()
        released.wait(10)
        return iter(())

class Proc:
    def __init__(self, args, **kwargs):
        Path(args[-1]).write_bytes(b"x" * 8192)
        self.stderr = Stderr()
        self.returncode = 0
    def poll(self):
        return None if not released.is_set() else self.returncode
    def terminate(self):
        self.returncode = -15
        released.set()
    def wait(self):
        released.wait(10)
        return self.returncode

trim_module.subprocess.Popen = Proc
app = QApplication(sys.argv[:1])
worker = TrimWorker(src, 0.0, 2.0, out)
thread = QThread()
worker.moveToThread(thread)
thread.started.connect(worker.run)
worker.finished.connect(thread.quit)

class Host:
    pass

host = Host()
host._trim_worker = worker
host._trim_thread = thread
host._app_quitting = False
host._cancel_active_trim_for_quit = MethodType(EditorWindow._cancel_active_trim_for_quit, host)
app.aboutToQuit.connect(host._cancel_active_trim_for_quit)
thread.start()

def request_quit():
    if not started.is_set():
        QTimer.singleShot(10, request_quit)
        return
    app.quit()

QTimer.singleShot(10, request_quit)
rc = app.exec()
ok = rc == 0 and not thread.isRunning() and released.is_set() and not out.exists()
raise SystemExit(0 if ok else 3)
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(child), str(tmp)],
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    check(
        "quit: QApplication shutdown cancels and drains the active trim thread",
        proc.returncode == 0,
    )
    if proc.returncode != 0 and (proc.stdout or proc.stderr):
        print((proc.stdout + proc.stderr)[-1200:])


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="momento_trim_lifecycle_") as d:
        tmp = Path(d)
        for fn in (
            test_trim_uses_atomic_temporary_output,
            test_real_ffmpeg_publishes_completed_clip,
            test_partial_trim_is_not_library_media,
            test_delete_and_rename_refuse_busy_files,
            test_close_to_tray_keeps_trim_running,
            test_quit_cancels_and_drains_active_trim,
        ):
            sub = tmp / fn.__name__
            sub.mkdir()
            try:
                fn(sub)
            except Exception as exc:
                check(f"{fn.__name__} raised unexpectedly: {exc!r}", False)

    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
