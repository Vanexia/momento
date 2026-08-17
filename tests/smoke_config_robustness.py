"""Regression checks for type-invalid but valid-JSON configuration values."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.config import Config, load_config, save_config  # noqa: E402


_results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    _results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} - {name}")


def test_invalid_fields_degrade_independently(tmp: Path) -> None:
    output = (tmp / "recordings").resolve()
    cfg = Config.from_dict(
        {
            "mic_device": ["not", "a", "device"],
            "system_audio_device": {"name": "not a device"},
            "mic_volume_pct": "oops",
            "system_volume_pct": "125",
            "output_folder": str(output),
            "autostart_with_windows": "false",
            "framerate": "not-a-number",
            "framerate_auto": "false",
            "bookmark_hotkey": ["F8"],
            "record_any_fullscreen": 1,
            "show_failure_toast": {"enabled": True},
            "known_games": ["Wow.exe", 42, "", "wow.exe", {"exe": "bad"}],
            "disabled_games": ["Wow.exe", None, "wow.exe"],
            "youtube_default_tags": {"tag": "bad"},
            "youtube_channel_name": ["bad"],
            "youtube_default_category": "bad",
        }
    )

    check("invalid mic device falls back to a string default", cfg.mic_device == "")
    check("invalid system device falls back to a string default", cfg.system_audio_device == "")
    check("invalid volume falls back without losing a valid neighbour", cfg.mic_volume_pct == 100 and cfg.system_volume_pct == 125)
    check("valid absolute output folder survives", cfg.output_folder == output)
    check("string booleans are not treated as booleans", cfg.autostart_with_windows is False and cfg.framerate_auto is False)
    check("invalid framerate falls back to an int", cfg.framerate == 60 and type(cfg.framerate) is int)
    check("invalid hotkey falls back to a string", cfg.bookmark_hotkey == "F8")
    check("integer boolean falls back to a bool", cfg.record_any_fullscreen is False and type(cfg.record_any_fullscreen) is bool)
    check("nested boolean falls back to a bool", cfg.show_failure_toast is True and type(cfg.show_failure_toast) is bool)
    check("game lists keep only non-empty strings and dedupe", cfg.known_games == ["Wow.exe"] and cfg.disabled_games == ["Wow.exe"])
    check("invalid YouTube strings do not get stringified", cfg.youtube_default_tags == "" and cfg.youtube_channel_name == "")
    check("invalid YouTube category falls back", cfg.youtube_default_category == 20)


def test_bad_output_path_does_not_discard_other_fields(tmp: Path) -> None:
    path = tmp / "config.json"
    path.write_text(
        json.dumps(
            {
                "output_folder": 123,
                "mic_volume_pct": 137,
                "system_volume_pct": 88,
                "bookmark_sound": False,
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(path)
    check("invalid output path falls back to the default Path", isinstance(cfg.output_folder, Path))
    check("valid values survive an invalid output path", cfg.mic_volume_pct == 137 and cfg.system_volume_pct == 88 and cfg.bookmark_sound is False)
    check("per-field recovery does not create a broken-config backup", not list(tmp.glob("config.json.broken-*")))


def test_round_trip(tmp: Path) -> None:
    path = tmp / "roundtrip.json"
    original = Config(
        output_folder=(tmp / "library").resolve(),
        mic_device="Mic Name",
        framerate=144,
        record_any_fullscreen=True,
    )
    save_config(original, path)
    loaded = load_config(path)
    check("validated config round-trips", loaded == original)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="momento_config_robustness_") as d:
        tmp = Path(d)
        test_invalid_fields_degrade_independently(tmp)
        test_bad_output_path_does_not_discard_other_fields(tmp)
        test_round_trip(tmp)

    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
