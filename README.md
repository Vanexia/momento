# Momento

Local Windows desktop app that auto-records games to MP4 files. No cloud, no
accounts, no telemetry, no upload, no auto-update. Everything stays on your
machine.

It lives in the system tray. When a known game's process appears, it starts
recording the **game's window** (not your whole desktop) along with your
microphone and a chosen audio loopback. When the game closes, the recording
finalises automatically. A built-in editor lets you scrub through recordings
and trim clips.

## Features

- **Auto-start / auto-stop** based on a configurable known-games list.
- **Per-window video capture** via Windows Graphics Capture (WGC) — captures
  only the game's window, not other apps that happen to be on the same screen.
- **System audio** captured via WASAPI loopback from any playback endpoint
  (Speakers / headset / HDMI sink). Mic captured via DirectShow.
- **In-game bookmarks**: press a configurable hotkey (default `F8`) during
  gameplay to drop a marker. Bookmarks appear on the timeline in the editor.
- **Editor** with H.264 preview, scrubber, volume / mute, timeline with
  draggable trim handles, and stream-copy export (no re-encode → fast).
- Recordings stay in the folder you choose (default `Videos/Momento`).

## Requirements

- Windows 10 / 11 (Desktop Duplication API + WASAPI loopback are required).
- A GPU with a hardware H.264 encoder is recommended — Momento
  auto-detects **NVIDIA NVENC**, **AMD AMF**, or **Intel QuickSync**
  in that priority order. If none are usable it falls back to
  **libx264** (pure CPU encode) which works on any machine but uses
  meaningfully more CPU during recording.
- Python 3.12 (only for running from source; the packaged exe ships its own).
- ~700 MB of disk space for the packaged build (mostly the bundled `ffmpeg`).

## Install

### Run the packaged build (recommended)

1. Grab `dist/Momento/` (one-folder build produced by PyInstaller).
2. Double-click `Momento.exe`. It registers in the system tray.

### Run from source

```powershell
git clone <repo> C:\dev\Momento
cd C:\dev\Momento
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .[dev]
.\.venv\Scripts\python.exe -m momento
```

Recording is fully in-process via **PyAV** (libav) — no ffmpeg subprocess
on the live recording path. A bundled `ffmpeg.exe` (full build from
gyan.dev) lives in `resources/ffmpeg/` and is only used offline for
trim-export and thumbnail extraction. The Python code resolves to it via
`momento.util.ffmpeg_path`, and the same path works under PyInstaller via
`sys._MEIPASS`.

## First-time setup

Open the tray icon → **Settings…** and configure:

| Field | What it does |
|---|---|
| Microphone | WASAPI capture device for your mic input |
| Mic volume % | Per-input gain applied at record time (0–200) |
| System audio | WASAPI playback endpoint to record (Speakers, headset, HDMI) |
| System volume % | Per-input gain (0–200) |
| Match my monitor | Auto-records at the primary display's refresh rate (default on) |
| Framerate | Manual fallback (24–240) when “Match my monitor” is off |
| Output folder | Where recordings land (as `.mkv` — see below) |
| Start with Windows | Adds an HKCU Run entry so Momento starts on login |
| Bookmark hotkey | Global hotkey to drop a marker while recording (default F8) |
| Play chime on bookmark | Soft 200 ms confirmation when the hotkey lands |
| Known games | One executable name per line (case-insensitive match) |
| Record any fullscreen | Catch-all for games not in the list (be selective — also matches fullscreen videos / browsers) |

The recordings folder fills up fast at high framerate / quality — keep an eye
on disk usage.

## Usage

1. Make sure Momento is running (tray icon visible).
2. Launch a game whose `.exe` matches an entry in **Known games**.
3. Tray icon flips red; recording starts at the game's natural window size.
4. Press your bookmark hotkey (default **F8**) at any interesting moment.
5. Quit the game. Recording finalises automatically as an `.mkv`.
6. Click the tray icon → editor opens. Pick the recording, scrub, drag the
   trim handles, click **Export clip**, name it. Trims export as `.mp4`
   (the shareable format); the source recording stays as `.mkv` (matches
   OBS — crash-safe).

Editor keyboard shortcuts (when a clip is loaded):

