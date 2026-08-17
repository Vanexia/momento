"""Contract for Momento's deliberately minimized Qt runtime bundle."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "build" / "pyinstaller.spec"

EXPECTED_PRUNED_FILES = {
    "pyqt6/qt6/plugins/imageformats/qgif.dll",
    "pyqt6/qt6/plugins/imageformats/qicns.dll",
    "pyqt6/qt6/plugins/imageformats/qtga.dll",
    "pyqt6/qt6/plugins/imageformats/qtiff.dll",
    "pyqt6/qt6/plugins/imageformats/qwbmp.dll",
    "pyqt6/qt6/plugins/imageformats/qwebp.dll",
}
EXPECTED_PRUNED_DIRECTORIES = {
    "pyqt6/qt6/translations",
}
EXPECTED_REQUIRED_FILES = {
    "pyqt6/qt6/bin/opengl32sw.dll",
    "pyqt6/qt6/plugins/imageformats/qico.dll",
    "pyqt6/qt6/plugins/imageformats/qjpeg.dll",
    "pyqt6/qt6/plugins/imageformats/qsvg.dll",
    "pyqt6/qt6/plugins/multimedia/ffmpegmediaplugin.dll",
    "pyqt6/qt6/plugins/multimedia/windowsmediaplugin.dll",
    "pyqt6/qt6/plugins/platforms/qwindows.dll",
    "pyqt6/qt6/plugins/tls/qcertonlybackend.dll",
    "pyqt6/qt6/plugins/tls/qopensslbackend.dll",
    "pyqt6/qt6/plugins/tls/qschannelbackend.dll",
}

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


def _literal_assignment(tree: ast.Module, name: str) -> set[str] | None:
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (TypeError, ValueError):
            return None
        if isinstance(value, (set, frozenset, tuple, list)):
            return {str(item).casefold() for item in value}
    return None


def _check_spec() -> None:
    source = SPEC_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(SPEC_PATH))

    check(
        "spec: exact unused Qt image plugins are pruned",
        _literal_assignment(tree, "QT_PRUNED_FILES") == EXPECTED_PRUNED_FILES,
    )
    check(
        "spec: only the Qt translations subtree is pruned",
        _literal_assignment(tree, "QT_PRUNED_DIRECTORIES")
        == EXPECTED_PRUNED_DIRECTORIES,
    )
    check(
        "spec: required Qt runtime files are explicit",
        _literal_assignment(tree, "QT_REQUIRED_FILES") == EXPECTED_REQUIRED_FILES,
    )
    check(
        "spec: Qt destinations are normalized before exact filtering",
        all(
            token in source
            for token in (
                "def normalized_toc_destination(",
                'replace("\\\\", "/")',
                ".casefold()",
                "destination in QT_PRUNED_FILES",
                'destination.startswith(directory + "/")',
            )
        ),
    )
    check(
        "spec: required Qt files are verified after pruning",
        "missing_required_qt" in source and "required Qt runtime files" in source,
    )


def _check_bundle(bundle: Path) -> None:
    check("bundle: requested path exists", bundle.is_dir())
    if not bundle.is_dir():
        return

    inventory = set()
    for path in bundle.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(bundle).as_posix().casefold()
        if relative.startswith("_internal/"):
            relative = relative.removeprefix("_internal/")
        inventory.add(relative)
    for relative in sorted(EXPECTED_PRUNED_FILES):
        check(f"bundle: excludes {Path(relative).name}", relative not in inventory)
    for directory in sorted(EXPECTED_PRUNED_DIRECTORIES):
        check(
            f"bundle: excludes {directory}",
            not any(
                relative == directory or relative.startswith(directory + "/")
                for relative in inventory
            ),
        )
    for relative in sorted(EXPECTED_REQUIRED_FILES):
        check(f"bundle: retains {relative}", relative in inventory)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "bundle",
        nargs="?",
        type=Path,
        help="Optional PyInstaller one-folder bundle to inspect",
    )
    args = parser.parse_args()

    _check_spec()
    if args.bundle is not None:
        _check_bundle(args.bundle.resolve())

    print(f"\n{checks - failures}/{checks} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
