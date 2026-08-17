"""Smoke tests for the Windows updater handoff boundary."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.updater.handoff import (  # noqa: E402
    UpdateHandoffCoordination,
    UpdateHandoffError,
    claim_parent_process,
    launch_update,
    readiness_event_name,
)
from momento.util.single_instance import (  # noqa: E402
    INSTALLER_MUTEX_NAME,
    UPDATE_GATE_MUTEX_NAME,
    SingleInstance,
    is_update_gate_active,
)


def _check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"PASS: {label}")


def _close_windows_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    close_handle(handle)


def _create_windows_mutex(name: str) -> int:
    import ctypes
    from ctypes import wintypes

    create_mutex = ctypes.WinDLL("kernel32", use_last_error=True).CreateMutexW
    create_mutex.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    create_mutex.restype = wintypes.HANDLE
    handle = create_mutex(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(handle)


class _FakeProcess:
    def __init__(self, *, running: bool = True) -> None:
        self.running = running
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return None if self.running else 1

    def terminate(self) -> None:
        self.terminated = True
        self.running = False

    def kill(self) -> None:
        self.killed = True
        self.running = False

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.running = False
        return 1


class _FakeCoordination:
    def __init__(self, *, ready: bool) -> None:
        self.ready = ready
        self.closed = False
        self.waited = False

    def wait_until_ready(self, process: _FakeProcess, timeout: float) -> bool:
        del process, timeout
        self.waited = True
        return self.ready

    def close(self) -> None:
        self.closed = True


def _acquired_instance(tmp: Path, suffix: str) -> SingleInstance:
    instance = SingleInstance(
        tmp / f"instance-{suffix}.lock",
        mutex_name=f"{INSTALLER_MUTEX_NAME}.Test.Handoff.{suffix}",
        update_gate_name=f"{UPDATE_GATE_MUTEX_NAME}.Test.Handoff.{suffix}",
    )
    instance.acquire()
    return instance


def test_launch_contract_and_verified_handle_lifetime(tmp: Path) -> None:
    suffix = f"{os.getpid()}.Launch"
    instance = _acquired_instance(tmp, suffix)
    installer = tmp / "MomentoSetup-0.2.3.exe"
    installer.write_bytes(b"verified setup payload")
    coordination = _FakeCoordination(ready=True)
    captured: dict[str, object] = {}
    launch_order: list[str] = []

    try:
        with installer.open("rb") as verified_handle:

            def start_process(args, **kwargs):
                launch_order.append("process")
                captured["args"] = list(args)
                captured["kwargs"] = kwargs
                captured["instance_held"] = instance.is_acquired
                captured["handle_open"] = not verified_handle.closed
                os.fstat(verified_handle.fileno())
                return _FakeProcess()

            def create_coordination(token, gate):
                del token, gate
                launch_order.append("readiness-event")
                return coordination

            process = launch_update(
                installer,
                attempt_token="a" * 64,
                single_instance=instance,
                verified_handle=verified_handle,
                readiness_timeout=0.1,
                _coordination_factory=create_coordination,
                _popen_factory=start_process,
            )

        args = captured["args"]
        assert isinstance(args, list)
        _check("verified installer is the executable", args[0] == str(installer.resolve()))
        _check("readiness event is created before Setup", launch_order == ["readiness-event", "process"])
        _check(
            "silent Setup switches are present",
            args[1:5] == ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"],
        )
        _check("update mode is explicit", "/MOMENTOUPDATE" in args)
        _check("Setup receives the still-live parent PID", f"/PARENTPID={os.getpid()}" in args)
        _check("Setup receives the attempt token", f"/ATTEMPTTOKEN={'a' * 64}" in args)
        _check("normal instance lock remains held during process creation", captured["instance_held"] is True)
        _check("verified installer handle remains open during process creation", captured["handle_open"] is True)
        _check("launch waits for Setup readiness", coordination.waited)
        _check("app-side readiness handle closes after readiness", coordination.closed)
        _check("launch does not release the normal instance lock", instance.is_acquired)
        _check("successful launch returns the Setup process", isinstance(process, _FakeProcess))
    finally:
        instance.release()


def test_launch_failures_keep_old_instance_and_release_gate(tmp: Path) -> None:
    suffix = f"{os.getpid()}.Failure"
    instance = _acquired_instance(tmp, suffix)
    installer = tmp / "MomentoSetup-failure.exe"
    installer.write_bytes(b"verified setup payload")

    try:
        coordination = _FakeCoordination(ready=True)
        with installer.open("rb") as verified_handle:
            try:
                launch_update(
                    installer,
                    attempt_token="b" * 64,
                    single_instance=instance,
                    verified_handle=verified_handle,
                    _coordination_factory=lambda token, gate: coordination,
                    _popen_factory=lambda *args, **kwargs: (_ for _ in ()).throw(
                        OSError("launch failed")
                    ),
                )
            except UpdateHandoffError:
                pass
            else:
                raise AssertionError("process creation failure was accepted")
        _check("process creation failure releases app-side coordination", coordination.closed)
        _check("process creation failure leaves old Momento running", instance.is_acquired)

        coordination = _FakeCoordination(ready=False)
        setup = _FakeProcess()
        with installer.open("rb") as verified_handle:
            try:
                launch_update(
                    installer,
                    attempt_token="c" * 64,
                    single_instance=instance,
                    verified_handle=verified_handle,
                    readiness_timeout=0,
                    _coordination_factory=lambda token, gate: coordination,
                    _popen_factory=lambda *args, **kwargs: setup,
                )
            except UpdateHandoffError:
                pass
            else:
                raise AssertionError("Setup readiness timeout was accepted")
        _check("readiness failure stops the untrusted Setup run", setup.terminated or setup.killed)
        _check("readiness failure releases app-side coordination", coordination.closed)
        _check("readiness failure leaves old Momento running", instance.is_acquired)
    finally:
        instance.release()


def test_launch_rejects_unlocked_inputs(tmp: Path) -> None:
    suffix = f"{os.getpid()}.Reject"
    instance = _acquired_instance(tmp, suffix)
    installer = tmp / "MomentoSetup-input.exe"
    other = tmp / "other.exe"
    installer.write_bytes(b"one")
    other.write_bytes(b"two")
    instance.release()

    with installer.open("rb") as verified_handle:
        try:
            launch_update(
                installer,
                attempt_token="d" * 64,
                single_instance=instance,
                verified_handle=verified_handle,
                _coordination_factory=lambda token, gate: _FakeCoordination(ready=True),
                _popen_factory=lambda *args, **kwargs: _FakeProcess(),
            )
        except UpdateHandoffError:
            pass
        else:
            raise AssertionError("launch accepted a released normal instance lock")
    _check("released normal lock is rejected before Setup starts", True)

    instance.acquire()
    try:
        with other.open("rb") as wrong_handle:
            try:
                launch_update(
                    installer,
                    attempt_token="e" * 64,
                    single_instance=instance,
                    verified_handle=wrong_handle,
                    _coordination_factory=lambda token, gate: _FakeCoordination(ready=True),
                    _popen_factory=lambda *args, **kwargs: _FakeProcess(),
                )
            except UpdateHandoffError:
                pass
            else:
                raise AssertionError("launch accepted a handle for another file")
        _check("installer path must identify the still-open verified file", True)
    finally:
        instance.release()


def test_windows_exact_parent_claim(tmp: Path) -> None:
    del tmp
    if sys.platform != "win32":
        _check("Windows exact-parent coordination is skipped off Windows", True)
        return

    token = "f" * 64
    gate_name = f"{UPDATE_GATE_MUTEX_NAME}.Test.Claim.{os.getpid()}"
    coordination = UpdateHandoffCoordination(token, gate_name=gate_name)
    setup_gate = None
    parent = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    claim = None
    try:
        _check("app readiness event does not pre-create SetupMutex", not is_update_gate_active(gate_name))
        setup_gate = _create_windows_mutex(gate_name)
        claim = claim_parent_process(parent.pid, token, gate_name=gate_name)
        _check("installer-side claim signals readiness", coordination.wait_until_ready(parent, 1.0))
        coordination.close()
        _close_windows_handle(setup_gate)
        setup_gate = None
        _check("installer-side claim retains the update gate", is_update_gate_active(gate_name))
        _check("exact live parent handle is initially unsignalled", not claim.wait_for_parent_exit(0))
        parent.terminate()
        parent.wait(timeout=5)
        _check("exact parent handle signals after that process exits", claim.wait_for_parent_exit(1.0))
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        coordination.close()
        if setup_gate is not None:
            _close_windows_handle(setup_gate)
        if claim is not None:
            claim.close()
    _check("gate closes after both app and installer claims release it", not is_update_gate_active(gate_name))


def test_attempt_token_names() -> None:
    token = "1a2b" * 16
    name = readiness_event_name(token)
    _check(
        "readiness event exactly matches the Inno contract",
        name == f"Local\\Momento.GameRecorder.UpdateReady.{token}",
    )
    try:
        readiness_event_name("short")
    except ValueError:
        pass
    else:
        raise AssertionError("short attempt token was accepted")
    _check("weak attempt tokens are rejected", True)

    try:
        readiness_event_name("A" * 64)
    except ValueError:
        pass
    else:
        raise AssertionError("uppercase attempt token was accepted")
    _check("attempt token contract requires lowercase hex", True)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="momento-handoff-") as folder:
        tmp = Path(folder)
        test_launch_contract_and_verified_handle_lifetime(tmp)
        test_launch_failures_keep_old_instance_and_release_gate(tmp)
        test_launch_rejects_unlocked_inputs(tmp)
        test_windows_exact_parent_claim(tmp)
    test_attempt_token_names()
    print("PASS: updater handoff smoke checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
