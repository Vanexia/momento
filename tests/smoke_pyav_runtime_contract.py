"""Executable contract for Momento's minimized PyAV runtime."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.verify_pyav_runtime import (  # noqa: E402
    RuntimeReport,
    canonical_dll_name,
    load_contract,
    verify_runtime,
)


EXPECTED_STOCK_FORBIDDEN = {
    "libdav1d",
    "libmp3lame",
    "libopencore-amrnb",
    "libopencore-amrwb",
    "libopenh264",
    "libopus",
    "libsvtav1enc",
    "libvorbis",
    "libvorbisenc",
    "libvpx",
    "libwebp",
    "libwebpmux",
    "libx265",
}


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"PASS: {label}")


def _unit_contract_checks(contract_path: Path) -> None:
    contract = load_contract(contract_path)
    check("contract schema is version 1", contract["schema_version"] == 1)
    check("PyAV is pinned to 17.0.1", contract["runtime"]["pyav_version"] == "17.0.1")
    check("FFmpeg is pinned to 8.0.1", contract["runtime"]["ffmpeg_version"] == "8.0.1")
    check(
        "application audio graph filters are retained",
        {"abuffer", "abuffersink", "aformat", "amix", "aresample", "volume"}
        <= set(contract["capabilities"]["filters"])
        and any(
            option.startswith("--enable-filter=")
            and "volume" in option.removeprefix("--enable-filter=").split(",")
            for option in contract["ffmpeg_configure"]
        ),
    )

    check(
        "delvewheel hashes are removed from FFmpeg DLL names",
        canonical_dll_name("avcodec-62-0123456789abcdef.dll") == "avcodec-62.dll",
    )
    check(
        "ordinary version suffixes remain part of DLL names",
        canonical_dll_name("libx264-165.dll") == "libx264-165.dll",
    )

    with tempfile.TemporaryDirectory(prefix="momento-pyav-contract-") as temp:
        malformed = Path(temp) / "malformed.json"
        malformed.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")
        try:
            load_contract(malformed)
        except ValueError as exc:
            check("duplicate manifest keys are rejected", "duplicate" in str(exc).lower())
        else:
            raise AssertionError("duplicate manifest keys are rejected")

    validation = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "build_pyav_runtime.ps1"),
            "-ValidateOnly",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
        check=False,
    )
    check(
        "build tooling validates every pinned local input",
        validation.returncode == 0 and "PASS: PyAV build inputs" in validation.stdout,
    )


def _stock_failure_is_exact(report: RuntimeReport) -> None:
    functional_failures = report.failures(group="functional")
    check(
        "stock runtime passes every Momento functional capability",
        not functional_failures,
    )

    native_failures = report.failures(group="native")
    forbidden = {
        item.subject.lower()
        for item in native_failures
        if item.code == "forbidden-dll"
    }
    check(
        "stock runtime exposes every expected forbidden codec family",
        EXPECTED_STOCK_FORBIDDEN <= forbidden,
    )
    unexpected = [
        item
        for item in report.failures()
        if not (
            item.group == "native"
            and item.code in {"forbidden-dll", "required-license"}
        )
    ]
    check("stock runtime has no unexpected native-contract failures", not unexpected)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "runtime",
        nargs="?",
        type=Path,
        default=ROOT / ".venv" / "Lib" / "site-packages",
        help="PyAV wheel or site-packages directory to verify",
    )
    parser.add_argument(
        "--expect-stock-failure",
        action="store_true",
        help="prove the stock runtime is functional but violates the DLL contract",
    )
    args = parser.parse_args()

    contract_path = ROOT / "build" / "pyav_runtime.json"
    _unit_contract_checks(contract_path)
    report = verify_runtime(args.runtime, contract_path=contract_path)
    report.print()

    if args.expect_stock_failure:
        _stock_failure_is_exact(report)
        print("PASS: stock PyAV baseline fails the minimized native-runtime contract")
        return 0

    if report.failures():
        print(f"FAIL: PyAV runtime contract has {len(report.failures())} failure(s)")
        return 1

    print("PASS: PyAV runtime satisfies Momento's minimized runtime contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
