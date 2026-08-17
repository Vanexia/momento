# Momento

Momento is a local-first Windows game recorder that lives in the system tray. It
detects known game processes, captures the game's window with microphone and
system audio, and finalizes the recording when the game closes. The built-in
editor handles playback, bookmarks, fast clip export, and library maintenance.
Distributors can optionally enable user-initiated YouTube uploads with an
approved OAuth project; the standard public build leaves that integration out.

Recordings stay on the computer unless the user explicitly starts an upload.
Momento has no product account, telemetry, analytics, cloud library, or
auto-updater.

## Features

- Automatic start and stop from a configurable known-games list.
- Optional foreground-fullscreen fallback with non-game protections.
- Per-window Windows Graphics Capture instead of desktop-wide recording.
- Microphone and playback-loopback capture through PortAudio/WASAPI.
- Crash-tolerant MKV recording through PyAV/libav.
- Hardware H.264 probing for NVIDIA NVENC, AMD AMF, Intel QuickSync, and Media
  Foundation, with libx264 as a CPU fallback.
- Configurable quality, frame rate, audio gain/offset, notifications, storage
  quota, and low-disk warnings.
- Global in-game bookmark hotkey, `F8` by default.
- Editor with search, game filters, sorting, thumbnails, playback, timeline,
  bookmarks, rename/delete/reveal, repair, and storage management.
- Stream-copy clip export to MP4 without quality loss.
- Optional resumable YouTube upload with local DPAPI-encrypted OAuth tokens.
- Close-to-tray playback parking that restores the same recording and position.

## Requirements

- 64-bit Windows 10 version 1903 or newer, or Windows 11.
- Python 3.12 only when running from source.
- A hardware H.264 encoder is recommended. CPU encoding is available but uses
  considerably more resources at high resolutions and frame rates.
- Momento includes and probes NVIDIA NVENC, AMD AMF, Intel QuickSync, and Media
  Foundation. NVIDIA has passed physical hardware testing; AMD and Intel use the
  same production-profile checks but still need wider testing on those GPUs.
- Roughly 615 MB for the current one-folder package, mostly native Qt, media,
  capture, and FFmpeg dependencies.

## Run From Source

```powershell
git clone <repo> C:\dev\Momento
cd C:\dev\Momento
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .[dev]
.\scripts\fetch_ffmpeg.ps1
.\.venv\Scripts\python.exe -m momento
```

Use `--show` to open the editor automatically during development:

```powershell
.\.venv\Scripts\python.exe -m momento --show
```

## Install Momento

Run `MomentoSetup-0.2.1.exe`. It installs for the current Windows user without
requiring Python or administrator access, creates a Start Menu shortcut, and can
optionally create a desktop shortcut.

Windows can show an Unknown Publisher or SmartScreen warning because the first
public installer is not yet code-signed.

## First Use

1. Complete the setup window. Momento suggests the Windows default audio
   devices, the redirected Videos folder, fixed 60 fps, and 16 Mbit/s capture.
2. Confirm the known-games list or enable the optional fullscreen fallback.
3. Launch a game. The tray state and notification report recording status.
4. Press the bookmark hotkey during useful moments.
5. Close the game to finalize the MKV, then open Momento from the tray.

Momento excludes the active recording from the editor and prevents output-folder
changes until recording has finalized.

## Editor Shortcuts

| Key | Action |
|---|---|
| `Space` | Play or pause |
| `Left` / `Right` | Seek -5 / +5 seconds |
| `Shift+Left` / `Shift+Right` | Seek -1 / +1 second |
| `Home` / `End` | Jump to start / end |
| `M` | Toggle mute |
| `F` or double-click | Toggle fullscreen preview |
| `Escape` | Exit fullscreen preview |

## Media Behaviour

- Recordings are MKV for better crash tolerance.
- Exported clips are MP4 and live under `<output>\clips`.
- Trim export stream-copies, so boundaries are keyframe-accurate rather than
  frame-accurate.
- System audio is the full mix sent to the selected playback endpoint.
- Capture size is fixed after an initial settle period. Later window resizes are
  cropped or padded to keep the stream valid.
- Startup can repair an unfinalized MKV when enough container data survived.

## Runtime Files

- Config: `%APPDATA%\Momento\config.json`
- Logs: `%APPDATA%\Momento\logs\momento.log`
- Window state: `%APPDATA%\Momento\window_state.ini`
- Lock: `%APPDATA%\Momento\momento.lock`
- YouTube token: `%APPDATA%\Momento\youtube_token.dat`
- Recordings: configured output folder, default Windows Videos known folder
  plus `Momento`
- Bookmarks: `<media>.bookmarks.json`
- Thumbnails: `<media>.thumb.jpg`

## Development

Fast checks:

```powershell
.\.venv\Scripts\python.exe -m compileall -q momento
.\.venv\Scripts\ruff.exe check momento tests --select F,E9
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\pip-audit.exe --local
```

The hardware-independent regression suite is listed in
`.github\workflows\ci.yml`. Real WGC, audio-device, and encoder smokes require a
Windows machine with a visible target window, working audio endpoints, and GPU.

Build the public installer from a clean committed tree with Momento closed:

```powershell
.\.venv\Scripts\python.exe -m pip install -c constraints-release.txt -e .[dev]
.\scripts\fetch_ffmpeg.ps1
.\scripts\build_installer.ps1
```

The FFmpeg fetch script downloads the pinned Windows build, verifies its
published SHA-256 checksum, and installs `ffmpeg.exe` and `ffprobe.exe` under
`resources\ffmpeg` for source runs and packaging.

## Project Layout

- `momento\__main__.py`: application and tray bootstrap.
- `momento\config.py`: validated settings persistence.
- `momento\core`: watcher, session, capture, audio, encoder, repair, storage, and
  thumbnail services.
- `momento\ui`: editor, settings, tray, preview, timeline, and notifications.
- `momento\youtube`: OAuth and resumable uploads.
- `momento\trim`: FFmpeg stream-copy clip export.
- `momento\util`: Windows integration, paths, logging, resources, and hotkeys.
- `resources`: bundled FFmpeg, game data, fonts, icons, and optional OAuth client.
- `tests`: smoke, regression, diagnostic, and hardware scripts.
- `build\pyinstaller.spec`: packaging recipe.
- `build\installer.iss`: per-user Windows installer recipe.
- `scripts\build_installer.ps1`: clean public release and source-archive build.

## Out Of Scope

- Replay ring buffer or "last N minutes" capture.
- Live recording preview or streaming.
- Webcam, HDR, and per-application audio isolation.
- Frame-accurate re-encoded editing.
- Momento cloud accounts, telemetry, and automatic updates.

## Troubleshooting

- If a game is not recorded, check the known-games entry, monitoring state, and
  `%APPDATA%\Momento\logs\momento.log`. The log records process detection, window
  retries, encoder selection, stream counts, drops, and finalization.
- If audio is missing, reselect and test both devices in Settings. Device names
  can change after Windows or driver updates.
- If video drops frames, check the recording log for the selected encoder and
  drop count. GPU saturation, another encoder session, or CPU fallback are common
  causes.
- If a hardware encoder fails to open or stops during a recording, Momento tries
  the next compatible backend. A tray warning tells you when it reaches the CPU
  fallback.
- If a hotkey does not fire in one game, choose another combination; some games
  intercept global input.
- If the tray icon appears missing, check Windows tray overflow.

## License

Momento is licensed under GPL-3.0-only. The installer includes the corresponding
Momento source archive, build information, and third-party notices. Bundled
components retain their own copyright and license terms.
