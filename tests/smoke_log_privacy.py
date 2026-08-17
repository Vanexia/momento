"""Regressions for share-safe, bounded persistent diagnostics."""

from __future__ import annotations

import ast
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import momento.trim.ffmpeg_trim as ffmpeg_trim  # noqa: E402
import momento.util.logging_setup as logging_setup  # noqa: E402
from momento.trim.ffmpeg_trim import TrimWorker  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_USER = "PRIVATE_USER_9A2"
PRIVATE_MEDIA = "PRIVATE_CAMPAIGN_FINALE_9A2"
PRIVATE_TRAILING_PATH = "PRIVATE_FOLDER_AFTER_SPACE_4B7"
PRIVATE_ENDPOINT = "{0.0.1.00000000}.{12345678-1234-1234-1234-123456789abc}"
MAX_TRIM_LOG_BYTES = 512 * 1024
MAX_TRIM_LOG_FILES = 5

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


def _close_handlers(handlers: list[logging.Handler]) -> None:
    for handler in handlers:
        try:
            handler.flush()
        finally:
            handler.close()


def test_persistent_formatter_redacts_private_values(tmp: Path) -> None:
    log_root = tmp / "logs"
    # Build the synthetic profile path from segments so the source archive
    # privacy gate does not mistake the fixture itself for a real user path.
    private_path = (
        Path("C:\\")
        / "Users"
        / PRIVATE_USER
        / "Videos"
        / f"Shared {PRIVATE_TRAILING_PATH}"
        / f"{PRIVATE_MEDIA}.mkv"
    )
    private_filename = f"{PRIVATE_MEDIA} Final Cut.mkv"
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    original_logs_dir = logging_setup.logs_dir
    for handler in original_handlers:
        root_logger.removeHandler(handler)
    logging_setup.logs_dir = lambda: log_root
    try:
        logging_setup.setup_logging()
        logging.getLogger("momento.privacy-smoke").error(
            "media=%r bare filename=%s endpoint=%s",
            str(private_path),
            private_filename,
            PRIVATE_ENDPOINT,
        )
        try:
            raise OSError(f"Could not open {private_path}")
        except OSError:
            logging.getLogger("momento.privacy-smoke").exception(
                "private media operation failed"
            )
        installed_handlers = list(root_logger.handlers)
        for handler in installed_handlers:
            handler.flush()
        payload = (log_root / "momento.log").read_text(
            encoding="utf-8",
            errors="replace",
        )
        check(
            "persistent formatter redacts paths, filenames, endpoints, and tracebacks",
            PRIVATE_USER not in payload
            and PRIVATE_MEDIA not in payload
            and PRIVATE_TRAILING_PATH not in payload
            and PRIVATE_ENDPOINT not in payload,
        )
        rotating = [
            handler
            for handler in installed_handlers
            if isinstance(handler, logging.handlers.RotatingFileHandler)
        ]
        check(
            "main persistent log remains size/count bounded",
            len(rotating) == 1
            and rotating[0].maxBytes == 2 * 1024 * 1024
            and rotating[0].backupCount == 5,
        )
    finally:
        installed_handlers = list(root_logger.handlers)
        for handler in installed_handlers:
            root_logger.removeHandler(handler)
        _close_handlers(installed_handlers)
        logging_setup.logs_dir = original_logs_dir
        root_logger.setLevel(original_level)
        for handler in original_handlers:
            root_logger.addHandler(handler)


AUDITED_LOG_ARGUMENTS = {
    "momento/__main__.py": {"name", "Path(path_str).name", "p.name"},
    "momento/core/recorder.py": {"mkv_path"},
    "momento/core/session.py": {"store.recording_path.name", "final"},
    "momento/core/encoder.py": {"self._path"},
    "momento/core/bookmarks.py": {"path", "self._path"},
    "momento/core/media_probe.py": {
        "self._path",
        "dst.name",
        "src.name",
        "resolved.name",
        "Path(path).name",
        "p.name",
    },
    "momento/core/audio_loopback.py": {
        "name_or_id",
        "migrated",
        "self._device_key",
        "self._resolved_name",
    },
    "momento/core/audio_devices.py": {"name", "name_or_id"},
    "momento/core/mic_capture.py": {
        "name_or_id",
        "migrated",
        "self._device_key",
        "self._resolved_name",
    },
    "momento/core/mic_monitor.py": {"device.id"},
    "momento/ui/preview.py": {
        "self._audio.device().description()",
        "new_default.description()",
    },
    "momento/util/single_instance.py": {"self._lock_path"},
    "momento/core/storage_cleanup.py": {"path.name", "src.name", "sidecar.name"},
    "momento/core/thumbnails.py": {"media_path.name", "self._path"},
}


