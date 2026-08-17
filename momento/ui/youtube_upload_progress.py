"""Progress dialog that owns an upload job + its worker thread.

Lifecycle:

    dlg = YouTubeUploadProgressDialog(creds, opts, parent)
    dlg.exec()  # blocks until upload finishes, fails, or user cancels

The dialog handles the full state machine: in-progress → finished | failed,
with explicit user confirmation if the upload is still running when the user
tries to close it. On success the dialog swaps to a "View on YouTube" /
"Close" footer and shows the watch URL.
"""

from __future__ import annotations

import time
import webbrowser
from typing import Optional

from PyQt6.QtCore import QThread
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from google.oauth2.credentials import Credentials

from momento.util.format import format_bytes
from momento.youtube.uploader import UploadJob, UploadOptions

# Upload worker threads outlive their dialog. Keep a strong reference to each
# (thread, job) until the thread actually finishes so (a) it isn't
# garbage-collected while running and (b) the dialog can close INSTANTLY without
# the GUI thread ever blocking on QThread.wait() — waiting on a worker wedged in
# a stalled network send is what froze the entire app on cancel. When the thread
# finishes it deletes itself and drops out of here. The registry entry is kept
# until Qt confirms that the thread object itself was destroyed; releasing on
# ``finished`` leaves a deferred deletion pending and can crash during a fast
# application shutdown.
_ACTIVE_WORKERS: set = set()


def _retain_worker(thread, job) -> None:
    _ACTIVE_WORKERS.add((thread, job))


def _release_worker(thread, job) -> None:
    _ACTIVE_WORKERS.discard((thread, job))


def has_active_uploads() -> bool:
    """Return whether an upload worker is still running or winding down."""
    return bool(_ACTIVE_WORKERS)


