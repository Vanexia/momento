"""Static contract for the versioned per-user Windows installer."""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento import __version__  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "0.2.2"

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
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version_info = (ROOT / "resources" / "version_info.txt").read_text(encoding="utf-8")
    installer_path = ROOT / "build" / "installer.iss"
    builder_path = ROOT / "scripts" / "build_installer.ps1"
    installer = installer_path.read_text(encoding="utf-8") if installer_path.is_file() else ""
    builder = builder_path.read_text(encoding="utf-8") if builder_path.is_file() else ""
    spec = (ROOT / "build" / "pyinstaller.spec").read_text(encoding="utf-8")
    build_info = (ROOT / "BUILD_INFO.txt").read_text(encoding="utf-8")
    notices = (ROOT / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
    attributes = (ROOT / ".gitattributes").read_text(encoding="ascii")
    check("version: Python package matches public release", __version__ == EXPECTED_VERSION)
    check("version: pyproject matches public release", pyproject["project"]["version"] == EXPECTED_VERSION)
    check("version: Windows file metadata matches public release", f"'ProductVersion', '{EXPECTED_VERSION}'" in version_info)
    check("installer: Inno source exists", installer_path.is_file())
    check("installer: deterministic build wrapper exists", builder_path.is_file())
    check("installer: version matches application", f'#define MyAppVersion "{EXPECTED_VERSION}"' in installer)
    check("installer: per-user privileges", "PrivilegesRequired=lowest" in installer)
    check("installer: installs below LocalAppData", r"{localappdata}\Programs\Momento" in installer)
    check("installer: targets supported 64-bit Windows", "ArchitecturesAllowed=x64compatible" in installer)
    check(
        "installer: checks the running-app mutex for normal installs",
        "AppMutexName = 'Momento.GameRecorder.Instance'" in installer
        and "CheckForMutexes(AppMutexName)" in installer,
    )
    check(
        "installer: owns a separate update gate",
        "SetupMutex=Momento.GameRecorder.Update" in installer,
    )
    check("installer: removes stale autostart on uninstall", "RegDeleteValue(HKCU" in installer)
    check("installer: preserves user data by default", "PURGEUSERDATA" in installer)
    check(
        "installer: purge cannot recursively delete the whole AppData root",
        "DelTree(ExpandConstant('{userappdata}\\Momento')," not in installer,
    )
    check(
        "installer: never deletes the live runtime before replacement succeeds",
        "[InstallDelete]" not in installer,
    )
    check(
        "installer: successful upgrade removes known stale runtime only afterwards",
        "procedure RemoveObsoleteRuntime" in installer
        and "CurStep = ssPostInstall" in installer,
    )
    current_pyav_dlls = (
        "avcodec-62-4d28b54037f2761423840318c68e5a32.dll",
        "avdevice-62-2802c4446b384f78b9e92b17563c14d5.dll",
        "avfilter-11-d64fa8e58ac927e2679aa711773901ba.dll",
        "avformat-62-282ffcbf655477408ab5c1c3f4adf54e.dll",
        "avutil-60-833ee04a13e9310cb90177b2d206b51c.dll",
        "libgcc_s_seh-1-fb5a9b1bb254026169325ae2b3cad1cc.dll",
        "libstdc++-6-7c98bad87f582095f4ac9a5958b22abc.dll",
        "libvpl-6b8c4104601b4dc1ce504e4781e02378.dll",
        "libwinpthread-1-ed858c25be05f072fe6dc08bd9b9fc79.dll",
        "libx264-165-ba2f6715c25b2cff1d57af10039bb25e.dll",
        "swresample-6-2d4ebf3ac3b90ec6232a79211c05d5ef.dll",
        "swscale-9-500b7eada7986e0a5e17199f8cad8cb7.dll",
    )
    check(
        "installer: post-copy cleanup keeps only the exact minimized PyAV DLLs",
        "FindFirst(PyAvLibraries + '*.dll'" in installer
        and "DeleteFile(PyAvLibraries + FindRec.Name)" in installer
        and all(name in installer for name in current_pyav_dlls),
    )
    check(
        "installer: update mode requires parent and attempt identity",
        all(
            term in installer
            for term in ("/MOMENTOUPDATE", "/PARENTPID=", "/ATTEMPTTOKEN=")
        ),
    )
    check(
        "installer: updater opens and waits for the exact parent process",
        all(
            term in installer
            for term in ("OpenProcess", "ParentProcessHandle", "WaitForSingleObject")
        ),
    )
    prepare_to_install = re.search(
        r"function PrepareToInstall\b.*?\nend;",
        installer,
        re.DOTALL,
    )
    prepare_body = prepare_to_install.group(0) if prepare_to_install else ""
    check(
        "installer: exact-parent wait has a fixed upper bound",
        "UpdateParentWaitTimeoutMs = 30000;" in installer
        and bool(
            re.search(
                r"WaitForSingleObject\(\s*ParentProcessHandle,\s*"
                r"UpdateParentWaitTimeoutMs\s*\)",
                prepare_body,
            )
        )
        and "repeat" not in prepare_body.casefold(),
    )
    check(
        "installer: only a signalled exact-parent handle permits installation",
        "UpdateParentExited := True" in prepare_body
        and "WaitResult = WaitTimeout" in prepare_body
        and "WaitResult = WaitFailed" in prepare_body,
    )
    check(
        "installer: updater signals readiness only after opening the parent",
        "SignalUpdateReady" in installer
        and installer.find("OpenUpdateParent") >= 0
        and installer.find("OpenUpdateParent") < installer.find("SignalUpdateReady"),
    )
    check(
        "installer: successful update relaunch carries the exact attempt token",
        "--updated={code:GetAttemptToken}" in installer,
    )
    recovery = re.search(
        r"procedure RelaunchAfterFailedUpdate\b.*?\nend;",
        installer,
        re.DOTALL,
    )
    recovery_body = recovery.group(0) if recovery else ""
    deinitialize = re.search(
        r"procedure DeinitializeSetup\b.*?\nend;",
        installer,
        re.DOTALL,
    )
    deinitialize_body = deinitialize.group(0) if deinitialize else ""
    check(
        "installer: failed update recovery rechecks that the exact parent exited",
        "WaitForSingleObject(ParentProcessHandle, 0) <> WaitObject0" in recovery_body
        and recovery_body.find("WaitForSingleObject") < recovery_body.find("Exec("),
    )
    check(
        "installer: failed update recovery carries the token and is best effort",
        "'--updated=' + UpdateAttemptToken" in recovery_body
        and "ewNoWait" in recovery_body
        and "Log(" in recovery_body,
    )
    check(
        "installer: recovery runs only after an unsuccessful update teardown",
        "UpdateMode and UpdateParentExited and (not UpdateInstallSucceeded)"
        in deinitialize_body
        and "RelaunchAfterFailedUpdate" in deinitialize_body
        and "CurStep = ssDone" in installer
        and "UpdateInstallSucceeded := True" in installer,
    )
    check(
        "installer: purge removes corrupt-config backups",
        r"{userappdata}\Momento\config.json.broken-*.txt" in installer,
    )
    for filename in (
        "config.json.tmp",
        "youtube_token.dat.tmp",
        "youtube_avatar.png.tmp",
    ):
        check(
            f"installer: purge removes {filename}",
            rf"{{userappdata}}\Momento\{filename}" in installer,
        )
    check("installer: carries the complete one-folder release", r"dist\Momento\*" in installer)
    check("installer: exposes public licence at install root", r"..\LICENSE" in installer)
    check(
        "installer: carries the exact source offer",
        r"..\SOURCE_OFFER.txt" in installer,
    )
    check(
        "installer: keeps large source archives as separate release assets",
        "third-party-source.zip\"; DestDir" not in installer,
    )
    check("builder: invokes PyInstaller source recipe", "pyinstaller.spec" in builder.casefold())
    check("builder: invokes Inno Setup compiler", "ISCC.exe" in builder)
    check(
        "builder: recreates an isolated release environment",
        "release_env" in builder and "-m venv" in builder and "--clear" in builder,
    )
    check(
        "builder: installs only the locked release dependency set",
        "requirements-release-hashed.txt" in builder
        and "--require-hashes" in builder
        and "--only-binary=:all:" in builder
        and "--no-build-isolation" in builder,
    )
    check(
        "builder: installs the exact custom PyAV wheel before the locked set",
        "pyav_runtime.json" in builder
        and "$pyavContract.artifact.sha256" in builder
        and "smoke_pyav_runtime_contract.py" in builder
        and builder.find("--no-index --no-deps $pyavWheel")
        < builder.find('requirements-release-hashed.txt'),
    )
    check(
        "bundle: rejects any PyAV DLL outside the minimized contract",
        "PYAV_RUNTIME_CONTRACT" in spec
        and "expected_pyav_dlls" in spec
        and "if pyav_dlls != expected_pyav_dlls:" in spec
        and 'pyav_runtime_contract["native_runtime"]["allowed_dlls"]' in spec,
    )
    check(
        "builder: rejects undeclared release-environment packages",
        bool(re.search(r'smoke_release_environment\.py"\s+--strict', builder)),
    )
    check(
        "bundle: derives Python licence inventory from the release lock",
        "constraints-release.txt" in spec and "runtime_distributions = (" not in spec,
    )
    check(
        "bundle: refuses a locked distribution without licence evidence",
        "supplies no licence or notice file" in spec,
    )
    check(
        "compliance: build record identifies Momento's custom PyAV runtime",
        all(
            term in build_info
            for term in (
                "PyAV 17.0.1",
                "FFmpeg 8.0.1",
                "Runtime contract: build/pyav_runtime.json",
            )
        ),
    )
    check(
        "compliance: notices identify the complete native source bundle",
        "PyAV 17.0.1" in notices
        and "Momento-0.2.2-third-party-source.zip" in notices,
    )
    check(
        "builder: creates and verifies third-party corresponding source",
        "build_corresponding_source.py" in builder
        and "smoke_corresponding_source.py" in builder,
    )
    check(
        "builder: rejects unused OpenCV and Qt PDF runtimes",
        all(
            term in builder
            for term in ("opencv_videoio_ffmpeg", "Qt6Pdf", "qpdf")
        ),
    )
    check(
        "builder: emits the reviewed downloadable FFmpeg helper",
        "package_ffmpeg_helper.py" in builder
        and "BB8E4FC7A4E8E3BB5EA4F509BFA49E01BAD1932F8CD1E4399D145D90C080F0B5"
        in builder,
    )
    check(
        "helper: checksum-covered text payload keeps exact bytes on Windows",
        all(
            f"/resources/ffmpeg/{name} binary" in attributes
            for name in ("LICENSE.txt", "README.txt", "SHA256SUMS.txt")
        ),
    )
    check(
        "builder: emits one checksum list for all release assets",
        "SHA256SUMS-0.2.2.txt" in builder
        and all(
            term in builder
            for term in (
                "$installer",
                "$sourceArchive",
                "$thirdPartySource",
                "$helperArchive",
                "$updateMetadata",
                "$updateSignature",
            )
        ),
    )
    check(
        "builder: signs and verifies updater release assets",
        "build_update_metadata.py" in builder
        and "Momento-update.json" in builder
        and "Momento-update.json.sig" in builder
        and "smoke_update_release_tools.py" in builder,
    )
    updater_release_checks = (
        "smoke_update_metadata.py",
        "smoke_update_client.py",
        "smoke_update_attempts.py",
        "smoke_update_lifecycle.py",
        "smoke_update_service.py",
        "smoke_update_handoff.py",
        "smoke_update_runtime.py",
        "smoke_single_instance.py",
        "smoke_update_release_tools.py",
    )
    check(
        "builder: runs the complete updater regression suite",
        all(name in builder for name in updater_release_checks),
    )
    check("builder: scans the public bundle before publishing", bool(re.search(r"forbidden|personal", builder, re.I)))
    check("builder: runs installed release checks", "smoke_installed_release.ps1" in builder)
    installed_smoke = (ROOT / "tests" / "smoke_installed_release.ps1").read_text(
        encoding="utf-8"
    )
    check(
        "installed release: verifies source offer and explicit purge",
        "SOURCE_OFFER.txt" in installed_smoke
        and "/PURGEUSERDATA" in installed_smoke
        and "Purge uninstall removed the mock recording" in installed_smoke,
    )

    print(f"\n{checks - failures}/{checks} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
