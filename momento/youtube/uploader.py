"""Resumable YouTube upload worker.

Runs entirely on a worker QThread; emits Qt signals back to the GUI.

The transfer is driven directly over a connection-pooled ``requests`` session
(``google.auth.transport.requests.AuthorizedSession``) rather than through
``google-api-python-client``'s ``MediaFileUpload`` + ``next_chunk()`` loop. The
library path runs over ``httplib2`` and tears the TCP/TLS connection down after
every chunk's ``308 Resume Incomplete`` ack, so each chunk re-handshakes and
re-ramps TCP slow-start — which caps throughput at single-digit MB/s no matter
how large the chunk is (google-api-python-client issues #625 / #793; the same
plateau shows on Google Drive's API while its desktop client saturates the link
on the same machine, proving it's the client path, not a YouTube throttle).
Holding ONE pooled keep-alive connection across all the chunked PUTs is what lets
the upload use the user's full bandwidth — the way YouTube Studio's own uploader
does. We still chunk (rather than one giant PUT) so the live progress bar, the
Cancel button, and resume-on-network-drop all keep working.

YouTube now applies a separate Video Uploads quota bucket. Google currently
documents a default allowance of 100 ``videos.insert`` calls per day for each
project, but users should treat the Cloud Console as authoritative because
Google can change or customise project quotas.

Public surface:

- ``UploadOptions`` — dataclass the dialog populates.
- ``UploadJob`` — QObject. Construct, ``moveToThread(worker)``, connect signals,
  ``thread.started.connect(job.run)``. Job emits one of
  ``finished(video_id, watch_url)`` or ``failed(error_message)`` exactly once and
  then doesn't talk again.
- ``cancel()`` — sets the cancel flag and aborts the upload's live sockets, so a
  request in flight fails immediately and surfaces as
  ``failed("Cancelled by user")``. If the cancel interrupts the final chunk,
  one status check decides: a video YouTube already created is reported as
  ``finished`` rather than as a cancellation.
"""

from __future__ import annotations

import _socket
import logging
import random
import socket
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock
from typing import Optional

import requests
from PyQt6.QtCore import QObject, pyqtSignal
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

logger = logging.getLogger(__name__)


