# Momento Current Handoff

Last updated: 2026-08-20

Codex is the main project driver. This is the canonical repository handoff.
Trust source and executable checks over prose if they ever disagree.

## Product

Momento is a local-first Windows tray application that automatically records a
game window, microphone, and selected playback endpoint. It writes
crash-tolerant MKV recordings and includes playback, bookmarks, clip export,
repair, library management, storage controls, and optional YouTube upload using
a Google Cloud project supplied by the user.

Product boundaries:

- Windows 10 1903 or newer and Windows 11, x64 only.
- No Momento account, telemetry, analytics, or cloud library.
- Recordings stay local unless the user configures YouTube and confirms an
  upload.
- No replay buffer, streaming, webcam, HDR, or live recording preview.
- System audio captures the selected playback endpoint's mix, not isolated
  per-application audio.

## Current Release

- Version: `0.2.6` release candidate.
- License: GPL-3.0-only. The installer includes the GPL text, build information,
  third-party notices/licenses, and an exact source offer. Matching Momento and
  third-party source archives are separate assets on the same GitHub release.
- The 0.2.5 public build includes the Google upload runtime and setup UI but no
  OAuth identity. Users import a Desktop OAuth JSON from their own Google Cloud
  project. The build rejects the old `MOMENTO_INCLUDE_YOUTUBE_OAUTH` profile
  and never bundles `resources/youtube/client_secrets.json`.
- Public installer: per-user Inno Setup package under
  `dist/installer/MomentoSetup-0.2.6.exe`.
- Version 0.2.6 blocks Steam storefront trailers, embedded launcher browsers,
  gaming overlays, and launcher-only executables from automatic detection.
  Version 0.2.5 added user-owned Google OAuth setup, share-safe diagnostics,
  upload input bounds, and public-release hardening. Version 0.2.4 centres
  update-result dialogs, including native
  Windows frame offsets, and repairs the Windows
  CI dependency-install step. Version 0.2.2 hardened disk/output failures,
  capture recovery, stalled audio,
  sustained frame-loss reporting, storage ownership, trim/repair locking,
  settings accessibility, signed updates, privacy scans, and reproducible
  release packaging.
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

## Updates

- Installed Windows builds check the latest stable GitHub Release once in a
  background worker each time the process starts. Momento has no periodic
  update timer, background service, scheduled task, or prerelease channel.
- GitHub receives the request's source IP address and fixed
  `Momento-Updater/1` User-Agent. Momento sends no account, recording,
  game-list, or device data with the check.
- Users can also run the same check from the tray menu or from **Help > Check
  for updates** in the editor. Momento coalesces concurrent automatic and
  manual requests into one check. Interactive results are owned by the visible
  editor so Windows centres them over Momento.
- Every accepted release must include `Momento-update.json`,
  `Momento-update.json.sig`, and the versioned installer. Momento verifies the
  canonical metadata with the Ed25519 public key embedded in the application,
  then verifies the installer size and SHA-256 before staging it.
- The updater accepts only newer stable releases from the expected GitHub
  repository and release-asset hosts. Metadata has a monotonic version and an
  expiry time; `trusted-state.json` prevents signed metadata replay and clock
  rollback.
- Installation starts only after Momento atomically stops monitoring and
  proves that recording startup, recording, finalization, repair, trim, upload,
  authentication, and modal/editor work are idle. If the app is busy, the
  verified installer remains staged and Momento tries again on the next launch.
- Setup waits for the exact running Momento process to exit, preserves user
  data, installs silently, and relaunches the new version with a one-use attempt
  token. Ordinary launches remain blocked throughout the handoff.
- Update attempts persist across restarts with bounded retry delays. Momento
  quarantines the same failed installer after three attempts; a newer signed
  release can supersede it. The new process must confirm the exact attempt token
  and target version before the staged payload is removed.
- Source and editable Python runs do not perform automatic network checks or
  self-update. Their manual command reports that update checks are available
  only in the installed build.

## Technology

