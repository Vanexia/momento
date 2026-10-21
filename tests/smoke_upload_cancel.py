"""Regression: cancelling a YouTube upload must actually stop it, promptly.

History:
- 2026-07: cancelling froze the app; the dialog wait()-ed on a worker wedged in
  a stalled send. Fixed by un-parenting the worker thread and never waiting.
- 2026-10 review: cancel() only closed the requests Session, which drops IDLE
  pooled connections. The connection carrying the current PUT is checked out
  of the pool, so the send ran on until its own timeouts (minutes on a stalled
  link) and a "cancelled" upload could still finish and go live. Esc also hid
  the dialog without cancelling at all.

These checks use real sockets: a local server that accepts but never reads, so
the client's send genuinely blocks in the kernel, over both TCP and TLS.
"""

from __future__ import annotations

import datetime
import socket
import ssl
import sys
import tempfile
import threading
import time
import warnings
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402
from PyQt6 import sip  # noqa: E402
from PyQt6.QtCore import QObject, pyqtSignal  # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from momento.youtube import uploader as up  # noqa: E402
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


# ------------------------------------------------------------ real sockets
def _self_signed_context(folder: Path) -> ssl.SSLContext:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = folder / "cert.pem", folder / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


class _StallingServer:
    """Accepts one connection (optionally TLS) and never answers.

    ``drain=False`` never reads, so the client blocks sending the body.
    ``drain=True`` reads everything, so the client blocks in recv() waiting
    for the acknowledgement — where an upload spends each chunk's round trip.
    """

    def __init__(self, tls_context: ssl.SSLContext | None, *, drain: bool = False) -> None:
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._tls = tls_context
        self._drain = drain
        self._conns: list[socket.socket] = []
        self.received = 0
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        try:
            conn, _ = self._listener.accept()
            if self._tls is not None:
                conn = self._tls.wrap_socket(conn, server_side=True)
            self._conns.append(conn)
            while self._drain:
                data = conn.recv(1 << 20)
                if not data:
                    return
                self.received += len(data)
        except OSError:
            pass

    def close(self) -> None:
        for conn in self._conns:
            try:
                conn.close()
            except OSError:
                pass
        self._listener.close()


def _run_transfer(job: UploadJob, session, url: str, src: Path, total: int):
    outcome: dict[str, str] = {}

    def run() -> None:
        try:
            job._transfer(session, url, src, total)
            outcome["result"] = "returned"
        except _UploadCancelled:
            outcome["result"] = "cancelled"
        except Exception as exc:  # noqa: BLE001
            outcome["result"] = f"error:{type(exc).__name__}"

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return worker, outcome


