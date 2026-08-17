"""Regression: YouTube resumable-upload resume/recovery correctness.

Covers the two uploader fixes from the 2026-07-04 audit:

  #2 (HIGH) LOST FINAL ACK — if the last chunk's response is lost (dropped TCP /
     edge 5xx) AFTER YouTube committed the bytes, the resume-status query returns
     the video resource. It must be recovered as SUCCESS, not reported as
     "Upload finished sending but YouTube did not confirm it." (a false failure
     that tempts a duplicate re-upload burning the scarce daily quota).

  #5 (MED) MISSING RANGE ON 308 — a 308 whose Range header is absent means
     "unknown bytes stored". The transfer must query the authoritative offset,
     NOT optimistically assume the whole chunk persisted (which would skip
     un-acked bytes and corrupt the upload).

Pure logic with a fake requests session — no network — so it runs on CI.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402
from PyQt6.QtCore import QCoreApplication  # noqa: E402

import momento.youtube.uploader as up  # noqa: E402
from momento.youtube.uploader import (  # noqa: E402
    UploadJob,
    UploadOptions,
    _confirmed_offset,
)

_app = QCoreApplication.instance() or QCoreApplication([])
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


class _Resp:
    def __init__(self, status, headers=None, body=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


class _FakeSession:
    """Returns/raises scripted results for each .put(); records the kwargs."""

    def __init__(self, actions):
        self._actions = list(actions)
        self.calls = []

    def put(self, url, **kw):
        self.calls.append(kw)
        action = self._actions.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


def _job(path: Path) -> UploadJob:
    job = UploadJob(object(), UploadOptions(file_path=path, title="t"))
    job._sleep_backoff = lambda _d: None  # don't actually sleep the backoff
    return job


def test_confirmed_offset() -> None:
    check(_confirmed_offset(_Resp(308, {"Range": "bytes=0-99"})) == 100,
          "_confirmed_offset: valid Range -> N+1")
    check(_confirmed_offset(_Resp(308, {})) is None,
          "_confirmed_offset: missing Range -> None")
    check(_confirmed_offset(_Resp(308, {"Range": "nonsense"})) is None,
          "_confirmed_offset: unparseable Range -> None")


def test_lost_final_ack(tmp: Path) -> None:
    src = tmp / "clip.mp4"
    src.write_bytes(b"x" * 500)
    job = _job(src)
    # The one chunk PUT drops its response after the bytes were committed; the
    # status query then reports the upload complete WITH the video resource.
    sess = _FakeSession([
        requests.ConnectionError("connection reset after body sent"),
        _Resp(200, body={"id": "vid123"}),
    ])
    result = job._transfer(sess, "http://upload", src, 500)
    check(result == {"id": "vid123"},
          "lost final ack: recovered as success with the real video id")


def test_missing_range_308(tmp: Path) -> None:
    src = tmp / "clip2.mp4"
    src.write_bytes(b"y" * 1000)
    old_chunk = up._CHUNK_SIZE
    up._CHUNK_SIZE = 256  # force multiple chunks from a tiny file
    try:
        job = _job(src)
        sess = _FakeSession([
            _Resp(308, headers={}),                        # chunk0: 308, NO Range
            _Resp(308, headers={"Range": "bytes=0-127"}),  # status query -> only 128 stored
            _Resp(308, headers={"Range": "bytes=0-511"}),  # chunk @128 -> 512
            _Resp(200, body={"id": "ok"}),                 # final -> done
        ])
        result = job._transfer(sess, "http://upload", src, 1000)
        check(result == {"id": "ok"}, "missing-Range 308: completes successfully")
        queried = any(
            c.get("headers", {}).get("Content-Range") == "bytes */1000"
            for c in sess.calls
        )
        check(queried, "missing-Range 308: issues an authoritative status query")
        # The query said only 128 bytes were stored; the resume MUST continue
        # from 128, NOT optimistically from the full chunk end (256).
        third = sess.calls[2].get("headers", {}).get("Content-Range", "")
        check(third.startswith("bytes 128-"),
              "missing-Range 308: resumes from the queried offset, not end+1")
    finally:
        up._CHUNK_SIZE = old_chunk


def main() -> int:
    test_confirmed_offset()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_lost_final_ack(tmp)
        test_missing_range_308(tmp)
    print(f"\n{_passed}/{_passed + _failed} checks passed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
