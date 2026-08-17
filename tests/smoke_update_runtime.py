"""Installed updater startup confirmation and launch glue checks."""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.updater.handoff import UpdateHandoffError  # noqa: E402
from momento.updater.runtime import UpdateRuntime, updated_attempt_token  # noqa: E402


_results: list[tuple[str, bool]] = []


def check(label: str, condition: bool) -> None:
    _results.append((label, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'} - {label}")


class FakeCache:
    def __init__(self) -> None:
        self.lock_calls: list[tuple[object, str]] = []
        self.discarded: list[str] = []
        self.handle = object()

    @contextlib.contextmanager
    def lock_for_launch(self, staged, *, current_version: str):
        self.lock_calls.append((staged, current_version))
        yield SimpleNamespace(
            installer_path=Path("MomentoSetup-0.2.3.exe"),
            manifest=staged.manifest,
            file=self.handle,
        )

    def discard_version(self, version: str) -> None:
        self.discarded.append(version)


class FakeAttempts:
    def __init__(self) -> None:
        self.confirm_result = True
        self.begin_result = SimpleNamespace(attempt_token="a" * 64)
        self.confirm_calls: list[tuple[str, str]] = []
        self.begin_calls: list[dict[str, str]] = []
        self.exit_calls: list[tuple[str, int]] = []

    def confirm_startup(self, token: str, version: str) -> bool:
        self.confirm_calls.append((token, version))
        return self.confirm_result

    def begin_attempt(self, **kwargs):
        self.begin_calls.append(kwargs)
        return self.begin_result

    def record_setup_exit(self, token: str, exit_code: int) -> bool:
        self.exit_calls.append((token, exit_code))
        return True


def _staged():
    return SimpleNamespace(
        manifest=SimpleNamespace(
            version="0.2.3",
            installer=SimpleNamespace(sha256="b" * 64),
        )
    )


def test_updated_argument_is_strict() -> None:
    token = "1" * 64
    check("one exact updated token is parsed", updated_attempt_token([f"--updated={token}"]) == token)
    check("ordinary launch has no update token", updated_attempt_token(["--show"]) is None)
    check("uppercase update tokens are rejected", updated_attempt_token([f"--updated={'A' * 64}"]) is None)
    check(
        "duplicate update tokens are rejected",
        updated_attempt_token([f"--updated={token}", f"--updated={token}"]) is None,
    )


def test_startup_confirmation_cleans_only_authenticated_payload() -> None:
    cache = FakeCache()
    attempts = FakeAttempts()
    runtime = UpdateRuntime(
        current_version="0.2.3",
        single_instance=object(),
        cache=cache,
        attempts=attempts,
        launch_handoff=lambda **_kwargs: None,
    )
    token = "2" * 64
    check("exact startup token confirms the installed target", runtime.confirm_startup(token))
    check("confirmation uses the running version", attempts.confirm_calls == [(token, "0.2.3")])
    check("confirmed payload is removed from cache", cache.discarded == ["0.2.3"])

    attempts.confirm_result = False
    check("forged or stale startup token is rejected", not runtime.confirm_startup("3" * 64))
    check("rejected token does not remove another payload", cache.discarded == ["0.2.3"])


def test_startup_confirmation_waits_for_event_loop_stability() -> None:
    cache = FakeCache()
    attempts = FakeAttempts()
    runtime = UpdateRuntime(
        current_version="0.2.3",
        single_instance=object(),
        cache=cache,
        attempts=attempts,
        launch_handoff=lambda **_kwargs: None,
    )
    scheduled: list[tuple[int, object]] = []
    completed: list[bool] = []
    token = "4" * 64
    accepted = runtime.schedule_startup_confirmation(
        token,
        schedule=lambda delay, callback: scheduled.append((delay, callback)),
        on_complete=completed.append,
    )
    check("valid update startup schedules one delayed confirmation", accepted and len(scheduled) == 1)
    check("startup is not confirmed before the stability callback", not attempts.confirm_calls)
    check("staged payload remains before startup proves stable", not cache.discarded)
    check("confirmation waits at least five seconds", scheduled[0][0] >= 5_000)

    callback = scheduled[0][1]
    assert callable(callback)
    callback()
    check("stable event loop confirms the exact attempt", attempts.confirm_calls == [(token, "0.2.3")])
    check("stable confirmation then cleans the payload", cache.discarded == ["0.2.3"])
    check("automatic follow-up can start after confirmation", completed == [True])

    rejected_schedules: list[object] = []
    check(
        "invalid startup markers cannot schedule confirmation",
        not runtime.schedule_startup_confirmation(
            "A" * 64,
            schedule=lambda _delay, callback: rejected_schedules.append(callback),
        )
        and not rejected_schedules,
    )


def test_verified_launch_and_failure_accounting() -> None:
    staged = _staged()
    cache = FakeCache()
    attempts = FakeAttempts()
    instance = object()
    handoff_calls: list[dict[str, object]] = []

    runtime = UpdateRuntime(
        current_version="0.2.2",
        single_instance=instance,
        cache=cache,
        attempts=attempts,
        launch_handoff=lambda **kwargs: handoff_calls.append(kwargs),
    )
    check("verified staged update launches", runtime.launch(staged))
    check("installer stays locked through handoff", handoff_calls[0]["verified_handle"] is cache.handle)
    check("handoff receives the owning single instance", handoff_calls[0]["single_instance"] is instance)
    check(
        "attempt identity is bound to signed release fields",
        attempts.begin_calls == [{
            "source_version": "0.2.2",
            "target_version": "0.2.3",
            "installer_sha256": "b" * 64,
        }],
    )

    failing_attempts = FakeAttempts()

    def fail_handoff(**_kwargs):
        raise UpdateHandoffError("simulated")

    failed = UpdateRuntime(
        current_version="0.2.2",
        single_instance=instance,
        cache=FakeCache(),
        attempts=failing_attempts,
        launch_handoff=fail_handoff,
    )
    check("failed handoff is contained", not failed.launch(staged))
    check(
        "failed process launch enters bounded retry state",
        failing_attempts.exit_calls == [("a" * 64, -1)],
    )

    suppressed_attempts = FakeAttempts()
    suppressed_attempts.begin_result = None
    suppressed_calls: list[bool] = []
    suppressed = UpdateRuntime(
        current_version="0.2.2",
        single_instance=instance,
        cache=FakeCache(),
        attempts=suppressed_attempts,
        launch_handoff=lambda **_kwargs: suppressed_calls.append(True),
    )
    check("backoff suppresses duplicate installer launch", not suppressed.launch(staged))
    check("suppressed attempt never reaches process creation", not suppressed_calls)


def main() -> int:
    test_updated_argument_is_strict()
    test_startup_confirmation_cleans_only_authenticated_payload()
    test_startup_confirmation_waits_for_event_loop_stability()
    test_verified_launch_and_failure_accounting()
    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