def _wait_for_live_socket(job: UploadJob, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(job._sockets._sockets):
            time.sleep(0.5)  # let the body send fill the socket buffers
            return True
        time.sleep(0.02)
    return False


def _stalled_upload(tmp: Path, tls: bool, *, drain: bool = False):
    ctx = _self_signed_context(tmp) if tls else None
    server = _StallingServer(ctx, drain=drain)
    src = tmp / f"clip_{'tls' if tls else 'tcp'}_{'drain' if drain else 'stall'}.bin"
    src.write_bytes(b"\0" * (40 * 1024 * 1024))  # first 16 MiB chunk isn't final
    job = UploadJob(object(), UploadOptions(file_path=src, title="t"))
    session = requests.Session()
    adapter = up._CancellableAdapter(job._sockets)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.verify = False  # self-signed test certificate
    job._session = session
    scheme = "https" if tls else "http"
    url = f"{scheme}://127.0.0.1:{server.port}/upload"
    worker, outcome = _run_transfer(job, session, url, src, src.stat().st_size)
    return server, job, session, worker, outcome


def test_session_close_alone_does_not_interrupt(tmp: Path) -> None:
    """Pins WHY the socket abort exists: Session.close() leaves the in-flight
    connection sending."""
    server, job, session, worker, _ = _stalled_upload(tmp, tls=False)
    try:
        check(_wait_for_live_socket(job), "control: upload reached a blocked send")
        session.close()
        worker.join(1.5)
        check(worker.is_alive(), "control: Session.close() alone leaves the send blocked")
    finally:
        job._cancel.set()
        job._sockets.abort()
        worker.join(5)
        server.close()


def _check_cancel_aborts(tmp: Path, tls: bool, *, drain: bool) -> None:
    label = f"{'TLS' if tls else 'TCP'} {'awaiting reply' if drain else 'sending'}"
    server, job, _session, worker, outcome = _stalled_upload(tmp, tls=tls, drain=drain)
    try:
        check(_wait_for_live_socket(job), f"{label}: upload reached a blocked state")
        if drain:
            deadline = time.monotonic() + 10
            while server.received < up._CHUNK_SIZE and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(0.3)
            check(server.received >= up._CHUNK_SIZE,
                  f"{label}: whole chunk delivered; client is waiting in recv()")
        started = time.monotonic()
        job.cancel()
        worker.join(5)
        elapsed = time.monotonic() - started
        check(not worker.is_alive(), f"{label}: worker stopped after cancel")
        check(outcome.get("result") == "cancelled",
              f"{label}: cancel surfaced as a cancellation (got {outcome.get('result')})")
        check(elapsed < 2.0, f"{label}: cancel took effect promptly ({elapsed:.2f}s)")
    finally:
        server.close()


def test_cancel_aborts_a_blocked_tcp_send(tmp: Path) -> None:
    _check_cancel_aborts(tmp, tls=False, drain=False)


def test_cancel_aborts_a_blocked_tls_send(tmp: Path) -> None:
    _check_cancel_aborts(tmp, tls=True, drain=False)


def test_cancel_aborts_a_tcp_wait_for_acknowledgement(tmp: Path) -> None:
    _check_cancel_aborts(tmp, tls=False, drain=True)


def test_cancel_aborts_a_tls_wait_for_acknowledgement(tmp: Path) -> None:
    _check_cancel_aborts(tmp, tls=True, drain=True)


def test_socket_opened_after_cancel_is_aborted() -> None:
    tracker = up._SocketTracker()
    tracker.abort()
    a, b = socket.socketpair()
    try:
        tracker.add(a)
        b.settimeout(2)
        check(b.recv(1) == b"", "a connection opened after cancel is shut down at once")
    finally:
        a.close()
        b.close()


# ------------------------------------------------------- final-chunk race
class _FakeTracker:
    def __init__(self) -> None:
        self.aborted = threading.Event()

    def abort(self) -> None:
        self.aborted.set()


class _AbortableSession:
    """put() blocks until the tracker is aborted, then fails like a reset."""

    def __init__(self, tracker: _FakeTracker) -> None:
        self._tracker = tracker
        self.put_entered = threading.Event()

    def put(self, url, **kw):
        self.put_entered.set()
        if self._tracker.aborted.wait(10):
            raise requests.ConnectionError("connection aborted")
        raise AssertionError("put() was never interrupted")

    def close(self) -> None:
        pass


def _race(tmp: Path, *, size: int, status_result, label: str):
    src = tmp / f"{label}.bin"
    src.write_bytes(b"x" * size)
    job = UploadJob(object(), UploadOptions(file_path=src, title="t"))
    tracker = _FakeTracker()
    job._sockets = tracker
    session = _AbortableSession(tracker)
    calls: list[int] = []

    def fake_status(_url, total):
        calls.append(total)
        return status_result

    job._status_after_cancel = fake_status
    worker, outcome = _run_transfer(job, session, "https://upload.invalid", src, size)
    session.put_entered.wait(3)
    job.cancel()
    worker.join(5)
    return outcome, calls


def test_cancel_during_final_chunk_reports_a_completed_video(tmp: Path) -> None:
    outcome, calls = _race(tmp, size=500, status_result={"id": "vid1"}, label="final_done")
    check(outcome.get("result") == "returned",
          "final chunk: a video YouTube already created is reported, not 'cancelled'")
    check(calls == [500], "final chunk: exactly one status check after cancel")


def test_cancel_during_final_chunk_otherwise_cancels(tmp: Path) -> None:
    outcome, _ = _race(tmp, size=500, status_result=None, label="final_not_done")
    check(outcome.get("result") == "cancelled",
          "final chunk: an incomplete upload is reported as cancelled")


def test_cancel_mid_upload_skips_the_status_check(tmp: Path) -> None:
    outcome, calls = _race(
        tmp, size=up._CHUNK_SIZE * 2, status_result={"id": "x"}, label="mid"
    )
    check(outcome.get("result") == "cancelled" and calls == [],
          "mid-upload cancel: no status check, reported as cancelled")


def test_cancel_interrupts_blocking_initiation(tmp: Path) -> None:
    src = tmp / "init.bin"
    src.write_bytes(b"x")
    job = UploadJob(object(), UploadOptions(file_path=src, title="t"))
    tracker = _FakeTracker()
    job._sockets = tracker
    session = _AbortableSession(tracker)
    session.post = session.put
    outcome: dict[str, str] = {}

    def run() -> None:
        try:
            job._initiate(session, {}, 1)
            outcome["result"] = "returned"
        except _UploadCancelled:
            outcome["result"] = "cancelled"
        except Exception as exc:  # noqa: BLE001
            outcome["result"] = type(exc).__name__

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    session.put_entered.wait(3)
    job.cancel()
    worker.join(5)
    check(outcome.get("result") == "cancelled",
          f"initiation: cancel surfaced as a cancellation (got {outcome.get('result')})")


def test_network_errors_never_expose_the_session_url() -> None:
    secret = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=SECRET123"
    exc = requests.ConnectionError(f"Max retries exceeded with url: {secret}")
    described = up._describe_network_error(exc)
    check("SECRET123" not in described and "upload_id" not in described,
          "network error text shown/logged omits the resumable session URL")


# ------------------------------------------------------------------ dialog
class _FakeJob(QObject):
    """Blocks like a wedged upload until cancel(), then reports ``outcome``."""

    progress = pyqtSignal(int)
    bytes_uploaded = pyqtSignal(float)
    speed = pyqtSignal(float)
    state_changed = pyqtSignal(str)
    finished = pyqtSignal(str, str)
    failed = pyqtSignal(str)
    outcome = "cancelled"

    def __init__(self, credentials, options):
        super().__init__()
        self._ev = threading.Event()
        self.cancel_calls = 0

    def run(self):
        self._ev.wait(timeout=10)
        if type(self).outcome == "finished":
            self.finished.emit("vid", "https://www.youtube.com/watch?v=vid")
        else:
            self.failed.emit("Cancelled by user")

    def cancel(self):
        self.cancel_calls += 1
        self._ev.set()


def _pump_until(app, predicate, timeout_s=5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return predicate()


def _dialog(prog):
    opts = UploadOptions(file_path=Path(__file__), title="t")
    dlg = prog.YouTubeUploadProgressDialog(object(), opts, parent=None)
    dlg.show()
    return dlg


def _answer(reply):
    return mock.patch.object(QMessageBox, "question", return_value=reply)


def test_dialog(app) -> None:
    import momento.ui.youtube_upload_progress as prog

    real = prog.UploadJob
    prog.UploadJob = _FakeJob
    yes, no = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No
    try:
        # Esc answered "No": the upload keeps going and stays visible.
        _FakeJob.outcome = "cancelled"
        dlg = _dialog(prog)
        with _answer(no):
            dlg.reject()
        check(dlg.isVisible() and dlg._job.cancel_calls == 0,
              "Esc + No: dialog stays open and the upload continues")

        # Esc answered "Yes": cancel requested, dialog waits for the outcome.
        keep = (dlg._thread, dlg._job)
        check(keep[0].parent() is None, "worker QThread is not parented to the dialog")
        check(keep in prog._ACTIVE_WORKERS, "worker is retained while running")
        with _answer(yes):
            dlg.reject()
        check(dlg._job.cancel_calls == 1, "Esc + Yes: the upload is cancelled")
        closed = _pump_until(app, lambda: not dlg.isVisible())
        check(closed and dlg.result() == QDialog.DialogCode.Rejected,
              "dialog closes once the worker confirms the cancellation")
        finished = _pump_until(app, lambda: not keep[0].isRunning())
        check(finished, "worker thread finished after cancel (no GUI-thread wait)")
        released = _pump_until(app, lambda: keep not in prog._ACTIVE_WORKERS, 3.0)
        check(released and sip.isdeleted(keep[0]) and sip.isdeleted(keep[1]),
              "worker released only after its Qt objects are destroyed")

        # Window close answered "Yes" while the final bytes had already landed.
        _FakeJob.outcome = "finished"
        dlg = _dialog(prog)
        with _answer(yes):
            dlg.close()
        check(dlg.isVisible(), "close + Yes: dialog stays open while cancelling")
        done = _pump_until(app, lambda: dlg._terminal)
        check(done and dlg.isVisible(), "finished-before-cancel keeps the dialog open")
        check("finished before the cancel" in dlg._state_label.text(),
              "finished-before-cancel is reported honestly")
        check(dlg._view_btn.isVisibleTo(dlg), "finished-before-cancel offers the video link")
        dlg.accept()
        _pump_until(app, lambda: not prog._ACTIVE_WORKERS, 3.0)
    finally:
        prog.UploadJob = real
        _FakeJob.outcome = "cancelled"


def main() -> int:
    warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    app = QApplication.instance() or QApplication(sys.argv[:1])
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        for test in (
            test_session_close_alone_does_not_interrupt,
            test_cancel_aborts_a_blocked_tcp_send,
            test_cancel_aborts_a_blocked_tls_send,
            test_cancel_aborts_a_tcp_wait_for_acknowledgement,
            test_cancel_aborts_a_tls_wait_for_acknowledgement,
            test_cancel_during_final_chunk_reports_a_completed_video,
            test_cancel_during_final_chunk_otherwise_cancels,
            test_cancel_mid_upload_skips_the_status_check,
            test_cancel_interrupts_blocking_initiation,
        ):
            test(tmp)
    test_socket_opened_after_cancel_is_aborted()
    test_network_errors_never_expose_the_session_url()
    test_dialog(app)
    print(f"\n{_passed}/{_passed + _failed} checks passed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
