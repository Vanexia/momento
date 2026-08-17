"""Executable contract for Momento's minimized PyAV runtime."""

from __future__ import annotations

import argparse
import json
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
import scripts.verify_pyav_runtime as verifier  # noqa: E402


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


def _unit_contract_checks(
    contract_path: Path,
    *,
    validate_build_inputs: bool = True,
) -> None:
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

        mismatched_wheel = Path(temp) / "mismatched.whl"
        mismatched_wheel.write_bytes(b"this is deliberately not a ZIP archive")
        mismatched_contract = Path(temp) / "mismatched-contract.json"
        mismatched_payload = json.loads(contract_path.read_text(encoding="utf-8"))
        mismatched_payload["artifact"]["sha256"] = "0" * 64
        mismatched_contract.write_text(
            json.dumps(mismatched_payload),
            encoding="utf-8",
        )

        functional_called = False
        real_functional_checks = verifier._functional_checks

        def forbidden_functional_checks(*_args, **_kwargs):
            nonlocal functional_called
            functional_called = True
            raise AssertionError("a hash-mismatched wheel reached functional imports")

        verifier._functional_checks = forbidden_functional_checks
        try:
            mismatch_report = verifier.verify_runtime(
                mismatched_wheel,
                contract_path=mismatched_contract,
            )
        finally:
            verifier._functional_checks = real_functional_checks

        mismatch_failures = mismatch_report.failures()
        check(
            "hash-mismatched wheel stops before archive or runtime inspection",
            len(mismatch_failures) == 1
            and mismatch_failures[0].group == "artifact"
            and mismatch_failures[0].code == "wheel-hash"
            and not functional_called,
        )

    if validate_build_inputs:
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
    parser.add_argument(
        "--unit-only",
        action="store_true",
        help="run verifier unit contracts without importing a PyAV runtime",
    )
    args = parser.parse_args()

    contract_path = ROOT / "build" / "pyav_runtime.json"
    _unit_contract_checks(
        contract_path,
        validate_build_inputs=not args.unit_only,
    )
    if args.unit_only:
        print("PASS: PyAV verifier unit contracts")
        return 0
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
