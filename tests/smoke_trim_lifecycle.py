r"""Focused regressions for trim output and editor file-lifecycle safety.

Run:
    C:\dev\Momento\.venv\Scripts\python.exe tests\smoke_trim_lifecycle.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import av  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import momento.trim.ffmpeg_trim as ffmpeg_trim  # noqa: E402
import momento.ui.editor as editor_module  # noqa: E402
import momento.core.recording_safety as safety_module  # noqa: E402
from momento.core.storage_cleanup import (  # noqa: E402
    MigrationWorker,
    enforce_storage_limit,
)
from momento.trim.ffmpeg_trim import TrimWorker  # noqa: E402
from momento.ui.editor import EditorWindow, _list_recordings  # noqa: E402
from media_fixture import make_momento_mkv  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv[:1])
_results: list[tuple[str, bool]] = []
_OWNERSHIP_SUFFIX = ".momento.json"


def check(name: str, ok: bool) -> None:
    _results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} - {name}")


def _ownership_sidecar(path: Path) -> Path:
    return path.with_name(path.name + _OWNERSHIP_SUFFIX)


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
    tmp: Path,
    *,
    returncode: int,
    cancel_during_read: bool = False,
    validation_error: str | None = None,
) -> tuple[Path, Path | None, list[str], list[str]]:
    src = tmp / "source.mkv"
    src.write_bytes(b"source")
    output = tmp / "clips" / "result.mp4"
    outputs: list[str] = []
    failures: list[str] = []
    launched_targets: list[Path] = []
    worker = TrimWorker(src, 0.0, 2.0, output)
    real_popen = ffmpeg_trim.subprocess.Popen
    real_validate = ffmpeg_trim.validate_trim_candidate

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
    ffmpeg_trim.validate_trim_candidate = (
        lambda *_args, **_kwargs: validation_error
    )
    try:
        worker.done.connect(outputs.append)
        worker.failed.connect(failures.append)
        worker.run()
    finally:
        ffmpeg_trim.subprocess.Popen = real_popen
        ffmpeg_trim.validate_trim_candidate = real_validate

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

    invalid = tmp / "invalid"
    invalid.mkdir()
    output, target, done, failed = _run_worker_with_fake_process(
        invalid,
        returncode=0,
        validation_error="candidate is not readable media",
    )
    check(
        "trim-validation: exit-zero corrupt bytes are never published",
        not output.exists() and not done and bool(failed),
    )
    check(
        "trim-validation: rejected temporary output is removed",
        target is not None and not target.exists(),
    )


def test_real_ffmpeg_publishes_completed_clip(tmp: Path) -> None:
    source = tmp / "source.mkv"
    output = tmp / "clips" / "real-result.mp4"
    try:
        make_momento_mkv(source, game_slug="trim-fixture")
    except Exception:
        check("real-trim: recording-shaped source was generated", False)
        return
    check("real-trim: recording-shaped source was generated", source.stat().st_size >= 4096)

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
    media = av.open(str(output))
    try:
        stream_codecs = {(stream.type, stream.codec_context.name) for stream in media.streams}
        preserved_tag = media.metadata.get("MOMENTO_GAME") == "trim-fixture"
    finally:
        media.close()
    check(
        "real-trim: H.264 and AAC were stream-copied",
        {("video", "h264"), ("audio", "aac")} <= stream_codecs,
    )
    check("real-trim: MOMENTO_GAME metadata survives MP4 export", preserved_tag)
    payload = output.read_bytes()
    check(
        "real-trim: fast-start places moov before media data",
        0 <= payload.find(b"moov") < payload.find(b"mdat"),
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
        _refresh_and_reselect = EditorWindow._refresh_and_reselect

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


def test_ownership_sidecar_follows_rename_and_delete(tmp: Path) -> None:
    source = tmp / "owned.mkv"
    source.write_bytes(b"media")
    thumb = editor_module.thumb_path_for(source)
    bookmark = editor_module.bookmark_sidecar(source)
    owner = _ownership_sidecar(source)
    thumb.write_bytes(b"thumb")
    bookmark.write_text("[]", encoding="utf-8")
    marker_created = safety_module.mark_recording_owned(source)
    check("ownership fixture: source has a valid bound marker", marker_created)
    if not marker_created:
        return

    class AcceptRename:
        @staticmethod
        def getText(*_args, **_kwargs):
            return "renamed", True

    class AcceptDelete(_Dialogs):
        @classmethod
        def question(cls, *_args, **_kwargs):
            cls.question_calls += 1
            return cls.StandardButton.Yes

    real_message_box = editor_module.QMessageBox
    real_input_dialog = editor_module.QInputDialog
    real_is_repairing = editor_module.is_repairing
    editor_module.QMessageBox = AcceptDelete
    editor_module.QInputDialog = AcceptRename
    editor_module.is_repairing = lambda _path: False
    host = _editor_harness(set())
    host.refresh = lambda *args, **kwargs: None
    try:
        EditorWindow._on_rename_requested(host, source)
        renamed = tmp / "renamed.mkv"
        check(
            "ownership marker follows rename with existing sidecars",
            renamed.exists()
            and editor_module.thumb_path_for(renamed).exists()
            and editor_module.bookmark_sidecar(renamed).exists()
            and _ownership_sidecar(renamed).exists()
            and safety_module.has_valid_ownership_marker(renamed)
            and not owner.exists(),
        )

        EditorWindow._on_delete_requested(host, [renamed])
        check(
            "ownership marker is removed with a deleted recording",
            not renamed.exists()
            and not editor_module.thumb_path_for(renamed).exists()
            and not editor_module.bookmark_sidecar(renamed).exists()
            and not _ownership_sidecar(renamed).exists(),
        )
    finally:
        editor_module.QMessageBox = real_message_box
        editor_module.QInputDialog = real_input_dialog
        editor_module.is_repairing = real_is_repairing


def test_mutation_refresh_keeps_list_and_preview_in_sync(tmp: Path) -> None:
    target = tmp / "target.mkv"
    other = tmp / "other.mkv"
    target.write_bytes(b"target")
    other.write_bytes(b"other")

    class SelectedSignal:
        def __init__(self, owner) -> None:
            self.owner = owner
            self.emitted: list[Path] = []

        def emit(self, path: Path) -> None:
            self.emitted.append(path)
            self.owner._current_selection = path

    class Host:
        def __init__(self, filtered: bool) -> None:
            self._current_selection = None
            self.filtered = filtered
            self.refresh_calls: list[bool] = []
            self.selected_changed = SelectedSignal(self)

        def refresh(self, preserve_selection: bool = False) -> None:
            self.refresh_calls.append(preserve_selection)
            if self.filtered:
                self._current_selection = other

    visible = Host(filtered=False)
    EditorWindow._refresh_and_reselect(visible, target)
    check(
        "mutation selection: visible target is both preserved and reloaded",
        visible.refresh_calls == [True]
        and visible._current_selection == target
        and visible.selected_changed.emitted == [target],
    )

    filtered = Host(filtered=True)
    EditorWindow._refresh_and_reselect(filtered, target)
    check(
        "mutation selection: filtered target never overrides the highlighted row",
        filtered.refresh_calls == [True]
        and filtered._current_selection == other
        and filtered.selected_changed.emitted == [],
    )


def test_shared_activity_registry_and_editor_trim_lifecycle(tmp: Path) -> None:
    source = tmp / "source.mkv"
    output = tmp / "clips" / "result.mp4"
    source.write_bytes(b"source")
    first = safety_module.begin_file_activity(source)
    second_ready = threading.Event()
    release_second = threading.Event()

    def overlap_activity() -> None:
        second = safety_module.begin_file_activity(source)
        second_ready.set()
        release_second.wait(5)
        second.release()

    thread = threading.Thread(target=overlap_activity)
    thread.start()
    second_ready.wait(5)
    first.release()
    check(
        "activity registry is thread-safe and reference-counted",
        safety_module.is_file_active(source),
    )
    release_second.set()
    thread.join(5)
    check(
        "activity registry clears after every holder releases",
        not thread.is_alive() and not safety_module.is_file_active(source),
    )

    class Signal:
        def __init__(self) -> None:
            self.callbacks = []

        def connect(self, callback) -> None:
            self.callbacks.append(callback)

    class FakeWorker:
        def __init__(self, *_args) -> None:
            self.progress = Signal()
            self.done = Signal()
            self.failed = Signal()
            self.finished = Signal()

        def moveToThread(self, _thread) -> None:
            pass

        def run(self) -> None:
            pass

        def deleteLater(self) -> None:
            pass

    class FakeThread:
        def __init__(self, _parent=None) -> None:
            self.started = Signal()
            self.finished = Signal()

        def start(self) -> None:
            pass

        def quit(self) -> None:
            pass

        def deleteLater(self) -> None:
            pass

    class WidgetState:
        def setEnabled(self, _enabled: bool) -> None:
            pass

        def setVisible(self, _visible: bool) -> None:
            pass

        def setValue(self, _value: int) -> None:
            pass

        def setFormat(self, _text: str) -> None:
            pass

    real_worker = editor_module.TrimWorker
    real_thread = editor_module.QThread
    real_is_repairing = editor_module.is_repairing
    editor_module.TrimWorker = FakeWorker
    editor_module.QThread = FakeThread
    editor_module.is_repairing = lambda _path: False
    host = SimpleNamespace(
        _trim_thread=None,
        _trim_worker=None,
        _trim_input_key=None,
        _trimming_paths=set(),
        _app_quitting=False,
        _status=_Status(),
        _export_btn=WidgetState(),
        _export_progress=WidgetState(),
        preview=SimpleNamespace(duration=lambda: 1.0),
        refresh=lambda **_kwargs: None,
        _file_key=EditorWindow._file_key,
        _on_trim_progress=lambda *_args: None,
        _on_trim_done=lambda *_args: None,
        _on_trim_failed=lambda *_args: None,
        _on_trim_thread_finished=lambda: None,
    )
    try:
        EditorWindow._launch_trim(host, source, 0.0, 1.0, output)
        check(
            "editor registers trim input and output as active before launch",
            safety_module.is_file_active(source)
            and safety_module.is_file_active(output),
        )
        with source.open("r+b") as fh:
            fh.truncate(2 * 1024**3)
        safety_module.mark_recording_owned(source)
        deleted = enforce_storage_limit(tmp, max_gb=1)
        check(
            "active export input is protected from quota eviction",
            deleted == 0 and source.exists(),
        )
        migration_dst = tmp / "migrated" / source.name
        moved, failed = MigrationWorker(tmp, tmp / "migrated").run(
            pairs=[(source, migration_dst)]
        )
        check(
            "active export input is protected from folder migration",
            moved == 0
            and failed == 1
            and source.exists()
            and not migration_dst.exists(),
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"clip")
        EditorWindow._on_trim_done(host, str(output))
        check(
            "successful export receives a durable ownership marker",
            _ownership_sidecar(output).exists(),
        )
        EditorWindow._on_trim_thread_finished(host)
        check(
            "editor releases trim activity only after its thread finishes",
            not safety_module.is_file_active(source)
            and not safety_module.is_file_active(output),
        )
    finally:
        activity = getattr(host, "_trim_activity", None)
        if activity is not None:
            activity.release()
        editor_module.TrimWorker = real_worker
        editor_module.QThread = real_thread
        editor_module.is_repairing = real_is_repairing


def test_repair_does_not_claim_unrelated_media(tmp: Path) -> None:
    unrelated = tmp / "unrelated.mkv"
    unrelated.write_bytes(b"media")
    host = SimpleNamespace(
        _repair_target=unrelated,
        _repair_was_owned=False,
        _repair_tick=None,
        _repair_progress=None,
        _status=_Status(),
        _game_slug_cache={},
        _duration_cache={},
        _duration_hint_cache={},
        _thumb_submitted=set(),
        _current_selection=None,
        refresh=lambda **_kwargs: None,
        preview=SimpleNamespace(load=lambda _path: None),
        _restore_splitter_after_repair=lambda: None,
    )
    host.selected_changed = SimpleNamespace(
        emit=lambda path: setattr(host, "_current_selection", path)
    )
    host._refresh_and_reselect = lambda path: EditorWindow._refresh_and_reselect(
        host, path
    )

    EditorWindow._on_repair_done(host, str(unrelated), True, "")
    check(
        "repair does not create ownership for unrelated media",
        not _ownership_sidecar(unrelated).exists(),
    )

    owned = tmp / "owned.mkv"
    owned.write_bytes(b"media")
    host._repair_target = owned
    host._repair_was_owned = True
    EditorWindow._on_repair_done(host, str(owned), True, "")
    check(
        "repair refreshes ownership for an existing Momento recording",
        _ownership_sidecar(owned).exists(),
    )


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
sys.path.insert(0, sys.argv[2])

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
        [
            sys.executable,
            str(child),
            str(tmp),
            str(Path(__file__).resolve().parents[1]),
        ],
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
            test_ownership_sidecar_follows_rename_and_delete,
            test_mutation_refresh_keeps_list_and_preview_in_sync,
            test_shared_activity_registry_and_editor_trim_lifecycle,
            test_repair_does_not_claim_unrelated_media,
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