class _SocketTracker:
    """Live sockets of one upload, so cancel() can abort a request in flight.

    ``Session.close()`` only closes idle pooled connections; the connection
    carrying the current PUT is checked out of the pool and keeps sending until
    its own timeouts expire (minutes on a stalled link). Shutting the socket
    down makes that blocked send/receive fail immediately.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._sockets: "weakref.WeakSet[socket.socket]" = weakref.WeakSet()
        self._aborted = False

    def add(self, sock: socket.socket) -> None:
        with self._lock:
            if not self._aborted:
                self._sockets.add(sock)
                return
        _shutdown_socket(sock)

    def discard(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.discard(sock)

    def abort(self) -> None:
        with self._lock:
            self._aborted = True
            sockets = list(self._sockets)
        for sock in sockets:
            _shutdown_socket(sock)


def _shutdown_socket(sock: socket.socket) -> None:
    """Abort a socket another thread may be blocked on, in send or receive.

    shutdown() alone is not enough on Windows: it does not wake a thread
    waiting in recv(), which is where an upload sits while YouTube acknowledges
    each chunk. Closing the OS handle wakes both directions. Both calls use the
    C-level base methods: SSLSocket's own shutdown()/close() discard its SSL
    state, which is unsafe while the worker is mid-call, and socket.close()
    defers the real close while http.client's response reader holds a
    reference. Closing marks the Python socket invalid, so the worker's next
    call fails cleanly instead of touching a recycled handle.
    """
    try:
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        _socket.socket.close(sock)
    except OSError:
        pass


def _tracking_pool_classes(tracker: _SocketTracker) -> dict:
    def track(base):
        class _Tracked(base):
            def connect(self) -> None:
                super().connect()
                if self.sock is not None:
                    tracker.add(self.sock)

            def close(self) -> None:
                sock = self.sock
                try:
                    super().close()
                finally:
                    if sock is not None:
                        tracker.discard(sock)

        return _Tracked

    class _HTTPPool(HTTPConnectionPool):
        ConnectionCls = track(HTTPConnection)

    class _HTTPSPool(HTTPSConnectionPool):
        ConnectionCls = track(HTTPSConnection)

    return {"http": _HTTPPool, "https": _HTTPSPool}


class _CancellableAdapter(HTTPAdapter):
    """A requests adapter whose connections register with a tracker."""

    def __init__(self, tracker: _SocketTracker, **kwargs) -> None:
        self._tracker = tracker
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs) -> None:
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = _tracking_pool_classes(self._tracker)


def _cancellable_session(credentials, tracker: _SocketTracker) -> AuthorizedSession:
    session = AuthorizedSession(credentials)
    adapter = _CancellableAdapter(tracker)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# The transport error text from requests includes the full request URL, and the
# resumable session URL is a bearer credential for the unfinished upload. Never
# log or display it; the exception type is enough to diagnose.
def _describe_network_error(exc: BaseException) -> str:
    return type(exc).__name__

# YouTube resumable-upload endpoints (note the /upload/ host prefix — distinct
# from the regular Data API host).
_VIDEOS_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
_THUMBNAIL_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"

# 16 MiB transfer chunks. The throughput win over the old client comes from the
# POOLED KEEP-ALIVE connection (no per-chunk TCP/TLS re-handshake + slow-start
# restart) — NOT from chunk size — so we can keep chunks modest without losing
# it. requests emits no in-flight progress within a single PUT, so chunk size ==
# progress granularity: 128 MiB froze the bar at 0% for the whole upload of any
# sub-128 MiB clip (most gaming clips), then jumped to 100%. 16 MiB gives smooth
# progress + snappy Cancel + low RAM while the per-chunk 308-ack overhead on a
# keep-alive link is still only ~one RTT (a fraction of a percent). Must stay a
# multiple of 256 KiB per the resumable protocol (16 MiB = 64 * 256 KiB).
_CHUNK_SIZE = 16 * 1024 * 1024

# requests timeouts: (connect, read). The read timeout governs waiting for a
# response AFTER the body is sent; sending the body itself isn't bounded by it,
# so a slow-but-progressing upload never trips it.
_CONNECT_TIMEOUT = 30
_READ_TIMEOUT = 300

# Retry policy for transient transfer failures: 5xx responses + raw network
# errors. 4xx (auth, quota, validation) are caller-actionable and not retried.
_MAX_RETRIES = 8
_RETRIABLE_STATUS_CODES = {500, 502, 503, 504}

# YouTube accepts custom JPEG/PNG thumbnails up to 2 MiB.  Read one byte past
# the limit so oversized files are rejected without ever loading an unbounded
# path into memory.
MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


class ThumbnailValidationError(ValueError):
    """A selected thumbnail is not safe or supported for upload."""


def validate_thumbnail(path: Path) -> tuple[str, bytes]:
    """Return ``(content_type, bytes)`` for a bounded JPEG/PNG thumbnail.

    The MIME type comes from file magic, with the suffix checked separately so
    a renamed arbitrary file cannot be presented to YouTube as an image.
    """
    suffix = path.suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png"}:
        raise ThumbnailValidationError(
            "Thumbnail must use a .jpg, .jpeg, or .png file extension."
        )

    try:
        with path.open("rb") as source:
            data = source.read(MAX_THUMBNAIL_BYTES + 1)
    except OSError as exc:
        raise ThumbnailValidationError(
            "Thumbnail file could not be read. Choose it again and retry."
        ) from exc

    if not data:
        raise ThumbnailValidationError("Thumbnail file is empty.")
    if len(data) > MAX_THUMBNAIL_BYTES:
        raise ThumbnailValidationError("Thumbnail must be 2 MiB or smaller.")

    if data.startswith(_PNG_MAGIC):
        content_type = "image/png"
        suffix_matches = suffix == ".png"
    elif data.startswith(_JPEG_MAGIC):
        content_type = "image/jpeg"
        suffix_matches = suffix in {".jpg", ".jpeg"}
    else:
        raise ThumbnailValidationError(
            "Thumbnail content must be a JPEG or PNG image."
        )

    if not suffix_matches:
        raise ThumbnailValidationError(
            "Thumbnail file extension does not match its image content."
        )
    return content_type, data


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
    # float: total bytes uploaded so far (carried as float so 64-bit sizes
    # don't overflow the 32-bit int a plain pyqtSignal(int) marshals to).
    bytes_uploaded = pyqtSignal(float)
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
        self._cancel = Event()
        # Every socket this upload opens, so cancel() can abort the request in
        # flight. Guarded internally: cancel() runs on the GUI thread while the
        # worker sends on its own thread.
        self._sockets = _SocketTracker()
        self._session: Optional[AuthorizedSession] = None
        self._session_lock = Lock()

    # ------ External controls --------------------------------------------

    def cancel(self) -> None:
        """Request cancellation. Safe to call from any thread.

        Sets the cancel flag and shuts down the live sockets. The flag alone is
        only checked between chunks, and closing the session only drops idle
        pooled connections, so a worker inside a blocking ``session.put()`` (a
        16 MiB body send on a slow or stalled link) would otherwise keep going
        for minutes — long enough for a "cancelled" upload to finish and go
        live. Aborting the socket makes that request fail immediately.
        """
        self._cancel.set()
        self._sockets.abort()
        with self._session_lock:
            sess = self._session
        if sess is not None:
            try:
                sess.close()
            except Exception:  # noqa: BLE001 — best-effort interrupt
                pass

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
            if isinstance(exc, (requests.RequestException, OSError)):
                # Transport errors embed the secret resumable-session URL.
                logger.error("Unhandled network error during YouTube upload (%s)",
                             _describe_network_error(exc))
                self.failed.emit(f"Upload failed ({_describe_network_error(exc)}).")
                return
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
        self.bytes_uploaded.emit(0.0)
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
            },
        }

        total = path.stat().st_size
        # AuthorizedSession wraps a requests.Session: urllib3 connection pooling
        # (keep-alive across the chunked PUTs — the whole point) plus automatic
        # access-token refresh mid-upload for multi-hour transfers.
        session = _cancellable_session(self._creds, self._sockets)
        with self._session_lock:
            self._session = session

        try:
            try:
                upload_url = self._initiate(session, body, total)
                self.state_changed.emit("Uploading")
                video = self._transfer(session, upload_url, path, total)
            except _UploadCancelled:
                raise
            except _UploadError as exc:
                self.failed.emit(str(exc))
                return
            except (requests.RequestException, OSError) as exc:  # noqa: BLE001
                # An aborted socket from cancel() surfaces here — treat it as a
                # cancellation, not a network failure to report.
                self._raise_if_cancelled()
                logger.error(
                    "Network error during YouTube upload (%s)",
                    _describe_network_error(exc),
                )
                self.failed.emit(
                    "Network error during upload "
                    f"({_describe_network_error(exc)}). Check your connection "
                    "and try again."
                )
                return

            video_id = (video or {}).get("id")
            if not video_id:
                self.failed.emit("Upload completed but YouTube did not return a video ID.")
                return

            # Thumbnail is best-effort. If it fails we still consider the upload a
            # success — the video is up; the user can swap the thumbnail later from
            # YouTube Studio. Skip it if the user already asked to cancel: the
            # video only exists because it finished before the cancel landed.
            if opts.thumbnail_path and not self._cancel.is_set():
                self.state_changed.emit("Setting thumbnail")
                try:
                    self._set_thumbnail(session, video_id, opts.thumbnail_path)
                except ThumbnailValidationError as exc:
                    logger.warning("Thumbnail validation failed (continuing): %s", exc)
                except Exception:  # noqa: BLE001
                    logger.exception("Thumbnail upload failed (continuing)")

            self.state_changed.emit("Finalising")
            self.progress.emit(100)
            self.bytes_uploaded.emit(float(total))
            watch_url = f"https://www.youtube.com/watch?v={video_id}"
            self.finished.emit(video_id, watch_url)
        finally:
            with self._session_lock:
                self._session = None
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass

    def _initiate(self, session: AuthorizedSession, body: dict, total: int) -> str:
        """Start a resumable session; return the session upload URL.

        POSTs the metadata + the content length/type headers; YouTube replies
        with the resumable session URI in the ``Location`` header.
        """
        headers = {
            "X-Upload-Content-Length": str(total),
            "X-Upload-Content-Type": "video/*",
        }
        try:
            resp = session.post(
                _VIDEOS_UPLOAD_URL,
                params={"uploadType": "resumable", "part": "snippet,status"},
                json=body,
                headers=headers,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
                allow_redirects=False,
            )
        except (requests.RequestException, OSError) as exc:
            # cancel() aborts the live socket to interrupt a request that is
            # still negotiating the resumable upload. Preserve cancellation as
            # a user action instead of relabelling it as a network failure.
            self._raise_if_cancelled()
            raise _UploadError(
                "Couldn't start the upload (network error: "
                f"{_describe_network_error(exc)})."
            )
        if resp.status_code not in (200, 201):
            raise _UploadError(_format_resp_error(resp))
        location = resp.headers.get("Location")
        if not location:
            raise _UploadError("YouTube did not return an upload URL (no Location header).")
        return location

    def _transfer(
        self, session: AuthorizedSession, upload_url: str, path: Path, total: int
    ) -> dict:
        """Stream the file to the resumable session in keep-alive chunks.

        Each non-final chunk is acked with ``308`` + a ``Range`` header telling
        us how much YouTube has; the final chunk returns ``200/201`` with the
        video resource. On a transient failure we re-query the confirmed offset
        and resume from there (so a network blip costs at most one chunk, never
        the whole file).
        """
        uploaded = 0
        last_tick = time.monotonic()
        delay = 1.0
        failures = 0

        with open(path, "rb") as f:
            while uploaded < total:
                self._raise_if_cancelled()
                f.seek(uploaded)
                data = f.read(_CHUNK_SIZE)
                if not data:
                    break
                start = uploaded
                end = start + len(data) - 1

                try:
                    resp = session.put(
                        upload_url,
                        data=data,
                        headers={"Content-Range": f"bytes {start}-{end}/{total}"},
                        timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
                        allow_redirects=False,  # 308 is "Resume Incomplete" here, NOT a redirect
                    )
                except (requests.RequestException, OSError) as exc:
                    if self._cancel.is_set():
                        # cancel() aborted this request. If it was carrying the
                        # final bytes, YouTube may already have created the
                        # video; find out rather than report a cancellation for
                        # a video that is live.
                        if end + 1 >= total:
                            resource = self._status_after_cancel(upload_url, total)
                            if resource is not None:
                                return self._complete(resource, total)
                        raise _UploadCancelled()
                    failures += 1
                    if failures > _MAX_RETRIES:
                        raise _UploadError(
                            "Upload failed after repeated network errors "
                            f"({_describe_network_error(exc)})."
                        )
                    logger.warning(
                        "Network error sending chunk at byte %d (try %d/%d): %s",
                        start, failures, _MAX_RETRIES, _describe_network_error(exc),
                    )
                    self._sleep_backoff(delay)
                    delay = min(30.0, delay * 2 + random.random())
                    uploaded, resource = self._query_status(
                        session, upload_url, total, uploaded
                    )
                    if resource is not None:
                        return self._complete(resource, total)
                    continue

                code = resp.status_code
                now = time.monotonic()

                if code in (200, 201):
                    try:
                        return self._complete(resp.json(), total)
                    except ValueError:
                        return self._complete({}, total)  # success, unusable body

                if code == 308:
                    confirmed = _confirmed_offset(resp)
                    if confirmed is None:
                        # A 308 whose Range header is absent/unparseable means
                        # "unknown bytes stored" — do NOT assume the whole chunk
                        # persisted (that would skip un-acked bytes and corrupt
                        # the upload). Ask YouTube for the authoritative offset.
                        confirmed, resource = self._query_status(
                            session, upload_url, total, start
                        )
                        if resource is not None:
                            return self._complete(resource, total)
                    if confirmed <= start:
                        # No forward progress — guard against an infinite loop.
                        failures += 1
                        if failures > _MAX_RETRIES:
                            raise _UploadError("Upload stalled: YouTube reported no progress.")
                        self._sleep_backoff(delay)
                        delay = min(30.0, delay * 2 + random.random())
                        uploaded, resource = self._query_status(
                            session, upload_url, total, start
                        )
                        if resource is not None:
                            return self._complete(resource, total)
                        continue
                    chunk_bytes = confirmed - start
                    uploaded = confirmed
                    elapsed = max(now - last_tick, 1e-6)
                    pct = int(uploaded * 100 / total) if total else 0
                    self.progress.emit(max(0, min(99, pct)))
                    self.bytes_uploaded.emit(float(uploaded))
                    self.speed.emit(chunk_bytes / elapsed)
                    last_tick = now
                    failures = 0
                    delay = 1.0
                    continue

                if code in _RETRIABLE_STATUS_CODES:
                    failures += 1
                    if failures > _MAX_RETRIES:
                        raise _UploadError(_format_resp_error(resp))
                    logger.warning(
                        "Transient HTTP %s sending chunk at byte %d (try %d/%d)",
                        code, start, failures, _MAX_RETRIES,
                    )
                    self._sleep_backoff(delay)
                    delay = min(30.0, delay * 2 + random.random())
                    uploaded, resource = self._query_status(
                        session, upload_url, total, uploaded
                    )
                    if resource is not None:
                        return self._complete(resource, total)
                    continue

                # Anything else (4xx etc.) is caller-actionable — surface it.
                raise _UploadError(_format_resp_error(resp))

        # Sent every byte but never saw the 200/201 that carries the video
        # resource (the final ack was lost, or a resume reported completion
        # without a body). One authoritative status query recovers it — the
        # video is almost certainly live, so reporting failure here would tempt
        # a duplicate re-upload that burns the scarce daily quota.
        _, resource = self._query_status(session, upload_url, total, total)
        if resource is not None:
            return self._complete(resource, total)
        raise _UploadError("Upload finished sending but YouTube did not confirm it.")

    def _complete(self, resource: dict, total: int) -> dict:
        """Mark the transfer done (progress 100%) and return the video resource."""
        self.bytes_uploaded.emit(float(total))
        return resource or {}

    def _query_status(
        self, session: AuthorizedSession, upload_url: str, total: int, fallback: int
    ) -> tuple[int, Optional[dict]]:
        """Query the resumable session's state to resume (or recover) an upload.

        A ``PUT`` with ``Content-Range: bytes */total`` and an empty body is the
        resumable-protocol status query. Returns ``(next_offset, resource)``:

        - ``resource`` is the completed video JSON when YouTube reports the upload
          already complete (200/201) — recovering it prevents reporting a false
          failure over a video that is genuinely live when the final ack was lost.
        - otherwise ``resource`` is ``None`` and ``next_offset`` is the byte to
          resume from (the confirmed offset on a 308, else ``fallback``).
        """
        try:
            resp = session.put(
                upload_url,
                headers={"Content-Range": f"bytes */{total}"},
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
                allow_redirects=False,
            )
        except (requests.RequestException, OSError):
            return fallback, None
        if resp.status_code in (200, 201):
            try:
                return total, (resp.json() or {})
            except ValueError:
                return total, {}  # complete but no/invalid body — still a success
        if resp.status_code == 308:
            off = _confirmed_offset(resp)
            return (off if off is not None else fallback), None
        return fallback, None

    def _status_after_cancel(self, upload_url: str, total: int) -> Optional[dict]:
        """One bounded status check after a cancel interrupted the final chunk.

        Uses a fresh, untracked session: the upload's own sockets are aborted.
        Returns the video resource if YouTube already completed the upload.
        """
        probe = AuthorizedSession(self._creds)
        try:
            resp = probe.put(
                upload_url,
                headers={"Content-Range": f"bytes */{total}"},
                timeout=(10, 15),
                allow_redirects=False,
            )
        except (requests.RequestException, OSError) as exc:
            logger.warning(
                "Couldn't confirm upload state after cancel (%s)",
                _describe_network_error(exc),
            )
            return None
        finally:
            probe.close()
        if resp.status_code not in (200, 201):
            return None
        try:
            return resp.json() or {}
        except ValueError:
            return {}

    def _set_thumbnail(
        self, session: AuthorizedSession, video_id: str, thumb_path: Path
    ) -> None:
        content_type, data = validate_thumbnail(thumb_path)
        resp = session.post(
            _THUMBNAIL_UPLOAD_URL,
            params={"videoId": video_id, "uploadType": "media"},
            data=data,
            headers={"Content-Type": content_type},
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            allow_redirects=False,
        )
        if resp.status_code not in (200, 201):
            raise _UploadError(_format_resp_error(resp))

    def _sleep_backoff(self, delay: float) -> None:
        """Sleep ``delay`` seconds in short ticks so cancellation lands fast."""
        slept = 0.0
        while slept < delay:
            self._raise_if_cancelled()
            step = min(0.2, delay - slept)
            time.sleep(step)
            slept += step

    def _raise_if_cancelled(self) -> None:
        if self._cancel.is_set():
            raise _UploadCancelled()


class _UploadCancelled(Exception):
    """Internal sentinel — caught at run() boundary, mapped to ``failed``."""


class _UploadError(Exception):
    """Carries an already-formatted, user-facing failure message."""


# ---------- Resumable-protocol + error helpers ----------------------------

def _confirmed_offset(resp: requests.Response) -> Optional[int]:
    """Next byte to send, parsed from a 308's ``Range: bytes=0-N`` header.

    The range is inclusive, so the next offset is ``N + 1``. Returns ``None`` when
    the header is missing/unparseable — the caller must then query the
    authoritative offset rather than assume the whole chunk persisted.
    """
    rng = resp.headers.get("Range")  # e.g. "bytes=0-12345"
    if rng and "-" in rng:
        try:
            return int(rng.rsplit("-", 1)[1]) + 1
        except ValueError:
            pass
    return None


def _format_resp_error(resp: requests.Response) -> str:
    """Turn a failed ``requests`` response into something a user can act on."""
    status = resp.status_code
    reason = ""
    try:
        body = resp.json()
        errors = body.get("error", {}).get("errors", [])
        if errors:
            reason = errors[0].get("reason", "")
        if not reason:
            reason = body.get("error", {}).get("status", "") or ""
    except Exception:  # noqa: BLE001
        reason = ""
    return _error_message(status, reason)


def _error_message(status: int, reason: str) -> str:
    """Map a YouTube (HTTP status, reason) pair to a user-facing message."""
    if status == 401:
        return (
            "YouTube rejected the saved sign-in (the token may have been "
            "revoked or expired). Open Settings → YouTube → Disconnect, "
            "then connect again."
        )
    if status == 403:
        if reason in ("quotaExceeded", "dailyLimitExceeded"):
            return (
                "This Google Cloud project's daily YouTube upload quota is "
                "exhausted. Check YouTube Data API v3 quotas in your own "
                "Google Cloud project, or try again after Google resets it."
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