class YouTubeUploadProgressDialog(QDialog):
    """Modal that runs a single UploadJob to completion.

    Owns the QThread. Calling code does not need to manage either — the
    dialog cleans both up on close.
    """

    def __init__(
        self,
        credentials: Credentials,
        options: UploadOptions,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Uploading to YouTube")
        self.setModal(True)
        self.setMinimumWidth(480)

        self._options = options
        self._start_time = time.monotonic()
        self._last_speed_bps = 0.0
        self._uploaded_bytes = 0.0  # real bytes sent (from the job's bytes_uploaded signal)
        self._terminal = False  # set when finished/failed signal arrives
        self._watch_url = ""

        # ---- UI ----
        self._title_label = QLabel(self)
        self._title_label.setWordWrap(True)
        self._title_label.setText(
            f"<b>{_escape(options.title or options.file_path.name)}</b><br>"
            f"<span style='color:#888'>{options.file_path.name}</span>"
        )

        self._state_label = QLabel("Preparing…", self)
        self._state_label.setStyleSheet("color: #aaa;")

        self._progress = QProgressBar(self)
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFixedHeight(18)

        self._stats_label = QLabel(self)
        self._stats_label.setStyleSheet("color: #888;")
        self._stats_label.setText(" ")

        self._cancel_btn = QPushButton("Cancel", self)
        self._cancel_btn.clicked.connect(self._request_cancel)

        self._view_btn = QPushButton("View on YouTube", self)
        self._view_btn.setVisible(False)
        self._view_btn.clicked.connect(self._open_watch_url)

        self._close_btn = QPushButton("Close", self)
        self._close_btn.setVisible(False)
        self._close_btn.clicked.connect(self.accept)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(self._view_btn)
        button_row.addWidget(self._cancel_btn)
        button_row.addWidget(self._close_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self._title_label)
        layout.addSpacing(4)
        layout.addWidget(self._state_label)
        layout.addWidget(self._progress)
        layout.addWidget(self._stats_label)
        layout.addSpacing(8)
        layout.addLayout(button_row)

        # ---- Worker thread + job ----
        # The QThread is deliberately NOT parented to this dialog. If it were,
        # closing the dialog while an upload is still winding down (e.g. a cancel
        # whose stalled network send hasn't unblocked yet) would destroy a
        # running QThread -> "QThread: Destroyed while thread is still running"
        # -> the process aborts/hangs. Instead the thread outlives the dialog,
        # kept alive by the module registry until it truly finishes, then deletes
        # itself. So the GUI thread never has to wait() on the worker.
        self._thread = QThread()
        self._job: UploadJob = UploadJob(credentials, options)
        self._job.moveToThread(self._thread)
        _retain_worker(self._thread, self._job)

        self._thread.started.connect(self._job.run)
        self._job.progress.connect(self._on_progress)
        self._job.bytes_uploaded.connect(self._on_bytes)
        self._job.speed.connect(self._on_speed)
        self._job.state_changed.connect(self._on_state)
        self._job.finished.connect(self._on_finished)
        self._job.failed.connect(self._on_failed)
        # Quit the thread on either terminal signal, then let it + the job delete
        # themselves and drop out of the registry once deletion has completed.
        # Qt auto-severs the connections to THIS dialog's slots if the dialog is
        # destroyed first, so a late signal from a still-winding-down worker can
        # never touch a deleted dialog.
        self._job.finished.connect(self._thread.quit)
        self._job.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._job.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.destroyed.connect(
            lambda _obj=None, t=self._thread, j=self._job: _release_worker(t, j)
        )

        self._thread.start()

    # ---- Job signal handlers --------------------------------------------

    def _on_progress(self, pct: int) -> None:
        self._progress.setValue(pct)
        self._refresh_stats(pct)

    def _on_bytes(self, uploaded: float) -> None:
        self._uploaded_bytes = max(0.0, uploaded)
        self._refresh_stats(self._progress.value())

    def _on_speed(self, bps: float) -> None:
        self._last_speed_bps = max(0.0, bps)
        self._refresh_stats(self._progress.value())

    def _on_state(self, state: str) -> None:
        self._state_label.setText(f"{state}…")

    def _on_finished(self, _video_id: str, watch_url: str) -> None:
        self._terminal = True
        self._watch_url = watch_url
        self._progress.setValue(100)
        self._state_label.setText("Upload complete.")
        self._state_label.setStyleSheet("color: #6c6; font-weight: bold;")
        self._stats_label.setText(watch_url)
        self._cancel_btn.setVisible(False)
        self._view_btn.setVisible(True)
        self._close_btn.setVisible(True)
        self._close_btn.setDefault(True)
        self._close_btn.setFocus()

    def _on_failed(self, message: str) -> None:
        self._terminal = True
        # Distinguish user-cancel (silent close) from real failure (show msg).
        if message == "Cancelled by user":
            self.reject()
            return
        self._state_label.setText("Upload failed.")
        self._state_label.setStyleSheet("color: #e66; font-weight: bold;")
        self._stats_label.setText(message)
        self._stats_label.setWordWrap(True)
        self._cancel_btn.setVisible(False)
        self._close_btn.setVisible(True)
        self._close_btn.setDefault(True)
        self._close_btn.setFocus()

    # ---- Helpers ---------------------------------------------------------

    def _refresh_stats(self, pct: int) -> None:
        if self._terminal:
            return
        elapsed = max(0.001, time.monotonic() - self._start_time)
        size = self._options.file_path.stat().st_size if self._options.file_path.is_file() else 0
        uploaded = min(int(self._uploaded_bytes), size) if size else int(self._uploaded_bytes)
        # Average bps over the whole run for a stable ETA; fall back to the
        # most-recent-chunk speed when it's higher (early on, or after a stall).
        avg_bps = uploaded / elapsed
        eta_bps = max(self._last_speed_bps, avg_bps)
        eta = _format_eta(size - uploaded, eta_bps)
        speed_str = f"{format_bytes(int(self._last_speed_bps))}/s" \
            if self._last_speed_bps > 0 else "—"
        self._stats_label.setText(
            f"{pct}%  ·  {format_bytes(uploaded)} / {format_bytes(size)}  ·  "
            f"{speed_str}  ·  {eta}"
        )

    def _request_cancel(self) -> None:
        if self._terminal:
            self.reject()
            return
        reply = QMessageBox.question(
            self,
            "Cancel upload?",
            "The upload is still in progress. Cancel it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._cancel_btn.setEnabled(False)
            self._state_label.setText("Cancelling…")
            self._job.cancel()

    def _open_watch_url(self) -> None:
        if self._watch_url:
            webbrowser.open(self._watch_url, new=2)

    # ---- Window-close interception --------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._terminal:
            reply = QMessageBox.question(
                self,
                "Cancel upload?",
                "Closing this dialog will cancel the upload. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            # Request cancellation and close the dialog immediately. cancel()
            # closes the HTTP session so a wedged send unblocks promptly, and the
            # worker + thread outlive this dialog (kept by the module registry),
            # so they tear themselves down in the background. Crucially we do NOT
            # wait() on the GUI thread here — waiting on a worker stuck in a
            # stalled 16 MiB send is exactly what froze the whole app.
            self._job.cancel()
        super().closeEvent(event)


# ---- Module helpers ------------------------------------------------------

def _format_eta(bytes_remaining: int, bps: float) -> str:
    if bps <= 0 or bytes_remaining <= 0:
        return "ETA —"
    seconds = bytes_remaining / bps
    if seconds < 60:
        return f"ETA {seconds:.0f}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"ETA {m}m {s:02d}s"
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"ETA {h}h {m:02d}m"


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