| Key | Action |
|---|---|
| Space | Play / pause |
| ← / → | Seek -5s / +5s |
| Shift+← / → | Seek -1s / +1s |
| Home / End | Jump to start / end |
| M | Mute toggle |
| F or double-click | Toggle fullscreen preview (Escape exits) |

## File layout

- `momento/__main__.py` — entry point (single-instance + tray bootstrap)
- `momento/config.py` — schema + JSON load/save
- `momento/core/` — recorder, game watcher, session manager, **encoder**
  (PyAV/libav, in-process), WGC video capture, WASAPI mic + loopback,
  game-name humaniser, bookmarks, thumbnails
- `momento/ui/` — tray, settings dialog, editor, preview (with fullscreen),
  timeline, toast notifications
- `momento/trim/ffmpeg_trim.py` — stream-copy clip export (MKV→MP4)
- `momento/util/` — paths, ffmpeg path, autostart registry, hotkeys, logging,
  single-instance, Windows API helpers, screen refresh-rate detection
- `resources/` — bundled `ffmpeg.exe`/`ffprobe.exe`, `known_games.json`,
  app icon, bookmark chime
- `tests/` — smoke + diagnostic scripts (`check_pyav_nvenc.py`,
  `smoke_recorder.py`, `smoke_encoder.py`, `smoke_trim.py`, etc.)
- `build/pyinstaller.spec` — packaging recipe

## Where things live at runtime

- **Config**: `%APPDATA%\Momento\config.json`
- **Logs**: `%APPDATA%\Momento\logs\` (rotating; per-recording log alongside)
- **Lock file**: `%APPDATA%\Momento\momento.lock` (single-instance enforcement)
- **Bookmarks**: `<recording>.mkv.bookmarks.json` next to each recording
- **Thumbnails**: `<recording>.thumb.jpg` next to each recording
- **Recordings**: user-configured (default `Videos/Momento`), `.mkv` format

## Build the standalone exe

```powershell
.\.venv\Scripts\python.exe -m PyInstaller build\pyinstaller.spec --noconfirm
```

Output lands in `dist/Momento/`. Distribute the whole folder.

## Known limitations / out of scope

The spec is intentionally narrow:

- **No live preview** during recording.
- **No ring-buffer / "last N minutes"** continuous recording.
- **No per-app audio separation** — system audio is the full mix of whatever's
  going to the chosen playback endpoint (game + Discord + browser, etc.).
- **No cloud / accounts / telemetry / auto-update.**
- **Trim is keyframe-accurate**, not frame-accurate — cut points may snap up
  to ~1s earlier than the dragged position because we stream-copy rather than
  re-encode (much faster, no quality loss).
- **No HDR, no webcam, no streaming.**

## Troubleshooting

- **Recording finalised but won't play** — MKV files are cluster-based, so
  even a hard crash mid-recording leaves something playable. Try `ffprobe`
  on the file directly; if the audio/video streams parse, any modern player
  (VLC, mpv, Windows Media Player) handles it.
- **Audio plays behind the video in the recording** — WASAPI loopback has
  ~30–80 ms inherent buffer latency. Default sync offset (`-50 ms`) covers
  most setups; if yours is unusual, edit `audio_offset_ms` in
  `%APPDATA%\Momento\config.json` (negative = audio earlier).
- **Recording drops a lot of frames during gameplay** — check the per-recording
  log for `drops=N` on the video stream. Common causes: GPU pegged by the
  game itself (encoder time-slice starvation), another active hardware-encode
  session (OBS, ShadowPlay, Discord screen-share), or — if Momento fell back
  to the libx264 software encoder — the CPU just can't keep up at the
  configured resolution/framerate. Search the log for "Selected video
  encoder:" to see which backend is in use.
- **Hotkey doesn't fire in a specific game** — some games using exclusive
  raw input block global hotkeys. Pick a different combo in Settings.
- **Tray icon doesn't appear** — your tray may have icon overflow on; check
  the `^` chevron in the system tray.

## License

MIT — see [LICENSE](LICENSE). FFmpeg is bundled under its own (LGPL/GPL)
terms; the bundled binary comes from gyan.dev.
