# Secure Updates And Runtime Footprint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add authenticated launch-time and manual updates while reducing Momento's installed footprint without changing capture quality, playback, encoder coverage, or graphics compatibility.

**Architecture:** A pure-Python updater validates signed GitHub Release metadata, stages a hash-verified installer atomically, and asks the existing Qt application lifecycle to hand it to Inno Setup only while recording is idle. Packaging keeps the current one-folder layout, explicitly prunes unused Qt translations/image plugins, and builds PyAV against a pinned minimal shared FFmpeg runtime using the existing local MSYS2 toolchain.

**Tech Stack:** Python 3.12, PyQt6, `requests`, `cryptography` Ed25519, `packaging.version`, PyInstaller, Inno Setup 6.7.3, MSYS2/UCRT64, FFmpeg 8.0.1, PyAV 17.0.1, PowerShell, and the existing smoke-test harness.

## Global Constraints

- Check for a stable update exactly once per application process, at launch only; do not add a timer, service, scheduled task, prerelease channel, downgrade, or telemetry.
- Automatic checks and downloads stay silent; the manual Settings action reports concise progress and failures.
- Never install or force-close Momento during recording startup, active recording, or finalization.
- Accept only signed schema-1 stable metadata for `Vanexia/momento`, HTTPS release assets, monotonic unexpired metadata, a strictly newer normalized application version, bounded payloads, exact byte size, and exact SHA-256.
- Keep the current installation untouched when checking, downloading, verification, setup, or relaunch fails.
- Keep Qt software OpenGL, Qt Multimedia, PNG/JPEG/SVG/ICO support, NumPy, NVENC, AMF, QuickSync, Media Foundation, libx264, H.264/AAC, Matroska/MP4, scaling, and audio resampling.
- Remove only Qt translations and GIF/ICNS/TGA/TIFF/WBMP/WebP plugins, plus PyAV codec dependencies proven unrelated to Momento.
- Do not publish, force-push, delete a release, or upload assets without the user's separate approval.

---

### Task 1: Signed Metadata Contract And Release Key

**Files:**
- Create: `momento/updater/__init__.py`
- Create: `momento/updater/metadata.py`
- Create: `resources/update_public_key.pem`
- Create: `scripts/manage_update_key.py`
- Create: `scripts/build_update_metadata.py`
- Create: `tests/smoke_update_metadata.py`
- Modify: `momento/util/paths.py`
- Modify: `tests/smoke_public_release.py`

**Interfaces:**
- Produces: `UpdateManifest.from_signed_bytes(metadata: bytes, signature: bytes, public_key: bytes, current_version: str) -> UpdateManifest`.
- Produces: `canonical_manifest_bytes(payload: dict[str, object]) -> bytes` using sorted compact UTF-8 JSON.
- Produces: `update_cache_dir() -> Path`, with runtime data under `%LOCALAPPDATA%\Momento\updates` and no source/bundle leakage.
- Produces: a release-only signing command that reads a private Ed25519 PEM outside the repository and writes `Momento-update.json` plus `Momento-update.json.sig`.

- [ ] **Step 1: Write failing metadata tests**

Cover canonical byte stability, valid Ed25519 signatures, altered-byte rejection, unknown schema/channel rejection, malformed JSON, missing/extra keys, invalid timestamps, non-HTTPS URLs, wrong owner/repository/tag/filename, same-version and rollback rejection, installer-size bounds, malformed hashes, and unsupported minimum-updater versions.

- [ ] **Step 2: Prove the tests fail before implementation**

Run: `.\.venv\Scripts\python.exe tests\smoke_update_metadata.py`

Expected: import failure for `momento.updater.metadata`.

- [ ] **Step 3: Implement immutable metadata parsing and validation**

Use a frozen `UpdateManifest` dataclass with exact schema fields. Verify the Ed25519 signature over the original bytes before decoding JSON, reject duplicate JSON keys, parse versions with `packaging.version.Version`, and compare the installer URL to `https://github.com/Vanexia/momento/releases/download/v{version}/MomentoSetup-{version}.exe`.

- [ ] **Step 4: Add external key management and release metadata generation**

Generate the private key at `%LOCALAPPDATA%\MomentoRelease\update-signing-key.pem`, restrict its ACL to the current user and SYSTEM, and write only its public half to `resources/update_public_key.pem`. The signing script must fail closed if the key, installer, version, filename, size, or final self-verification differs.

- [ ] **Step 5: Extend privacy checks**

Assert that no private-key marker, private-key filename, release-cache payload, username, or machine path appears in tracked/public output, while the public key is present exactly once.

