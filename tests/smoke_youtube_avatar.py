"""Regression checks for bounded, HTTPS-only YouTube avatar caching."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.util import paths  # noqa: E402
from momento.youtube import auth  # noqa: E402


class _FakeResponse:
    def __init__(self, *, url: str, chunks=(), content_length: int = 0) -> None:
        self.url = url
        self._chunks = tuple(chunks)
        self.headers = {"Content-Length": str(content_length)}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        yield from self._chunks


def main() -> int:
    passed = 0
    failed = 0

    def check(condition: bool, label: str) -> None:
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"PASS - {label}")
        else:
            failed += 1
            print(f"FAIL - {label}")

    original_avatar_path = paths.youtube_avatar_path
    original_get = auth.requests.get
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = root / "local-secret.bin"
        target = root / "avatar.png"
        source.write_bytes(b"not a remote avatar")
        paths.youtube_avatar_path = lambda: target
        try:
            result = auth.cache_channel_avatar(source.as_uri())
        finally:
            paths.youtube_avatar_path = original_avatar_path

        check(result is None, "local file URLs are rejected")
        check(not target.exists(), "rejected URL writes no avatar cache")

        paths.youtube_avatar_path = lambda: target
        auth.requests.get = lambda *args, **kwargs: _FakeResponse(
            url="https://yt3.example.test/avatar.png",
            chunks=(b"small-avatar",),
            content_length=len(b"small-avatar"),
        )
        try:
            result = auth.cache_channel_avatar("https://yt3.example.test/avatar.png")
        finally:
            auth.requests.get = original_get
            paths.youtube_avatar_path = original_avatar_path
        check(result == target, "bounded HTTPS avatar is cached")
        check(target.read_bytes() == b"small-avatar", "cached avatar bytes are intact")

        target.unlink()
        paths.youtube_avatar_path = lambda: target
        auth.requests.get = lambda *args, **kwargs: _FakeResponse(
            url="https://yt3.example.test/avatar.png",
            content_length=auth._MAX_CHANNEL_AVATAR_BYTES + 1,
        )
        try:
            result = auth.cache_channel_avatar("https://yt3.example.test/avatar.png")
        finally:
            auth.requests.get = original_get
            paths.youtube_avatar_path = original_avatar_path
        check(result is None, "oversized avatar is rejected")
        check(not target.exists(), "oversized avatar writes no cache file")

    print(f"\n{passed}/{passed + failed} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
