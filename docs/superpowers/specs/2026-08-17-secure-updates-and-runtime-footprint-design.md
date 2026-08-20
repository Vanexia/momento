# Secure Updates And Runtime Footprint Design

## Status

Approved for implementation on 2026-08-17.

## Goals

- Check for a stable Momento update exactly once when the application starts.
- Download, authenticate, install, and relaunch an update without asking the
  user to visit GitHub or operate the installer.
- Never interrupt an active or pending recording.
- Provide a manual **Check for updates** action that downloads and verifies the
  update, then offers **Install now** and **Later**.
- Reduce installed size without changing recording quality, encoder order,
  supported capture formats, playback behavior, or graphics compatibility.

## Non-Goals

- A background service, scheduled task, or periodic update timer.
- Prerelease channels, forced downgrades, delta patches, or remote telemetry.
- Removing Qt's software OpenGL fallback, Qt Multimedia playback support,
  NumPy, hardware encoders, or CPU fallback.
- Operating-system code signing. The existing unsigned-installer disclosure
  remains until a certificate is obtained.

## Decision

Momento will use a small in-process updater backed by public GitHub Releases.
Each release publishes canonical JSON metadata and an Ed25519 signature as
separate assets. The release-signing private key stays outside the repository;
the matching public key is embedded in Momento.

This is preferred over WinSparkle because it adds no updater DLL or separate
UI model, and over MSIX or Winget because those routes do not provide the
required per-launch, unattended update behavior for this unsigned preview.
The protocol is deliberately versioned so it can later move to fuller TUF
metadata without changing the user-facing workflow.

## Update Metadata

`Momento-update.json` is UTF-8 canonical JSON with sorted keys and no
insignificant whitespace. Schema version 1 contains:

- schema version and stable channel
- monotonic metadata version and expiry timestamp
- Momento version and publication timestamp
- installer filename, HTTPS download URL, byte size, and SHA-256
- minimum supported updater version

`Momento-update.json.sig` contains the base64-encoded Ed25519 signature over
the exact JSON bytes. Both files and the installer are included in the release
checksum list. Release packaging fails if the signing key is absent, the
signature cannot be verified with the tracked public key, or any declared
installer property differs from the built installer.

The client accepts only the expected GitHub repository, stable published
releases, HTTPS asset URLs, a newer normalized version, the known schema and
channel, monotonic unexpired metadata, bounded metadata and installer sizes,
an exact Ed25519 signature, and an exact installer SHA-256. A local trust
record remembers the highest signed metadata version and last trusted clock
value, preventing network replay or clock rollback from reviving older or
expired metadata. Schema 1 remains compatible with updater version 0.2.2 so a
future release cannot strand the first updater behind a bridge version.

## Startup Flow

1. Momento starts normally, exposes the tray, and begins monitoring according
   to the saved configuration.
2. One background worker checks GitHub's latest stable release. There is no
   periodic timer.
3. A newer installer downloads to a versioned partial file under Momento's
   local update cache. The worker enforces timeouts, size limits, and a bounded
   streamed response, then verifies and atomically publishes the staged file.
4. If onboarding, recording startup, recording, finalization, repair, trim,
   upload, or another modal workflow is active, the update remains staged.
   Momento installs it automatically at a later launch.
5. Otherwise Momento atomically acquires update quiescence: it blocks new
   watcher callbacks, clears deferred starts, stops monitoring, cancels and
   fully joins pending startup, and proves no finalizer or recorder is active.
   If that proof times out, the update remains staged and Setup never starts.
6. Momento opens the staged installer through a Windows handle that denies
   writes and deletion, rehashes it through that handle, records a bounded
   update attempt, and launches Setup while the application mutex is held.
7. Setup creates an update gate, opens a handle to the still-live parent,
   signals readiness, and waits for that exact process to exit. Momento releases
   its mutex and exits only after readiness. The gate blocks a second instance
   throughout handoff and installation.
8. Setup preserves user data, replaces application files without destructive
   pre-copy deletion, and launches the new Momento version with an unguessable
   `--updated=<attempt-token>` marker.