- [ ] **Step 6: Run and commit the contract**

Run: `.\.venv\Scripts\python.exe tests\smoke_update_metadata.py`

Run: `.\.venv\Scripts\python.exe tests\smoke_public_release.py`

Expected: both pass.

Commit: `feat: define signed update metadata`

### Task 2: Bounded Update Client And Atomic Cache

**Files:**
- Create: `momento/updater/client.py`
- Create: `momento/updater/cache.py`
- Create: `tests/smoke_update_client.py`
- Modify: `momento/updater/__init__.py`

**Interfaces:**
- Consumes: `UpdateManifest.from_signed_bytes(...)` and `update_cache_dir()`.
- Produces: `UpdateClient.check(current_version: str) -> UpdateResult`.
- Produces: `UpdateCache.load_verified(current_version: str) -> StagedUpdate | None` and `stage(manifest: UpdateManifest, chunks: Iterable[bytes]) -> StagedUpdate`.
- `UpdateResult` distinguishes `CURRENT`, `AVAILABLE`, and `FAILED`; `StagedUpdate` contains the verified manifest and absolute installer path.

- [ ] **Step 1: Write a local fake-release test server**

Exercise a valid latest release, current release, draft/prerelease response, explicit approved/denied redirects, timeout, non-200 response, duplicate/missing assets, oversized metadata/signature, truncated installer, oversized installer, wrong hash, monotonic replay/expiry/clock rollback, concurrent callers, stale partial files, stale versions, reparse/hard-link substitution, launch-time mutation, and a successful atomic stage.

- [ ] **Step 2: Prove the client tests fail**

Run: `.\.venv\Scripts\python.exe tests\smoke_update_client.py`

Expected: import failure for `momento.updater.client`.

- [ ] **Step 3: Implement the GitHub client**

Fetch `https://api.github.com/repos/Vanexia/momento/releases/latest` with fixed connect/read timeouts, a Momento user agent, bounded streaming, and no credentials. Require a published stable `v{version}` release and exact unique assets named `Momento-update.json`, `Momento-update.json.sig`, and `MomentoSetup-{version}.exe`.

- [ ] **Step 4: Implement the cache**

Write only Momento-owned `.partial`, manifest, signature, installer, and trust-state names inside a local non-reparse cache root; flush and `fsync` before `os.replace`; persist highest-seen metadata version/trusted time; reverify signature, freshness, version, size, hash, link count, and file identity whenever loading or launching; retain one valid newer version; and remove only stale files matching the owned naming policy.

- [ ] **Step 5: Run and commit the client**

Run: `.\.venv\Scripts\python.exe tests\smoke_update_client.py`

Expected: all update-client scenarios pass with no test-server thread left alive.

Commit: `feat: download and stage verified updates`

### Task 3: Recording-Safe Update Orchestration

**Files:**
- Create: `momento/updater/service.py`
- Create: `momento/updater/handoff.py`
- Create: `tests/smoke_update_lifecycle.py`
- Modify: `momento/core/session.py`
- Modify: `momento/util/single_instance.py`
- Modify: `momento/__main__.py`
- Modify: `momento/ui/tray.py`

**Interfaces:**
- Consumes: `UpdateClient`, `UpdateCache`, and `StagedUpdate`.
- Produces: `SessionManager.acquire_update_quiescence() -> UpdateQuiescence | None`, which atomically blocks starts and proves recorder, starter, and finalizer inactivity.
- Produces: `UpdateService.check_once(*, manual: bool = False) -> bool`, `status_changed(str)`, and `install_requested(StagedUpdate)`.
- Produces: `launch_update(installer: Path) -> subprocess.Popen`, invoking Setup with `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /MOMENTOUPDATE`.

- [ ] **Step 1: Write lifecycle tests**

Cover one automatic check per process, manual/automatic request coalescing, staged-before-network priority, current/offline/invalid results, install while atomically quiescent, deferral during onboarding/modal/repair/trim/upload/recorder startup/recording/finalization, a game-start race, starter/finalizer timeout, no installation later in the same busy process, durable attempt backoff/quarantine, Setup readiness before mutex release, exact-parent wait, and token-bound relaunch confirmation.

- [ ] **Step 2: Prove lifecycle tests fail**

Run: `.\.venv\Scripts\python.exe tests\smoke_update_lifecycle.py`

Expected: missing service and update-busy API failures.

- [ ] **Step 3: Add one authoritative busy predicate**

