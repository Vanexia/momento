"""Atomic local staging for authenticated Momento installers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator

from packaging.version import Version

from momento.updater.file_lock import open_locked_read
from momento.updater.metadata import (
    UpdateManifest,
    UpdateMetadataError,
    authenticate_manifest,
    parse_signed_manifest,
    validate_manifest_freshness,
)


_VERSION_TEXT = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
_METADATA_FILE = re.compile(rf"^Momento-update-({_VERSION_TEXT})\.json$")
_PARTIAL_FILE = re.compile(rf"^\.MomentoSetup-({_VERSION_TEXT})\.exe\.[0-9a-f]+\.partial$")


@dataclass(frozen=True, slots=True)
class StagedUpdate:
    manifest: UpdateManifest
    installer_path: Path


@dataclass(slots=True)
class LockedInstaller:
    installer_path: Path
    manifest: UpdateManifest
    file: BinaryIO


class UpdateCache:
    """Owns only versioned Momento update files beneath one cache root."""

    def __init__(self, root: Path, public_key: bytes, *, now_provider=None) -> None:
        candidate = root.expanduser().absolute()
        if str(candidate).startswith("\\\\"):
            raise UpdateMetadataError("Update cache cannot use a network path")
        if candidate.exists() and _is_reparse_point(candidate):
            raise UpdateMetadataError("Update cache root cannot be a reparse point")
        self.root = candidate.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(self.root):
            raise UpdateMetadataError("Update cache root cannot be a reparse point")
        self._public_key = public_key
        self._now_provider = now_provider or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._remove_stale_partials()

    def load_verified(self, *, current_version: str) -> StagedUpdate | None:
        with self._lock:
            candidates: list[StagedUpdate] = []
            for metadata_path in sorted(self.root.glob("Momento-update-*.json")):
                match = _METADATA_FILE.fullmatch(metadata_path.name)
                if match is None or metadata_path.is_symlink():
                    continue
                version_text = match.group(1)
                signature_path = self._signature_path(version_text)
                installer_path = self._installer_path(version_text)
                try:
                    metadata = metadata_path.read_bytes()
                    effective_now = self._effective_now()
                    manifest = parse_signed_manifest(
                        metadata,
                        signature_path.read_bytes(),
                        self._public_key,
                        current_version=current_version,
                        now=effective_now,
                    )
                    self._accept_trust(manifest, metadata)
                    if str(manifest.version) != version_text:
                        raise UpdateMetadataError("Cached update version does not match its filename")
                    staged = StagedUpdate(manifest=manifest, installer_path=installer_path)
                    if not self._installer_matches(staged):
                        raise UpdateMetadataError("Cached installer does not match signed metadata")
                    candidates.append(staged)
                except (OSError, UpdateMetadataError):
                    self._remove_version(version_text)

            if not candidates:
                return None
            selected = max(candidates, key=lambda item: item.manifest.version)
            for candidate in candidates:
                if candidate.manifest.version != selected.manifest.version:
                    self._remove_version(str(candidate.manifest.version))
            return selected

    def stage(
        self,
        manifest: UpdateManifest,
        metadata: bytes,
        signature: bytes,
        chunks: Iterable[bytes],
    ) -> StagedUpdate:
        """Stream, verify, and atomically publish one installer."""
        with self._lock:
            authenticated = authenticate_manifest(metadata, signature, self._public_key)
            if authenticated != manifest:
                raise UpdateMetadataError("Signed metadata changed before staging")
            self._accept_trust(manifest, metadata)

            version_text = str(manifest.version)
            final_installer = self._installer_path(version_text)
            partial = self.root / (
                f".{final_installer.name}.{uuid.uuid4().hex}.partial"
            )
            self._remove_version(version_text)
            digest = hashlib.sha256()
            written = 0
            try:
                with partial.open("xb") as handle:
                    for chunk in chunks:
                        if not chunk:
                            continue
                        if not isinstance(chunk, bytes):
                            raise UpdateMetadataError("Installer stream yielded non-byte data")
                        written += len(chunk)
                        if written > manifest.installer.size:
                            raise UpdateMetadataError("Installer exceeds its signed size")
                        handle.write(chunk)
                        digest.update(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                if written != manifest.installer.size:
                    raise UpdateMetadataError("Installer is shorter than its signed size")
                if digest.hexdigest() != manifest.installer.sha256:
                    raise UpdateMetadataError("Installer SHA-256 does not match signed metadata")

                os.replace(partial, final_installer)
                self._atomic_write(self._metadata_path(version_text), metadata)
                self._atomic_write(self._signature_path(version_text), signature)
                staged = StagedUpdate(manifest=manifest, installer_path=final_installer)
                if not self._installer_matches(staged):
                    raise UpdateMetadataError("Published installer failed re-verification")
                self._remove_other_versions(keep=Version(version_text))
                return staged
            except Exception:
                partial.unlink(missing_ok=True)
                self._remove_version(version_text)
                raise

    def verify(self, staged: StagedUpdate | None, *, current_version: str) -> bool:
        if staged is None:
            return False
        with self._lock:
            version_text = str(staged.manifest.version)
            try:
                metadata = self._metadata_path(version_text).read_bytes()
                verified = parse_signed_manifest(
                    metadata,
                    self._signature_path(version_text).read_bytes(),
                    self._public_key,
                    current_version=current_version,
                    now=self._effective_now(),
                )
                self._accept_trust(verified, metadata)
                valid = verified == staged.manifest and self._installer_matches(staged)
            except (OSError, UpdateMetadataError):
                valid = False
            if not valid:
                self._remove_version(version_text)
            return valid

    def accept_metadata(self, manifest: UpdateManifest, metadata: bytes) -> None:
        """Persist rollback/freeze state before downloading the installer."""
        with self._lock:
            self._accept_trust(manifest, metadata)

    def discard_version(self, version: str) -> None:
        """Remove one confirmed/stale payload without erasing trust history."""
        if not isinstance(version, str) or re.fullmatch(_VERSION_TEXT, version) is None:
            raise UpdateMetadataError("Update cache version is invalid")
        with self._lock:
            self._remove_version(version)

    @contextmanager
    def lock_for_launch(
        self,
        staged: StagedUpdate,
        *,
        current_version: str,
    ) -> Iterator[LockedInstaller]:
        """Reverify and hold an installer open with writes/deletes denied."""
        with self._lock:
            version_text = str(staged.manifest.version)
            try:
                metadata = self._metadata_path(version_text).read_bytes()
                verified = parse_signed_manifest(
                    metadata,
                    self._signature_path(version_text).read_bytes(),
                    self._public_key,
                    current_version=current_version,
                    now=self._effective_now(),
                )
                self._accept_trust(verified, metadata)
                if verified != staged.manifest:
                    raise UpdateMetadataError("Staged update metadata changed before launch")
                with open_locked_read(staged.installer_path) as handle:
                    stat = os.fstat(handle.fileno())
                    if stat.st_nlink != 1:
                        raise UpdateMetadataError("Staged installer has unexpected hard links")
                    if stat.st_size != verified.installer.size:
                        raise UpdateMetadataError("Staged installer size changed before launch")
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
                    if digest != verified.installer.sha256:
                        raise UpdateMetadataError("Staged installer changed before launch")
                    handle.seek(0)
                    yield LockedInstaller(
                        installer_path=staged.installer_path,
                        manifest=verified,
                        file=handle,
                    )
            except UpdateMetadataError:
                self._remove_version(version_text)
                raise
            except OSError as exc:
                self._remove_version(version_text)
                raise UpdateMetadataError("Staged installer could not be locked") from exc

    def _accept_trust(self, manifest: UpdateManifest, metadata: bytes) -> None:
        state = self._load_trust_state()
        wall_now = self._utc_now()
        effective_now = max(wall_now, state["trusted_time"] if state else wall_now)
        validate_manifest_freshness(manifest, now=effective_now)
        digest = hashlib.sha256(metadata).hexdigest()
        if state is not None:
            highest = state["metadata_version"]
            if manifest.metadata_version < highest:
                raise UpdateMetadataError("Signed update metadata was rolled back")
            if manifest.metadata_version == highest and digest != state["metadata_sha256"]:
                raise UpdateMetadataError("Signed metadata version was reused with different bytes")

        if (
            state is None
            or manifest.metadata_version > state["metadata_version"]
            or effective_now > state["trusted_time"]
        ):
            payload = {
                "metadata_sha256": digest if state is None or manifest.metadata_version >= state["metadata_version"] else state["metadata_sha256"],
                "metadata_version": max(
                    manifest.metadata_version,
                    state["metadata_version"] if state else 0,
                ),
                "trusted_time": effective_now.isoformat().replace("+00:00", "Z"),
            }
            if state is not None and manifest.metadata_version == state["metadata_version"]:
                payload["metadata_sha256"] = state["metadata_sha256"]
            self._atomic_write(
                self._trust_state_path(),
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            )

    def _effective_now(self) -> datetime:
        wall_now = self._utc_now()
        state = self._load_trust_state()
        return max(wall_now, state["trusted_time"] if state else wall_now)

    def _utc_now(self) -> datetime:
        value = self._now_provider()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise UpdateMetadataError("Update clock is invalid")
        return value.astimezone(UTC)

    def _load_trust_state(self) -> dict[str, object] | None:
        path = self._trust_state_path()
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise UpdateMetadataError("Update trust state is not a regular file")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or set(payload) != {
                "metadata_sha256", "metadata_version", "trusted_time"
            }:
                raise ValueError
            version = payload["metadata_version"]
            digest = payload["metadata_sha256"]
            time_text = payload["trusted_time"]
            if (
                type(version) is not int
                or version < 1
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not isinstance(time_text, str)
                or not time_text.endswith("Z")
            ):
                raise ValueError
            trusted_time = datetime.fromisoformat(time_text[:-1] + "+00:00")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise UpdateMetadataError("Update trust state is corrupt") from exc
        return {
            "metadata_sha256": digest,
            "metadata_version": version,
            "trusted_time": trusted_time,
        }

    def _installer_matches(self, staged: StagedUpdate) -> bool:
        path = staged.installer_path
        if path != self._installer_path(str(staged.manifest.version)):
            return False
        if path.is_symlink() or not path.is_file():
            return False
        try:
            stat = path.stat()
            if stat.st_nlink != 1 or stat.st_size != staged.manifest.installer.size:
                return False
            with path.open("rb") as handle:
                return (
                    hashlib.file_digest(handle, "sha256").hexdigest()
                    == staged.manifest.installer.sha256
                )
        except OSError:
            return False

    def _remove_stale_partials(self) -> None:
        for path in self.root.iterdir():
            if path.is_file() and _PARTIAL_FILE.fullmatch(path.name):
                path.unlink(missing_ok=True)

    def _remove_other_versions(self, *, keep: Version) -> None:
        for metadata_path in self.root.glob("Momento-update-*.json"):
            match = _METADATA_FILE.fullmatch(metadata_path.name)
            if match is not None and Version(match.group(1)) != keep:
                self._remove_version(match.group(1))

    def _remove_version(self, version: str) -> None:
        for path in (
            self._installer_path(version),
            self._metadata_path(version),
            self._signature_path(version),
        ):
            try:
                if path.parent == self.root and not path.is_symlink():
                    path.unlink(missing_ok=True)
                elif path.is_symlink():
                    path.unlink(missing_ok=True)
            except OSError:
                pass

    def _installer_path(self, version: str) -> Path:
        return self.root / f"MomentoSetup-{version}.exe"

    def _metadata_path(self, version: str) -> Path:
        return self.root / f"Momento-update-{version}.json"

    def _signature_path(self, version: str) -> Path:
        return self.root / f"Momento-update-{version}.json.sig"

    def _trust_state_path(self) -> Path:
        return self.root / "trusted-state.json"

    def _atomic_write(self, path: Path, data: bytes) -> None:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=self.root
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)


def _is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & 0x400)
    except OSError:
        return True
