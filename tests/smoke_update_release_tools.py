"""End-to-end checks for external update keys and release metadata tooling."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # noqa: E402

from momento.updater.metadata import authenticate_manifest  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
_results: list[tuple[str, bool]] = []


def check(label: str, condition: bool) -> None:
    _results.append((label, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'} - {label}")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(PYTHON), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def main() -> int:
    spec = (ROOT / "build" / "pyinstaller.spec").read_text(encoding="utf-8")
    check(
        "PyInstaller requires and bundles only the public update key",
        spec.count("update_public_key.pem") >= 2
        and "update-signing-key.pem" not in spec,
    )
    metadata_builder = (ROOT / "scripts" / "build_update_metadata.py").read_text(
        encoding="utf-8"
    )
    check(
        "metadata builder locks and rechecks one installer identity",
        "open_locked_read" in metadata_builder
        and metadata_builder.count("os.fstat") >= 2
        and "verify_installer" in metadata_builder,
    )

    with tempfile.TemporaryDirectory(prefix="momento_update_release_") as d:
        tmp = Path(d)
        private_key = tmp / "private" / "update-signing-key.pem"
        public_key = tmp / "public" / "update_public_key.pem"

        generated = _run(
            "scripts/manage_update_key.py",
            "--key",
            str(private_key),
            "--public-key",
            str(public_key),
        )
        check("key tool creates an external private key", generated.returncode == 0 and private_key.is_file())
        check("key tool writes the matching public key", public_key.is_file())
        check("key tool never prints private key material", "PRIVATE KEY" not in (generated.stdout + generated.stderr))

        original_private = private_key.read_bytes()
        original_public = public_key.read_bytes()
        repeated = _run(
            "scripts/manage_update_key.py",
            "--key",
            str(private_key),
            "--public-key",
            str(public_key),
        )
        check(
            "re-running key setup is idempotent",
            repeated.returncode == 0
            and private_key.read_bytes() == original_private
            and public_key.read_bytes() == original_public,
        )

        loaded_public = serialization.load_pem_public_key(original_public)
        check("generated public key is Ed25519", isinstance(loaded_public, Ed25519PublicKey))

        installer = tmp / "MomentoSetup-0.2.3.exe"
        installer.write_bytes((b"MOMENTO-INSTALLER-TEST\0" * 4096) + b"end")
        output = tmp / "release"
        built = _run(
            "scripts/build_update_metadata.py",
            "--installer",
            str(installer),
            "--version",
            "0.2.3",
            "--minimum-updater-version",
            "0.2.2",
            "--metadata-version",
            "1",
            "--published-at",
            "2026-08-17T12:00:00Z",
            "--expires-at",
            "2027-02-13T12:00:00Z",
            "--key",
            str(private_key),
            "--public-key",
            str(public_key),
            "--output-dir",
            str(output),
        )
        metadata_path = output / "Momento-update.json"
        signature_path = output / "Momento-update.json.sig"
        check("metadata builder succeeds", built.returncode == 0)
        check("metadata builder writes exactly the public update files", metadata_path.is_file() and signature_path.is_file())
        check("metadata output never contains the private key", not (output / private_key.name).exists())

        metadata = metadata_path.read_bytes()
        signature_text = signature_path.read_bytes()
        manifest = authenticate_manifest(metadata, signature_text, original_public)
        check("built metadata authenticates independently", str(manifest.version) == "0.2.3")
        check("built metadata carries the exact installer size", manifest.installer.size == installer.stat().st_size)
        check("built metadata carries monotonic freshness", manifest.metadata_version == 1 and manifest.expires_at.year == 2027)
        check("signature asset is canonical base64 plus newline", base64.b64encode(base64.b64decode(signature_text.strip())) + b"\n" == signature_text)
        payload = json.loads(metadata)
        check("metadata uses the exact stable GitHub asset URL", payload["installer"]["url"].endswith("/v0.2.3/MomentoSetup-0.2.3.exe"))

        missing_key = _run(
            "scripts/build_update_metadata.py",
            "--installer",
            str(installer),
            "--version",
            "0.2.3",
            "--metadata-version",
            "1",
            "--published-at",
            "2026-08-17T12:00:00Z",
            "--expires-at",
            "2027-02-13T12:00:00Z",
            "--key",
            str(tmp / "missing.pem"),
            "--public-key",
            str(public_key),
            "--output-dir",
            str(tmp / "missing-key-output"),
        )
        check("metadata builder fails closed when the private key is absent", missing_key.returncode != 0)

        wrong_name = tmp / "setup.exe"
        wrong_name.write_bytes(b"not the named release asset")
        wrong_installer = _run(
            "scripts/build_update_metadata.py",
            "--installer",
            str(wrong_name),
            "--version",
            "0.2.3",
            "--metadata-version",
            "1",
            "--published-at",
            "2026-08-17T12:00:00Z",
            "--expires-at",
            "2027-02-13T12:00:00Z",
            "--key",
            str(private_key),
            "--public-key",
            str(public_key),
            "--output-dir",
            str(tmp / "wrong-name-output"),
        )
        check("metadata builder rejects a mismatched installer filename", wrong_installer.returncode != 0)

        other_private = tmp / "other-private.pem"
        other_public = tmp / "other-public.pem"
        _run(
            "scripts/manage_update_key.py",
            "--key",
            str(other_private),
            "--public-key",
            str(other_public),
        )
        mismatch = _run(
            "scripts/build_update_metadata.py",
            "--installer",
            str(installer),
            "--version",
            "0.2.3",
            "--metadata-version",
            "1",
            "--published-at",
            "2026-08-17T12:00:00Z",
            "--expires-at",
            "2027-02-13T12:00:00Z",
            "--key",
            str(private_key),
            "--public-key",
            str(other_public),
            "--output-dir",
            str(tmp / "mismatch-output"),
        )
        check("metadata builder rejects a public/private key mismatch", mismatch.returncode != 0)

        incompatible_floor = _run(
            "scripts/build_update_metadata.py",
            "--installer",
            str(installer),
            "--version",
            "0.2.3",
            "--minimum-updater-version",
            "0.2.3",
            "--metadata-version",
            "1",
            "--published-at",
            "2026-08-17T12:00:00Z",
            "--expires-at",
            "2027-02-13T12:00:00Z",
            "--key",
            str(private_key),
            "--public-key",
            str(public_key),
            "--output-dir",
            str(tmp / "incompatible-floor-output"),
        )
        check(
            "schema 1 cannot strand old clients behind a newer updater floor",
            incompatible_floor.returncode != 0,
        )

    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