Set an update-quiescing flag under the session lock before stopping the watcher. Clear deferred starts, cancel and join startup, wait for finalization, and return no lease if inactivity cannot be proven. Do not infer safety from tray status text, an earlier snapshot, or only `is_recording`.

- [ ] **Step 4: Implement the Qt service**

Run the synchronous client on one worker, marshal completion through Qt signals, suppress automatic error UI, expose manual status, and mark a busy-process staged update as deferred until a future launch.

- [ ] **Step 5: Refactor bootstrap into an explicit handoff**

Preserve normal startup and first-run behavior. Begin automatic checks only after onboarding and startup work are registered. On install: obtain application and session quiescence, lock and reverify the installer, create durable attempt state, spawn Setup while `SingleInstance` is still held, wait for Setup's update gate/readiness, then close playback/windows, quit Qt, shut down hotkeys/session, release the mutex, and exit. Abort and resume monitoring if Setup readiness is not proven.

- [ ] **Step 6: Run adjacent lifecycle checks and commit**

Run: `.\.venv\Scripts\python.exe tests\smoke_update_lifecycle.py`

Run: `.\.venv\Scripts\python.exe tests\smoke_single_instance.py`

Run: `.\.venv\Scripts\python.exe tests\smoke_window_retry.py`

Run: `.\.venv\Scripts\python.exe tests\smoke_close_to_tray_pauses.py`

Expected: all pass.

Commit: `feat: install updates only while recording is idle`

### Task 4: About And Updates Settings Page

**Files:**
- Modify: `momento/ui/settings_dialog.py`
- Modify: `momento/ui/editor.py`
- Modify: `momento/ui/tray.py`
- Modify: `momento/ui/icons.py`
- Create: `tests/smoke_update_settings.py`
- Modify: `tests/smoke_settings.py`
- Modify: `tests/smoke_worker_lifecycle.py`

**Interfaces:**
- Consumes: `UpdateService.check_once(manual=True)` and `status_changed(str)`.
- Produces: an `About & Updates` navigation page displaying `Momento {__version__}`, a `Check for updates` button, and one accessible status line.

- [ ] **Step 1: Write UI behavior tests**

Assert the page is keyboard reachable, reports the installed version, disables the button during a check/download, prevents duplicate workers, shows current/download/install/failure states, survives panel teardown, and leaves ordinary Settings save/cancel behavior unchanged.

- [ ] **Step 2: Prove the UI tests fail**

Run: `.\.venv\Scripts\python.exe tests\smoke_update_settings.py`

Expected: the page and injected service contract are absent.

- [ ] **Step 3: Build and wire the page**

Pass the service from bootstrap through tray and editor to `SettingsPanel`; use the existing settings layout, button, icon, focus, and text styles; and keep automatic errors out of the page until a user initiates a manual check.

- [ ] **Step 4: Run UI regressions and commit**

Run: `.\.venv\Scripts\python.exe tests\smoke_update_settings.py`

Run: `.\.venv\Scripts\python.exe tests\smoke_settings.py`

Run: `.\.venv\Scripts\python.exe tests\smoke_settings_save.py`

Run: `.\.venv\Scripts\python.exe tests\smoke_worker_lifecycle.py`

Expected: all pass under `QT_QPA_PLATFORM=offscreen` where supported.

Commit: `feat: add manual update status to settings`

### Task 5: Silent Inno Setup Upgrade Contract

**Files:**
- Modify: `build/installer.iss`
- Modify: `tests/smoke_installer_contract.py`
- Modify: `tests/smoke_installed_release.ps1`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: `/MOMENTOUPDATE` from `launch_update()`.
- Produces: a silent update run that creates the update gate, waits on the exact live parent handle, preserves per-user settings/data, writes a setup log, avoids destructive pre-copy deletion, and relaunches `{app}\Momento.exe --updated=<token>` only after successful installation.

- [ ] **Step 1: Add failing installer-contract assertions**

Require the update marker, unchanged `AppId` and per-user path, `CloseApplications=no`, `RestartApplications=no`, no force-close task, a successful-update-only relaunch entry, and preservation of the current autostart value.

- [ ] **Step 2: Implement the update-specific Inno path**

Retain normal interactive install behavior. For `/MOMENTOUPDATE`, skip user choices, preserve files and data, log setup, relaunch with `--updated`, and never override a recording-held mutex.

- [ ] **Step 3: Exercise the contract and commit**

Run: `.\.venv\Scripts\python.exe tests\smoke_installer_contract.py`

Expected: all installer contract checks pass.

Commit: `feat: support unattended updater handoff`

