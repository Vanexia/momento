"""Regression checks for bounded YouTube thumbnail validation.

The dialog validates early for useful feedback, while the uploader validates
the bytes again immediately before the HTTP request so replacing a selected
file cannot bypass the boundary.  All checks are local and network-free.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication, QDialog, QLabel  # noqa: E402

from momento.config import Config  # noqa: E402
import momento.ui.youtube_upload_dialog as dialog_module  # noqa: E402
import momento.youtube.uploader as uploader  # noqa: E402


_MAX_BYTES = 2 * 1024 * 1024
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"

_passed = 0
_failed = 0


def check(condition: bool, label: str) -> None:
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"PASS - {label}")
    else:
        _failed += 1
        print(f"FAIL - {label}")


def _write_sized(path: Path, magic: bytes, size: int) -> None:
    path.write_bytes(magic + b"x" * (size - len(magic)))


class _GuardedReader(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.requested_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.requested_sizes.append(size)
        if size < 0 or size > _MAX_BYTES + 1:
            raise AssertionError(f"unbounded thumbnail read requested: {size}")
        return super().read(size)


class _GuardedPath:
    suffix = ".jpg"

    def __init__(self, data: bytes) -> None:
        self.reader = _GuardedReader(data)

    def open(self, mode: str):
        assert mode == "rb"
        return self.reader


class _Response:
    status_code = 200


class _UploadSession:
    instances: list["_UploadSession"] = []

    def __init__(self, _credentials) -> None:
        self.post_calls: list[dict] = []
        self.closed = False
        self.instances.append(self)

    def post(self, _url: str, **kwargs):
        self.post_calls.append(kwargs)
        return _Response()

    def close(self) -> None:
        self.closed = True


def test_validator_contract(root: Path) -> None:
    validate = getattr(uploader, "validate_thumbnail", None)
    check(callable(validate), "shared thumbnail validator is available")
    if not callable(validate):
        return

    jpeg = root / "valid.jpg"
    png = root / "valid.png"
    jpeg.write_bytes(_JPEG_MAGIC + b"jpeg")
    png.write_bytes(_PNG_MAGIC + b"png")

    jpeg_type, jpeg_data = validate(jpeg)
    png_type, png_data = validate(png)
    check(
        (jpeg_type, jpeg_data) == ("image/jpeg", jpeg.read_bytes()),
        "JPEG content type and bytes derive from JPEG magic",
    )
    check(
        (png_type, png_data) == ("image/png", png.read_bytes()),
        "PNG content type and bytes derive from PNG magic",
    )

    exactly = root / "exactly-2-mib.jpeg"
    _write_sized(exactly, _JPEG_MAGIC, _MAX_BYTES)
    exact_type, exact_data = validate(exactly)
    check(
        exact_type == "image/jpeg" and len(exact_data) == _MAX_BYTES,
        "a thumbnail of exactly 2 MiB is accepted",
    )

    invalid_cases = [
        ("over-2-mib.jpg", _JPEG_MAGIC + b"x" * (_MAX_BYTES + 1), "over 2 MiB"),
        ("empty.png", b"", "empty file"),
        ("wrong-magic.jpg", b"not an image", "wrong magic"),
        ("mismatch.jpg", _PNG_MAGIC + b"png", "suffix/magic mismatch"),
        ("unsupported.gif", _JPEG_MAGIC + b"jpeg", "unsupported suffix"),
    ]
    for filename, payload, label in invalid_cases:
        candidate = root / filename
        candidate.write_bytes(payload)
        try:
            validate(candidate)
        except (OSError, ValueError):
            rejected = True
        else:
            rejected = False
        check(rejected, f"{label} is rejected")

    missing = root / "missing.png"
    try:
        validate(missing)
    except (OSError, ValueError):
        rejected = True
    else:
        rejected = False
    check(rejected, "missing thumbnail is rejected")

    guarded = _GuardedPath(_JPEG_MAGIC + b"bounded")
    guarded_type, guarded_data = validate(guarded)
    check(
        guarded_type == "image/jpeg" and guarded_data.endswith(b"bounded"),
        "validator accepts a JPEG through the bounded reader",
    )
    check(
        guarded.reader.requested_sizes == [_MAX_BYTES + 1],
        "validator performs one bounded 2 MiB + 1 byte read",
    )

    upload_session = _UploadSession(object())
    job = uploader.UploadJob(
        object(), uploader.UploadOptions(file_path=jpeg, title="test")
    )
    job._set_thumbnail(upload_session, "video123", png)
    request = upload_session.post_calls[0]
    check(
        request["headers"] == {"Content-Type": "image/png"}
        and request["data"] == png.read_bytes(),
        "uploader sends the validator's magic-derived type and bounded bytes",
    )


def test_dialog_and_upload_revalidation(root: Path) -> None:
    validate = getattr(uploader, "validate_thumbnail", None)
    if not callable(validate):
        return

    clip = root / "clip.mp4"
    clip.write_bytes(b"video")
    thumbnail = root / "selected.jpg"
    thumbnail.write_bytes(_JPEG_MAGIC + b"selected")

    warnings: list[tuple[str, str]] = []
    original_warning = dialog_module.QMessageBox.warning
    dialog_module.QMessageBox.warning = (
        lambda _parent, title, message: warnings.append((title, message))
    )
    try:
        hostile_channel = "<img src=x>PRIVATE_CHANNEL_MARKUP"
        markup_dialog = dialog_module.YouTubeUploadDialog(
            clip, Config(), hostile_channel
        )
        header_text = "\n".join(
            label.text() for label in markup_dialog.findChildren(QLabel)
        )
        check(
            "<img src=x>" not in header_text
            and "&lt;img src=x&gt;PRIVATE_CHANNEL_MARKUP" in header_text,
            "remote channel names are escaped before rich-text display",
        )

        invalid = root / "invalid.png"
        invalid.write_bytes(b"not a png")
        invalid_dialog = dialog_module.YouTubeUploadDialog(
            clip, Config(), "Test channel"
        )
        invalid_dialog._thumb_edit.setText(str(invalid))
        invalid_dialog._on_upload_clicked()
        check(
            invalid_dialog.result() == QDialog.DialogCode.Rejected and bool(warnings),
            "dialog rejects invalid thumbnail bytes before upload",
        )

        dialog = dialog_module.YouTubeUploadDialog(clip, Config(), "Test channel")
        dialog._thumb_edit.setText(str(thumbnail))
        dialog._on_upload_clicked()
        check(
            dialog.result() == QDialog.DialogCode.Accepted,
            "dialog accepts a valid thumbnail",
        )
        options = dialog.get_options()
    finally:
        dialog_module.QMessageBox.warning = original_warning

    # Replace the checked JPEG before the worker reaches the upload boundary.
    thumbnail.write_bytes(b"replacement is not a JPEG")
    original_session = uploader.AuthorizedSession
    _UploadSession.instances.clear()
    uploader.AuthorizedSession = _UploadSession
    finished: list[tuple[str, str]] = []
    failed: list[str] = []
    states: list[str] = []
    try:
        job = uploader.UploadJob(object(), options)
        job.finished.connect(lambda video_id, url: finished.append((video_id, url)))
        job.failed.connect(failed.append)
        job.state_changed.connect(states.append)
        job._initiate = lambda _session, _body, _total: "upload-url"
        job._transfer = lambda _session, _url, _path, _total: {"id": "video123"}
        job._do_upload()
    finally:
        uploader.AuthorizedSession = original_session

    session = _UploadSession.instances[0]
    check(
        session.post_calls == [],
        "uploader revalidates replaced thumbnail before the HTTP request",
    )
    check(
        finished == [
            ("video123", "https://www.youtube.com/watch?v=video123")
        ]
        and failed == [],
        "post-upload thumbnail rejection preserves video success",
    )
    check(
        "Setting thumbnail" in states and states[-1] == "Finalising",
        "best-effort thumbnail failure continues through finalisation",
    )
    check(session.closed, "uploader session still closes after thumbnail rejection")


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        test_validator_contract(root)
        test_dialog_and_upload_revalidation(root)
    app.processEvents()
    print(f"\n{_passed}/{_passed + _failed} checks passed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
