"""Glue between verified update state, Setup handoff, and app startup."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from momento.updater.attempts import AttemptStateError, UpdateAttemptStore
from momento.updater.cache import UpdateCache
from momento.updater.handoff import UpdateHandoffError, launch_update
from momento.updater.metadata import UpdateMetadataError
from momento.util.single_instance import is_valid_update_attempt_token


logger = logging.getLogger(__name__)
STARTUP_CONFIRMATION_DELAY_MS = 5_000


def updated_attempt_token(arguments: Sequence[str]) -> str | None:
    """Extract exactly one strict ``--updated=<token>`` startup marker."""
    values = [item.removeprefix("--updated=") for item in arguments if item.startswith("--updated=")]
    if len(values) != 1 or not is_valid_update_attempt_token(values[0]):
        return None
    return values[0]


class UpdateRuntime:
    """Own durable attempt transitions around a verified installer launch."""

    def __init__(
        self,
        *,
        current_version: str,
        single_instance,
        cache: UpdateCache,
        attempts: UpdateAttemptStore,
        launch_handoff: Callable[..., object] = launch_update,
    ) -> None:
        self.current_version = current_version
        self.cache = cache
        self.attempts = attempts
        self._single_instance = single_instance
        self._launch_handoff = launch_handoff

    def confirm_startup(self, attempt_token: str) -> bool:
        """Confirm an exact token/version pair and remove only its payload."""
        if not is_valid_update_attempt_token(attempt_token):
            return False
        try:
            confirmed = self.attempts.confirm_startup(
                attempt_token, self.current_version
            )
            if confirmed:
                self.cache.discard_version(self.current_version)
            return confirmed
        except (AttemptStateError, UpdateMetadataError, OSError) as exc:
            logger.warning("Update startup confirmation failed (%s)", type(exc).__name__)
            return False

    def schedule_startup_confirmation(
        self,
        attempt_token: str,
        *,
        schedule: Callable[[int, Callable[[], None]], None],
        on_complete: Callable[[bool], None] | None = None,
    ) -> bool:
        """Confirm only after the new event loop survives a stability window."""
        if not is_valid_update_attempt_token(attempt_token):
            return False

        def finish() -> None:
            confirmed = self.confirm_startup(attempt_token)
            if confirmed:
                logger.info("Confirmed successful update startup")
            else:
                logger.warning("Rejected stale or mismatched update startup marker")
            if on_complete is not None:
                on_complete(confirmed)

        schedule(STARTUP_CONFIRMATION_DELAY_MS, finish)
        return True

    def launch(self, staged) -> bool:
        """Lock, identify, and hand a staged installer to Setup."""
        attempt = None
        try:
            with self.cache.lock_for_launch(
                staged, current_version=self.current_version
            ) as locked:
                attempt = self.attempts.begin_attempt(
                    source_version=self.current_version,
                    target_version=str(locked.manifest.version),
                    installer_sha256=locked.manifest.installer.sha256,
                )
                if attempt is None:
                    return False
                self._launch_handoff(
                    installer=locked.installer_path,
                    attempt_token=attempt.attempt_token,
                    single_instance=self._single_instance,
                    verified_handle=locked.file,
                )
            return True
        except (
            AttemptStateError,
            UpdateHandoffError,
            UpdateMetadataError,
            OSError,
            ValueError,
        ) as exc:
            logger.warning("Verified update launch failed (%s)", type(exc).__name__)
            if attempt is not None:
                try:
                    self.attempts.record_setup_exit(attempt.attempt_token, -1)
                except AttemptStateError:
                    logger.warning("Could not record failed update launch")
            return False