### Task 6: Qt Runtime Pruning

**Files:**
- Modify: `build/pyinstaller.spec`
- Modify: `scripts/build_installer.ps1`
- Create: `tests/smoke_qt_bundle_contract.py`
- Modify: `tests/smoke_public_release.py`
- Modify: `build/corresponding_sources.json`

**Interfaces:**
- Produces: a bundle with no Qt translation catalogs or `qgif`, `qicns`, `qtga`, `qtiff`, `qwbmp`, or `qwebp` plugin, while retaining `qjpeg`, `qsvg`, `qico`, `qwindows`, both multimedia integrations, TLS, and `opengl32sw.dll`.

- [ ] **Step 1: Write allowlist/rejection tests**

Test both the spec transformation and a built bundle. Assert every required DLL/plugin exists and every approved removal is absent; reject Qt PDF and OpenCV as before.

- [ ] **Step 2: Prove the current bundle violates the new contract**

Run: `.\.venv\Scripts\python.exe tests\smoke_qt_bundle_contract.py dist\Momento`

Expected: failure listing translation files and unused image plugins.

- [ ] **Step 3: Filter PyInstaller tables and add hard release gates**

Match complete normalized destination paths rather than broad substrings, so no similarly named required library can be removed. Update corresponding-source inputs only when the shipped binary/source relationship genuinely changes.

- [ ] **Step 4: Build a temporary application bundle and test media/UI assets**

Run PyInstaller to a temporary output, then run the Qt contract, editor/settings smoke tests, thumbnail generation, SVG icon rendering, JPEG loading, and QMediaPlayer playback against that bundle.

- [ ] **Step 5: Commit Qt pruning**

Commit: `build: remove unused Qt translations and image plugins`

### Task 7: Minimized PyAV FFmpeg Runtime

**Files:**
- Create: `build/pyav_runtime.json`
- Create: `scripts/build_pyav_runtime.ps1`
- Create: `scripts/build_pyav_runtime.sh`
- Create: `scripts/verify_pyav_runtime.py`
- Create: `tests/smoke_pyav_runtime_contract.py`
- Modify: `scripts/build_installer.ps1`
- Modify: `constraints-release.txt`
- Modify: `requirements-release-hashed.txt`
- Modify: `build/corresponding_sources.json`
- Modify: `BUILD_INFO.txt`
- Modify: `THIRD_PARTY_NOTICES.txt`
- Modify: `CLAUDE.md`

**Interfaces:**
- Produces: a CPython 3.12 Windows x64 PyAV 17.0.1 wheel built against pinned FFmpeg 8.0.1 shared libraries.
- Produces: `scripts/verify_pyav_runtime.py <wheel-or-site-packages>` which checks wheel hash, PE imports, exact DLL allowlist, FFmpeg/PyAV versions, codecs, formats, filters, hardware encoder names/options, and licences.
- The release environment installs this verified local wheel separately, then installs every other dependency from the existing hash-locked binary list.

- [ ] **Step 1: Write a failing runtime contract from actual Momento use**

Require imports plus H.264 decode; AAC decode/encode; Matroska/MP4 mux/demux; `h264_nvenc`, `h264_amf`, `h264_qsv`, `h264_mf`, and `libx264`; `scale`, `format`, `aresample`, and audio mixing filters; ndarray video conversion; audio resampling; encoder option introspection; and rejection of x265, SVT-AV1, dav1d, VPX, WebP, Vorbis, Opus, LAME, OpenH264, and AMR DLLs.

- [ ] **Step 2: Prove the stock wheel fails only the size/dependency allowlist**

Run: `.\.venv\Scripts\python.exe tests\smoke_pyav_runtime_contract.py .\.venv\Lib\site-packages`

Expected: functional checks pass and forbidden general-purpose codec DLLs are reported.

- [ ] **Step 3: Pin all build inputs and configure minimal FFmpeg**

Use the existing `tmp\ffmpeg-build-tools\msys64` UCRT64 toolchain. Disable programs, docs, network, autodetection, and all components, then explicitly enable the shared FFmpeg libraries and Momento-required protocols, formats, codecs, parsers, bitstream filters, filters, scaling/resampling, NVENC headers, AMF, QSV/oneVPL, Media Foundation, and GPL libx264. Keep source URLs and SHA-256 values in `build/pyav_runtime.json`.

- [ ] **Step 4: Build PyAV and repair the wheel deterministically**

