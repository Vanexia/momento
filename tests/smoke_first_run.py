"""Fresh-user defaults and startup policy checks."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento import __main__ as app_main  # noqa: E402
from momento.config import Config  # noqa: E402
from momento.core import audio_devices  # noqa: E402
from momento.ui import welcome as welcome_mod  # noqa: E402


checks = 0
failures = 0


def check(label: str, condition: bool) -> None:
    global checks, failures
    checks += 1
    if condition:
        print(f"PASS - {label}")
    else:
        failures += 1
        print(f"FAIL - {label}")


def main() -> int:
    cfg = Config()
    check("fresh default: capture is fixed at 60 fps", cfg.framerate == 60 and not cfg.framerate_auto)
    check(
        "fresh default: quality is a predictable 16 Mbit/s",
        cfg.quality_preset == "custom" and cfg.custom_bitrate_kbps == 16_000,
    )
    check("fresh default: setup is incomplete", not cfg.setup_complete)
    check(
        "legacy config: existing users stay onboarded",
        Config.from_dict({}).setup_complete,
    )
    check(
        "setup marker: an unfinished setup round-trips",
        not Config.from_dict(cfg.to_dict()).setup_complete,
    )
    check("fresh default: autostart requires opt-in", not cfg.autostart_with_windows)
    check("fresh default: arbitrary fullscreen recording is off", not cfg.record_any_fullscreen)

    policy = getattr(app_main, "_monitoring_allowed_on_launch", None)
    check("startup policy helper exists", callable(policy))
    if callable(policy):
        check(
            "returning user: saved monitoring preference starts the watcher",
            policy(cfg, is_first_run=False, setup_accepted=False),
        )
        check(
            "first run: monitoring stays paused before setup",
            not policy(cfg, is_first_run=True, setup_accepted=False),
        )
        check(
            "first run: accepted setup enables opted-in monitoring",
            policy(cfg, is_first_run=True, setup_accepted=True),
        )
        paused = Config(start_monitoring_on_launch=False)
        check(
            "first run: accepted setup respects an opted-out watcher",
            not policy(paused, is_first_run=True, setup_accepted=True),
        )

    persist = getattr(welcome_mod, "_persist_setup_config", None)
    check("setup persistence helper exists", callable(persist))
    if callable(persist):
        calls: list[tuple[str, object]] = []
        real_save = welcome_mod.save_config
        real_autostart = welcome_mod.set_autostart
        welcome_mod.save_config = lambda value: calls.append(("save", value))
        welcome_mod.set_autostart = lambda enabled: calls.append(("autostart", enabled))
        try:
            opted_in = Config(autostart_with_windows=True)
            persist(opted_in)
        finally:
            welcome_mod.save_config = real_save
            welcome_mod.set_autostart = real_autostart
        check(
            "setup finish saves before applying autostart",
            calls == [("save", opted_in), ("autostart", True)],
        )

    real_list = audio_devices.list_input_device_names
    real_probe = audio_devices.probe_open
    probed: list[str] = []
    audio_devices.list_input_device_names = lambda _p: [
        ("Secondary Microphone", False),
        ("Windows Default Microphone", True),
    ]
    audio_devices.probe_open = lambda _p, name, *, loopback: (
        probed.append(name) or True
    )
    try:
        picked = app_main._first_openable_mic_name(object())
    finally:
        audio_devices.list_input_device_names = real_list
        audio_devices.probe_open = real_probe
    check(
        "first-run audio: Windows default microphone is tried first",
        picked == "Windows Default Microphone"
        and probed == ["Windows Default Microphone"],
    )

    print(f"\n{checks - failures}/{checks} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
