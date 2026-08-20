"""Application-level update orchestration checks."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from PyQt6.QtCore import QPoint, QRect
from PyQt6.QtWidgets import QApplication, QMessageBox

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.updater.client import UpdateResult, UpdateStatus  # noqa: E402
from momento.updater.service import UpdateService  # noqa: E402
from momento.ui import tray as tray_module  # noqa: E402
from momento.ui.tray import MomentoTray  # noqa: E402


_results: list[tuple[str, bool]] = []


def check(label: str, condition: bool) -> None:
    _results.append((label, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'} - {label}")


class FakeClient:
    def __init__(self, result: UpdateResult) -> None:
        self.result = result
        self.calls = 0

    def check(self, *, current_version: str) -> UpdateResult:
        assert current_version == "0.2.2"
        self.calls += 1
        return self.result


class FakeLease:
    def __init__(self) -> None:
        self.committed = False
        self.released = False

    def commit(self) -> None:
        self.committed = True

    def release(self) -> None:
        self.released = True


class FakeSession:
    def __init__(self) -> None:
        self.is_monitoring = True
        self.scan_ready = True
        self.lease: FakeLease | None = FakeLease()
        self.acquire_calls = 0

    def wait_initial_scan(self, timeout: float | None = None) -> bool:
        assert timeout == 0
        return self.scan_ready

    def acquire_update_quiescence(self):
        self.acquire_calls += 1
        return self.lease


class FakeAction:
    def setEnabled(self, _enabled: bool) -> None:
        pass

    def setText(self, _text: str) -> None:
        pass


def _staged(version: str = "0.2.3"):
    return SimpleNamespace(
        manifest=SimpleNamespace(version=version),
        installer_path=Path("MomentoSetup.exe"),
    )


def _service(
    result: UpdateResult,
    *,
    session: FakeSession | None = None,
    can_install=lambda: True,
    confirmer=None,
    launcher=lambda _staged: True,
):
    client = FakeClient(result)
    session = session or FakeSession()
    quit_calls: list[bool] = []
    service = UpdateService(
        current_version="0.2.2",
        session=session,
        client=client,
        can_install=can_install,
        confirm_install=confirmer,
        launch_installer=launcher,
        quit_callback=lambda: quit_calls.append(True),
        submit=lambda work: work(),
        enabled=True,
    )
    statuses: list[tuple[str, str, bool]] = []
    service.status_changed.connect(
        lambda code, message, interactive: statuses.append(
            (code, message, interactive)
        )
    )
    return service, client, session, statuses, quit_calls


def test_manual_current_is_visible() -> None:
    service, client, _session, statuses, _quit = _service(
        UpdateResult(UpdateStatus.CURRENT)
    )
    service.check_now()
    check("manual check runs once", client.calls == 1)
    check(
        "manual current result is interactive",
        statuses[-1][0] == "current" and statuses[-1][2],
    )


def test_update_result_is_owned_by_visible_editor() -> None:
    editor = SimpleNamespace(isVisible=lambda: True)
    dialogs: list[tuple[QMessageBox.Icon, str, str]] = []
    tray = SimpleNamespace(
        _editor=editor,
        _check_updates_action=FakeAction(),
        _show_update_message=lambda icon, title, message: dialogs.append(
            (icon, title, message)
        ),
    )
    MomentoTray._on_update_status(
        tray,
        "current",
        "You're using the latest version of Momento.",
        True,
    )
    check(
        "current update result uses an information dialog",
        dialogs
        == [
            (
                QMessageBox.Icon.Information,
                "Momento updates",
                "You're using the latest version of Momento.",
            )
        ],
    )


def test_centering_compensates_for_native_window_frame() -> None:
    center_window = getattr(MomentoTray, "_center_window_frame", None)
    if center_window is None:
        check("update centering compensates for native window borders", False)
        return

    class FakeParent:
        @staticmethod
        def frameGeometry() -> QRect:
            return QRect(0, 0, 1000, 700)

    class FakeDialog:
        moved_to: QPoint | None = None

        @staticmethod
        def frameGeometry() -> QRect:
            return QRect(300, 200, 360, 150)

        @staticmethod
        def geometry() -> QRect:
            return QRect(308, 231, 344, 111)

        def move(self, point: QPoint) -> None:
            self.moved_to = point

    dialog = FakeDialog()
    center_window(dialog, FakeParent())
    check(
        "update centering compensates for native window borders",
        dialog.moved_to == QPoint(328, 306),
    )


def test_automatic_check_runs_once_and_stays_noninteractive() -> None:
    service, client, _session, statuses, _quit = _service(
        UpdateResult(UpdateStatus.FAILED, error="offline")
    )
    service.start_automatic_check()
    service.start_automatic_check()
    check("automatic check is once per process", client.calls == 1)
    check(
        "automatic failure remains noninteractive",
        statuses[-1] == ("failed", "offline", False),
    )


def test_manual_update_requires_an_install_choice() -> None:
    result = UpdateResult(UpdateStatus.AVAILABLE, staged=_staged())
    launch_calls: list[object] = []
    decision_calls: list[object] = []
    service, _client, session, statuses, quit_calls = _service(
        result,
        confirmer=lambda staged: decision_calls.append(staged) or False,
        launcher=lambda staged: launch_calls.append(staged) or True,
    )
    service.check_now()
    check("manual update asks once after download", decision_calls == [result.staged])
    check("Later keeps the staged installer", not launch_calls)
    check("Later does not quiesce the app", session.acquire_calls == 0)
    check("Later keeps Momento running", not quit_calls)
    check(
        "Later schedules installation for next start",
        statuses[-1]
        == (
            "deferred",
            "The update is ready and will install the next time Momento starts.",
            False,
        ),
    )


def test_manual_install_now_uses_the_staged_update() -> None:
    result = UpdateResult(UpdateStatus.AVAILABLE, staged=_staged())
    launch_calls: list[object] = []
    decision_calls: list[object] = []
    service, _client, _session, statuses, quit_calls = _service(
        result,
        confirmer=lambda staged: decision_calls.append(staged) or True,
        launcher=lambda staged: launch_calls.append(staged) or True,
    )
    service.check_now()
    check("Install now asks once", decision_calls == [result.staged])
    check("Install now launches the verified staged update", launch_calls == [result.staged])
    check("Install now reports installation", statuses[-1][0] == "installing")
    check("Install now closes the old app", len(quit_calls) == 1)


def test_automatic_update_never_prompts() -> None:
    result = UpdateResult(UpdateStatus.AVAILABLE, staged=_staged())
    launch_calls: list[object] = []
    decision_calls: list[object] = []
    service, _client, _session, statuses, quit_calls = _service(
        result,
        confirmer=lambda staged: decision_calls.append(staged) or False,
        launcher=lambda staged: launch_calls.append(staged) or True,
    )
    service.start_automatic_check()
    check("startup update does not ask permission", not decision_calls)
    check("startup update installs automatically", launch_calls == [result.staged])
    check("startup update reports installation", statuses[-1][0] == "installing")
    check("startup update closes the old app", len(quit_calls) == 1)


def test_manual_update_prompt_copy_and_choices() -> None:
    confirm = getattr(MomentoTray, "_confirm_update_install", None)
    if confirm is None:
        check("manual update prompt exists", False)
        return

    dialogs: list[object] = []

    class FakeButton:
        def __init__(self, label: str) -> None:
            self.label = label

    class FakeMessageBox:
        choose_later = False

        class Icon:
            Question = "question"

        class ButtonRole:
            AcceptRole = "accept"
            RejectRole = "reject"

        def __init__(self, parent=None) -> None:
            self.parent = parent
            self.icon = None
            self.title = ""
            self.text = ""
            self.informative_text = ""
            self.buttons: list[tuple[FakeButton, object]] = []
            self.default_button = None
            self.escape_button = None
            self.clicked = None
            dialogs.append(self)

        def setIcon(self, icon) -> None:
            self.icon = icon

        def setWindowTitle(self, title: str) -> None:
            self.title = title

        def setText(self, text: str) -> None:
            self.text = text

        def setInformativeText(self, text: str) -> None:
            self.informative_text = text

        def addButton(self, label: str, role) -> FakeButton:
            button = FakeButton(label)
            self.buttons.append((button, role))
            return button

        def setDefaultButton(self, button) -> None:
            self.default_button = button

        def setEscapeButton(self, button) -> None:
            self.escape_button = button

        def exec(self) -> None:
            self.clicked = self.escape_button if self.choose_later else self.default_button

        def clickedButton(self):
            return self.clicked

    real_box = tray_module.QMessageBox
    tray_module.QMessageBox = FakeMessageBox
    try:
        editor = SimpleNamespace(
            visible=True,
            hide_calls=0,
            isVisible=lambda: editor.visible,
            hide=lambda: (
                setattr(editor, "visible", False),
                setattr(editor, "hide_calls", editor.hide_calls + 1),
            ),
        )
        tray = SimpleNamespace(
            _editor=editor,
            _update_dialog_parent=lambda: editor,
        )
        approved = confirm(tray, _staged("0.2.7"))
        later_editor = SimpleNamespace(
            visible=True,
            hide_calls=0,
            isVisible=lambda: later_editor.visible,
            hide=lambda: (
                setattr(later_editor, "visible", False),
                setattr(later_editor, "hide_calls", later_editor.hide_calls + 1),
            ),
        )
        later_tray = SimpleNamespace(
            _editor=later_editor,
            _update_dialog_parent=lambda: later_editor,
        )
        FakeMessageBox.choose_later = True
        approved_later = confirm(later_tray, _staged("0.2.7"))
    finally:
        tray_module.QMessageBox = real_box

    dialog = dialogs[0]
    labels = [button.label for button, _role in dialog.buttons]
    check("manual update prompt defaults to Install now", approved)
    check("Install now closes the editor before the safety gate", editor.hide_calls == 1)
    check("Later keeps the editor open", not approved_later and later_editor.hide_calls == 0)
    check("manual update prompt names the downloaded version", "0.2.7" in dialog.text)
    check("manual update prompt offers Install now and Later", labels == ["Install now", "Later"])
    check("Later is the escape choice", dialog.escape_button.label == "Later")
    check(
        "manual update prompt explains next-start installation",
        "next time Momento starts" in dialog.informative_text,
    )
    check(
        "manual update prompt promises user-data preservation",
        "recordings and settings" in dialog.informative_text,
    )


def test_entrypoint_wires_manual_confirmation() -> None:
    entrypoint = Path(__file__).resolve().parents[1] / "momento" / "__main__.py"
    source = entrypoint.read_text(encoding="utf-8")
    check(
        "installed app wires the manual update confirmation",
        "confirm_install=tray._confirm_update_install" in source,
    )


def test_busy_work_defers_without_quiescing() -> None:
    result = UpdateResult(UpdateStatus.AVAILABLE, staged=_staged())
    service, _client, session, statuses, quit_calls = _service(
        result, can_install=lambda: False
    )
    service.start_automatic_check()
    check("active app work defers staged update", statuses[-1][0] == "deferred")
    check("deferred update does not stop recording monitor", session.acquire_calls == 0)
    check("deferred update does not quit", not quit_calls)


def test_visible_editor_defers_update_installation() -> None:
    editor = SimpleNamespace(
        isVisible=lambda: True,
        has_update_blocking_activity=lambda: False,
    )
    tray = SimpleNamespace(_editor=editor)
    check(
        "visible editor defers background update installation",
        not MomentoTray.is_update_install_ready(tray),
    )


def test_initial_game_scan_is_a_hard_gate() -> None:
    session = FakeSession()
    session.scan_ready = False
    result = UpdateResult(UpdateStatus.AVAILABLE, staged=_staged())
    service, _client, session, statuses, _quit = _service(result, session=session)
    service.start_automatic_check()
    check("update defers before first game scan", statuses[-1][0] == "deferred")
    check("pre-scan update never acquires quiescence", session.acquire_calls == 0)


def test_paused_monitoring_does_not_wait_for_scan() -> None:
    session = FakeSession()
    session.is_monitoring = False
    session.scan_ready = False
    result = UpdateResult(UpdateStatus.AVAILABLE, staged=_staged())
    service, _client, session, statuses, quit_calls = _service(result, session=session)
    service.start_automatic_check()
    check("paused monitoring can install without a watcher scan", statuses[-1][0] == "installing")
    check("paused monitoring still acquires application quiescence", session.acquire_calls == 1)
    check("successful handoff quits old app", len(quit_calls) == 1)


def test_success_commits_and_failure_releases() -> None:
    result = UpdateResult(UpdateStatus.AVAILABLE, staged=_staged())
    service, _client, session, statuses, quit_calls = _service(result)
    lease = session.lease
    service.start_automatic_check()
    assert lease is not None
    check("successful launch commits update lease", lease.committed and lease.released)
    check("successful launch reports installing", statuses[-1][0] == "installing")
    check("successful launch requests one quit", len(quit_calls) == 1)

    failed_session = FakeSession()
    failed_service, _client, _session, failed_statuses, failed_quit = _service(
        result, session=failed_session, launcher=lambda _staged: False
    )
    failed_lease = failed_session.lease
    failed_service.start_automatic_check()
    assert failed_lease is not None
    check(
        "failed launch releases uncommitted lease",
        failed_lease.released and not failed_lease.committed,
    )
    check("failed launch keeps app running", not failed_quit)
    check("failed launch reports a contained failure", failed_statuses[-1][0] == "failed")


def main() -> int:
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    test_manual_current_is_visible()
    test_update_result_is_owned_by_visible_editor()
    test_centering_compensates_for_native_window_frame()
    test_automatic_check_runs_once_and_stays_noninteractive()
    test_manual_update_requires_an_install_choice()
    test_manual_install_now_uses_the_staged_update()
    test_automatic_update_never_prompts()
    test_manual_update_prompt_copy_and_choices()
    test_entrypoint_wires_manual_confirmation()
    test_busy_work_defers_without_quiescing()
    test_visible_editor_defers_update_installation()
    test_initial_game_scan_is_a_hard_gate()
    test_paused_monitoring_does_not_wait_for_scan()
    test_success_commits_and_failure_releases()
    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
