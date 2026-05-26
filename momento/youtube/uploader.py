"""Resumable YouTube upload worker.

Runs entirely on a worker QThread; emits Qt signals back to the GUI. Uses
``MediaFileUpload(resumable=True)`` so a partial network failure mid-upload
resumes from the last acknowledged chunk rather than re-sending the whole
file.

Quota cost reminder: each ``videos.insert`` is **1600 units**. The default
project quota is 10000 units / day — i.e. **6 uploads / day across ALL users
of one Google Cloud project**. After Google verification we'll request a
quota increase based on real usage data.

Public surface:

- ``UploadOptions`` — dataclass the dialog populates
- ``UploadJob`` — QObject. Construct, ``moveToThread(worker)``, connect
  signals, ``thread.started.connect(job.run)``. Job emits one of
  ``finished(video_id, watch_url)`` or ``failed(error_message)`` exactly
  once and then doesn't talk again.
- ``cancel()`` — flips an atomic; the next chunk boundary surfaces the
  cancellation as ``failed("Cancelled by user")``.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)

# 4 MiB chunks: big enough to keep HTTP overhead reasonable on fast links,
# small enough to give responsive progress updates and let cancellation
# take effect within seconds. YouTube docs recommend a multiple of 256 KiB
# and at least 256 KiB; 4 MiB is comfortable in the middle.
_CHUNK_SIZE = 4 * 1024 * 1024

# Retry policy for resumable upload chunks. We back off on 5xx + connection
# errors (the API client raises HttpError for the former, OSError/IOError
# for the latter). Stops well short of the API client's own retry helpers,
# which have surprising failure modes on partially-uploaded chunks.
_MAX_RETRIES = 5
_RETRIABLE_STATUS_CODES = {500, 502, 503, 504}


@dataclass
class UploadOptions:
    """Everything the dialog collects, in one bag."""

    file_path: Path
    title: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    category_id: int = 20  # Gaming
    privacy: str = "unlisted"  # public / unlisted / private
    thumbnail_path: Optional[Path] = None
    # Required by YouTube as of 2020. False = not specifically directed at
    # kids — for gaming clips this is the correct value. We don't surface
    # this in the UI; "made for kids" content has gameplay restrictions
    # (no comments, lower watch-time recommendations, no personalized ads)
    # and creators uploading kid content would normally do it via YouTube
    # Studio with full context.
    made_for_kids: bool = False


class UploadJob(QObject):
    """Resumable upload as a Qt-friendly worker.

    Lifecycle:
        job = UploadJob(creds, options)
        thread = QThread()
        job.moveToThread(thread)
        thread.started.connect(job.run)
        job.finished.connect(lambda vid, url: ...)
        job.failed.connect(lambda msg: ...)
        job.finished.connect(thread.quit)
        job.failed.connect(thread.quit)
        thread.start()

    Cancellation is cooperative; call ``cancel()`` from any thread.
    """

    # int: 0..100 (clamped). Emitted on every chunk acknowledgement.
    progress = pyqtSignal(int)
    # float: bytes-per-second over the last chunk. -1 if unknown yet.
    speed = pyqtSignal(float)
    # Human-readable state: "Preparing", "Uploading", "Setting thumbnail",
    # "Finalising". UI surfaces verbatim.
    state_changed = pyqtSignal(str)
    # Final success. Args: video_id, full watch URL.
    finished = pyqtSignal(str, str)
    # Final failure. Single human-readable message; UI shows in a dialog.
    failed = pyqtSignal(str)

    def __init__(self, credentials: Credentials, options: UploadOptions) -> None:
        super().__init__()
        self._creds = credentials
        self._options = options
        self._cancel = threading.Event()

    # ------ External controls --------------------------------------------

    def cancel(self) -> None:
        """Request a graceful stop at the next chunk boundary.

        Safe to call from any thread. The worker emits ``failed("Cancelled
        by user")`` shortly after; receivers should treat it as a normal
        terminal state (don't show an error dialog for it).
        """
        self._cancel.set()

    # ------ Worker entry point -------------------------------------------

    def run(self) -> None:
        """Drive the upload to a terminal signal.

        Always emits exactly one of ``finished`` / ``failed``. Defensive
        catch-all at the end so a programmer error here never leaves the
        UI's progress dialog spinning forever.
        """
        try:
            self._do_upload()
        except _UploadCancelled:
            self.failed.emit("Cancelled by user")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unhandled error during YouTube upload")
            self.failed.emit(f"Upload failed: {exc}")

    # ------ Internals -----------------------------------------------------

    def _do_upload(self) -> None:
        opts = self._options
        path = opts.file_path
        if not path.is_file():
            self.failed.emit(f"File no longer exists: {path}")
            return

        self.state_changed.emit("Preparing")
        self.progress.emit(0)
        self.speed.emit(-1.0)

        body = {
            "snippet": {
                "title": opts.title,
                "description": opts.description,
                "tags": opts.tags,
                "categoryId": str(opts.category_id),
            },
            "status": {
                "privacyStatus": opts.privacy,
                "selfDeclaredMadeForKids": opts.made_for_kids,
                # Default. Content owners can set "embeddable": False etc;
                # we leave platform defaults so the upload behaves like one
                # done through YouTube Studio.
            },
        }

        # cache_discovery=False — Google's discovery doc cache races with
        # itself when two threads build clients simultaneously, and produces
        # noisy warnings about missing file_cache. We don't need the cache.
        yt = build(
            "youtube", "v3", credentials=self._creds, cache_discovery=False
        )

        media = MediaFileUpload(
            str(path),
            chunksize=_CHUNK_SIZE,
            resumable=True,
            mimetype="video/*",
        )

        request = yt.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        total = path.stat().st_size
        uploaded = 0
        last_tick = time.monotonic()
        response = None

        self.state_changed.emit("Uploading")

        # Resumable upload loop: call next_chunk() until response is non-None.
        # next_chunk() returns (status, response) where status has
        # resumable_progress (bytes uploaded so far) and resumable_progress
        # is None on the final chunk.
        while response is None:
            self._raise_if_cancelled()

            try:
                status, response = self._with_retries(request.next_chunk)
            except _UploadCancelled:
                raise
            except HttpError as exc:
                self.failed.emit(_format_http_error(exc))
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("next_chunk() failed irrecoverably")
                self.failed.emit(f"Network error during upload: {exc}")
                return

            now = time.monotonic()
            if status:
                chunk_bytes = status.resumable_progress - uploaded
                uploaded = status.resumable_progress
                pct = int(uploaded * 100 / total) if total else 0
                self.progress.emit(max(0, min(99, pct)))

                elapsed = max(now - last_tick, 1e-6)
                self.speed.emit(chunk_bytes / elapsed)
                last_tick = now

        video_id = (response or {}).get("id")
        if not video_id:
            self.failed.emit("Upload completed but YouTube did not return a video ID.")
            return

        # Thumbnail is best-effort. If it fails we still consider the upload
        # a success — the video is up, the user can swap the thumbnail
        # later from YouTube Studio. Surface a soft warning via state_changed.
        if opts.thumbnail_path and opts.thumbnail_path.is_file():
            self.state_changed.emit("Setting thumbnail")
            try:
                self._set_thumbnail(yt, video_id, opts.thumbnail_path)
            except Exception:  # noqa: BLE001
                logger.exception("Thumbnail upload failed (continuing)")

        self.state_changed.emit("Finalising")
        self.progress.emit(100)
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        self.finished.emit(video_id, watch_url)

    def _set_thumbnail(self, yt, video_id: str, thumb_path: Path) -> None:
        media = MediaFileUpload(str(thumb_path), resumable=False)
        yt.thumbnails().set(videoId=video_id, media_body=media).execute()

    def _with_retries(self, call):
        """Invoke ``call()`` with exponential backoff for transient failures.

        Retries on 5xx HTTP responses + raw network errors. Does NOT retry
        4xx (auth, quota, validation) — those are caller-actionable.
        """
        delay = 1.0
        for attempt in range(_MAX_RETRIES):
            self._raise_if_cancelled()
            try:
                return call()
            except HttpError as exc:
                if exc.resp.status not in _RETRIABLE_STATUS_CODES:
                    raise
                logger.warning(
                    "Transient HTTP %s during upload (attempt %d/%d); "
                    "retrying in %.1fs",
                    exc.resp.status, attempt + 1, _MAX_RETRIES, delay,
                )
            except (OSError, IOError) as exc:
                logger.warning(
                    "Network error during upload (attempt %d/%d): %s; "
                    "retrying in %.1fs",
                    attempt + 1, _MAX_RETRIES, exc, delay,
                )

            # Sleep in short ticks so cancellation takes effect quickly.
            slept = 0.0
            while slept < delay:
                self._raise_if_cancelled()
                step = min(0.2, delay - slept)
                time.sleep(step)
                slept += step

            # Exponential with jitter, capped at 30 s.
            delay = min(30.0, delay * 2 + random.random())

        # Out of retries: re-raise the last exception by letting the next
        # call propagate. We let it through unwrapped so the dispatcher
        # above can render its specific message.
        return call()

    def _raise_if_cancelled(self) -> None:
        if self._cancel.is_set():
            raise _UploadCancelled()


class _UploadCancelled(Exception):
    """Internal sentinel — caught at run() boundary, mapped to ``failed``."""


# ---------- Error message formatting --------------------------------------

def _format_http_error(exc: HttpError) -> str:
    """Turn an HttpError into something a user can act on.

    YouTube's error responses are JSON with a ``reason`` field that maps
    to specific situations. We translate the common ones; everything else
    falls through to the raw API message.
    """
    status = exc.resp.status
    reason = ""
    try:
        # exc.content is bytes. The HttpError class parses it lazily into
        # ``error_details`` on some versions; fall back to raw decode.
        import json as _json
        body = _json.loads(exc.content.decode("utf-8", errors="replace"))
        errors = body.get("error", {}).get("errors", [])
        if errors:
            reason = errors[0].get("reason", "")
    except Exception:  # noqa: BLE001
        reason = ""

    if status == 401:
        return (
            "YouTube rejected the saved sign-in (the token may have been "
            "revoked or expired). Open Settings → YouTube → Disconnect, "
            "then connect again."
        )
    if status == 403:
        if reason in ("quotaExceeded", "dailyLimitExceeded"):
            return (
                "Daily YouTube upload quota exceeded. The quota resets at "
                "midnight Pacific time (08:00 UTC). Try again then, or ask "
                "the Momento developer to request a higher quota."
            )
        if reason == "youtubeSignupRequired":
            return (
                "The signed-in Google account has no YouTube channel. "
                "Create one at youtube.com, then reconnect."
            )
        if reason == "forbidden":
            return (
                "YouTube refused the upload (HTTP 403). Common causes: the "
                "Cloud project isn't verified for upload scope, or the "
                "account is not on the project's Test users list."
            )
        return f"YouTube refused the upload (HTTP 403, reason: {reason or 'unknown'})."
    if status == 400:
        return (
            f"YouTube rejected the video metadata (HTTP 400, reason: "
            f"{reason or 'unknown'}). Check the title length, tag total, "
            "and category."
        )
    if status == 413:
        return "File too large for YouTube. Maximum is 256 GB or 12 hours."

    return f"Upload failed with HTTP {status} ({reason or 'unknown reason'})."
