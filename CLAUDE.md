# Momento Current Handoff

Last updated: 2026-08-17

Codex is the main project driver. This is the canonical repository handoff.
Trust source and executable checks over prose if they ever disagree.

## Product

Momento is a local-first Windows tray application that automatically records a
game window, microphone, and selected playback endpoint. It writes
crash-tolerant MKV recordings and includes playback, bookmarks, clip export,
repair, library management, storage controls, and an optional distributor-only
YouTube upload integration.

Product boundaries:

- Windows 10 1903 or newer and Windows 11, x64 only.
- No Momento account, telemetry, analytics, cloud library, or auto-updater.
- Recordings stay local unless a build deliberately enables YouTube and the
  user explicitly uploads a file.
- No replay buffer, streaming, webcam, HDR, or live recording preview.
- System audio captures the selected playback endpoint's mix, not isolated
  per-application audio.
- No C++ or other native-language rewrite is planned.

## Current Release

- Version: `0.2.1`.
- License: GPL-3.0-only. The installer includes Momento source, the GPL text,
  build information, and third-party notices/licenses.
- Public builds exclude `resources/youtube/client_secrets.json` unless
  `MOMENTO_INCLUDE_YOUTUBE_OAUTH=1` is explicitly set for an approved OAuth
  identity. The unavailable YouTube UI is hidden.
- Public installer: per-user Inno Setup package under
  `dist/installer/MomentoSetup-0.2.1.exe`.
- Version 0.2.1 adds exact-profile encoder qualification, production-stream
  fallback, failed-backend demotion, CPU fallback warnings, and WGC recovery.
- Installed location: `%LOCALAPPDATA%\Programs\Momento`.
- The installer and executable are unsigned until a code-signing certificate
  is obtained, so Windows may show an Unknown Publisher or SmartScreen warning.

## New-User Behavior

- First-run completion is stored explicitly as `setup_complete`; closing setup
  does not mark it complete and setup returns on the next launch.
- Game monitoring remains paused until setup finishes successfully.
- Setup prefers the actual Windows default microphone and playback endpoint,
  validates that selected devices can open, and asks for explicit confirmation
  before continuing without an audio leg.
- The output folder uses Windows' redirected Videos known folder and must be
  writable before setup can finish.
- Fresh capture defaults are source resolution, fixed 60 fps, and Custom
  16,000 kbit/s. Expect roughly 7.2 GB/hour plus audio/container overhead.
- Autostart is opt-in and the setup wizard applies the Windows Run entry.
- After setup, monitoring follows the user's choice and the editor opens.

## Technology

- Python 3.12, PyQt6, PyInstaller one-folder release, and Inno Setup.
- `psutil` plus Win32 APIs for process, foreground, and fullscreen detection.
- `windows-capture` for per-window Windows Graphics Capture video.
- PyAudioWPatch/PortAudio WASAPI for microphone and playback-loopback audio.
- PyAV/libav for live H.264 encoding and MKV muxing.
- Encoder order: NVIDIA NVENC, AMD AMF, Intel QuickSync, Media Foundation,
  then libx264. Selection probes the requested resolution, frame rate, and
  quality options. Recorder startup opens the full stream and encodes one frame
  before it publishes the recording.
- Bundled FFmpeg/ffprobe 8.1.2 handles trim, thumbnail, metadata, and repair.
- Optional Google API clients support explicit YouTube uploads. User tokens are
  encrypted with Windows DPAPI.

## Recording Lifecycle

1. `GameWatcher` polls configured games and can optionally inspect a sustained
   foreground fullscreen window. Known non-games are blocked from fallback.
2. Processes are identified by `(pid, create_time)`, not PID alone.
3. `SessionManager` waits for a usable game window on a background thread and
   retries no-window failures while the same process remains alive.
4. WGC settles an even capture size and retains an owned startup frame so a
   static window can be recorded immediately.
5. Video, microphone, and system audio feed bounded encoder queues. Configured
   audio failures produce a visible warning; video can continue without it.
6. Encoder failure closes submission gates and routes through finalization.
7. Starts during another start/finalization are deferred and retried when idle.
8. Finished MKVs enter the editor without replacing the current selection.

## Editor, Recovery, And Storage

- Recordings and Clips support search, game filters, sorting, thumbnails,
  playback, repair, rename, delete, reveal, trim, bookmarks, and quotas.
- Live media cannot be previewed, moved, renamed, deleted, repaired, trimmed,
  or uploaded while the encoder owns it.
- Closing to tray pauses playback, releases the decoder after a delay, and
  restores the same selection and playback position when reopened.
- Clip export uses a sibling partial file, validates it, and atomically
  publishes the final MP4. Failure/cancellation removes the partial file.
- Startup scans eligible MKVs for broken metadata and queues stream-copy repair.
  Repair preserves the original unless a validated replacement is ready.
- Output-folder migration skips active, trimming, and repairing media.
- Bookmark sidecars are atomic and serialized per recording.

## Security And Privacy

- Normal logs avoid full output paths, audio endpoint identifiers, channel IDs,
  and account names. Detailed diagnostics should be explicitly gated if added.
- Runtime config, logs, locks, window state, OAuth tokens, avatars, recordings,
  and thumbnails are never copied into the public bundle or source archive.
- Release dependencies are pinned in `constraints-release.txt`; CI runs static
  checks, executable regression programs, `pip check`, and `pip-audit`.
