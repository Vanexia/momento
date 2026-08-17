"""Build and self-verify signed metadata for a Momento installer."""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from momento.updater.metadata import (  # noqa: E402
    MAX_INSTALLER_BYTES,
    authenticate_manifest,
    canonical_manifest_bytes,
    validate_manifest_freshness,
)
from momento.updater.file_lock import open_locked_read  # noqa: E402


def _default_private_key() -> Path:
    configured = os.environ.get("MOMENTO_UPDATE_SIGNING_KEY")
    if configured:
        return Path(configured).expanduser()
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "MomentoRelease" / "update-signing-key.pem"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def build_metadata(
    *,
    installer: Path,
    version: str,
    minimum_updater_version: str,
    metadata_version: int,
    published_at: str,
    expires_at: str,
    private_key_path: Path,
    public_key_path: Path,
    output_dir: Path,
) -> tuple[Path, Path]:
    if minimum_updater_version != "0.2.2":
        raise ValueError(
            "Schema 1 releases must remain compatible with updater 0.2.2"
        )
    expected_name = f"MomentoSetup-{version}.exe"
    if installer.name != expected_name:
        raise ValueError(f"Installer must be named {expected_name}")
    private_key = serialization.load_pem_private_key(
        private_key_path.read_bytes(), password=None
    )
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("The update signing key is not Ed25519")
    public_bytes = public_key_path.read_bytes()
    expected_public = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    loaded_public = serialization.load_pem_public_key(public_bytes)
    actual_public = loaded_public.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if actual_public != expected_public:
        raise ValueError("The update public key does not match the signing key")

    with open_locked_read(installer) as handle:
        before = os.fstat(handle.fileno())
        if before.st_nlink != 1 or not (0 < before.st_size <= MAX_INSTALLER_BYTES):
            raise ValueError("Installer identity or size is outside the updater policy")
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
        after = os.fstat(handle.fileno())
        if _file_identity(before) != _file_identity(after):
            raise ValueError("Installer changed while release metadata was generated")
        size = before.st_size
    payload: dict[str, object] = {
        "channel": "stable",
        "expires_at": expires_at,
        "installer": {
            "name": expected_name,
            "sha256": digest,
            "size": size,
            "url": (
                "https://github.com/Vanexia/momento/releases/download/"
                f"v{version}/{expected_name}"
            ),
        },
        "minimum_updater_version": minimum_updater_version,
        "metadata_version": metadata_version,
        "published_at": published_at,
        "schema": 1,
        "version": version,
    }
    metadata = canonical_manifest_bytes(payload)
    signature = base64.b64encode(private_key.sign(metadata)) + b"\n"
    manifest = authenticate_manifest(metadata, signature, public_bytes)
    validate_manifest_freshness(manifest, now=datetime.now(UTC))

    metadata_path = output_dir / "Momento-update.json"
    signature_path = output_dir / "Momento-update.json.sig"
    _atomic_write(metadata_path, metadata)
    _atomic_write(signature_path, signature)

    # Verify the bytes after publication, not only the in-memory values.
    published_manifest = authenticate_manifest(
        metadata_path.read_bytes(),
        signature_path.read_bytes(),
        public_bytes,
    )
    validate_manifest_freshness(published_manifest, now=datetime.now(UTC))
    verify_installer(installer, expected_size=size, expected_sha256=digest)
    return metadata_path, signature_path


def _file_identity(stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_nlink,
        stat.st_size,
        stat.st_mtime_ns,
    )


def verify_installer(
    installer: Path,
    *,
    expected_size: int,
    expected_sha256: str,
) -> None:
    """Independently re-open and verify the final release artifact."""
    with open_locked_read(installer) as handle:
        before = os.fstat(handle.fileno())
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
        after = os.fstat(handle.fileno())
    if (
        _file_identity(before) != _file_identity(after)
        or before.st_nlink != 1
        or before.st_size != expected_size
        or digest != expected_sha256
    ):
        raise ValueError("Installer changed after update metadata was written")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--minimum-updater-version", default="0.2.2")
    parser.add_argument("--metadata-version", type=int, required=True)
    parser.add_argument("--published-at", required=True)
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--key", type=Path, default=_default_private_key())
    parser.add_argument(
        "--public-key",
        type=Path,
        default=ROOT / "resources" / "update_public_key.pem",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        metadata, signature = build_metadata(
            installer=args.installer.resolve(),
            version=args.version,
            minimum_updater_version=args.minimum_updater_version,
            metadata_version=args.metadata_version,
            published_at=args.published_at,
            expires_at=args.expires_at,
            private_key_path=args.key.expanduser().resolve(),
            public_key_path=args.public_key.expanduser().resolve(),
            output_dir=args.output_dir.resolve(),
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Update metadata: {metadata}")
    print(f"Update signature: {signature}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