- Python 3.12, PyQt6, PyInstaller one-folder release, and Inno Setup.
- `psutil` plus Win32 APIs for process, foreground, and fullscreen detection.
- `windows-capture` for per-window Windows Graphics Capture video.
- PyAudioWPatch/PortAudio WASAPI for microphone and playback-loopback audio.
- PyAV/libav for live H.264 encoding and MKV muxing. Public releases use
  Momento's reproducible PyAV 17.0.1 wheel and minimal FFmpeg 8.0.1 runtime.
- Encoder order: NVIDIA NVENC, AMD AMF, Intel QuickSync, Media Foundation,
  then libx264. Selection probes the requested resolution, frame rate, and
  quality options. Recorder startup opens the full stream and encodes one frame
  before it publishes the recording.
- A reproducible minimal FFmpeg/ffprobe 8.1.2 helper handles trim, thumbnail,
  metadata, and repair. It has no network support or external DLL dependency.
- Google API clients support explicit YouTube uploads after the user imports a
  Desktop OAuth client. DPAPI encrypts the imported client and account token as
  separate AppData files.

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
7. A window that closes during encoder startup poisons the pending start, so a
   capture that has already ended can never be published as live.
8. Disk/output failures are classified before encoder fallback. A full or lost
   drive cannot demote a healthy GPU backend or trigger a backend retry loop.
9. Repeated audio encode/mux failures become fatal after a short threshold;
   isolated bad frames remain recoverable and a healthy frame resets the count.
10. Starts during another start/finalization are deferred and retried when idle.
11. Finished MKVs enter the editor without replacing the current selection.

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
  Only verified Momento-owned files are eligible. Repair preserves the original
  unless a replacement with the expected container, streams, codec, duration,
  and game metadata has been validated.
- Output-folder migration skips active, trimming, and repairing media.
- Output-folder changes pause monitoring before commit, cancel and drain any
  unpublished start, apply the move synchronously, then restore monitoring.
- Bookmark sidecars are atomic and serialized per recording.
- Storage quotas delete only media carrying a verified Momento ownership
  marker. Legacy files can acquire a marker only after embedded metadata is
  validated; replaced or forged markers are rejected.

## Security And Privacy

- Normal logs avoid full output paths, audio endpoint identifiers, channel IDs,
  and account names. Detailed diagnostics should be explicitly gated if added.
- Runtime config, logs, locks, window state, imported OAuth clients, OAuth
  tokens, avatars, recordings, and thumbnails are never copied into the public
  bundle or source archive.
- Release dependencies are pinned in `constraints-release.txt`, and every
  approved CPython 3.12 Windows x64 wheel is SHA-256 locked in
  `requirements-release-hashed.txt`. CI runs static checks and executable
  regressions; release verification also runs `pip check` and `pip-audit`.
- YouTube avatar downloads accept HTTPS only, cap payloads, and publish caches
  atomically.
- FFmpeg downloads are pinned and hash-verified by `scripts/fetch_ffmpeg.ps1`.
- `build/corresponding_sources.json` pins 17 native source inputs. The release
  builder creates and re-verifies a deterministic third-party source bundle.
- The source set covers the minimal helper; the custom PyAV runtime's PyAV,
  FFmpeg, x264, NVIDIA, AMD, oneVPL, GCC, and winpthreads inputs; PyQt/SIP; Qt
  Base/SVG/Multimedia/Translations; and Qt Multimedia's FFmpeg and zlib inputs.
  Unused OpenCV and Qt PDF binaries are rejected from the bundle.
- The custom PyAV wheel is 6,636,889 bytes with SHA-256
  `fd605ec4ab782c3829bfb4f11c512d7db3bc230d0f889a28f29aab0fb793bf3b`.
  Its native DLL payload is 13,058,674 bytes instead of the general-purpose
  wheel's 67,351,552 bytes, an 80.6% reduction. Two clean builds matched
  byte-for-byte.
- Packaged builds also omit Qt translation catalogs and the unused GIF, ICNS,
  TGA, TIFF, WBMP, and WebP image plugins. Required Windows, multimedia, TLS,
  JPEG, SVG, ICO, and software OpenGL support remains bundled. This Qt pruning
  removes another 7.88 MiB.
