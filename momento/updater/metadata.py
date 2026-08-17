"""Signed metadata contract for stable Momento releases."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from packaging.version import InvalidVersion, Version


SCHEMA_VERSION = 1
UPDATE_CHANNEL = "stable"
MAX_METADATA_BYTES = 64 * 1024
MAX_SIGNATURE_BYTES = 256
MAX_INSTALLER_BYTES = 512 * 1024 * 1024
MAX_FUTURE_CLOCK_SKEW = timedelta(hours=24)

_VERSION = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOP_LEVEL_KEYS = {
    "channel",
    "expires_at",
    "installer",
    "metadata_version",
    "minimum_updater_version",
    "published_at",
    "schema",
    "version",
}
_INSTALLER_KEYS = {"name", "sha256", "size", "url"}


class UpdateMetadataError(ValueError):
    """The signed payload is malformed or does not match schema 1."""


class UpdateSignatureError(UpdateMetadataError):
    """The metadata signature or public key is invalid."""


class UpdatePolicyError(UpdateMetadataError):
    """The metadata is authentic but is not an allowed Momento update."""


class UpdateNotNewerError(UpdatePolicyError):
    """The signed release is not newer than the running application."""


@dataclass(frozen=True, slots=True)
class InstallerInfo:
    name: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class UpdateManifest:
    schema: int
    channel: str
    metadata_version: int
    version: Version
    published_at: datetime
    expires_at: datetime
    minimum_updater_version: Version
    installer: InstallerInfo

    @classmethod
    def from_signed_bytes(
        cls,
        metadata: bytes,
        signature: bytes,
        public_key: bytes,
        current_version: str,
        now: datetime | None = None,
    ) -> "UpdateManifest":
        return parse_signed_manifest(
            metadata,
            signature,
            public_key,
            current_version=current_version,
            now=now,
        )


def canonical_manifest_bytes(payload: dict[str, object]) -> bytes:
    """Serialize a manifest with the one byte representation Momento signs."""
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise UpdateMetadataError("Update metadata cannot be serialized") from exc


def parse_signed_manifest(
    metadata: bytes,
    signature: bytes,
    public_key: bytes,
    *,
    current_version: str,
    now: datetime | None = None,
    allow_current: bool = False,
) -> UpdateManifest:
    """Authenticate and validate a stable update manifest.

    Signature verification intentionally precedes JSON parsing so untrusted
    release data cannot influence update policy before its publisher is known.
    """
    if not isinstance(metadata, bytes) or not (0 < len(metadata) <= MAX_METADATA_BYTES):
        raise UpdateMetadataError("Update metadata size is invalid")
    if not isinstance(signature, bytes) or not (0 < len(signature) <= MAX_SIGNATURE_BYTES):
        raise UpdateSignatureError("Update signature size is invalid")

    manifest = authenticate_manifest(metadata, signature, public_key)
    validate_manifest_freshness(manifest, now=now or datetime.now(UTC))
    running = _stable_version(current_version, field="current_version")
    if manifest.version < running or (
        manifest.version == running and not allow_current
    ):
        raise UpdateNotNewerError("Update version is not newer than Momento")
    if manifest.minimum_updater_version > running:
        raise UpdatePolicyError("Update requires a newer updater")
    return manifest


def authenticate_manifest(
    metadata: bytes,
    signature: bytes,
    public_key: bytes,
) -> UpdateManifest:
    """Authenticate a release manifest without comparing it to a client.

    Release tooling uses this after signing the current release, while clients
    use :func:`parse_signed_manifest` to apply the additional upgrade policy.
    """
    if not isinstance(metadata, bytes) or not (0 < len(metadata) <= MAX_METADATA_BYTES):
        raise UpdateMetadataError("Update metadata size is invalid")
    if not isinstance(signature, bytes) or not (0 < len(signature) <= MAX_SIGNATURE_BYTES):
        raise UpdateSignatureError("Update signature size is invalid")

    verified_signature = _decode_signature(signature)
    _verify_signature(public_key, metadata, verified_signature)
    payload = _decode_canonical_json(metadata)
    return _validate_payload(payload)


def validate_manifest_freshness(
    manifest: UpdateManifest,
    *,
    now: datetime,
) -> None:
    """Reject expired metadata and clocks implausibly behind publication."""
    if now.tzinfo is None:
        raise UpdatePolicyError("Update freshness clock must include a timezone")
    now = now.astimezone(UTC)
    if manifest.published_at > now + MAX_FUTURE_CLOCK_SKEW:
        raise UpdatePolicyError("Update metadata publication time is implausibly in the future")
    if manifest.expires_at <= now:
        raise UpdatePolicyError("Update metadata has expired")


def _decode_signature(value: bytes) -> bytes:
    encoded = value.strip()
    try:
        signature = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UpdateSignatureError("Update signature is not valid base64") from exc
    if len(signature) != 64:
        raise UpdateSignatureError("Update signature has the wrong length")
    return signature


def _verify_signature(public_key: bytes, metadata: bytes, signature: bytes) -> None:
    try:
        key = serialization.load_pem_public_key(public_key)
    except (TypeError, ValueError) as exc:
        raise UpdateSignatureError("Update public key is invalid") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise UpdateSignatureError("Update public key is not Ed25519")
    try:
        key.verify(signature, metadata)
    except InvalidSignature as exc:
        raise UpdateSignatureError("Update metadata signature is invalid") from exc


def _decode_canonical_json(metadata: bytes) -> dict[str, Any]:
    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise UpdateMetadataError(f"Duplicate update metadata key: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            metadata.decode("utf-8"),
            object_pairs_hook=reject_duplicate,
            parse_constant=lambda value: (_raise_invalid_number(value)),
        )
    except UpdateMetadataError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateMetadataError("Update metadata is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise UpdateMetadataError("Update metadata root must be an object")
    if canonical_manifest_bytes(payload) != metadata:
        raise UpdateMetadataError("Update metadata is not canonical JSON")
    return payload


def _raise_invalid_number(value: str) -> None:
    raise UpdateMetadataError(f"Invalid JSON number: {value}")


def _validate_payload(payload: dict[str, Any]) -> UpdateManifest:
    if set(payload) != _TOP_LEVEL_KEYS:
        raise UpdateMetadataError("Update metadata has missing or unknown fields")
    if type(payload["schema"]) is not int:
        raise UpdateMetadataError("Update schema must be an integer")
    if payload["schema"] != SCHEMA_VERSION:
        raise UpdatePolicyError("Update schema is not supported")
    if payload["channel"] != UPDATE_CHANNEL:
        raise UpdatePolicyError("Update channel is not stable")
    metadata_version = payload["metadata_version"]
    if type(metadata_version) is not int or metadata_version < 1:
        raise UpdatePolicyError("Update metadata version must be a positive integer")

    version = _stable_version(payload["version"], field="version")
    minimum = _stable_version(
        payload["minimum_updater_version"],
        field="minimum_updater_version",
    )
    published_at = _utc_timestamp(payload["published_at"])
    expires_at = _utc_timestamp(payload["expires_at"])
    if expires_at <= published_at:
        raise UpdatePolicyError("Update metadata expiry must follow publication")
    installer = _installer_info(payload["installer"], version=str(version))
    return UpdateManifest(
        schema=SCHEMA_VERSION,
        channel=UPDATE_CHANNEL,
        metadata_version=metadata_version,
        version=version,
        published_at=published_at,
        expires_at=expires_at,
        minimum_updater_version=minimum,
        installer=installer,
    )


def _stable_version(value: Any, *, field: str) -> Version:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise UpdatePolicyError(f"Update {field} is not a canonical stable version")
    try:
        version = Version(value)
    except InvalidVersion as exc:
        raise UpdatePolicyError(f"Update {field} is invalid") from exc
    if version.is_prerelease or version.is_devrelease or version.is_postrelease or version.local:
        raise UpdatePolicyError(f"Update {field} is not stable")
    return version


def _utc_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise UpdatePolicyError("Update publication timestamp must be UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise UpdatePolicyError("Update publication timestamp is invalid") from exc
    if parsed.tzinfo != UTC:
        raise UpdatePolicyError("Update publication timestamp must be UTC")
    return parsed


def _installer_info(value: Any, *, version: str) -> InstallerInfo:
    if not isinstance(value, dict) or set(value) != _INSTALLER_KEYS:
        raise UpdateMetadataError("Update installer has missing or unknown fields")
    name = value["name"]
    url = value["url"]
    sha256 = value["sha256"]
    size = value["size"]
    if not isinstance(name, str) or not isinstance(url, str):
        raise UpdateMetadataError("Update installer name and URL must be strings")
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        raise UpdatePolicyError("Update installer SHA-256 is invalid")
    if type(size) is not int or not (0 < size <= MAX_INSTALLER_BYTES):
        raise UpdatePolicyError("Update installer size is invalid")

    expected_name = f"MomentoSetup-{version}.exe"
    expected_url = (
        "https://github.com/Vanexia/momento/releases/download/"
        f"v{version}/{expected_name}"
    )
    if name != expected_name:
        raise UpdatePolicyError("Update installer filename is not allowed")
    if url != expected_url:
        raise UpdatePolicyError("Update installer URL is not allowed")
    return InstallerInfo(name=name, url=url, size=size, sha256=sha256)
