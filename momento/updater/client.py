"""Bounded GitHub Releases client for authenticated Momento updates."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, auto
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from packaging.version import Version

from momento.updater.cache import StagedUpdate, UpdateCache
from momento.updater.metadata import (
    MAX_METADATA_BYTES,
    MAX_SIGNATURE_BYTES,
    UpdateMetadataError,
    parse_signed_manifest,
)
from momento.util.paths import update_cache_dir
from momento.util.resources import update_public_key_path


logger = logging.getLogger(__name__)

GITHUB_LATEST_API = "https://api.github.com/repos/Vanexia/momento/releases/latest"
MAX_RELEASE_BYTES = 256 * 1024
_CONNECT_TIMEOUT = 5
_READ_TIMEOUT = 20
_CHUNK_SIZE = 64 * 1024
_VERSION_TAG = re.compile(
    r"^v((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))$"
)
_ASSET_REDIRECT_HOSTS = {
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_ASSET_REDIRECTS = 3


class UpdateStatus(Enum):
    CURRENT = auto()
    AVAILABLE = auto()
    FAILED = auto()


@dataclass(frozen=True, slots=True)
class UpdateResult:
    status: UpdateStatus
    staged: StagedUpdate | None = None
    error: str = ""


@dataclass(frozen=True, slots=True)
class _Asset:
    name: str
    url: str
    size: int
    digest: str | None


class UpdateClient:
    def __init__(
        self,
        *,
        session=None,
        cache: UpdateCache | None = None,
        public_key: bytes | None = None,
        now_provider=None,
    ) -> None:
        self._public_key = public_key or update_public_key_path().read_bytes()
        self._session = session or requests.Session()
        self._now_provider = now_provider or (lambda: datetime.now(UTC))
        self._cache = cache or UpdateCache(
            update_cache_dir(), self._public_key, now_provider=self._now_provider
        )

    def check(self, *, current_version: str) -> UpdateResult:
        """Return a verified staged update, current, or a contained failure."""
        try:
            cached = self._cache.load_verified(current_version=current_version)
        except (OSError, ValueError) as exc:
            logger.warning("Cached update verification failed (%s)", type(exc).__name__)
            return UpdateResult(UpdateStatus.FAILED, error=_safe_error(exc))

        try:
            release_bytes = self._download_bytes(
                GITHUB_LATEST_API,
                limit=MAX_RELEASE_BYTES,
            )
        except (OSError, requests.RequestException) as exc:
            if cached is not None:
                logger.info("Using a verified staged update while GitHub is unavailable")
                return UpdateResult(UpdateStatus.AVAILABLE, staged=cached)
            logger.warning("Update check failed (%s)", type(exc).__name__)
            return UpdateResult(UpdateStatus.FAILED, error=_safe_error(exc))
        except ValueError as exc:
            logger.warning("Update check failed (%s)", type(exc).__name__)
            return UpdateResult(UpdateStatus.FAILED, error=_safe_error(exc))

        try:
            release = _decode_release(release_bytes)
            version = _release_version(release)
            if cached is not None and cached.manifest.version >= version:
                return UpdateResult(UpdateStatus.AVAILABLE, staged=cached)
            running = Version(current_version)
            if version < running:
                return UpdateResult(UpdateStatus.CURRENT)

            assets = _release_assets(release)
            expected_base = (
                "https://github.com/Vanexia/momento/releases/download/"
                f"v{version}"
            )
            metadata_asset = _one_asset(assets, "Momento-update.json")
            signature_asset = _one_asset(assets, "Momento-update.json.sig")
            installer_name = f"MomentoSetup-{version}.exe"
            installer_asset = _one_asset(assets, installer_name)
            if metadata_asset.url != f"{expected_base}/Momento-update.json":
                raise UpdateMetadataError("Release metadata URL is not allowed")
            if signature_asset.url != f"{expected_base}/Momento-update.json.sig":
                raise UpdateMetadataError("Release signature URL is not allowed")
            if installer_asset.url != f"{expected_base}/{installer_name}":
                raise UpdateMetadataError("Release installer URL is not allowed")

            metadata = self._download_bytes(
                metadata_asset.url,
                limit=MAX_METADATA_BYTES,
            )
            signature = self._download_bytes(
                signature_asset.url,
                limit=MAX_SIGNATURE_BYTES,
            )
            manifest = parse_signed_manifest(
                metadata,
                signature,
                self._public_key,
                current_version=current_version,
                now=self._now_provider(),
                allow_current=version == running,
            )
            if manifest.version != version:
                raise UpdateMetadataError("Signed version does not match the GitHub release")
            if installer_asset.size != manifest.installer.size:
                raise UpdateMetadataError("GitHub installer size differs from signed metadata")
            if installer_asset.digest is not None:
                if installer_asset.digest != f"sha256:{manifest.installer.sha256}":
                    raise UpdateMetadataError("GitHub installer digest differs from signed metadata")
            self._cache.accept_metadata(manifest, metadata)

            if version == running:
                return UpdateResult(UpdateStatus.CURRENT)

            with self._get(installer_asset.url) as response:
                staged = self._cache.stage(
                    manifest,
                    metadata,
                    signature,
                    response.iter_content(chunk_size=_CHUNK_SIZE),
                )
            return UpdateResult(UpdateStatus.AVAILABLE, staged=staged)
        except (OSError, ValueError, requests.RequestException) as exc:
            logger.warning("Update check failed (%s)", type(exc).__name__)
            return UpdateResult(UpdateStatus.FAILED, error=_safe_error(exc))

    def _download_bytes(self, url: str, *, limit: int) -> bytes:
        with self._get(url) as response:
            declared = _content_length(response)
            if declared is not None and declared > limit:
                raise UpdateMetadataError("Update response is too large")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                if not chunk:
                    continue
                total += len(chunk)
                if total > limit:
                    raise UpdateMetadataError("Update response is too large")
                chunks.append(chunk)
            if declared is not None and total != declared:
                raise UpdateMetadataError("Update response length is incomplete")
            return b"".join(chunks)

    def _get(self, url: str):
        is_api = url == GITHUB_LATEST_API
        _validate_request_url(url, is_api=is_api, redirect=False)
        current = url
        redirects = 0
        while True:
            response = self._session.get(
                current,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Accept-Encoding": "identity",
                    "User-Agent": "Momento-Updater/1",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
                stream=True,
                allow_redirects=False,
            )
            if response.status_code not in _REDIRECT_STATUSES:
                response.raise_for_status()
                _validate_request_url(response.url, is_api=is_api, redirect=redirects > 0)
                encoding = response.headers.get("Content-Encoding", "identity").casefold()
                if encoding not in {"", "identity"}:
                    response.close()
                    raise UpdateMetadataError("Update response used unexpected content encoding")
                return response

            location = response.headers.get("Location")
            response.close()
            if is_api:
                raise UpdateMetadataError("GitHub API redirects are not allowed")
            redirects += 1
            if redirects > _MAX_ASSET_REDIRECTS:
                raise UpdateMetadataError("Update asset redirected too many times")
            if not location:
                raise UpdateMetadataError("Update redirect is missing a destination")
            current = urljoin(current, location)
            _validate_request_url(current, is_api=False, redirect=True)


def _decode_release(value: bytes) -> dict[str, Any]:
    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise UpdateMetadataError(f"Duplicate GitHub release key: {key}")
            result[key] = item
        return result

    try:
        release = json.loads(value.decode("utf-8"), object_pairs_hook=reject_duplicate)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateMetadataError("GitHub release response is not valid JSON") from exc
    if not isinstance(release, dict):
        raise UpdateMetadataError("GitHub release response is not an object")
    return release


def _release_version(release: dict[str, Any]) -> Version:
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise UpdateMetadataError("Latest GitHub release is not stable and published")
    tag = release.get("tag_name")
    if not isinstance(tag, str):
        raise UpdateMetadataError("GitHub release tag is missing")
    match = _VERSION_TAG.fullmatch(tag)
    if match is None:
        raise UpdateMetadataError("GitHub release tag is not a stable Momento version")
    return Version(match.group(1))


def _release_assets(release: dict[str, Any]) -> list[_Asset]:
    value = release.get("assets")
    if not isinstance(value, list):
        raise UpdateMetadataError("GitHub release assets are missing")
    assets: list[_Asset] = []
    for item in value:
        if not isinstance(item, dict):
            raise UpdateMetadataError("GitHub release asset is malformed")
        name = item.get("name")
        url = item.get("browser_download_url")
        size = item.get("size")
        digest = item.get("digest")
        if (
            not isinstance(name, str)
            or not isinstance(url, str)
            or type(size) is not int
            or size < 0
            or (digest is not None and not isinstance(digest, str))
        ):
            raise UpdateMetadataError("GitHub release asset fields are malformed")
        assets.append(_Asset(name=name, url=url, size=size, digest=digest))
    return assets


def _one_asset(assets: list[_Asset], name: str) -> _Asset:
    matches = [asset for asset in assets if asset.name == name]
    if len(matches) != 1:
        raise UpdateMetadataError(f"GitHub release must contain exactly one {name}")
    return matches[0]


def _content_length(response) -> int | None:
    value = response.headers.get("Content-Length")
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise UpdateMetadataError("Update response Content-Length is invalid") from exc
    if parsed < 0:
        raise UpdateMetadataError("Update response Content-Length is invalid")
    return parsed


def _validate_request_url(url: str, *, is_api: bool, redirect: bool) -> None:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as exc:
        raise UpdateMetadataError("Update URL is malformed") from exc
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or bool(parsed.fragment)
    ):
        raise UpdateMetadataError("Update URL is not allowed")
    if is_api:
        allowed = url == GITHUB_LATEST_API and parsed.hostname == "api.github.com"
    elif redirect:
        allowed = parsed.hostname in _ASSET_REDIRECT_HOSTS
    else:
        allowed = parsed.hostname == "github.com"
    if not allowed:
        raise UpdateMetadataError("Update response redirected to an untrusted host")


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, requests.Timeout):
        return "The update check timed out. Momento will try again next launch."
    if isinstance(exc, requests.RequestException):
        return "Momento could not reach the update service. It will try again next launch."
    return str(exc) or "The update could not be verified."