- CI, Pages, and the public builder scan every reachable Git object plus commit
  and tag identities for private development data.
- Installer upgrades are blocked by the named mutex
  `Momento.GameRecorder.Instance`; setup must never force-stop a recording.
- Uninstall removes the Windows startup entry, preserves user data by default,
  and never deletes recordings or clips. `/PURGEUSERDATA` removes only Momento's
  AppData state.
- GitHub private vulnerability reporting is the security-reporting channel.
  Public issues must not contain OAuth files, tokens, unredacted logs, or other
  sensitive evidence.

### Public GitHub privacy rules

- Prevent exposure before the first public push. Use the approved GitHub
  no-reply identity for commit and tag metadata, and keep personal paths,
  account details, logs, credentials, OAuth clients, and local agent files out
  of tracked content.
- Audit the complete publication surface. Scan reachable Git objects,
  commit/tag identities, source and release archives, screenshots and their
  metadata, release notes, Actions records, deployment records, and Pages
  content before sharing the repository.
- Run `tests/smoke_git_history_privacy.py` from a fresh anonymous clone. A
  passing working-tree grep does not prove that history, tags, pull-request
  refs, or hosted metadata are clean.
- Treat an ignored local file as private runtime state. Exclude it from build
  inputs. Packaging must fail closed if it encounters an OAuth identity, token,
  private signing key, user-profile path, or developer-only workspace material.
- GitHub can retain managed pull-request refs, cached object views, workflow
  metadata, and deployment history after a branch/tag rewrite. Remove all
  owner-controlled references, then ask GitHub Support to dereference affected
  pull requests, clear caches, and run server-side garbage collection.
- After a history rewrite, discard or quarantine old clones. Re-clone the clean
  graph. A merge or push from a stale clone can restore removed objects.
- Rotate an exposed credential before historical cleanup. Deleting Git objects
  leaves the secret usable by anyone who copied it during the exposure.

## Runtime Data

- Config: `%APPDATA%\Momento\config.json`
- Logs: `%APPDATA%\Momento\logs\momento.log` and rotating backups
- Window state: `%APPDATA%\Momento\window_state.ini`
- Lock: `%APPDATA%\Momento\momento.lock`
- YouTube token: `%APPDATA%\Momento\youtube_token.dat` (DPAPI encrypted)
- YouTube OAuth client: `%APPDATA%\Momento\youtube_oauth_client.dat` (DPAPI
  encrypted; imported from a user-owned Google Cloud project)
- YouTube avatar: `%APPDATA%\Momento\youtube_avatar.png`
- Update cache: `%LOCALAPPDATA%\Momento\updates`
- Update trust and retry state: `trusted-state.json` and `attempt-state.json`
  inside the update cache; verified metadata, signatures, and a staged installer
  also live there until confirmation or replacement
- Recordings: configured output folder, default Windows Videos known folder
  plus `Momento`
- Clips: `<output>\clips`
- Bookmarks: `<media>.bookmarks.json`
- Thumbnails: `<media>.thumb.jpg`
- Ownership markers: `<media>.momento.json`

## Module Map

- `momento/__main__.py`: bootstrap, onboarding policy, tray/session wiring,
  recovery, and storage checks.
- `momento/config.py`: validated configuration and atomic JSON persistence.
- `momento/core/`: watcher, recording, WGC, WASAPI, encoding, repair, metadata,
  thumbnails, and storage.
- `momento/ui/`: tray, editor, settings, onboarding, playback, and timeline.
- `momento/youtube/`: OAuth client validation and DPAPI storage, account auth,
  avatar caching, and resumable upload support.
