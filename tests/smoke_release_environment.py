"""Require the release builder to use the exact pinned environment."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from collections import defaultdict
from importlib.metadata import distributions
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s]+)$")
HASHED_PIN = re.compile(
    r"^([A-Za-z0-9_.-]+)==([^\s]+) --hash=sha256:([0-9a-f]{64})$"
)


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).casefold()


def _licence_files(dist) -> list[Path]:
    found: list[Path] = []
    for package_file in dist.files or ():
        relative = str(package_file).replace("\\", "/")
        basename = Path(relative).name.casefold()
        if not (
            "/licenses/" in f"/{relative.casefold()}"
            or basename.startswith(("license", "licence", "copying", "notice"))
        ):
            continue
        source = Path(dist.locate_file(package_file))
        if source.is_file():
            found.append(source)
    return found


def main(*, strict: bool = False) -> int:
    failures: list[str] = []
    if sys.version_info[:2] != (3, 12):
        failures.append(f"Python is {sys.version_info.major}.{sys.version_info.minor}, expected 3.12")

    locked: dict[str, tuple[str, str]] = {}
    for number, line in enumerate(
        (ROOT / "constraints-release.txt").read_text(encoding="utf-8").splitlines(), 1
    ):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = PIN.fullmatch(stripped)
        if not match:
            failures.append(
                f"constraints-release.txt:{number} is not one exact name==version pin"
            )
            continue
        name, expected = match.groups()
        canonical = _canonical_name(name)
        if canonical in locked:
            failures.append(f"{name} duplicates another release lock entry")
            continue
        locked[canonical] = (name, expected)

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project_name = pyproject["project"]["name"]
    project_version = pyproject["project"]["version"]
    locked[_canonical_name(project_name)] = (project_name, project_version)

    hashed: dict[str, tuple[str, str]] = {}
    for number, line in enumerate(
        (ROOT / "requirements-release-hashed.txt")
        .read_text(encoding="utf-8")
        .splitlines(),
        1,
    ):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = HASHED_PIN.fullmatch(stripped)
        if not match:
            failures.append(
                f"requirements-release-hashed.txt:{number} is not one exact hashed pin"
            )
            continue
        name, version, digest = match.groups()
        canonical = _canonical_name(name)
        if canonical in hashed:
            failures.append(f"{name} duplicates another hashed release entry")
            continue
        hashed[canonical] = (version, digest)
    constraint_set = {
        canonical: version
        for canonical, (name, version) in locked.items()
        if canonical != _canonical_name(project_name)
    }
    hashed_versions = {
        canonical: version for canonical, (version, _digest) in hashed.items()
    }
    if hashed_versions != constraint_set:
        failures.append("hashed wheel lock and release constraints do not exactly match")

    installed: dict[str, list] = defaultdict(list)
    for dist in distributions():
        name = dist.metadata.get("Name")
        if name:
            installed[_canonical_name(name)].append(dist)

    for canonical, (name, expected) in sorted(locked.items()):
        matches = installed.get(canonical, [])
        if not matches:
            failures.append(f"{name} is missing (expected {expected})")
            continue
        if len(matches) != 1:
            versions = ", ".join(sorted(dist.version for dist in matches))
            failures.append(f"{name} is installed more than once ({versions})")
            continue
        actual = matches[0].version
        if actual != expected:
            failures.append(f"{name} is {actual}, expected {expected}")
        if canonical != _canonical_name(project_name) and not _licence_files(matches[0]):
            failures.append(f"{name} {actual} supplies no licence or notice file")

    unexpected = sorted(set(installed) - set(locked))
    if strict:
        for canonical in unexpected:
            matches = installed[canonical]
            rendered = ", ".join(
                sorted(f"{dist.metadata.get('Name')}=={dist.version}" for dist in matches)
            )
            failures.append(f"unexpected distribution in release environment: {rendered}")

    if failures:
        for failure in failures:
            print(f"FAIL - {failure}")
        return 1
    if strict:
        print("PASS - Python and the installed distribution set exactly match the release lock")
    else:
        print("PASS - Python and all locked distributions match the release lock")
        if unexpected:
            print(f"INFO - Ignored {len(unexpected)} additional development distributions")
    print("PASS - Every locked third-party distribution supplies licence evidence")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--strict",
        action="store_true",
        help="reject every distribution not declared in constraints-release.txt",
    )
    raise SystemExit(main(strict=parser.parse_args().strict))
