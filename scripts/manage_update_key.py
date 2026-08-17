"""Create Momento's Ed25519 release key outside the source repository."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


ROOT = Path(__file__).resolve().parents[1]


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


def _restrict_private_key(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
        return
    whoami = subprocess.run(
        ["whoami"], capture_output=True, text=True, timeout=10, check=True
    ).stdout.strip()
    if not whoami:
        raise RuntimeError("Could not identify the Windows account for the key ACL")
    restricted = subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{whoami}:(F)",
            "SYSTEM:(F)",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if restricted.returncode != 0:
        raise RuntimeError("Could not restrict the update signing key ACL")


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("The update signing key is not Ed25519")
    return key


def ensure_key(private_path: Path, public_path: Path) -> None:
    private_path = private_path.expanduser().resolve()
    public_path = public_path.expanduser().resolve()
    try:
        private_path.relative_to(ROOT)
    except ValueError:
        pass
    else:
        raise ValueError("The update signing private key must remain outside the repository")

    if private_path.exists():
        private_key = _load_private_key(private_path)
    else:
        private_key = Ed25519PrivateKey.generate()
        private_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        _atomic_write(private_path, private_bytes)
    _restrict_private_key(private_path)

    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if public_path.exists():
        existing = serialization.load_pem_public_key(public_path.read_bytes())
        if not isinstance(existing, Ed25519PublicKey):
            raise ValueError("The configured update public key is not Ed25519")
        existing_bytes = existing.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        if existing_bytes != public_bytes:
            raise ValueError("The existing update public key does not match the private key")
    else:
        _atomic_write(public_path, public_bytes)

    print(f"Update signing key ready: {private_path}")
    print(f"Public verification key ready: {public_path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", type=Path, default=_default_private_key())
    parser.add_argument(
        "--public-key",
        type=Path,
        default=ROOT / "resources" / "update_public_key.pem",
    )
    args = parser.parse_args()
    try:
        ensure_key(args.key, args.public_key)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