- `momento/updater/`: signed metadata policy, GitHub client, trusted cache,
  durable attempts, application quiescence, and Windows installer handoff.
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
.\.venv\Scripts\python.exe tests\smoke_update_metadata.py
.\.venv\Scripts\python.exe tests\smoke_update_release_tools.py
.\.venv\Scripts\python.exe tests\smoke_update_client.py
.\.venv\Scripts\python.exe tests\smoke_update_service.py
.\.venv\Scripts\python.exe tests\smoke_update_runtime.py
.\.venv\Scripts\python.exe tests\smoke_encoder_portability.py
.\.venv\Scripts\python.exe tests\smoke_youtube_client_config.py
.\.venv\Scripts\python.exe tests\smoke_youtube_auth_config.py
.\.venv\Scripts\python.exe tests\smoke_youtube_setup.py
.\.venv\Scripts\python.exe tests\smoke_pyav_runtime_contract.py dist\pyav-runtime\av-17.0.1-cp311-abi3-win_amd64.whl
```

Rebuild the release PyAV runtime when its source, toolchain, or capability
contract changes:

```powershell
.\scripts\build_pyav_runtime.ps1
```

The script verifies every pinned input and tool version, builds twice, requires
identical wheel hashes, and runs the isolated runtime contract before
publishing the wheel under `dist\pyav-runtime`.

Build only from a clean committed tree, with Momento not running:

```powershell
.\scripts\build_installer.ps1
```

The release output is the installer, the Momento source archive, the
deterministic third-party source bundle, the reviewed helper archive, and one
SHA-256 list covering every asset. It also includes the signed update metadata
and detached signature. Do not distribute only `Momento.exe`.

### Update Release Signing

- `resources/update_public_key.pem` is the verification key embedded in every
  installed build. Do not rotate it casually: existing installations can only
  trust releases signed by its matching private key.
- Keep the Ed25519 private key outside the repository. The release tools use
  `%LOCALAPPDATA%\MomentoRelease\update-signing-key.pem` by default or the path
  in `MOMENTO_UPDATE_SIGNING_KEY`. Never add the private key to a commit,
  source archive, installer, log, or release asset; maintain a protected backup.
- `scripts/manage_update_key.py` creates or validates the private key, restricts
  its local permissions, and verifies that it matches the tracked public key.
- `scripts/build_installer.ps1` requires the matching private key and fails if
  the key is missing, the signature cannot be verified, or the installer no
  longer matches the signed size and SHA-256.
- Each stable `vX.Y.Z` GitHub Release must publish the exact versioned installer,
  `Momento-update.json`, and `Momento-update.json.sig` produced by the same
  build, along with the checksum and corresponding-source assets. Keep
  `metadata_version` strictly increasing. Schema 1 releases must remain
  compatible with updater version `0.2.2`.

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

The 2026-08-17 release audit added exact-profile encoder probes, synchronous
production-stream startup, backend fallback, failed-backend demotion, output
failure classification, startup capture-close handling, audio encode failure
escalation, pending-start cancellation, validated trim/repair publication,
owned-only recovery, serialized output migration, and a reproducible minimal
PyAV runtime. The runtime contract verifies the retained codecs, formats,
filters, encoder options, software H.264/AAC encode, and MKV/MP4 round trips.
The AMD mock covers AMF selection at 1440p60, production-open fallback,
driver-failure demotion, and the bundled encoder/options contract.

The final local physical pass enumerated five microphone endpoints and three
playback endpoints, completed 21/21 WASAPI checks with no leaked threads,
captured WGC frames, and encoded 600/600 2560x1440 frames at 60 fps through
NVENC with zero drops. A separate real 1202x714 60 fps WGC recording with
microphone and loopback audio encoded 301/301 frames with zero drops, decoded
cleanly in PyAV and QMediaPlayer, kept strictly increasing video DTS, and ended
its audio/video streams 61 ms apart.

The existing 0.2.1 installer also passed an isolated per-user install, fresh
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
- A Google project in External Testing expires refresh tokens after seven days
  when Momento's YouTube scopes are present. Users must reconnect or complete
  the applicable production and verification process.
- Google restricts uploads from unaudited API projects created after 28 July
  2020 to private visibility until the project passes an audit.
- The package is unsigned. Code signing would remove the Unknown Publisher
  warning, but it requires a paid certificate. One clean Windows VM test remains
  useful field evidence; Windows Sandbox is not available on the current machine.
