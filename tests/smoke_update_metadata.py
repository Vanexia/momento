"""Security contract for Momento's signed update metadata."""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

from momento.updater.metadata import (  # noqa: E402
    MAX_METADATA_BYTES,
    UpdateMetadataError,
    UpdateNotNewerError,
    UpdatePolicyError,
    UpdateSignatureError,
    canonical_manifest_bytes,
    parse_signed_manifest,
)
from momento.util.paths import update_cache_dir  # noqa: E402


_results: list[tuple[str, bool]] = []
_TEST_NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def check(label: str, condition: bool) -> None:
    _results.append((label, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'} - {label}")


def _payload(version: str = "0.2.3") -> dict[str, object]:
    filename = f"MomentoSetup-{version}.exe"
    return {
        "channel": "stable",
        "expires_at": "2027-02-13T12:00:00Z",
        "installer": {
            "name": filename,
            "sha256": "a" * 64,
            "size": 63_200_001,
            "url": (
                "https://github.com/Vanexia/momento/releases/download/"
                f"v{version}/{filename}"
            ),
        },
        "minimum_updater_version": "0.2.2",
        "metadata_version": 1,
        "published_at": "2026-08-17T12:00:00Z",
        "schema": 1,
        "version": version,
    }


def _public_pem(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _signed(
    private_key: Ed25519PrivateKey,
    payload: dict[str, object],
) -> tuple[bytes, bytes]:
    metadata = canonical_manifest_bytes(payload)
    signature = base64.b64encode(private_key.sign(metadata)) + b"\n"
    return metadata, signature


def _expect_failure(
    label: str,
    error_type: type[Exception],
    metadata: bytes,
    signature: bytes,
    public_key: bytes,
    *,
    current_version: str = "0.2.2",
) -> None:
    try:
        parse_signed_manifest(
            metadata,
            signature,
            public_key,
            current_version=current_version,
            now=_TEST_NOW,
        )
    except error_type:
        check(label, True)
    except Exception as exc:
        print(f"  unexpected {type(exc).__name__}: {exc}")
        check(label, False)
    else:
        check(label, False)


def test_valid_contract(private_key: Ed25519PrivateKey) -> None:
    payload = _payload()
    metadata, signature = _signed(private_key, payload)
    public_key = _public_pem(private_key)

    check(
        "canonical JSON is compact, sorted, UTF-8, and newline-free",
        metadata == json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        and not metadata.endswith(b"\n"),
    )
    manifest = parse_signed_manifest(
        metadata,
        signature,
        public_key,
        current_version="0.2.2",
        now=_TEST_NOW,
    )
    check("valid signed metadata is accepted", str(manifest.version) == "0.2.3")
    check("installer name is retained", manifest.installer.name == "MomentoSetup-0.2.3.exe")
    check("installer size is retained", manifest.installer.size == 63_200_001)
    check("installer SHA-256 is normalized", manifest.installer.sha256 == "a" * 64)
    check("metadata version is retained", manifest.metadata_version == 1)
    check(
        "published timestamp is parsed as UTC",
        manifest.published_at == datetime(2026, 8, 17, 12, tzinfo=UTC),
    )
    check(
        "expiry timestamp is parsed as UTC",
        manifest.expires_at == datetime(2027, 2, 13, 12, tzinfo=UTC),
    )


def test_signature_rejections(private_key: Ed25519PrivateKey) -> None:
    public_key = _public_pem(private_key)
    metadata, signature = _signed(private_key, _payload())

    altered = bytearray(metadata)
    altered[-2] = altered[-2] ^ 1
    _expect_failure(
        "altered metadata is rejected before use",
        UpdateSignatureError,
        bytes(altered),
        signature,
        public_key,
    )
    _expect_failure(
        "malformed base64 signature is rejected",
        UpdateSignatureError,
        metadata,
        b"not-base64!",
        public_key,
    )
    other_key = Ed25519PrivateKey.generate()
    _expect_failure(
        "signature from another key is rejected",
        UpdateSignatureError,
        metadata,
        base64.b64encode(other_key.sign(metadata)),
        public_key,
    )
    _expect_failure(
        "non-Ed25519 public keys are rejected",
        UpdateSignatureError,
        metadata,
        signature,
        b"not a public key",
    )


def test_structure_rejections(private_key: Ed25519PrivateKey) -> None:
    public_key = _public_pem(private_key)

    variants: list[tuple[str, dict[str, object]]] = []
    unknown_top = _payload()
    unknown_top["notes"] = "unsigned behavior must never grow accidentally"
    variants.append(("unknown top-level keys are rejected", unknown_top))

    missing = _payload()
    missing.pop("published_at")
    variants.append(("missing top-level keys are rejected", missing))

    unknown_installer = _payload()
    installer = dict(unknown_installer["installer"])  # type: ignore[arg-type]
    installer["mirrors"] = []
    unknown_installer["installer"] = installer
    variants.append(("unknown installer keys are rejected", unknown_installer))

    for label, payload in variants:
        metadata, signature = _signed(private_key, payload)
        _expect_failure(
            label,
            UpdateMetadataError,
            metadata,
            signature,
            public_key,
        )

    duplicate = (
        b'{"channel":"stable","channel":"preview","expires_at":"2027-02-13T12:00:00Z",'
        b'"installer":{},"metadata_version":1,"minimum_updater_version":"0.2.2",'
        b'"published_at":"2026-08-17T12:00:00Z","schema":1,"version":"0.2.3"}'
    )
    duplicate_signature = base64.b64encode(private_key.sign(duplicate))
    _expect_failure(
        "duplicate JSON keys are rejected",
        UpdateMetadataError,
        duplicate,
        duplicate_signature,
        public_key,
    )

    noncanonical = json.dumps(_payload(), indent=2).encode("utf-8")
    _expect_failure(
        "non-canonical signed JSON is rejected",
        UpdateMetadataError,
        noncanonical,
        base64.b64encode(private_key.sign(noncanonical)),
        public_key,
    )
    oversized = b" " * (MAX_METADATA_BYTES + 1)
    _expect_failure(
        "oversized metadata is rejected before signature work",
        UpdateMetadataError,
        oversized,
        base64.b64encode(private_key.sign(oversized)),
        public_key,
    )


def test_policy_rejections(private_key: Ed25519PrivateKey) -> None:
    public_key = _public_pem(private_key)

    def reject(
        label: str,
        mutate,
        error_type: type[Exception] = UpdatePolicyError,
        *,
        current_version: str = "0.2.2",
    ) -> None:
        payload = _payload()
        mutate(payload)
        metadata, signature = _signed(private_key, payload)
        _expect_failure(
            label,
            error_type,
            metadata,
            signature,
            public_key,
            current_version=current_version,
        )

    reject("unknown schema is rejected", lambda p: p.__setitem__("schema", 2))
    reject("non-stable channel is rejected", lambda p: p.__setitem__("channel", "beta"))
    reject("prerelease version is rejected", lambda p: p.__setitem__("version", "0.2.3rc1"))
    reject("non-canonical version is rejected", lambda p: p.__setitem__("version", "0.2.03"))
    reject(
        "same version is rejected",
        lambda p: (
            p.__setitem__("version", "0.2.2"),
            p.__setitem__(
                "installer",
                {
                    "name": "MomentoSetup-0.2.2.exe",
                    "sha256": "a" * 64,
                    "size": 63_200_001,
                    "url": "https://github.com/Vanexia/momento/releases/download/"
                    "v0.2.2/MomentoSetup-0.2.2.exe",
                },
            ),
        ),
        UpdateNotNewerError,
    )
    reject(
        "downgrade is rejected",
        lambda p: (
            p.__setitem__("version", "0.2.1"),
            p.__setitem__(
                "installer",
                {
                    "name": "MomentoSetup-0.2.1.exe",
                    "sha256": "a" * 64,
                    "size": 63_200_001,
                    "url": "https://github.com/Vanexia/momento/releases/download/"
                    "v0.2.1/MomentoSetup-0.2.1.exe",
                },
            ),
        ),
        UpdateNotNewerError,
    )
    reject(
        "newer metadata can require a newer updater",
        lambda p: p.__setitem__("minimum_updater_version", "0.3.0"),
    )
    reject(
        "HTTP installer URLs are rejected",
        lambda p: p["installer"].__setitem__(  # type: ignore[union-attr]
            "url", "http://github.com/Vanexia/momento/releases/download/v0.2.3/MomentoSetup-0.2.3.exe"
        ),
    )
    same_payload = _payload(version="0.2.2")
    same_metadata, same_signature = _signed(private_key, same_payload)
    same_manifest = parse_signed_manifest(
        same_metadata,
        same_signature,
        public_key,
        current_version="0.2.2",
        now=datetime(2026, 8, 17, 12, tzinfo=UTC),
        allow_current=True,
    )
    check(
        "authenticated current-version metadata can support status checks",
        str(same_manifest.version) == "0.2.2",
    )
    reject(
        "lookalike repository hosts are rejected",
        lambda p: p["installer"].__setitem__(  # type: ignore[union-attr]
            "url", "https://github.com.evil.test/Vanexia/momento/releases/download/v0.2.3/MomentoSetup-0.2.3.exe"
        ),
    )
    reject(
        "wrong GitHub repository paths are rejected",
        lambda p: p["installer"].__setitem__(  # type: ignore[union-attr]
            "url", "https://github.com/Vanexia/other/releases/download/v0.2.3/MomentoSetup-0.2.3.exe"
        ),
    )
    reject(
        "wrong installer names are rejected",
        lambda p: p["installer"].__setitem__("name", "Momento.exe"),  # type: ignore[union-attr]
    )
    reject(
        "non-hex hashes are rejected",
        lambda p: p["installer"].__setitem__("sha256", "z" * 64),  # type: ignore[union-attr]
    )
    reject(
        "zero-byte installers are rejected",
        lambda p: p["installer"].__setitem__("size", 0),  # type: ignore[union-attr]
    )
    reject(
        "unreasonably large installers are rejected",
        lambda p: p["installer"].__setitem__("size", 2 * 1024 * 1024 * 1024),  # type: ignore[union-attr]
    )
    reject("timestamps without UTC are rejected", lambda p: p.__setitem__("published_at", "2026-08-17T12:00:00"))
    reject("metadata version zero is rejected", lambda p: p.__setitem__("metadata_version", 0))
    reject("boolean metadata versions are rejected", lambda p: p.__setitem__("metadata_version", True))
    reject("expiry without UTC is rejected", lambda p: p.__setitem__("expires_at", "2027-02-13T12:00:00"))
    reject("expiry must follow publication", lambda p: p.__setitem__("expires_at", "2026-08-17T11:59:59Z"))
    reject("expired signed metadata is rejected", lambda p: p.__setitem__("expires_at", "2026-08-16T12:00:00Z"))
    reject("implausibly future metadata is rejected", lambda p: p.__setitem__("published_at", "2026-08-20T12:00:00Z"))


def test_update_cache_location() -> None:
    old_local = os.environ.get("LOCALAPPDATA")
    old_roaming = os.environ.get("APPDATA")
    try:
        with tempfile.TemporaryDirectory(prefix="momento_update_path_") as d:
            root = Path(d).resolve()
            os.environ["LOCALAPPDATA"] = str(root / "local")
            os.environ["APPDATA"] = str(root / "roaming")
            path = update_cache_dir()
            check("update cache uses local app data", path == root / "local" / "Momento" / "updates")
            check("update cache directory is created", path.is_dir())
            check("update cache does not use roaming config storage", (root / "roaming") not in path.parents)
    finally:
        if old_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = old_local
        if old_roaming is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = old_roaming


def main() -> int:
    private_key = Ed25519PrivateKey.generate()
    test_valid_contract(private_key)
    test_signature_rejections(private_key)
    test_structure_rejections(private_key)
    test_policy_rejections(private_key)
    test_update_cache_location()

    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
