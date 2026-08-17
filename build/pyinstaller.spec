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

import os
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

required_resources = [
    RESOURCES / "ffmpeg" / "ffmpeg.exe",
    RESOURCES / "ffmpeg" / "ffprobe.exe",
    RESOURCES / "ffmpeg" / "LICENSE.txt",
    RESOURCES / "ffmpeg" / "README.txt",
    RESOURCES / "known_games.json",
    RESOURCES / "icons" / "momento.ico",
    RESOURCES / "sounds" / "bookmark.wav",
    RESOURCES / "version_info.txt",
    PROJECT_ROOT / "LICENSE",
    PROJECT_ROOT / "THIRD_PARTY_NOTICES.txt",
    PROJECT_ROOT / "BUILD_INFO.txt",
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

# (src, dest_inside_bundle) — dest matches what util.ffmpeg_path / util.resources expect.
datas = [
    (str(RESOURCES / "ffmpeg" / "ffmpeg.exe"), "resources/ffmpeg"),
    (str(RESOURCES / "ffmpeg" / "ffprobe.exe"), "resources/ffmpeg"),
    (str(RESOURCES / "ffmpeg" / "LICENSE.txt"), "licenses/FFmpeg"),
    (str(RESOURCES / "ffmpeg" / "README.txt"), "licenses/FFmpeg"),
    (str(RESOURCES / "known_games.json"), "resources"),
    (str(PROJECT_ROOT / "LICENSE"), "."),
    (str(PROJECT_ROOT / "THIRD_PARTY_NOTICES.txt"), "."),
    (str(PROJECT_ROOT / "BUILD_INFO.txt"), "."),
    (str(Path(sys.base_prefix) / "LICENSE.txt"), "licenses/CPython"),
]

youtube_secrets = RESOURCES / "youtube" / "client_secrets.json"
include_youtube_oauth = os.environ.get("MOMENTO_INCLUDE_YOUTUBE_OAUTH") == "1"

runtime_distributions = (
    "PyQt6", "PyQt6-Qt6", "PyQt6-sip", "psutil", "PyAudioWPatch",
    "windows-capture", "av", "numpy", "opencv-python", "PyInstaller",
)
if include_youtube_oauth:
    runtime_distributions += (
        "requests", "urllib3", "certifi", "charset-normalizer", "idna",
        "google-api-python-client", "google-auth", "google-auth-oauthlib",
        "google-auth-httplib2", "httplib2", "cryptography", "cffi",
        "cachetools", "oauthlib", "requests-oauthlib", "rsa", "pyasn1",
        "pyasn1-modules", "protobuf", "proto-plus", "google-api-core",
        "googleapis-common-protos",
    )
for distribution_name in runtime_distributions:
    try:
        package = distribution(distribution_name)
    except PackageNotFoundError:
        continue
    for package_file in package.files or ():
        lowered = str(package_file).replace("\\", "/").casefold()
        name = Path(lowered).name
        if not (
            "/licenses/" in f"/{lowered}"
            or name.startswith(("license", "copying", "notice"))
        ):
            continue
        source = package.locate_file(package_file)
        if source.is_file():
            datas.append((str(source), f"licenses/{distribution_name}"))
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
]
if not include_youtube_oauth:
    excludes += [
        "google", "googleapiclient", "google_auth_oauthlib",
        "google_auth_httplib2", "httplib2", "requests", "urllib3",
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
