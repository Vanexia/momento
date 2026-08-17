"""Bounded GitHub update client and atomic staging-cache checks."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from momento.updater.cache import UpdateCache  # noqa: E402
from momento.updater.client import (  # noqa: E402
    GITHUB_LATEST_API,
    MAX_RELEASE_BYTES,
    UpdateClient,
    UpdateStatus,
)
from momento.updater.metadata import UpdateMetadataError, canonical_manifest_bytes  # noqa: E402


_results: list[tuple[str, bool]] = []


def check(label: str, condition: bool) -> None:
    _results.append((label, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'} - {label}")


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        url: str,
        status: int = 200,
        chunks: list[bytes] | None = None,
        history: list["FakeResponse"] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._body = body
        self.url = url
        self.status_code = status
        self.history = history or []
        self.headers = {"Content-Length": str(len(body)), **(headers or {})}
        self._chunks = chunks
        self.closed = False

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}", response=self)

    def close(self) -> None:
        self.closed = True

    def iter_content(self, chunk_size: int = 64 * 1024):
        chunks = self._chunks
        if chunks is None:
            chunks = [
                self._body[offset : offset + chunk_size]
                for offset in range(0, len(self._body), chunk_size)
            ]
        yield from chunks


class FakeSession:
    def __init__(self, responses: dict[str, FakeResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._lock = threading.Lock()

    def get(self, url: str, **kwargs):
        with self._lock:
            self.calls.append((url, kwargs))
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


def _public_pem(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _fixture(
    private_key: Ed25519PrivateKey,
    *,
    version: str = "0.2.3",
    installer: bytes = b"test installer payload" * 4096,
    signature_key: Ed25519PrivateKey | None = None,
    release_overrides: dict[str, object] | None = None,
    metadata_version: int = 1,
    expires_at: str = "2027-02-13T12:00:00Z",
) -> tuple[dict[str, FakeResponse | Exception], bytes, bytes, bytes, str]:
    filename = f"MomentoSetup-{version}.exe"
    release_base = f"https://github.com/Vanexia/momento/releases/download/v{version}"
    installer_url = f"{release_base}/{filename}"
    metadata_url = f"{release_base}/Momento-update.json"
    signature_url = f"{release_base}/Momento-update.json.sig"
    payload: dict[str, object] = {
        "channel": "stable",
        "expires_at": expires_at,
        "installer": {
            "name": filename,
            "sha256": hashlib.sha256(installer).hexdigest(),
            "size": len(installer),
            "url": installer_url,
        },
        "minimum_updater_version": "0.2.2",
        "metadata_version": metadata_version,
        "published_at": "2026-08-17T12:00:00Z",
        "schema": 1,
        "version": version,
    }
    metadata = canonical_manifest_bytes(payload)
    signature = base64.b64encode((signature_key or private_key).sign(metadata)) + b"\n"
    assets = [
        {
            "name": "Momento-update.json",
            "browser_download_url": metadata_url,
            "size": len(metadata),
        },
        {
            "name": "Momento-update.json.sig",
            "browser_download_url": signature_url,
            "size": len(signature),
        },
        {
            "name": filename,
            "browser_download_url": installer_url,
            "size": len(installer),
            "digest": f"sha256:{hashlib.sha256(installer).hexdigest()}",
        },
    ]
    release: dict[str, object] = {
        "draft": False,
        "prerelease": False,
        "tag_name": f"v{version}",
        "assets": assets,
    }
    if release_overrides:
        release.update(release_overrides)
    release_bytes = json.dumps(release, separators=(",", ":")).encode("utf-8")
    responses: dict[str, FakeResponse | Exception] = {
        GITHUB_LATEST_API: FakeResponse(release_bytes, url=GITHUB_LATEST_API),
        metadata_url: FakeResponse(metadata, url=metadata_url),
        signature_url: FakeResponse(signature, url=signature_url),
        installer_url: FakeResponse(installer, url=installer_url),
    }
    return responses, metadata, signature, installer, installer_url


def test_valid_download_and_cache(tmp: Path, private_key: Ed25519PrivateKey) -> None:
    responses, _metadata, _signature, installer, installer_url = _fixture(private_key)
    session = FakeSession(responses)
    cache = UpdateCache(tmp / "cache", _public_pem(private_key))
    client = UpdateClient(session=session, cache=cache, public_key=_public_pem(private_key))
    result = client.check(current_version="0.2.2")

    check("valid newer release is staged", result.status is UpdateStatus.AVAILABLE and result.staged is not None)
    assert result.staged is not None
    check("staged installer bytes are exact", result.staged.installer_path.read_bytes() == installer)
    check("partial download is atomically removed", not list((tmp / "cache").glob("*.partial")))
    check("GitHub requests use streaming and bounded timeouts", all(call[1].get("stream") is True and isinstance(call[1].get("timeout"), tuple) for call in session.calls))
    check("installer is fetched from the signed exact URL", any(call[0] == installer_url for call in session.calls))

    second_session = FakeSession(
        {GITHUB_LATEST_API: requests.ConnectionError("offline")}
    )
    cached = UpdateClient(
        session=second_session,
        cache=cache,
        public_key=_public_pem(private_key),
    ).check(current_version="0.2.2")
    check(
        "verified staged update remains the offline fallback",
        cached.status is UpdateStatus.AVAILABLE
        and [url for url, _ in second_session.calls] == [GITHUB_LATEST_API],
    )
    check("staged update is reverified before use", cache.verify(cached.staged, current_version="0.2.2"))

    newer_responses, *_ = _fixture(
        private_key,
        version="0.2.4",
        metadata_version=2,
    )
    superseding_session = FakeSession(newer_responses)
    superseding = UpdateClient(
        session=superseding_session,
        cache=cache,
        public_key=_public_pem(private_key),
    ).check(current_version="0.2.2")
    check(
        "a newer signed release supersedes a cached failed target",
        superseding.status is UpdateStatus.AVAILABLE
        and superseding.staged is not None
        and str(superseding.staged.manifest.version) == "0.2.4",
    )


def test_current_release_is_authenticated(tmp: Path, private_key: Ed25519PrivateKey) -> None:
    responses, *_ = _fixture(private_key, version="0.2.2")
    session = FakeSession(responses)
    result = UpdateClient(
        session=session,
        cache=UpdateCache(tmp / "current", _public_pem(private_key)),
        public_key=_public_pem(private_key),
    ).check(current_version="0.2.2")
    check("same latest release reports current", result.status is UpdateStatus.CURRENT)
    check(
        "same release authenticates metadata without downloading its installer",
        [url for url, _ in session.calls]
        == [
            GITHUB_LATEST_API,
            "https://github.com/Vanexia/momento/releases/download/v0.2.2/Momento-update.json",
            "https://github.com/Vanexia/momento/releases/download/v0.2.2/Momento-update.json.sig",
        ],
    )

    wrong_key = Ed25519PrivateKey.generate()
    invalid_responses, *_ = _fixture(
        private_key,
        version="0.2.2",
        signature_key=wrong_key,
    )
    invalid = UpdateClient(
        session=FakeSession(invalid_responses),
        cache=UpdateCache(tmp / "current-invalid", _public_pem(private_key)),
        public_key=_public_pem(private_key),
    ).check(current_version="0.2.2")
    check(
        "unsigned current status is rejected",
        invalid.status is UpdateStatus.FAILED,
    )


def test_release_policy_failures(tmp: Path, private_key: Ed25519PrivateKey) -> None:
    cases: list[tuple[str, dict[str, object]]] = [
        ("draft releases are rejected", {"draft": True}),
        ("prereleases are rejected", {"prerelease": True}),
        ("non-version tags are rejected", {"tag_name": "latest"}),
        ("release assets must be a list", {"assets": {}}),
    ]
    for index, (label, overrides) in enumerate(cases):
        responses, *_ = _fixture(private_key, release_overrides=overrides)
        result = UpdateClient(
            session=FakeSession(responses),
            cache=UpdateCache(tmp / f"policy-{index}", _public_pem(private_key)),
            public_key=_public_pem(private_key),
        ).check(current_version="0.2.2")
        check(label, result.status is UpdateStatus.FAILED)

    responses, *_ = _fixture(private_key)
    release_response = responses[GITHUB_LATEST_API]
    assert isinstance(release_response, FakeResponse)
    release = json.loads(release_response._body)
    release["assets"].append(dict(release["assets"][0]))
    release_response._body = json.dumps(release).encode("utf-8")
    release_response.headers["Content-Length"] = str(len(release_response._body))
    duplicate = UpdateClient(
        session=FakeSession(responses),
        cache=UpdateCache(tmp / "duplicate", _public_pem(private_key)),
        public_key=_public_pem(private_key),
    ).check(current_version="0.2.2")
    check("duplicate named assets are rejected", duplicate.status is UpdateStatus.FAILED)


def test_download_failures_leave_no_stage(tmp: Path, private_key: Ed25519PrivateKey) -> None:
    public_key = _public_pem(private_key)

    offline = UpdateClient(
        session=FakeSession({GITHUB_LATEST_API: requests.ConnectionError("offline")}),
        cache=UpdateCache(tmp / "offline", public_key),
        public_key=public_key,
    ).check(current_version="0.2.2")
    check("offline launch fails without raising", offline.status is UpdateStatus.FAILED)

    responses, *_ = _fixture(private_key)
    release_response = responses[GITHUB_LATEST_API]
    assert isinstance(release_response, FakeResponse)
    release_response._body = b"x" * (MAX_RELEASE_BYTES + 1)
    release_response.headers["Content-Length"] = str(len(release_response._body))
    oversized = UpdateClient(
        session=FakeSession(responses),
        cache=UpdateCache(tmp / "oversized-release", public_key),
        public_key=public_key,
    ).check(current_version="0.2.2")
    check("oversized release API response is rejected", oversized.status is UpdateStatus.FAILED)

    wrong_key = Ed25519PrivateKey.generate()
    responses, *_ = _fixture(private_key, signature_key=wrong_key)
    bad_signature_cache = tmp / "bad-signature"
    bad_signature = UpdateClient(
        session=FakeSession(responses),
        cache=UpdateCache(bad_signature_cache, public_key),
        public_key=public_key,
    ).check(current_version="0.2.2")
    check("invalid signed metadata is rejected", bad_signature.status is UpdateStatus.FAILED)
    check("invalid signed metadata never stages an installer", not list(bad_signature_cache.glob("*.exe")))

    responses, _metadata, _signature, installer, installer_url = _fixture(private_key)
    responses[installer_url] = FakeResponse(installer[:-11], url=installer_url)
    truncated_cache = tmp / "truncated"
    truncated = UpdateClient(
        session=FakeSession(responses),
        cache=UpdateCache(truncated_cache, public_key),
        public_key=public_key,
    ).check(current_version="0.2.2")
    check("truncated installer is rejected", truncated.status is UpdateStatus.FAILED)
    check("truncated download leaves no final or partial installer", not list(truncated_cache.glob("*.exe")) and not list(truncated_cache.glob("*.partial")))

    responses, _metadata, _signature, installer, installer_url = _fixture(private_key)
    corrupted = bytearray(installer)
    corrupted[-1] ^= 1
    responses[installer_url] = FakeResponse(bytes(corrupted), url=installer_url)
    corrupt_cache = tmp / "corrupt"
    mismatch = UpdateClient(
        session=FakeSession(responses),
        cache=UpdateCache(corrupt_cache, public_key),
        public_key=public_key,
    ).check(current_version="0.2.2")
    check("installer hash mismatch is rejected", mismatch.status is UpdateStatus.FAILED)
    check("hash mismatch removes partial output", not list(corrupt_cache.glob("*.partial")))


def test_metadata_freshness_and_clock_rollback(tmp: Path, private_key: Ed25519PrivateKey) -> None:
    public_key = _public_pem(private_key)
    now = datetime(2026, 8, 17, 12, tzinfo=UTC)
    cache = UpdateCache(tmp / "freshness", public_key, now_provider=lambda: now)

    newer_responses, *_ = _fixture(
        private_key,
        version="0.2.4",
        metadata_version=2,
    )
    newer = UpdateClient(
        session=FakeSession(newer_responses),
        cache=cache,
        public_key=public_key,
        now_provider=lambda: now,
    ).check(current_version="0.2.2")
    check("new metadata version is accepted and remembered", newer.status is UpdateStatus.AVAILABLE)
    assert newer.staged is not None
    newer.staged.installer_path.unlink()

    replay_responses, *_ = _fixture(
        private_key,
        version="0.2.3",
        metadata_version=1,
    )
    replay = UpdateClient(
        session=FakeSession(replay_responses),
        cache=cache,
        public_key=public_key,
        now_provider=lambda: now,
    ).check(current_version="0.2.2")
    check("older signed metadata is rejected after a newer one was seen", replay.status is UpdateStatus.FAILED)

    expired_responses, *_ = _fixture(
        private_key,
        version="0.2.5",
        metadata_version=3,
        expires_at="2026-08-16T12:00:00Z",
    )
    expired = UpdateClient(
        session=FakeSession(expired_responses),
        cache=cache,
        public_key=public_key,
        now_provider=lambda: now,
    ).check(current_version="0.2.2")
    check("expired signed metadata is rejected", expired.status is UpdateStatus.FAILED)

    clock_back = datetime(2026, 8, 1, 12, tzinfo=UTC)
    rollback_clock_responses, *_ = _fixture(
        private_key,
        version="0.2.6",
        metadata_version=4,
        expires_at="2026-08-10T12:00:00Z",
    )
    rollback_clock = UpdateClient(
        session=FakeSession(rollback_clock_responses),
        cache=cache,
        public_key=public_key,
        now_provider=lambda: clock_back,
    ).check(current_version="0.2.2")
    check("rolling the system clock back cannot revive expired metadata", rollback_clock.status is UpdateStatus.FAILED)


def test_redirect_policy(tmp: Path, private_key: Ed25519PrivateKey) -> None:
    public_key = _public_pem(private_key)
    responses, metadata, _signature, _installer, _installer_url = _fixture(private_key)
    release_response = responses[GITHUB_LATEST_API]
    assert isinstance(release_response, FakeResponse)
    responses[GITHUB_LATEST_API] = FakeResponse(
        b"",
        url=GITHUB_LATEST_API,
        status=302,
        headers={"Location": "https://github.com/Vanexia/momento"},
    )
    api_redirect = UpdateClient(
        session=FakeSession(responses),
        cache=UpdateCache(tmp / "api-redirect", public_key),
        public_key=public_key,
    ).check(current_version="0.2.2")
    check("GitHub API redirects are rejected", api_redirect.status is UpdateStatus.FAILED)

    responses, metadata, _signature, _installer, _installer_url = _fixture(private_key)
    metadata_url = "https://github.com/Vanexia/momento/releases/download/v0.2.3/Momento-update.json"
    cdn_url = "https://release-assets.githubusercontent.com/test/Momento-update.json"
    responses[metadata_url] = FakeResponse(
        b"",
        url=metadata_url,
        status=302,
        headers={"Location": cdn_url},
    )
    responses[cdn_url] = FakeResponse(metadata, url=cdn_url)
    approved = UpdateClient(
        session=FakeSession(responses),
        cache=UpdateCache(tmp / "approved-redirect", public_key),
        public_key=public_key,
    ).check(current_version="0.2.2")
    check("one approved GitHub CDN redirect is followed", approved.status is UpdateStatus.AVAILABLE)

    for index, target in enumerate(
        (
            "http://release-assets.githubusercontent.com/test/file",
            "https://github.com.evil.test/file",
            "https://user@release-assets.githubusercontent.com/file",
            "https://release-assets.githubusercontent.com:444/file",
            "https://127.0.0.1/file",
        )
    ):
        responses, *_ = _fixture(private_key)
        responses[metadata_url] = FakeResponse(
            b"", url=metadata_url, status=302, headers={"Location": target}
        )
        denied = UpdateClient(
            session=FakeSession(responses),
            cache=UpdateCache(tmp / f"denied-redirect-{index}", public_key),
            public_key=public_key,
        ).check(current_version="0.2.2")
        check(f"untrusted redirect is rejected: {target}", denied.status is UpdateStatus.FAILED)


def test_stale_and_tampered_cache(tmp: Path, private_key: Ed25519PrivateKey) -> None:
    public_key = _public_pem(private_key)
    cache_root = tmp / "stale-cache"
    cache_root.mkdir()
    (cache_root / ".MomentoSetup-0.2.9.exe.deadbeef.partial").write_bytes(b"partial")
    (cache_root / "not-momento.txt").write_text("leave me", encoding="utf-8")
    cache = UpdateCache(cache_root, public_key)
    check("owned stale partial files are cleaned", not list(cache_root.glob("*.partial")))
    check("cache cleanup leaves unrelated files alone", (cache_root / "not-momento.txt").is_file())

    responses, *_ = _fixture(private_key)
    staged = UpdateClient(
        session=FakeSession(responses), cache=cache, public_key=public_key
    ).check(current_version="0.2.2").staged
    assert staged is not None
    staged.installer_path.write_bytes(b"tampered after staging")
    check("tampered staged installer fails re-verification", not cache.verify(staged, current_version="0.2.2"))
    check("tampered staged installer is removed", not staged.installer_path.exists())


def test_launch_lock_and_cache_root(tmp: Path, private_key: Ed25519PrivateKey) -> None:
    public_key = _public_pem(private_key)
    responses, *_ = _fixture(private_key)
    cache = UpdateCache(tmp / "launch-lock", public_key)
    staged = UpdateClient(
        session=FakeSession(responses), cache=cache, public_key=public_key
    ).check(current_version="0.2.2").staged
    assert staged is not None

    write_blocked = False
    with cache.lock_for_launch(staged, current_version="0.2.2") as locked:
        check("launch lock retains the exact verified installer path", locked.installer_path == staged.installer_path)
        try:
            with staged.installer_path.open("r+b") as writer:
                writer.write(b"tamper")
        except OSError:
            write_blocked = True
    check("Windows launch lock denies concurrent installer mutation", write_blocked or sys.platform != "win32")
    cache.discard_version(str(staged.manifest.version))
    check("confirmed update cleanup removes the staged installer", not staged.installer_path.exists())
    check("confirmed update cleanup preserves anti-replay trust", (cache.root / "trusted-state.json").is_file())

    responses, *_ = _fixture(private_key)
    hardlink_cache = UpdateCache(tmp / "hardlink-cache", public_key)
    linked = UpdateClient(
        session=FakeSession(responses), cache=hardlink_cache, public_key=public_key
    ).check(current_version="0.2.2").staged
    assert linked is not None
    hardlink = linked.installer_path.with_suffix(".linked.exe")
    os.link(linked.installer_path, hardlink)
    try:
        with hardlink_cache.lock_for_launch(linked, current_version="0.2.2"):
            hardlink_rejected = False
    except UpdateMetadataError:
        hardlink_rejected = True
    check("multiply linked installers are rejected before launch", hardlink_rejected)
    hardlink.unlink(missing_ok=True)

    target = tmp / "real-cache-root"
    target.mkdir()
    linked_root = tmp / "linked-cache-root"
    try:
        linked_root.symlink_to(target, target_is_directory=True)
    except OSError:
        check("reparse cache root is rejected when link creation is available", True)
    else:
        try:
            UpdateCache(linked_root, public_key)
        except UpdateMetadataError:
            root_rejected = True
        else:
            root_rejected = False
        check("reparse cache root is rejected when link creation is available", root_rejected)


def main() -> int:
    spec = (Path(__file__).resolve().parents[1] / "build" / "pyinstaller.spec").read_text(
        encoding="utf-8"
    )
    check(
        "public builds retain requests and urllib3 for the updater",
        '"requests"' in spec
        and '"urllib3"' in spec
        and '"requests_oauthlib"' not in spec.split("excludes =", 1)[1],
    )

    private_key = Ed25519PrivateKey.generate()
    with tempfile.TemporaryDirectory(prefix="momento_update_client_") as d:
        tmp = Path(d)
        test_valid_download_and_cache(tmp, private_key)
        test_current_release_is_authenticated(tmp, private_key)
        test_release_policy_failures(tmp, private_key)
        test_download_failures_leave_no_stage(tmp, private_key)
        test_metadata_freshness_and_clock_rollback(tmp, private_key)
        test_redirect_policy(tmp, private_key)
        test_stale_and_tampered_cache(tmp, private_key)
        test_launch_lock_and_cache_root(tmp, private_key)

    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
