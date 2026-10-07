"""Reject both product names' runtime files from exact source archives."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    failures = 0
    cases = [
        ("mementos/__init__.py", True),
        ("resources/update_public_key.pem", True),
        ("profile/momento.log", False),
        ("profile/mementos.log", False),
        ("profile/mementoS.LOG.1", False),
        ("profile/momento.lock", False),
        ("profile/mementos.lock", False),
        ("profile/Momento-update.json", False),
        ("profile/Mementos-update.json.sig", False),
        ("profile/update-signing-key.pem", False),
        ("profile/attempt-state.json", False),
        ("momento.egg-info/PKG-INFO", False),
        ("mementos.egg-info/PKG-INFO", False),
    ]
    with tempfile.TemporaryDirectory(prefix="mementos-privacy-") as directory:
        archive = Path(directory) / "fixture.zip"
        for name, allowed in cases:
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr(name, "fixture")
            result = subprocess.run(
                [sys.executable, str(ROOT / "tests" / "smoke_source_archive.py"), str(archive)],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            passed = (result.returncode == 0) == allowed
            print(f"{'PASS' if passed else 'FAIL'} - source archive {'allows' if allowed else 'rejects'} {name}")
            failures += not passed
    print(f"\n{len(cases) - failures}/{len(cases)} checks passed")
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
