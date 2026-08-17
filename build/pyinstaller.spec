# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Momento.

One-folder build (faster startup than one-file). The output is
``dist/Momento/Momento.exe`` plus a sibling _internal folder containing
Qt6 dlls, our bundled ffmpeg.exe (offline remux + trim + thumbnails),
PyAV's libav shared libraries (live recording encode/mux),
numpy/PyAudioWPatch (PortAudio)/windows_capture binaries.

Run from the repo root:

    .\\.venv\\Scripts\\python.exe -m PyInstaller build\\pyinstaller.spec --noconfirm

The resulting Momento.exe is fully self-contained — no separate Python install
needed on the target machine.
"""

import json
import os
import re
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs

# When PyInstaller runs this spec, __file__ isn't defined the usual way; rely
# on the spec's working directory instead (PyInstaller cds to repo root).
PROJECT_ROOT = Path.cwd()
if not (PROJECT_ROOT / "momento" / "__main__.py").is_file():
    PROJECT_ROOT = Path(SPECPATH).resolve().parent

RESOURCES = PROJECT_ROOT / "resources"
RELEASE_LOCK = PROJECT_ROOT / "constraints-release.txt"
PYAV_RUNTIME_CONTRACT = PROJECT_ROOT / "build" / "pyav_runtime.json"
PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s]+)$")

required_resources = [
    RESOURCES / "ffmpeg" / "ffmpeg.exe",
    RESOURCES / "ffmpeg" / "ffprobe.exe",
    RESOURCES / "ffmpeg" / "LICENSE.txt",
    RESOURCES / "ffmpeg" / "README.txt",
    RESOURCES / "ffmpeg" / "SHA256SUMS.txt",
    RESOURCES / "known_games.json",
    RESOURCES / "update_public_key.pem",
    RESOURCES / "icons" / "momento.ico",
    RESOURCES / "sounds" / "bookmark.wav",
    RESOURCES / "version_info.txt",
    PROJECT_ROOT / "LICENSE",
    PROJECT_ROOT / "THIRD_PARTY_NOTICES.txt",
    PROJECT_ROOT / "BUILD_INFO.txt",
    RELEASE_LOCK,
    Path(sys.base_prefix) / "LICENSE.txt",
]
missing_resources = [path for path in required_resources if not path.is_file()]
if missing_resources:
    missing = "\n  ".join(str(path) for path in missing_resources)
    raise SystemExit(
        "Momento packaging resources are missing:\n  " + missing
        + "\nRun scripts\\fetch_ffmpeg.ps1 for FFmpeg, then restore the "
        "tracked resources before building."
    )

try:
    pyav_runtime_contract = json.loads(
        PYAV_RUNTIME_CONTRACT.read_text(encoding="utf-8")
    )
except (OSError, UnicodeError, json.JSONDecodeError) as exc:
    raise SystemExit("The minimized PyAV runtime contract is missing or invalid") from exc

# (src, dest_inside_bundle) — dest matches what util.ffmpeg_path / util.resources expect.
datas = [
    (str(RESOURCES / "ffmpeg" / "ffmpeg.exe"), "resources/ffmpeg"),
    (str(RESOURCES / "ffmpeg" / "ffprobe.exe"), "resources/ffmpeg"),
    (str(RESOURCES / "ffmpeg" / "LICENSE.txt"), "licenses/FFmpeg"),
    (str(RESOURCES / "ffmpeg" / "README.txt"), "licenses/FFmpeg"),
    (str(RESOURCES / "ffmpeg" / "SHA256SUMS.txt"), "licenses/FFmpeg"),
    (str(RESOURCES / "known_games.json"), "resources"),
    (str(RESOURCES / "update_public_key.pem"), "resources"),
    (str(PROJECT_ROOT / "LICENSE"), "."),
    (str(PROJECT_ROOT / "THIRD_PARTY_NOTICES.txt"), "."),
    (str(PROJECT_ROOT / "BUILD_INFO.txt"), "."),
    (str(Path(sys.base_prefix) / "LICENSE.txt"), "licenses/CPython"),
]

youtube_secrets = RESOURCES / "youtube" / "client_secrets.json"
include_youtube_oauth = os.environ.get("MOMENTO_INCLUDE_YOUTUBE_OAUTH") == "1"

locked_distributions = []
seen_distributions = set()
for number, line in enumerate(RELEASE_LOCK.read_text(encoding="utf-8").splitlines(), 1):
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        continue
    match = PIN.fullmatch(stripped)
    if not match:
        raise SystemExit(
            f"{RELEASE_LOCK.name}:{number} is not one exact name==version pin"
        )
    distribution_name, expected_version = match.groups()
    canonical_name = re.sub(r"[-_.]+", "-", distribution_name).casefold()
    if canonical_name in seen_distributions:
        raise SystemExit(f"Duplicate release lock entry for {distribution_name}")
    seen_distributions.add(canonical_name)
    locked_distributions.append((canonical_name, distribution_name, expected_version))

for canonical_name, distribution_name, expected_version in sorted(locked_distributions):
    try:
        package = distribution(distribution_name)
    except PackageNotFoundError as exc:
        raise SystemExit(
            f"Locked distribution {distribution_name}=={expected_version} is missing"
        ) from exc
    if package.version != expected_version:
        raise SystemExit(
            f"Locked distribution {distribution_name} is {package.version}, "
            f"expected {expected_version}"
        )

    licence_files = []
    for package_file in sorted(package.files or (), key=lambda item: str(item).casefold()):
        relative = str(package_file).replace("\\", "/")
        lowered = relative.casefold()
        name = Path(lowered).name
        if not (
            "/licenses/" in f"/{lowered}"
            or name.startswith(("license", "licence", "copying", "notice"))
        ):
            continue
        relative_path = Path(relative)
        if "__pycache__" in lowered or relative_path.suffix.casefold() == ".pyc":
            continue
        source = package.locate_file(package_file)
        if source.is_file():
            licence_files.append((source, relative_path))
    if not licence_files:
        raise SystemExit(
            f"Locked distribution {distribution_name}=={expected_version} "
            "supplies no licence or notice file"
        )
    for source, relative_path in licence_files:
        destination = (
            Path("licenses")
            / "Python"
            / f"{canonical_name}-{expected_version}"
            / relative_path.parent
        )
        datas.append((str(source), destination.as_posix()))
sounds_dir = RESOURCES / "sounds"
if sounds_dir.is_dir() and any(sounds_dir.iterdir()):
    datas.append((str(sounds_dir), "resources/sounds"))
# Icons folder is optional today (icons are drawn at runtime via QPainter) but
# include it so we can drop in a .ico later without re-editing the spec.
icons_dir = RESOURCES / "icons"
if icons_dir.is_dir() and any(icons_dir.iterdir()):
    datas.append((str(icons_dir), "resources/icons"))
# Bundled Plus Jakarta Sans weights — loaded at startup via QFontDatabase
# (momento.ui.theme.load_fonts). Without these the UI falls back to Segoe UI.
fonts_dir = RESOURCES / "fonts"
if fonts_dir.is_dir() and any(fonts_dir.iterdir()):
    datas.append((str(fonts_dir), "resources/fonts"))
# A public build must not silently inherit a developer's OAuth identity from
# an ignored local file. Distributors opt in deliberately for an approved
# production OAuth project; ordinary builds omit the identity and the UI.
if include_youtube_oauth and youtube_secrets.is_file():
    datas.append((str(youtube_secrets), "resources/youtube"))

# Hidden imports — most stuff PyInstaller finds automatically; this is a
# safety net for COM/ctypes-loaded modules.
hiddenimports = [
    "PyQt6.QtMultimedia",
    "PyQt6.QtMultimediaWidgets",
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    # Obsidian redesign: UI glyphs are recoloured SVGs rendered via QtSvg.
    "PyQt6.QtSvg",
    # PortAudio/WASAPI capture (mic + loopback). Its bundled PortAudio DLL is
    # collected into `binaries` below.
    "pyaudiowpatch",
    "numpy",
    "windows_capture",
    # PyAV: linked C extension; PyInstaller may miss the filter / format
    # submodules unless explicitly listed. They are dynamically imported
    # by libav's plugin lookup when we add filters like amix / aresample.
    "av",
    "av.audio",
    "av.audio.frame",
    "av.audio.stream",
    "av.audio.resampler",
    "av.video",
    "av.video.frame",
    "av.video.stream",
    "av.container",
    "av.filter",
    "av.codec",
    "av.codec.context",
]

if include_youtube_oauth:
    hiddenimports += [
        "googleapiclient", "googleapiclient.discovery", "googleapiclient.http",
        "googleapiclient.errors", "google.auth",
        "google.auth.transport.requests", "google.oauth2.credentials",
        "google_auth_oauthlib", "google_auth_oauthlib.flow",
        "google_auth_httplib2", "requests", "urllib3",
    ]

# Skip pulling in stuff we don't use to keep the bundle smaller.
excludes = [
    "tkinter",
    "matplotlib",
    "pytest",
    "PIL",
    "setuptools",
    "pip",
    "PyInstaller",
    # windows-capture imports cv2 only for optional image-save helpers that
    # Momento never calls. video_capture installs a tiny import-time stub.
    "cv2",
    "PyQt6.QtPdf",
    "PyQt6.QtPdfWidgets",
]
if not include_youtube_oauth:
    excludes += [
        "google", "googleapiclient", "google_auth_oauthlib",
        "google_auth_httplib2", "httplib2",
        "oauthlib", "requests_oauthlib",
    ]

block_cipher = None

# PyAudioWPatch ships its own PortAudio DLL alongside the _portaudio extension;
# collect it explicitly so the frozen build can open audio devices.
binaries = collect_dynamic_libs("pyaudiowpatch")

a = Analysis(
    [str(PROJECT_ROOT / "momento" / "__main__.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Qt's image-format hook brings in PDF support even though Momento never opens
# PDF files. Removing both files avoids shipping an unused QtWebEngine-derived
# component and keeps the corresponding-source set precise.
for toc in (a.binaries, a.datas):
    toc[:] = [
        entry
        for entry in toc
        if Path(entry[0]).name.casefold() not in {"qt6pdf.dll", "qpdf.dll"}
    ]

# Momento is currently English-only and does not open these image formats.
# Match complete bundle destinations so similarly named Qt components remain.
QT_PRUNED_FILES = {
    "pyqt6/qt6/plugins/imageformats/qgif.dll",
    "pyqt6/qt6/plugins/imageformats/qicns.dll",
    "pyqt6/qt6/plugins/imageformats/qtga.dll",
    "pyqt6/qt6/plugins/imageformats/qtiff.dll",
    "pyqt6/qt6/plugins/imageformats/qwbmp.dll",
    "pyqt6/qt6/plugins/imageformats/qwebp.dll",
}
QT_PRUNED_DIRECTORIES = {
    "pyqt6/qt6/translations",
}
QT_REQUIRED_FILES = {
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


def normalized_toc_destination(entry):
    return str(entry[0]).replace("\\", "/").lstrip("./").casefold()


def is_pruned_qt_destination(entry):
    destination = normalized_toc_destination(entry)
    return destination in QT_PRUNED_FILES or any(
        destination == directory or destination.startswith(directory + "/")
        for directory in QT_PRUNED_DIRECTORIES
    )


for toc in (a.binaries, a.datas):
    toc[:] = [entry for entry in toc if not is_pruned_qt_destination(entry)]

pyav_hash_suffix = re.compile(r"-([0-9a-f]{8,64})(?=\.dll$)", re.IGNORECASE)


def canonical_pyav_dll_name(name):
    return pyav_hash_suffix.sub("", Path(name).name).casefold()


pyav_dlls = {
    canonical_pyav_dll_name(entry[0])
    for entry in a.binaries
    if Path(str(entry[0]).replace("\\", "/")).parent.name.casefold() == "av.libs"
    and Path(entry[0]).suffix.casefold() == ".dll"
}
expected_pyav_dlls = {
    name.casefold()
    for name in pyav_runtime_contract["native_runtime"]["allowed_dlls"]
}
if pyav_dlls != expected_pyav_dlls:
    missing = sorted(expected_pyav_dlls - pyav_dlls)
    unexpected = sorted(pyav_dlls - expected_pyav_dlls)
    raise SystemExit(
        "Momento's packaged PyAV DLL inventory differs from the minimized contract: "
        f"missing={missing}, unexpected={unexpected}"
    )

qt_destinations = {
    normalized_toc_destination(entry)
    for toc in (a.binaries, a.datas)
    for entry in toc
}
missing_required_qt = sorted(QT_REQUIRED_FILES - qt_destinations)
if missing_required_qt:
    missing = "\n  ".join(missing_required_qt)
    raise SystemExit("Momento is missing required Qt runtime files:\n  " + missing)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Momento",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # tray app — no console window
    version=str(RESOURCES / "version_info.txt"),
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(RESOURCES / "icons" / "momento.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Momento",
)
