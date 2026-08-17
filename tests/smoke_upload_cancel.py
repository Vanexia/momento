"""Regression: cancelling a YouTube upload must not freeze/crash the app.

The 2026-07-04 live incident: a user cancelled an upload and the whole program
hung (Not Responding, 153 leaked threads). Root cause: the worker parks in a
blocking ``session.put()`` (a 16 MiB body send that stalls — the send is NOT
bounded by the read timeout), so a cancel wasn't honoured; the modal dialog then
``wait()``-ed on that wedged worker on the GUI thread, and the QThread (a child
of the dialog) was torn down while still running -> freeze/abort.

Two guarantees, both testable headlessly:

  A. cancel() interrupts a blocking send PROMPTLY (by closing the session) and
     surfaces as _UploadCancelled — it does not hang until the OS TCP timeout.

  B. The progress dialog's worker thread is NOT parented to the dialog and is
     tracked in a registry, so closing the dialog never blocks the GUI thread
     and never destroys a running QThread; the thread cleans itself up on cancel.
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402
from PyQt6 import sip  # noqa: E402
from PyQt6.QtCore import QObject, pyqtSignal  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from momento.youtube.uploader import UploadJob, UploadOptions, _UploadCancelled  # noqa: E402

_passed = 0
_failed = 0


def check(cond: bool, label: str) -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"PASS - {label}")
    else:
        _failed += 1
        print(f"FAIL - {label}")


# ---------------------------------------------------------------- Test A
class _BlockingSession:
    """put() blocks (like a stalled socket send) until close() is called, then
    raises — modelling a request that only unblocks when the session is closed."""

    def __init__(self):
        self._closed = threading.Event()
        self.put_entered = threading.Event()

    def put(self, url, **kw):
        self.put_entered.set()
        if self._closed.wait(timeout=10):
            raise requests.ConnectionError("session closed by cancel")
        raise AssertionError("put() was never interrupted (10 s) — cancel is broken")

    def close(self):
        self._closed.set()


class _BlockingInitiateSession(_BlockingSession):
    def post(self, url, **kw):
        return self.put(url, **kw)


def test_cancel_interrupts_blocking_send(tmp: Path) -> None:
    src = tmp / "clip.mp4"
    src.write_bytes(b"x" * 500)
    job = UploadJob(object(), UploadOptions(file_path=src, title="t"))
    sess = _BlockingSession()
    job._session = sess  # normally set inside _do_upload; wired directly here

    outcome = {}

    def run():
        try:
            job._transfer(sess, "http://upload", src, 500)
            outcome["result"] = "returned"
        except _UploadCancelled:
            outcome["result"] = "cancelled"
        except Exception as e:  # noqa: BLE001
            outcome["result"] = f"error:{type(e).__name__}"

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    check(sess.put_entered.wait(3), "worker reached the blocking send")

    t0 = time.monotonic()
    job.cancel()  # sets flag + closes session -> put raises -> _UploadCancelled
    worker.join(5)
    elapsed = time.monotonic() - t0

    check(not worker.is_alive(), "worker thread terminated after cancel")
    check(outcome.get("result") == "cancelled",
          f"cancel surfaced as _UploadCancelled (got {outcome.get('result')})")
    check(elapsed < 3.0, f"cancel was prompt ({elapsed:.2f}s, not a TCP-timeout hang)")


def test_cancel_interrupts_blocking_initiation(tmp: Path) -> None:
    src = tmp / "clip.mp4"
    src.write_bytes(b"x")
    job = UploadJob(object(), UploadOptions(file_path=src, title="t"))
    sess = _BlockingInitiateSession()
    job._session = sess

    outcome = {}

    def run():
        try:
            job._initiate(sess, {}, 1)
            outcome["result"] = "returned"
        except _UploadCancelled:
            outcome["result"] = "cancelled"
        except Exception as e:  # noqa: BLE001
            outcome["result"] = type(e).__name__

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    check(sess.put_entered.wait(3), "upload initiation reached the blocking request")
    job.cancel()
    worker.join(5)
    check(not worker.is_alive(), "upload initiation terminated after cancel")
    check(
        outcome.get("result") == "cancelled",
        f"initiation cancel surfaced as _UploadCancelled (got {outcome.get('result')})",
    )


# ---------------------------------------------------------------- Test B
class _FakeJob(QObject):
    """Stands in for UploadJob: run() blocks on the worker thread like a wedged
    upload until cancel() unblocks it, then emits the cancellation."""

    progress = pyqtSignal(int)
    bytes_uploaded = pyqtSignal(float)
    speed = pyqtSignal(float)
    state_changed = pyqtSignal(str)
    finished = pyqtSignal(str, str)
    failed = pyqtSignal(str)

    def __init__(self, credentials, options):
        super().__init__()
        self._ev = threading.Event()

    def run(self):
        self._ev.wait(timeout=10)
        self.failed.emit("Cancelled by user")

    def cancel(self):
        self._ev.set()


def _pump_until(app, predicate, timeout_s=5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
    app.processEvents()
    return predicate()


def test_dialog_lifecycle_no_freeze(app) -> None:
    import momento.ui.youtube_upload_progress as prog

    real = prog.UploadJob
    prog.UploadJob = _FakeJob  # dialog will build a _FakeJob instead
    try:
        opts = UploadOptions(file_path=Path(__file__), title="t")  # any real file
        dlg = prog.YouTubeUploadProgressDialog(object(), opts, parent=None)

        check(dlg._thread.parent() is None,
              "worker QThread is NOT parented to the dialog")
        check((dlg._thread, dlg._job) in prog._ACTIVE_WORKERS,
              "worker retained in the module registry while running")

        # Cancel (what closeEvent does). Must unblock + finish the thread.
        keep = (dlg._thread, dlg._job)
        dlg._job.cancel()
        finished = _pump_until(app, lambda: not keep[0].isRunning(), 5.0)
        check(finished, "worker thread finished after cancel (no GUI-thread wait)")

        # deleteLater + the release lambda run on the event loop.
        released = _pump_until(app, lambda: keep not in prog._ACTIVE_WORKERS, 3.0)
        check(released, "worker released from the registry once finished (no leak)")
        check(
            released and sip.isdeleted(keep[0]) and sip.isdeleted(keep[1]),
            "worker is released only after its Qt objects are fully destroyed",
        )
    finally:
        prog.UploadJob = real


def main() -> int:
    import tempfile

    app = QApplication.instance() or QApplication(sys.argv[:1])
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_cancel_interrupts_blocking_send(tmp)
        test_cancel_interrupts_blocking_initiation(tmp)
    test_dialog_lifecycle_no_freeze(app)
    print(f"\n{_passed}/{_passed + _failed} checks passed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
