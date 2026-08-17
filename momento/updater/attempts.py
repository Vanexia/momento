"""Durable, bounded state for unattended installer launch attempts."""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from packaging.version import InvalidVersion, Version

from momento.util.paths import update_cache_dir


MAX_ATTEMPTS = 3
BASE_RETRY_DELAY = timedelta(minutes=5)
MAX_RETRY_DELAY = timedelta(hours=1)
ATTEMPT_TIMEOUT = timedelta(minutes=15)

_STATE_FILENAME = "attempt-state.json"
_MAX_STATE_BYTES = 16 * 1024
_TOKEN = re.compile(r"[0-9a-f]{64}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
_STATES = frozenset({"launching", "awaiting_confirmation", "retry_wait", "quarantined", "confirmed"})
_FIELDS = frozenset({
    "attempt_count",
    "attempt_token",
    "installer_sha256",
    "next_retry_at",
    "setup_exit_code",
    "source_version",
    "started_at",
    "state",
    "target_version",
})


class AttemptStateError(RuntimeError):
    """Attempt state could not be trusted or persisted."""


@dataclass(frozen=True, slots=True)
class UpdateAttempt:
    attempt_token: str
    source_version: str
    target_version: str
    installer_sha256: str
    state: str
    attempt_count: int
    started_at: datetime
    next_retry_at: datetime | None
    setup_exit_code: int | None


class UpdateAttemptStore:
    """Persist one active update target and bound its installer retries."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        now_provider: Callable[[], datetime] | None = None,
        token_provider: Callable[[], str] | None = None,
    ) -> None:
        candidate = Path(root) if root is not None else update_cache_dir()
        candidate.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(candidate):
            raise AttemptStateError("Update attempt state root is not trusted")
        self.root = candidate.resolve()
        self.path = self.root / _STATE_FILENAME
        self._now_provider = now_provider or (lambda: datetime.now(UTC))
        self._token_provider = token_provider or (lambda: secrets.token_hex(32))
        self._lock = threading.RLock()

    def load(self) -> UpdateAttempt | None:
        with self._lock:
            return self._load()

    def begin_attempt(
        self,
        *,
        source_version: str,
        target_version: str,
        installer_sha256: str,
    ) -> UpdateAttempt | None:
        """Create the next launch attempt, or return None while suppressed."""
        source = _stable_version(source_version, field="source version")
        target = _stable_version(target_version, field="target version")
        if target <= source:
            raise AttemptStateError("Update attempt target must be newer than its source")
        if not isinstance(installer_sha256, str) or _SHA256.fullmatch(installer_sha256) is None:
            raise AttemptStateError("Update attempt installer digest is invalid")

        with self._lock:
            now = self._utc_now()
            current = self._load()
            if current is not None:
                current_target = Version(current.target_version)
                if target < current_target:
                    return None
                if target == current_target and installer_sha256 != current.installer_sha256:
                    raise AttemptStateError("Update attempt target identity changed")

                current = self._expire_active(current, now)
                if target == current_target:
                    if current.state in {"launching", "awaiting_confirmation", "confirmed", "quarantined"}:
                        return None
                    if current.next_retry_at is None or now < current.next_retry_at:
                        return None
                    if current.attempt_count >= MAX_ATTEMPTS:
                        quarantined = replace(current, state="quarantined", next_retry_at=None)
                        self._write(quarantined)
                        return None
                    attempt_count = current.attempt_count + 1
                else:
                    if current.state in {"launching", "awaiting_confirmation"}:
                        return None
                    attempt_count = 1
            else:
                attempt_count = 1

            token = self._new_token(current)
            attempt = UpdateAttempt(
                attempt_token=token,
                source_version=str(source),
                target_version=str(target),
                installer_sha256=installer_sha256,
                state="launching",
                attempt_count=attempt_count,
                started_at=now,
                next_retry_at=now + ATTEMPT_TIMEOUT,
                setup_exit_code=None,
            )
            self._write(attempt)
            return attempt

    def record_setup_exit(self, attempt_token: str, exit_code: int) -> bool:
        """Record Setup completion when it belongs to the current attempt."""
        if not isinstance(attempt_token, str) or _TOKEN.fullmatch(attempt_token) is None:
            return False
        if type(exit_code) is not int or not (-(2**31) <= exit_code <= (2**32 - 1)):
            raise AttemptStateError("Setup exit code is invalid")

        with self._lock:
            current = self._load()
            if (
                current is None
                or current.state != "launching"
                or not secrets.compare_digest(current.attempt_token, attempt_token)
            ):
                return False

            now = self._utc_now()
            if now >= current.next_retry_at:  # type: ignore[operator]
                self._write(self._expire_active(current, now))
                return False
            if exit_code == 0:
                updated = replace(
                    current,
                    state="awaiting_confirmation",
                    next_retry_at=now + ATTEMPT_TIMEOUT,
                    setup_exit_code=0,
                )
            elif current.attempt_count >= MAX_ATTEMPTS:
                updated = replace(
                    current,
                    state="quarantined",
                    next_retry_at=None,
                    setup_exit_code=exit_code,
                )
            else:
                updated = replace(
                    current,
                    state="retry_wait",
                    next_retry_at=now + _retry_delay(current.attempt_count),
                    setup_exit_code=exit_code,
                )
            self._write(updated)
            return True

    def confirm_startup(self, attempt_token: str, running_version: str) -> bool:
        """Confirm only the target process carrying the exact live token."""
        if not isinstance(attempt_token, str) or _TOKEN.fullmatch(attempt_token) is None:
            return False
        try:
            running = _stable_version(running_version, field="running version")
        except AttemptStateError:
            return False

        with self._lock:
            current = self._load()
            if (
                current is None
                or current.state not in {"launching", "awaiting_confirmation"}
                or str(running) != current.target_version
                or not secrets.compare_digest(current.attempt_token, attempt_token)
            ):
                return False
            now = self._utc_now()
            if current.next_retry_at is None or now >= current.next_retry_at:
                self._write(self._expire_active(current, now))
                return False
            self._write(replace(current, state="confirmed", next_retry_at=None))
            return True

    def _expire_active(self, attempt: UpdateAttempt, now: datetime) -> UpdateAttempt:
        if (
            attempt.state not in {"launching", "awaiting_confirmation"}
            or attempt.next_retry_at is None
            or now < attempt.next_retry_at
        ):
            return attempt
        if attempt.attempt_count >= MAX_ATTEMPTS:
            expired = replace(attempt, state="quarantined", next_retry_at=None)
        else:
            expired = replace(attempt, state="retry_wait", next_retry_at=now)
        self._write(expired)
        return expired

    def _new_token(self, previous: UpdateAttempt | None) -> str:
        for _ in range(3):
            token = self._token_provider()
            if (
                isinstance(token, str)
                and _TOKEN.fullmatch(token) is not None
                and (previous is None or not secrets.compare_digest(token, previous.attempt_token))
            ):
                return token
        raise AttemptStateError("Could not create a valid update attempt token")

    def _load(self) -> UpdateAttempt | None:
        if not self.path.exists():
            return None
        if _is_reparse_point(self.path) or not self.path.is_file():
            raise AttemptStateError("Update attempt state is corrupt")
        try:
            raw = self.path.read_bytes()
            if not raw or len(raw) > _MAX_STATE_BYTES:
                raise ValueError
            text = raw.decode("utf-8")
            payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
            if not isinstance(payload, dict) or set(payload) != _FIELDS:
                raise ValueError
            if json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") != raw:
                raise ValueError
            attempt = _validate_payload(payload)
        except AttemptStateError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise AttemptStateError("Update attempt state is corrupt") from exc
        return attempt

    def _write(self, attempt: UpdateAttempt) -> None:
        payload = asdict(attempt)
        payload["started_at"] = _format_time(attempt.started_at)
        payload["next_retry_at"] = (
            _format_time(attempt.next_retry_at) if attempt.next_retry_at is not None else None
        )
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        descriptor = -1
        temporary_path: Path | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{_STATE_FILENAME}.", suffix=".tmp", dir=self.root
            )
            temporary_path = Path(temporary)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise AttemptStateError("Update attempt state could not be saved") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _utc_now(self) -> datetime:
        value = self._now_provider()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise AttemptStateError("Update attempt clock is invalid")
        return value.astimezone(UTC)


def _validate_payload(payload: dict[str, object]) -> UpdateAttempt:
    token = payload["attempt_token"]
    source_text = payload["source_version"]
    target_text = payload["target_version"]
    digest = payload["installer_sha256"]
    state = payload["state"]
    count = payload["attempt_count"]
    exit_code = payload["setup_exit_code"]

    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        raise ValueError
    source = _stable_version(source_text, field="source version")
    target = _stable_version(target_text, field="target version")
    if target <= source:
        raise ValueError
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ValueError
    if not isinstance(state, str) or state not in _STATES:
        raise ValueError
    if type(count) is not int or not (1 <= count <= MAX_ATTEMPTS):
        raise ValueError
    if exit_code is not None and (
        type(exit_code) is not int or not (-(2**31) <= exit_code <= (2**32 - 1))
    ):
        raise ValueError

    started = _parse_time(payload["started_at"])
    next_retry = _parse_optional_time(payload["next_retry_at"])
    if state == "launching" and (exit_code is not None or next_retry is None or next_retry <= started):
        raise ValueError
    if state == "awaiting_confirmation" and (
        exit_code != 0 or next_retry is None or next_retry <= started
    ):
        raise ValueError
    if state == "retry_wait" and (next_retry is None or next_retry < started):
        raise ValueError
    if state == "quarantined" and next_retry is not None:
        raise ValueError
    if state == "confirmed" and (next_retry is not None or exit_code not in {None, 0}):
        raise ValueError

    return UpdateAttempt(
        attempt_token=token,
        source_version=str(source),
        target_version=str(target),
        installer_sha256=digest,
        state=state,
        attempt_count=count,
        started_at=started,
        next_retry_at=next_retry,
        setup_exit_code=exit_code,
    )


def _stable_version(value: object, *, field: str) -> Version:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise AttemptStateError(f"Update attempt {field} is invalid")
    try:
        version = Version(value)
    except InvalidVersion as exc:
        raise AttemptStateError(f"Update attempt {field} is invalid") from exc
    if version.is_prerelease or version.is_devrelease or version.is_postrelease or version.local:
        raise AttemptStateError(f"Update attempt {field} is invalid")
    return version


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo is None or _format_time(parsed) != value:
        raise ValueError
    return parsed.astimezone(UTC)


def _parse_optional_time(value: object) -> datetime | None:
    return None if value is None else _parse_time(value)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _retry_delay(attempt_count: int) -> timedelta:
    return min(BASE_RETRY_DELAY * (2 ** (attempt_count - 1)), MAX_RETRY_DELAY)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError
        payload[key] = value
    return payload


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
