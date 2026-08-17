"""Qt-thread orchestration for startup and manual update checks."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from momento.updater.client import UpdateClient, UpdateResult, UpdateStatus


logger = logging.getLogger(__name__)


class _WorkItem(QRunnable):
    def __init__(self, work: Callable[[], None]) -> None:
        super().__init__()
        self._work = work

    def run(self) -> None:
        self._work()


def _submit_to_pool(work: Callable[[], None]) -> None:
    QThreadPool.globalInstance().start(_WorkItem(work))


class UpdateService(QObject):
    """Run one launch check and install only after app-wide quiescence."""

    status_changed = pyqtSignal(str, str, bool)
    _result_ready = pyqtSignal(object)

    def __init__(
        self,
        *,
        current_version: str,
        session,
        can_install: Callable[[], bool],
        launch_installer: Callable[[object], bool],
        quit_callback: Callable[[], None],
        client: UpdateClient | None = None,
        submit: Callable[[Callable[[], None]], None] | None = None,
        enabled: bool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._current_version = current_version
        self._session = session
        self._can_install = can_install
        self._launch_installer = launch_installer
        self._quit_callback = quit_callback
        self._client = client or UpdateClient()
        self._submit = submit or _submit_to_pool
        self._enabled = (
            bool(getattr(sys, "frozen", False) and os.name == "nt")
            if enabled is None
            else bool(enabled)
        )
        self._automatic_started = False
        self._checking = False
        self._interactive_pending = False
        self._result_ready.connect(self._on_result)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start_automatic_check(self) -> None:
        if self._automatic_started:
            return
        self._automatic_started = True
        self._start_check(interactive=False)

    def check_now(self) -> None:
        self._start_check(interactive=True)

    def _start_check(self, *, interactive: bool) -> None:
        if not self._enabled:
            if interactive:
                self.status_changed.emit(
                    "unavailable",
                    "Update checks are available in the installed version of Momento.",
                    True,
                )
            return
        if self._checking:
            self._interactive_pending = self._interactive_pending or interactive
            return

        self._checking = True
        self._interactive_pending = interactive
        self.status_changed.emit("checking", "Checking for updates...", interactive)

        def work() -> None:
            try:
                result = self._client.check(current_version=self._current_version)
            except Exception as exc:
                logger.warning("Update worker failed (%s)", type(exc).__name__)
                result = UpdateResult(
                    UpdateStatus.FAILED,
                    error="The update check could not be completed.",
                )
            self._result_ready.emit(result)

        try:
            self._submit(work)
        except Exception as exc:
            self._checking = False
            logger.warning("Could not start update worker (%s)", type(exc).__name__)
            self.status_changed.emit(
                "failed",
                "The update check could not be started.",
                interactive,
            )

    def _on_result(self, result: UpdateResult) -> None:
        interactive = self._interactive_pending
        self._checking = False
        self._interactive_pending = False

        if result.status is UpdateStatus.CURRENT:
            self.status_changed.emit(
                "current", "You're using the latest version of Momento.", interactive
            )
            return
        if result.status is UpdateStatus.FAILED:
            self.status_changed.emit(
                "failed",
                result.error or "The update could not be verified.",
                interactive,
            )
            return
        if result.staged is None:
            self.status_changed.emit(
                "failed", "The downloaded update could not be verified.", interactive
            )
            return

        self._install_if_quiescent(result.staged, interactive=interactive)

    def _install_if_quiescent(self, staged, *, interactive: bool) -> None:
        if self._session.is_monitoring and not self._session.wait_initial_scan(timeout=0):
            self._defer(interactive)
            return
        try:
            app_idle = bool(self._can_install())
        except Exception:
            logger.exception("Application activity check failed")
            app_idle = False
        if not app_idle:
            self._defer(interactive)
            return

        lease = self._session.acquire_update_quiescence()
        if lease is None:
            self._defer(interactive)
            return

        launched = False
        try:
            launched = bool(self._launch_installer(staged))
        except Exception as exc:
            logger.warning("Update handoff failed (%s)", type(exc).__name__)
        if not launched:
            lease.release()
            self.status_changed.emit(
                "failed",
                "The update is downloaded, but Momento could not start the installer. "
                "It will try again next launch.",
                interactive,
            )
            return

        lease.commit()
        lease.release()
        self.status_changed.emit("installing", "Installing update...", interactive)
        self._quit_callback()

    def _defer(self, interactive: bool) -> None:
        self.status_changed.emit(
            "deferred",
            "The update is ready and will install the next time Momento starts.",
            interactive,
        )
