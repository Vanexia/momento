# Momento 0.2.2 Source Provenance

This record identifies the source and native build inputs for the Momento 0.2.2
Windows release.

## Momento

- Source archive: `Momento-0.2.2-source.zip`
- Archive producer: `git archive HEAD`
- Licence: GPL-3.0-only
- Release commit and archive hash: recorded in the release manifest and
  `SHA256SUMS-0.2.2.txt`

## Minimal FFmpeg Helper

- Identity: FFmpeg `8.1.2-momento-helper-1`
- Source: `ffmpeg-8.1.2.tar.xz`
- Source SHA-256:
  `464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c`
- Recipe: `scripts/build_ffmpeg_helper.sh`
- Workflow: `.github/workflows/build-ffmpeg-helper.yml`
- `ffmpeg.exe` SHA-256:
  `a53993c4fbfbc3fa9ed201ae03502f053182699b3580c7523dc66d176d0371fc`
- `ffprobe.exe` SHA-256:
  `dd7364cd03d86cb5f91fd028174cb6d5f1b2f3ba2606095676e0596b216a4d4d`

The recipe disables network access and external-library autodetection. It
enables only the formats, codecs, filters, protocols, parsers, and bitstream
filters used by Momento. The helper uses a statically linked GCC runtime and
imports only Windows system DLLs.

## PyAV Native Runtime

- PyAV: `17.0.1`
- Wheel: `av-17.0.1-cp311-abi3-win_amd64.whl`
- Wheel size: `6,636,889` bytes
- Wheel SHA-256:
  `fd605ec4ab782c3829bfb4f11c512d7db3bc230d0f889a28f29aab0fb793bf3b`
- FFmpeg runtime: `8.0.1`
- Runtime contract: `build/pyav_runtime.json`
- Windows recipe: `scripts/build_pyav_runtime.ps1`
- Native recipe: `scripts/build_pyav_runtime.sh`
- Toolchain: MSYS2 UCRT64, GCC `16.2.0`, Make `4.4.1`, NASM `3.02`,
  pkgconf `3.0.5`, CMake `4.4.2`, and Ninja `1.13.2`
- `SOURCE_DATE_EPOCH`: `1767139200`

The recipe builds PyAV and FFmpeg from pinned sources. It retains x264, NVIDIA
codec headers, AMD AMF headers, Intel oneVPL, GCC, and winpthreads. Its FFmpeg
allowlist covers H.264 encoding through NVENC, AMF, QuickSync, Media Foundation,
and libx264; AAC audio; Matroska and MP4 muxing; and the decoders and filters
used by Momento.

Two clean builds produced byte-for-byte identical wheels. The custom native
DLL payload is `13,058,674` bytes, compared with `67,351,552` bytes in the
general-purpose baseline, an `80.6%` reduction. Automated verification covered
the declared runtime files and capabilities, software encoding, and H.264/AAC
MKV and MP4 round trips. NVIDIA has separate physical recording evidence. AMD
AMF and Intel QuickSync still require testing on physical hardware and drivers.

## PyQt and Qt Runtime

- PyQt6 wheel: `pyqt6-6.11.0-cp310-abi3-win_amd64.whl`
- PyQt6 SHA-256:
  `bd11b459c54dca068e988a42cf838303334f0d441b9d16d92ae6719fcb5ac6ba`
- Qt wheel: `pyqt6_qt6-6.11.1-py3-none-win_amd64.whl`
- Qt wheel SHA-256:
  `7486c80512e823f2d3087e67f854f0556b345f4368040a853c8dc4d30fd3fe69`
- SIP wheel: `pyqt6_sip-13.11.1-cp312-cp312-win_amd64.whl`
- SIP wheel SHA-256:
  `1d1c67179c1924b28e3d7f04585639e7a7c0946f62390efc6ccf2a6206e595d3`
- Corresponding Qt source modules: Base, SVG, Multimedia, and Translations
  `6.11.1`
- Qt Multimedia native runtime: FFmpeg `7.1.3` with zlib `1.3.1`

The complete FFmpeg configure line embedded in the Qt Multimedia DLLs is
recorded under `binary_provenance.qt_multimedia_ffmpeg_configure` in
`build/corresponding_sources.json`. Rebuild FFmpeg with that configure line in
an MSVC shell, then configure Qt Multimedia 6.11.1 against that shared FFmpeg
installation. Build the Qt modules with CMake and install them into the prefix
used when building PyQt6. PyQt6 and SIP use their source archives' standard
PEP 517 build entry points against that Qt installation.

Momento does not use Qt PDF. `Qt6Pdf.dll` and the `qpdf` image plugin are
explicitly removed from the frozen application, avoiding an otherwise unused
QtWebEngine source dependency. Packaged builds also omit Qt translation
catalogs and the unused GIF, ICNS, TGA, TIFF, WBMP, and WebP image plugins. The
Qt pruning removes 7.88 MiB while preserving the Windows, multimedia, TLS,
JPEG, SVG, ICO, and software OpenGL components Momento uses.

## Third-Party Source Bundle

`Momento-0.2.2-third-party-source.zip` contains 17 exact source inputs:

- FFmpeg 8.1.2 for Momento's helper
- PyAV 17.0.1, FFmpeg 8.0.1, x264, NVIDIA codec headers, AMD AMF headers, and
  Intel oneVPL for the minimal recording runtime
- MSYS2 source packages for the shipped GCC/libstdc++ and winpthreads runtimes
- PyQt6 6.11.0, SIP 13.11.1, and Qt Base/SVG/Multimedia/Translations 6.11.1
- FFmpeg 7.1.3 and zlib 1.3.1 used by Qt Multimedia

`build/corresponding_sources.json` records every URL and SHA-256.
`scripts/build_corresponding_source.py` verifies each download, writes a
deterministic ZIP, reopens it, and verifies every embedded hash. The complete
bundle SHA-256 is recorded in `SHA256SUMS-0.2.2.txt` after the release build.

## Python Distributions

`constraints-release.txt` pins every Python distribution in the packaged
environment. `requirements-release-hashed.txt` additionally locks the exact
CPython 3.12 Windows x64 wheel for every direct and transitive package.
`build/pyinstaller.spec` copies every licence or notice file
provided by those distributions into `licenses/Python`, preserving its path and
grouping it by canonical distribution name and exact version. The build stops
if a pin is missing, mismatched, duplicated, undeclared, or lacks licence
evidence.
