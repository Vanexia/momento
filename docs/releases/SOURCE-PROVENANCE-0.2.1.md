# Momento 0.2.1 Source Provenance

This document identifies the source and build records corresponding to the Momento 0.2.1 Windows release.

## Momento

- Public source snapshot: `d92c0dd418199a9b3e06ebc546b7b21b6264081d`
- Licence: GPL-3.0-only
- Corresponding archive: `Momento-0.2.1-source.zip`
- Pinned Python environment: `constraints-release.txt` in the archive
- Packaging recipes: `build/pyinstaller.spec`, `build/installer.iss`, and `scripts/build_installer.ps1`

The source archive contains all 230 files tracked in the privacy-clean public
snapshot. It preserves the release source while replacing test-only private
identity fixtures with neutral placeholders. It also contains the dependency
lock, licence notices, build information, and the FFmpeg acquisition script.

## FFmpeg

- Binary provider: [Gyan FFmpeg 8.1.2 release](https://github.com/GyanD/codexffmpeg/releases/tag/8.1.2)
- Binary package: `ffmpeg-8.1.2-essentials_build.zip`
- Binary package SHA-256: `DB580001CAA24AC104C8CB856CD113A87B0A443F7BDF47D8C12B1D740584A2EC`
- FFmpeg commit: [`38b88335f9`](https://github.com/FFmpeg/FFmpeg/commit/38b88335f9)
- Corresponding archive: `ffmpeg-8.1.2.tar.xz`
- Source archive SHA-256: `464BEB5E7BF0C311E68B45AE2F04E9CC2AF88851ABB4082231742A74D97B524C`

The original `README.txt` shipped by the binary provider is retained under `resources/ffmpeg` in the Momento source archive and under `licenses/FFmpeg` in the installed application. It records the complete FFmpeg configuration and the provider's external-library versions. `BUILD_INFO.txt` records the same release's packaging details.

## Python And Qt Components

The exact Python package versions used for the release are pinned in `constraints-release.txt`. Their licence texts and notices, including PyQt6, SIP, Qt, CPython, PyInstaller, PyAV, OpenCV, NumPy, windows-capture, and PyAudioWPatch, are retained in the installed `licenses` directory where supplied by their distributions.

`THIRD_PARTY_NOTICES.txt` in the source archive identifies the upstream projects and applicable terms. Upstream source distributions can be matched against the exact versions in `constraints-release.txt`.
