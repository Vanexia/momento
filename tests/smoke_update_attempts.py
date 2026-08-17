"""Durable update-attempt state regression checks."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import momento.updater.attempts as attempts_module  # noqa: E402
from momento.updater.attempts import (  # noqa: E402
    ATTEMPT_TIMEOUT,
    BASE_RETRY_DELAY,
    MAX_ATTEMPTS,
    AttemptStateError,
    UpdateAttemptStore,
)


_results: list[tuple[str, bool]] = []


def check(label: str, condition: bool) -> None:
    _results.append((label, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'} - {label}")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def _begin(store: UpdateAttemptStore, *, target: str = "0.2.3", digest: str = "a" * 64):
    return store.begin_attempt(
        source_version="0.2.2",
        target_version=target,
        installer_sha256=digest,
    )


def _expect_state_error(label: str, operation) -> None:
    try:
        operation()
    except AttemptStateError:
        check(label, True)
    except Exception as exc:
        print(f"  unexpected {type(exc).__name__}: {exc}")
        check(label, False)
    else:
        check(label, False)


def test_atomic_per_user_state() -> None:
    old_local = os.environ.get("LOCALAPPDATA")
    try:
        with tempfile.TemporaryDirectory(prefix="momento_attempt_user_") as d:
            root = Path(d).resolve()
            os.environ["LOCALAPPDATA"] = str(root)
            clock = Clock()
            store = UpdateAttemptStore(now_provider=clock)
            attempt = _begin(store)
            path = root / "Momento" / "updates" / "attempt-state.json"
            payload = json.loads(path.read_text(encoding="utf-8"))

            check("attempt state is stored below per-user local app data", path.is_file())
            check("a first attempt is created", attempt is not None)
            check(
                "attempt tokens contain 256 bits of cryptographic material",
                attempt is not None
                and re.fullmatch(r"[0-9a-f]{64}", attempt.attempt_token) is not None,
            )
            check("state contains only the strict durable fields", set(payload) == {
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
            check(
                "state JSON is canonical and newline-free",
                path.read_bytes()
                == json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            )
            check("atomic writes leave no temporary files", not list(path.parent.glob(".attempt-state.json.*.tmp")))
    finally:
        if old_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = old_local


def test_backoff_quarantine_and_supersession() -> None:
    with tempfile.TemporaryDirectory(prefix="momento_attempt_retry_") as d:
        clock = Clock()
        store = UpdateAttemptStore(Path(d), now_provider=clock)
        first = _begin(store)
        assert first is not None

        check("a live attempt suppresses duplicate launches", _begin(store) is None)
        check("forged setup tokens are rejected", not store.record_setup_exit("f" * 64, 5))
        check("setup failure exit codes are recorded", store.record_setup_exit(first.attempt_token, 5))
        failed_first = store.load()
        check(
            "first failure uses the bounded base backoff",
            failed_first is not None
            and failed_first.state == "retry_wait"
            and failed_first.setup_exit_code == 5
            and failed_first.next_retry_at == clock.now + BASE_RETRY_DELAY,
        )
        check("the same failed target is suppressed before retry", _begin(store) is None)

        clock.advance(BASE_RETRY_DELAY)
        second = _begin(store)
        check(
            "retry creates a fresh token and increments the durable count",
            second is not None
            and second.attempt_count == 2
            and second.attempt_token != first.attempt_token,
        )
        assert second is not None
        store.record_setup_exit(second.attempt_token, 1603)
        failed_second = store.load()
        check(
            "second failure doubles the delay",
            failed_second is not None
            and failed_second.next_retry_at == clock.now + (BASE_RETRY_DELAY * 2),
        )

        clock.advance(BASE_RETRY_DELAY * 2)
        third = _begin(store)
        check("at most three attempts can launch", third is not None and third.attempt_count == MAX_ATTEMPTS)
        assert third is not None
        store.record_setup_exit(third.attempt_token, 1603)
        quarantined = store.load()
        check(
            "the third failure quarantines the target without another retry",
            quarantined is not None
            and quarantined.state == "quarantined"
            and quarantined.next_retry_at is None,
        )
        clock.advance(timedelta(days=365))
        check("a quarantined target remains suppressed", _begin(store) is None)

        newer = _begin(store, target="0.2.4", digest="b" * 64)
        check(
            "a newer signed target supersedes an older quarantined failure",
            newer is not None
            and newer.target_version == "0.2.4"
            and newer.installer_sha256 == "b" * 64
            and newer.attempt_count == 1,
        )


def test_confirmation_and_stale_tokens() -> None:
    with tempfile.TemporaryDirectory(prefix="momento_attempt_confirm_") as d:
        clock = Clock()
        store = UpdateAttemptStore(Path(d), now_provider=clock)
        first = _begin(store)
        assert first is not None

        check("a wrong running version cannot confirm an attempt", not store.confirm_startup(first.attempt_token, "0.2.4"))
        check("a forged token cannot confirm an attempt", not store.confirm_startup("0" * 64, "0.2.3"))
        check("successful Setup exit is recorded", store.record_setup_exit(first.attempt_token, 0))
        awaiting = store.load()
        check(
            "Setup success waits for token-bound application confirmation",
            awaiting is not None
            and awaiting.state == "awaiting_confirmation"
            and awaiting.setup_exit_code == 0,
        )
        check("the exact token and target version confirm startup", store.confirm_startup(first.attempt_token, "0.2.3"))
        confirmed = store.load()
        check("confirmed state is durable", confirmed is not None and confirmed.state == "confirmed")
        check("a confirmed token cannot be replayed", not store.confirm_startup(first.attempt_token, "0.2.3"))

    with tempfile.TemporaryDirectory(prefix="momento_attempt_stale_") as d:
        clock = Clock()
        store = UpdateAttemptStore(Path(d), now_provider=clock)
        stale = _begin(store)
        assert stale is not None
        clock.advance(ATTEMPT_TIMEOUT)
        retry = _begin(store)
        check(
            "an unconfirmed timed-out attempt becomes a bounded retry",
            retry is not None
            and retry.attempt_count == 2
            and retry.attempt_token != stale.attempt_token,
        )
        check("a stale token cannot confirm a newer retry", not store.confirm_startup(stale.attempt_token, "0.2.3"))


def test_strict_validation_and_corruption_containment() -> None:
    with tempfile.TemporaryDirectory(prefix="momento_attempt_corrupt_") as d:
        root = Path(d)
        path = root / "attempt-state.json"
        root.mkdir(parents=True, exist_ok=True)
        bad_payloads = (
            b"{not-json",
            b"{}",
            json.dumps({
                "attempt_count": True,
                "attempt_token": "a" * 64,
                "installer_sha256": "b" * 64,
                "next_retry_at": None,
                "setup_exit_code": None,
                "source_version": "0.2.2",
                "started_at": "2026-08-17T12:00:00Z",
                "state": "launching",
                "target_version": "0.2.3",
            }, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        for index, payload in enumerate(bad_payloads, start=1):
            path.write_bytes(payload)
            store = UpdateAttemptStore(root, now_provider=Clock())
            _expect_state_error(f"corrupt state variant {index} fails closed", store.load)
            check(f"corrupt state variant {index} is not silently discarded", path.read_bytes() == payload)

        token = "c" * 64
        path.write_text(
            '{"attempt_count":1,"attempt_token":"' + token
            + '","attempt_token":"' + ("d" * 64)
            + '","installer_sha256":"' + ("e" * 64)
            + '","next_retry_at":"2026-08-17T12:15:00Z","setup_exit_code":null,'
            + '"source_version":"0.2.2","started_at":"2026-08-17T12:00:00Z",'
            + '"state":"launching","target_version":"0.2.3"}',
            encoding="utf-8",
        )
        _expect_state_error("duplicate JSON keys fail closed", store.load)


def test_atomic_failure_preserves_previous_state() -> None:
    with tempfile.TemporaryDirectory(prefix="momento_attempt_atomic_") as d:
        clock = Clock()
        store = UpdateAttemptStore(Path(d), now_provider=clock)
        attempt = _begin(store)
        assert attempt is not None
        before = (Path(d) / "attempt-state.json").read_bytes()
        original_replace = attempts_module.os.replace

        def fail_replace(source, destination) -> None:
            raise OSError("simulated atomic publish failure")

        attempts_module.os.replace = fail_replace
        try:
            _expect_state_error(
                "atomic publication errors are contained",
                lambda: store.record_setup_exit(attempt.attempt_token, 5),
            )
        finally:
            attempts_module.os.replace = original_replace

        check("a failed atomic publication preserves the previous state", (Path(d) / "attempt-state.json").read_bytes() == before)
        check("failed atomic publication cleans its temporary file", not list(Path(d).glob(".attempt-state.json.*.tmp")))


def test_invalid_inputs_and_safe_diagnostics() -> None:
    with tempfile.TemporaryDirectory(prefix="momento_attempt_input_") as d:
        store = UpdateAttemptStore(Path(d), now_provider=Clock())
        _expect_state_error("unstable source versions are rejected", lambda: store.begin_attempt(
            source_version="0.2.2rc1", target_version="0.2.3", installer_sha256="a" * 64
        ))
        _expect_state_error("non-newer targets are rejected", lambda: store.begin_attempt(
            source_version="0.2.2", target_version="0.2.2", installer_sha256="a" * 64
        ))
        _expect_state_error("malformed installer hashes are rejected", lambda: store.begin_attempt(
            source_version="0.2.2", target_version="0.2.3", installer_sha256="secret-path-X:/Temp/name"
        ))

        sensitive_token = "9" * 64
        sensitive_path = str(Path(d).resolve())
        messages: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                messages.append(record.getMessage())

        handler = Capture()
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        try:
            check("unknown setup tokens are rejected without raising", not store.record_setup_exit(sensitive_token, 5))
        finally:
            root_logger.removeHandler(handler)
        rendered = "\n".join(messages)
        check("attempt diagnostics never expose tokens", sensitive_token not in rendered)
        check("attempt diagnostics never expose cache paths", sensitive_path not in rendered)


def main() -> int:
    test_atomic_per_user_state()
    test_backoff_quarantine_and_supersession()
    test_confirmation_and_stale_tokens()
    test_strict_validation_and_corruption_containment()
    test_atomic_failure_preserves_previous_state()
    test_invalid_inputs_and_safe_diagnostics()

    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
