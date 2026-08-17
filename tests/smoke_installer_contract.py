"""Static contract for the versioned per-user Windows installer."""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento import __version__  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "0.2.1"

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

    check("version: Python package matches public release", __version__ == EXPECTED_VERSION)
    check("version: pyproject matches public release", pyproject["project"]["version"] == EXPECTED_VERSION)
    check("version: Windows file metadata matches public release", f"'ProductVersion', '{EXPECTED_VERSION}'" in version_info)
    check("installer: Inno source exists", installer_path.is_file())
    check("installer: deterministic build wrapper exists", builder_path.is_file())
    check("installer: version matches application", f'#define MyAppVersion "{EXPECTED_VERSION}"' in installer)
    check("installer: per-user privileges", "PrivilegesRequired=lowest" in installer)
    check("installer: installs below LocalAppData", r"{localappdata}\Programs\Momento" in installer)
    check("installer: targets supported 64-bit Windows", "ArchitecturesAllowed=x64compatible" in installer)
    check("installer: checks the running-app mutex", "Momento.GameRecorder.Instance" in installer)
    check("installer: removes stale autostart on uninstall", "RegDeleteValue(HKCU" in installer)
    check("installer: preserves user data by default", "PURGEUSERDATA" in installer)
    check(
        "installer: purge cannot recursively delete the whole AppData root",
        "DelTree(ExpandConstant('{userappdata}\\Momento')," not in installer,
    )
    check("installer: carries the complete one-folder release", r"dist\Momento\*" in installer)
    check("installer: exposes public licence at install root", r"..\LICENSE" in installer)
    check("installer: carries exact FFmpeg source", "ffmpeg-8.1.2.tar.xz" in installer)
    check("builder: invokes PyInstaller source recipe", "pyinstaller.spec" in builder.casefold())
    check("builder: invokes Inno Setup compiler", "ISCC.exe" in builder)
    check("builder: scans the public bundle before publishing", bool(re.search(r"forbidden|personal", builder, re.I)))
    check("builder: runs installed release checks", "smoke_installed_release.ps1" in builder)

    print(f"\n{checks - failures}/{checks} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