Build PyAV 17.0.1 against the pinned prefix, bundle only transitive DLLs from the allowlist, normalize archive timestamps/order where the wheel format permits, record the resulting SHA-256, and fail if a second clean build differs. Do not patch or delete dependencies after import linking.

- [ ] **Step 5: Run functional, fallback, and binary-import verification**

Run the PyAV runtime contract, encoder portability suite, mock AMD/Intel/MF fallback checks, recorder/encoder tests, trim/repair tests, and PE dependency inspection in an isolated environment containing only the custom wheel and release dependencies.

- [ ] **Step 6: Integrate the wheel into release builds**

Make the release builder build or reuse only an exact hash-matching local artifact, install it without dependencies, reject stock `av.libs`, and include all retained native licences and corresponding source. A custom-wheel failure must stop the release; it must never fall back to the larger stock wheel silently.

- [ ] **Step 7: Commit the minimized runtime**

Commit: `build: ship a minimized PyAV runtime`

### Task 8: Release Metadata, Checksums, And Full Verification

**Files:**
- Modify: `scripts/build_installer.ps1`
- Modify: `tests/smoke_public_release.py`
- Modify: `tests/smoke_source_archive.py`
- Modify: `README.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: the final installer and external private update key.
- Produces: `Momento-update.json`, `Momento-update.json.sig`, source archives, helper/runtime provenance, installer hash, and one `SHA256SUMS-0.2.2.txt` covering every published asset.

- [ ] **Step 1: Extend release tests before packaging changes**

Require exact metadata/signature names, valid public-key verification, a manifest matching the final installer bytes, checksums for every asset, no signing private key, no runtime cache, and no removed Qt/PyAV component in the bundle or archives.

- [ ] **Step 2: Integrate signing after installer creation**

Generate metadata only after the final installer is immutable, verify it independently, then generate the complete checksum list. Fail before declaring release output when signing material is unavailable or any property differs.

- [ ] **Step 3: Run all hardware-independent checks**

Run compileall, Ruff `F,E9`, `pip check`, `pip-audit`, every `smoke_*.py` test applicable to the host, release/source/privacy/history checks, updater integration tests, Qt/PyAV contracts, and installer contract tests.

- [ ] **Step 4: Run physical Windows checks**

Run WASAPI endpoint/open/cleanup tests, WGC capture, a real local NVENC encode and decode, microphone plus loopback recording, QMediaPlayer playback, timestamps, and no leaked process/thread checks. Keep the AMD limitation documented as physical-hardware residual risk.

- [ ] **Step 5: Commit verified release preparation**

Commit: `release: prepare authenticated compact 0.2.2 build`

### Task 9: Final Build, Isolated Upgrade, And User Test Launch

**Files:**
- Generated: `dist/Momento/`
- Generated: `dist/installer/MomentoSetup-0.2.2.exe`
- Generated: `dist/installer/Momento-update.json`
- Generated: `dist/installer/Momento-update.json.sig`
- Generated: `dist/source/*.zip`
- Generated: `dist/SHA256SUMS-0.2.2.txt`

**Interfaces:**
- Produces: the final locally verified candidate and a publication-ready asset set; no GitHub mutation occurs in this task.

- [ ] **Step 1: Stop only Momento processes after confirming no recording is active**

Terminate the currently installed/test process cleanly, confirm no `Momento.exe`, source `python -m momento`, Setup, mock game, or test helper remains, and leave unrelated user applications alone.

- [ ] **Step 2: Build once from a clean committed tree**

Run: `.\scripts\build_installer.ps1`

Expected: signed metadata, installer, all source/provenance assets, checksums, and zero failed gates.

- [ ] **Step 3: Measure the result**

Compare installer and installed bytes/file counts with the 60.3 MiB and 226.8 MiB baselines, list the largest remaining components, and confirm no quality/encoder/playback feature was removed.

- [ ] **Step 4: Exercise a real isolated silent update**

Install the prior candidate in the isolated profile, seed settings and a recording, serve signed newer metadata locally through the test override, launch the old app, wait for silent Setup/relaunch, and confirm the new version, preserved settings/recording/autostart, one running instance, and no update loop.

- [ ] **Step 5: Install and launch the final candidate for the user**

Restore the real profile and installed registration, install the final build, launch it, verify the tray/editor/version/update page, and close all temporary windows/processes.

- [ ] **Step 6: Prepare but do not execute GitHub publication**

Refresh the sanitized-public-history candidate, prove its tree equals the reviewed release commit and contains no private data, list the exact release replacement/upload operations, and stop for explicit approval before any force-push, release deletion, tag replacement, or asset upload.