A verified staged update is checked before making a new network request on the
next launch. Failed attempts use bounded backoff and are quarantined after
three tries; a newer signed release supersedes a quarantined installer. The
installed application removes obsolete app-owned runtime files only after the
new version confirms its attempt token, so Setup never begins by deleting the
working runtime. Automatic network, verification, and download failures are
logged and retried on a later launch without creating an update-restart loop.

## Manual Flow

Settings gains an **About & Updates** page with the current version, a
**Check for updates** button, and one concise status line. A manual check uses
the same worker and trust policy. It reports checking, downloading, current,
installing, or a useful failure; it never runs a second concurrent check. Once
the verified installer is staged, **Install now** restarts Momento and
**Later** keeps the update for unattended installation on the next launch.

## Runtime Footprint

The existing PyInstaller fail gates will be expanded rather than replaced.

### Qt

Remove Qt translation catalogs because Momento currently ships an English-only
interface. Retain the Windows platform, multimedia, TLS, JPEG, SVG, ICO, and
software OpenGL components. Remove only the GIF, ICNS, TGA, TIFF, WBMP, and
WebP image plugins after tests prove Momento's PNG/JPEG/SVG/ICO assets and
thumbnail path still work.

### PyAV And FFmpeg

Build and hash-lock a Momento-specific Windows PyAV wheel against a minimized
FFmpeg configuration. It must retain:

- H.264 NVENC, AMF, QuickSync, Media Foundation, and libx264 encoding
- H.264 and AAC decode/encode needed by recording and fixtures
- Matroska and MP4 muxing/demuxing, PyAV frame conversion, scaling, and audio
  resampling
- every DLL and licence/source input required by the retained runtime

HEVC, AV1, VP8/VP9, and unrelated codec libraries may be removed only when
binary import inspection and the complete encoder, playback, trim, repair,
AMD/Intel fallback, and hardware tests remain green. The release build must
reject the previous general-purpose PyAV runtime once the minimized wheel is
adopted.

## Failure And Recovery

- Update checks have short connection and bounded read timeouts.
- Downloads use a partial filename, are flushed before rename, and are removed
  after verification failure.
- GitHub API redirects are rejected. Asset redirects are followed manually for
  at most three HTTPS hops to an exact GitHub release-CDN host allowlist, with
  identity encoding and no user-info, fragments, or non-default ports.
- The client retains only the current staged installer and safely removes stale
  Momento-owned update files.
- An active/pending recording or app-owned media task always wins over
  installation. Quiescence is an atomic lease, not an idle-state snapshot.
- The launch handle rejects reparse roots, hard-linked installers, mutation,
  deletion, and replacement between verification and process creation.
- Setup logs an update run. Durable attempt state confirms the target version
  and token on relaunch, detects failed/stale attempts, and prevents loops.
- Invalid signatures, unexpected hosts, malformed metadata, and hash mismatch
  are security failures and are never bypassed.

## Verification

- Unit tests cover canonical metadata, signatures, URL and redirect policy,
  metadata replay, expiry, clock rollback, application downgrade, size limits,
  partial downloads, hash mismatch, stale cache, file substitution, and
  concurrent requests.
- Integration tests serve a local fake release endpoint and installer payload,
  exercising automatic, manual, no-update, offline, staged, busy-recorder, and
  relaunch handoff behavior without executing an external installer.
- Installer tests exercise a real silent v0.2.2-to-newer upgrade in an isolated
  profile and confirm settings and recordings survive.
- Bundle tests verify the public key, release metadata/signature assets, exact
  checksums, excluded Qt plugins/translations, and minimized FFmpeg dependency
  allowlist.
- Existing source, privacy, licence, hardware, playback, and recording audits
  remain mandatory. Final size is measured against the current 226.8 MiB
  installed and 60.3 MiB installer baselines.

## Revisit Triggers

Adopt fuller TUF metadata or a dedicated update service if Momento adds
multiple release channels, differential updates, delegated publishers, or a
second distribution host. Reconsider OS-level signing when a free trusted
route becomes available or public distribution grows beyond the friend test.