- YouTube avatar downloads accept HTTPS only, cap payloads, and publish caches
  atomically.
- FFmpeg downloads are pinned and hash-verified by `scripts/fetch_ffmpeg.ps1`.
- Installer upgrades are blocked by the named mutex
  `Momento.GameRecorder.Instance`; setup must never force-stop a recording.
- Uninstall removes the Windows startup entry, preserves user data by default,
  and never deletes recordings or clips. `/PURGEUSERDATA` removes only Momento's
  AppData state.

## Runtime Data

- Config: `%APPDATA%\Momento\config.json`
- Logs: `%APPDATA%\Momento\logs\momento.log` and rotating backups
- Window state: `%APPDATA%\Momento\window_state.ini`
- Lock: `%APPDATA%\Momento\momento.lock`
- YouTube token: `%APPDATA%\Momento\youtube_token.dat` (DPAPI encrypted)
- YouTube avatar: `%APPDATA%\Momento\youtube_avatar.png`
- Recordings: configured output folder, default Windows Videos known folder
  plus `Momento`
- Clips: `<output>\clips`
- Bookmarks: `<media>.bookmarks.json`
- Thumbnails: `<media>.thumb.jpg`

## Module Map

- `momento/__main__.py`: bootstrap, onboarding policy, tray/session wiring,
  recovery, and storage checks.
- `momento/config.py`: validated configuration and atomic JSON persistence.
- `momento/core/`: watcher, recording, WGC, WASAPI, encoding, repair, metadata,
  thumbnails, and storage.
- `momento/ui/`: tray, editor, settings, onboarding, playback, and timeline.
- `momento/youtube/`: optional OAuth, avatar, and resumable upload support.
- `momento/trim/ffmpeg_trim.py`: cancellable atomic clip export.
- `momento/util/`: paths, logging, hotkey, autostart, DPAPI, resources, mutex,
  screens, and Win32 helpers.
- `build/pyinstaller.spec`: one-folder application recipe.
- `build/installer.iss`: stable per-user installer recipe.
- `scripts/build_installer.ps1`: clean release, source, privacy, and hash build.
- `tests/`: regression, UI, hardware, packaging, and diagnostic checks.

## Developer Commands

Install the release-compatible environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -c constraints-release.txt -e .[dev]
.\scripts\fetch_ffmpeg.ps1
```

Run source with the editor open:

```powershell
.\.venv\Scripts\python.exe -m momento --show
```

Core checks:

```powershell
.\.venv\Scripts\python.exe -m compileall -q momento tests
.\.venv\Scripts\ruff.exe check momento tests --select F,E9
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\pip-audit.exe --local
.\.venv\Scripts\python.exe tests\smoke_first_run.py
.\.venv\Scripts\python.exe tests\smoke_public_release.py
.\.venv\Scripts\python.exe tests\smoke_installer_contract.py
.\.venv\Scripts\python.exe tests\smoke_encoder_portability.py
```

Build only from a clean committed tree, with Momento not running:

```powershell
.\scripts\build_installer.ps1
```

The release output is the installer, its SHA-256 file, and the corresponding
source archive. Do not distribute only `Momento.exe`.

## Verified Reliability Baseline

The 2026-08-15 full audit covered encoder failure, start/finalize races, owned
WGC frames, watcher restart, explicit empty game lists, orphan cleanup, video
health telemetry, multichannel downmix, migration races, trim/repair atomicity,
upload cancellation, off-thread credential refresh, bounded avatar downloads,
preview callbacks, and dead-code removal.

Hardware evidence included real 1440p60 NVENC, WGC, microphone, system-loopback,
and sustained dual-audio recordings with strict timestamps and clean decode.
The observed High-preset storage use exceeded 10 GB/hour, which is why 0.2.0
uses a fixed 16 Mbit/s public default.

The 2026-08-17 portability pass added exact-profile encoder probes, synchronous
production-stream startup, backend fallback, failed-backend demotion, CPU
fallback warnings, and WGC termination recovery. The AMD mock covers AMF
selection at 1440p60, production-open fallback, driver-failure demotion, and the
bundled encoder/options contract. A real 1202x714 60 fps NVENC recording with
microphone and loopback audio encoded 301 frames with no drops; A/V end times
differed by 62 ms and DTS values increased strictly.

The existing 0.2.0 installer also passed an isolated per-user install, fresh
profile, bundled-tool, animated mock-game recording, same-version upgrade,
default uninstall, and data-preservation cycle. The test backed up and restored
the real uninstall entry, shortcuts, autostart value, settings, and recordings.

## Remaining Risks

- AMD AMF still needs a physical AMD GPU test. The bundled libav exposes AMF,
  its production options match AMD's current contract, and deterministic tests
  cover selection and every fallback path, but those checks cannot validate a
  specific Radeon driver or hybrid-GPU adapter choice.
- Intel QSV, Media Foundation, and libx264 need broader field testing.
- Game/GPU-load combinations can still produce scheduler misses; logs report
  missed slots and worst lateness so this is visible.
- Trim is keyframe-accurate because it stream-copies.
- Severely truncated MKVs may be unrecoverable.
- Optional live YouTube upload is not enabled for the standard public build.
- The package is large and unsigned. Code signing and one clean Windows VM test
  with the next patch-release installer remain recommended before wider public
  distribution; Windows Sandbox is not available on the current machine.
