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

from PyQt6.QtCore import Qt, QThread
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
        self._terminal = False  # set when finished/failed signal arrives
        self._video_id = ""
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
        self._thread = QThread(self)
        self._job: UploadJob = UploadJob(credentials, options)
        self._job.moveToThread(self._thread)

        self._thread.started.connect(self._job.run)
        self._job.progress.connect(self._on_progress)
        self._job.speed.connect(self._on_speed)
        self._job.state_changed.connect(self._on_state)
        self._job.finished.connect(self._on_finished)
        self._job.failed.connect(self._on_failed)
        # Always quit the thread on either terminal signal so the QThread
        # actually finishes and emits its own finished signal for cleanup.
        self._job.finished.connect(self._thread.quit)
        self._job.failed.connect(self._thread.quit)
        # Defer deletion until after the worker has fully exited.
        self._thread.finished.connect(self._job.deleteLater)

        self._thread.start()

    # ---- Job signal handlers --------------------------------------------

    def _on_progress(self, pct: int) -> None:
        self._progress.setValue(pct)
        self._refresh_stats(pct)

    def _on_speed(self, bps: float) -> None:
        self._last_speed_bps = max(0.0, bps)
        self._refresh_stats(self._progress.value())

    def _on_state(self, state: str) -> None:
        self._state_label.setText(f"{state}…")

    def _on_finished(self, video_id: str, watch_url: str) -> None:
        self._terminal = True
        self._video_id = video_id
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
        # Estimate average bps over the whole run for a more stable ETA;
        # fall back to most-recent-chunk speed if we don't have meaningful
        # progress yet.
        avg_bps = (pct / 100.0) * self._options.file_path.stat().st_size / elapsed
        eta_bps = max(self._last_speed_bps, avg_bps)
        size = self._options.file_path.stat().st_size if self._options.file_path.is_file() else 0
        uploaded = int(size * pct / 100)
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
            self._job.cancel()
            # Give the worker up to 2 s to surface the cancellation cleanly
            # before tearing down. If it overruns we accept the close anyway
            # — the thread will detach and the deleteLater chain still runs.
            self._thread.wait(2000)
        # Ensure thread is fully done before letting the dialog be destroyed.
        if self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(1000)
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