def _logger_call_arguments(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {
            "debug",
            "info",
            "warning",
            "error",
            "exception",
            "critical",
        }:
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != "logger":
            continue
        calls.extend(ast.unparse(argument) for argument in node.args[1:])
    return calls


def test_audited_log_calls_do_not_pass_private_values() -> None:
    leaks: list[str] = []
    for relative, forbidden in AUDITED_LOG_ARGUMENTS.items():
        for argument in _logger_call_arguments(ROOT / relative):
            if argument in forbidden:
                leaks.append(f"{relative}: {argument}")
    check(
        "audited log calls omit media paths, filenames, and device identities",
        not leaks,
    )
    if leaks:
        print("  " + "\n  ".join(leaks))
    mic_source = (ROOT / "momento/core/mic_capture.py").read_text(encoding="utf-8")
    loopback_source = (ROOT / "momento/core/audio_loopback.py").read_text(encoding="utf-8")
    check(
        "audio capture errors omit friendly device identifiers and raw open-error chains",
        "self._device_key!r" not in mic_source
        and "d.name!r" not in mic_source
        and "self._device_key!r" not in loopback_source
        and "d.name!r" not in loopback_source
        and 'logger.exception("Failed to open selected microphone")' not in mic_source
        and 'logger.exception("Failed to open selected loopback device")' not in loopback_source,
    )


class _FakeProcess:
    def __init__(self, args: list[str], private_line: str) -> None:
        self.target = Path(args[-1])
        self.target.write_bytes(b"x" * 8192)
        self.stderr = iter(
            (
                private_line,
                "x" * (MAX_TRIM_LOG_BYTES + 32 * 1024) + "\n",
            )
        )
        self.returncode = 0
        self.wait_called = False

    def poll(self):
        return None

    def terminate(self) -> None:
        self.returncode = -15

    def wait(self) -> int:
        self.wait_called = True
        return self.returncode


def test_trim_diagnostics_are_private_and_bounded(tmp: Path) -> None:
    private_root = tmp / PRIVATE_USER
    source = private_root / f"{PRIVATE_MEDIA}.mkv"
    output = private_root / "clips" / f"{PRIVATE_MEDIA}_clip.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    log_root = tmp / "logs"
    log_root.mkdir()
    unrelated = log_root / "momento.log"
    unrelated.write_text("keep", encoding="utf-8")
    for index in range(MAX_TRIM_LOG_FILES + 2):
        old = log_root / f"trim_20000101_00000{index}_{index:04x}.log"
        old.write_text("old", encoding="utf-8")
        os.utime(old, (index + 1, index + 1))

    private_line = (
        f"Input from '{source}' filename='{source.name}' "
        f"endpoint={PRIVATE_ENDPOINT}\n"
    )
    processes: list[_FakeProcess] = []
    real_logs_dir = ffmpeg_trim.logs_dir
    real_ffmpeg_exe = ffmpeg_trim.ffmpeg_exe
    real_popen = ffmpeg_trim.subprocess.Popen
    real_validate = ffmpeg_trim.validate_trim_candidate

    def fake_popen(args, **_kwargs):
        process = _FakeProcess(args, private_line)
        processes.append(process)
        return process

    ffmpeg_trim.logs_dir = lambda: log_root
    ffmpeg_trim.ffmpeg_exe = lambda: Path(r"C:\MomentoTools\ffmpeg.exe")
    ffmpeg_trim.subprocess.Popen = fake_popen
    ffmpeg_trim.validate_trim_candidate = lambda *_args, **_kwargs: None
    done: list[str] = []
    failed: list[str] = []
    try:
        worker = TrimWorker(source, 0.0, 2.0, output)
        worker.done.connect(done.append)
        worker.failed.connect(failed.append)
        worker.run()
    finally:
        ffmpeg_trim.logs_dir = real_logs_dir
        ffmpeg_trim.ffmpeg_exe = real_ffmpeg_exe
        ffmpeg_trim.subprocess.Popen = real_popen
        ffmpeg_trim.validate_trim_candidate = real_validate

    trim_logs = sorted(log_root.glob("trim_*.log"))
    payload = "\n".join(
        path.read_text(encoding="utf-8", errors="replace") for path in trim_logs
    )
    check(
        "trim diagnostics use generic names and redact private content",
        all(PRIVATE_MEDIA not in path.name for path in trim_logs)
        and PRIVATE_USER not in payload
        and PRIVATE_MEDIA not in payload
        and PRIVATE_ENDPOINT not in payload,
    )
    check(
        "trim diagnostics enforce per-file size and total-count bounds",
        len(trim_logs) <= MAX_TRIM_LOG_FILES
        and all(path.stat().st_size <= MAX_TRIM_LOG_BYTES for path in trim_logs),
    )
    check(
        "trim logging keeps draining stderr and preserves unrelated logs",
        len(processes) == 1
        and processes[0].wait_called
        and done == [str(output)]
        and not failed
        and unrelated.read_text(encoding="utf-8") == "keep",
    )
    check(
        "trim diagnostics record the process exit without private paths",
        "trim exit=0" in payload,
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="momento-log-privacy-") as folder:
        tmp = Path(folder)
        test_persistent_formatter_redacts_private_values(tmp / "formatter")
        test_audited_log_calls_do_not_pass_private_values()
        test_trim_diagnostics_are_private_and_bounded(tmp / "trim")
    print(f"\n{checks - failures}/{checks} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
