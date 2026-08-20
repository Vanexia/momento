"""Reject personal identity and private runtime data from public release inputs."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.config import Config  # noqa: E402
from privacy_scan import (  # noqa: E402
    binary_contains_blocked_identity,
    contains_blocked_identity,
    contains_private_windows_user_path,
)


ROOT = Path(__file__).resolve().parents[1]
TEXT_ROOTS = (
    ROOT / "momento",
    ROOT / "build",
    ROOT / "scripts",
    ROOT / "resources",
    ROOT / "docs",
)
TEXT_FILES = (
    ROOT / "README.md",
    ROOT / "LICENSE",
    ROOT / "pyproject.toml",
    ROOT / "constraints-release.txt",
    ROOT / "requirements-release-hashed.txt",
    ROOT / "SOURCE_OFFER.txt",
)
RUNTIME_NAMES = {
    "config.json",
    "momento.log",
    "momento.lock",
    "window_state.ini",
    "youtube_token.dat",
    "youtube_avatar.png",
    "youtube_oauth_client.dat",
    "youtube_oauth_client.dat.tmp",
    "Momento-update.json",
    "Momento-update.json.sig",
    "update-signing-key.pem",
    "trusted-state.json",
}
RUNTIME_SUFFIXES = (".thumb.jpg", ".bookmarks.json", ".momento.json")
GENERATED_PARTS = {"bdist.win-amd64", "lib", "pyinstaller_work", "release_env"}
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


def _project_text() -> str:
    paths = list(TEXT_FILES)
    for root in TEXT_ROOTS:
        if not root.exists():
            continue
        paths.extend(
            path
            for path in root.rglob("*")
            if path.is_file()
            and not (GENERATED_PARTS & set(path.parts))
            and "__pycache__" not in path.parts
            and path.suffix.lower()
            in {".py", ".md", ".txt", ".html", ".yml", ".yaml", ".toml", ".iss", ".ps1", ".json"}
            and path.name != "client_secrets.json"
        )
    chunks: list[str] = []
    for path in paths:
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(chunks).casefold()


def _bundle_has_private_identity(dist: Path) -> bool:
    for path in dist.rglob("*"):
        if not path.is_file():
            continue
        if binary_contains_blocked_identity(path.read_bytes()):
            return True
    return False


def main() -> int:
    text = _project_text()
    internal_agent_files = (
        ROOT / "AGENTS.md",
        ROOT / "CLAUDE.md",
        ROOT / "skills-lock.json",
    )
    internal_docs_root = ROOT / "docs" / "superpowers"
    check(
        "privacy: public tree omits internal agent instructions and plans",
        not any(path.is_file() for path in internal_agent_files)
        and not any(path.is_file() for path in internal_docs_root.rglob("*")),
    )
    check("privacy: public source omits blocked identity values", not contains_blocked_identity(text))
    check("privacy: public source omits Windows user-profile paths", not contains_private_windows_user_path(text))
    check(
        "privacy: public source omits private signing key material",
        "-----begin private key-----" not in text
        and "-----begin encrypted private key-----" not in text,
    )
    check(
        "updates: public verification key is tracked",
        (ROOT / "resources" / "update_public_key.pem").is_file(),
    )

    ci_workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    check(
        "CI: Windows invokes pip through the active Python interpreter",
        "python -m pip install -c constraints-release.txt -e .[dev]" in ci_workflow
        and "\n          pip install " not in ci_workflow,
    )

    spec = (ROOT / "build" / "pyinstaller.spec").read_text(encoding="utf-8")
    check(
        "privacy: PyInstaller has no OAuth identity bundle path",
        "client_secrets.json" not in spec and "MOMENTO_INCLUDE_YOUTUBE_OAUTH" not in spec,
    )
    builder = (ROOT / "scripts" / "build_installer.ps1").read_text(encoding="utf-8")
    check(
        "privacy: public builder rejects the obsolete OAuth bundling flag",
        "MOMENTO_INCLUDE_YOUTUBE_OAUTH" in builder and "will not include" in builder,
    )
    check(
        "YouTube: Google client runtime is included for every installer",
        all(
            name in spec
            for name in (
                '"googleapiclient"',
                '"google_auth_oauthlib"',
                '"google_auth_httplib2"',
                '"requests"',
            )
        ),
    )
    resources_module = (ROOT / "momento" / "util" / "resources.py").read_text(
        encoding="utf-8"
    )
    check(
        "YouTube: legacy feature probes remain safe and compatible",
        "def youtube_client_secrets_path" in resources_module
        and "def youtube_upload_available" in resources_module,
    )

    cfg = Config()
    check(
        "fresh user: 16 Mbit/s is the default quality",
        cfg.quality_preset == "custom" and cfg.custom_bitrate_kbps == 16_000,
    )
    check("fresh user: 60 fps is the default", cfg.framerate == 60 and not cfg.framerate_auto)

    dist = ROOT / "dist" / "Momento"
    if dist.exists():
        names = {path.name.casefold() for path in dist.rglob("*") if path.is_file()}
        check(
            "bundle: no runtime config/log/token/state files",
            not (names & RUNTIME_NAMES)
            and not any(name.endswith(RUNTIME_SUFFIXES) for name in names),
        )
        check(
            "bundle: no OAuth client identity or imported client state",
            "client_secrets.json" not in names
            and "youtube_oauth_client.dat" not in names
            and "youtube_oauth_client.dat.tmp" not in names,
        )
        check("bundle: no private identity strings", not _bundle_has_private_identity(dist))
        check(
            "bundle: unused OpenCV runtime and its FFmpeg DLL are excluded",
            not any("cv2" in name or "opencv_videoio_ffmpeg" in name for name in names),
        )
        check(
            "bundle: unused Qt PDF runtime is excluded",
            not any(name in {"qt6pdf.dll", "qpdf.dll"} for name in names),
        )

    print(f"\n{checks - failures}/{checks} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
