# Momento — Project Context for Claude Code

This file is the canonical handoff doc for any Claude Code session working
on Momento. Read it first — it contains the full spec, the deviations the
shipping code has taken from that spec, the current architecture, and a
focused punch list of known-rough edges.

Last comprehensive update: **2026-05-26** (YouTube upload bridge — Phase
11). The Phase 9 polish pass (fullscreen overlay + migration progress
dialog + violet brand swap + optimisation) remains the most recent UI
sweep; Phase 10 (Rust WMI watcher + perf hybrid) was scoped, paused, and
ultimately dropped in favour of shipping the YouTube feature first.

---

## Overview

Momento is a **local Windows desktop application** that auto-records games
to MKV files and includes a built-in clip editor. **No telemetry, no
auto-update, no background sync.** Recordings live on the user's machine
and never leave it unless the user explicitly clicks "Upload to YouTube"
on a clip (Phase 11 — opt-in OAuth Desktop flow; refresh token encrypted
on disk via Windows DPAPI; Momento never sees the user's password).

The app lives in the system tray. A `GameWatcher` polls `psutil` for known
game executables; when one launches, the `SessionManager` spins up a
`Recorder` that captures the game's window via WGC (Windows Graphics
Capture), the user's mic via WASAPI, and the system audio via WASAPI
loopback. Encoding happens **in-process via PyAV (libav)** — no ffmpeg
subprocess, no TCP plumbing. Frames are pushed into bounded drop-oldest
queues; an encoder worker thread feeds h264_nvenc and writes to an MKV
container (cluster-based, crash-safe).

When the game exits, the recording auto-finalises and the **MKV is the
canonical artefact** — Momento follows OBS's default and does not
auto-remux to MP4. The built-in editor lists past recordings (both .mkv
and legacy .mp4), plays them back via QMediaPlayer (Windows Media
Foundation handles MKV+H264+AAC natively), and exports trims as MP4 +
faststart via the bundled ffmpeg.exe — so the share-out artefact is the
universally-compatible container, but the local library stays MKV.

First-launch UX is now an **8-step setup wizard** (still in `welcome.py`)
that walks new users through folder + audio + capture + monitoring +
notifications + startup before they ever see a game launch.

---

## Target environment

- Windows 10 or 11
- Python 3.12
- A working H.264 encoder — auto-detected at startup from this priority
  chain: **NVENC** (NVIDIA) → **AMF** (AMD) → **QSV** (Intel) → **Media
  Foundation** (generic Windows hardware path) → **libx264** (pure-CPU
  software fallback). The software path works on any machine but uses
  meaningfully more CPU; the hardware paths are essentially free during
  recording. See `momento/core/encoders.py` for the probe + selection
  logic.
- Default capture: window's native resolution, **framerate auto-matched
  to the primary monitor's refresh rate** (Settings → Capture → "Match
  display refresh rate"). Falls back to manual 30 / 60 / 120 / Custom
  values when off.

---

## Tech stack (locked)

- **Python 3.12** (`>=3.12,<3.13` pin in `pyproject.toml`)
- **PyQt6** for UI and tray
- **PyAV (`av>=14`)** for in-process video + audio encoding/muxing. Bundles
  its own libav (ffmpeg) shared libraries — that's what does the actual
  h264_nvenc encode and MKV mux. We pass frames as numpy arrays straight
  into `stream.encode()`; the encoder lives in our process so there's no
  subprocess lifecycle, no stdin shutdown choreography, no TCP plumbing.
- **FFmpeg 8.1.1 full build (gyan.dev)** bundled at `resources/ffmpeg/`,
  used only for **offline** stream-copy tasks on closed files: trim
  export, duration / game-tag probing, repair re-mux, thumbnail
  extraction. The live recording path never touches this binary.
- **psutil** for process detection
- **soundcard** for WASAPI capture (mic + system audio loopback). One
  library, one ndarray format. Also used by `MicMonitor` for the
  in-settings Test mic + monitor-to-speaker flow.
- **windows-capture** (Rust-backed crate, pip-installable) for WGC
- **numpy** for ndarray-shaped frames + audio buffers
- **google-api-python-client + google-auth-oauthlib + google-auth-httplib2**
  (Phase 11 — YouTube upload). Loaded lazily by the upload code path; not
  pulled in during normal startup. OAuth refresh token persisted via
  Windows DPAPI (ctypes against `crypt32.dll`) — bound to the current
  Windows user account, undecryptable from any other account or machine.
- **Pillow** (dev only — for generating the app `.ico`)
- **PyInstaller** (dev only — packaging)
- Standard library: `logging`, `json`, `pathlib`, `ctypes`, `shutil`,
  `dataclasses`
- **No `pywin32`.** Everything Win32 goes through `ctypes`.
- **No `opencv-python`.**

---

## In scope

### Tray + recording loop
- System tray icon, runs in background, single instance enforced via
  `msvcrt.locking` on `%APPDATA%/Momento/momento.lock`
- psutil-based polling that detects known game executables every 2 s
- Auto-start recording on game launch, auto-stop on game exit
- Tray menu: Status / Open Momento / Open recordings folder / **Stop
  current recording** (visible only while recording) / **Pause⇄Resume
  monitoring** / Quit
- `SessionManager` exposes `pause_monitoring()`, `resume_monitoring()`,
  `stop_current_recording()`, `is_monitoring` property

### Recording pipeline
- Per-window video via Windows Graphics Capture (WGC) — only the game's
  window, not the desktop
- System audio loopback via WASAPI
- Microphone via WASAPI
- Mixed in-process via a libav filter graph (`amix=normalize=0` + per-input
  `volume`), AAC-encoded once
- Output is **MKV** (cluster-based / crash-safe). No auto-remux.
- Container metadata: `MOMENTO_GAME=<slug>` written at encode time so the
  editor can group by game even after a rename (see "Game-tag persistence"
  below).
- **Quality presets** map to NVENC options:
  - Low: `rc=vbr cq=28`
  - Medium: `rc=vbr cq=23`
  - High (default): `rc=vbr cq=19`
  - Custom: `rc=cbr b=<kbps>k` with user-specified bitrate
- **Resolution presets**: Match game / 1080p / 1440p / 4K. Non-source
  presets reformat each frame via `av.VideoFrame.reformat()` before
  encode. Momento never upscales — picking 4K with a 1080p game records
  at the game's native size.
- Trim export produces `<output>/clips/<name>.mp4` with `+faststart` and
  `+use_metadata_tags` so the `MOMENTO_GAME` tag survives MKV → MP4.

### Settings (tabbed panel inside the editor window)
Each tab is width-capped (`_DEFAULT_SETTINGS_WIDTH = 920`, Games tab uses
`_GAMES_SETTINGS_WIDTH = 1280`) and left-aligned.

**Audio:**
- Mic device picker (WASAPI capture endpoints) + ✓ Connected / ✕ Not
  detected status label
- Mic volume slider 0–200 % (default 100)
- **Test mic** toggle button + **"Mic input level"** + `LevelMeter` +
  three-state caption: "Listening…" → "Input detected" (peak > 5 %) or
  "No input detected" (4 s of silence). Driven by `MicMonitor`.
- System audio device picker + status label + hint
- System volume slider 0–200 %
- **Test system audio** button — plays the bookmark chime through the
  configured playback device (QMediaPlayer routed to the QAudioDevice)
- Refresh device list button
- `audio_offset_ms` (default -50) is config-only, no UI

**Capture:**
- Resolution combo with concrete dimensions in each label: *"Match game
  (native — no scaling)"* / *"1080p (1920×1080)"* / *"1440p (2560×1440)"* /
  *"4K (3840×2160)"*
- **FPS** combo (single row): Match display refresh rate ({Hz}) / 30 / 60
  / 120 / Custom… (Custom reveals a spinner row beneath)
- Quality combo: Low / Medium / High (recommended) / Custom bitrate
  (custom reveals a kbps spinner)
- **Quality description label** beneath the combo that updates per
  selection — explains the trade-off ("Smallest files. Visible
  compression in fast motion." / "Balanced size and quality." /
  "Near-source quality with reasonable file sizes." / "You set the
  bitrate in the row below."). Seeded with the default so it never paints
  blank.
- "Recommended for most users" tips card + "Capture tips" card

**Output:**
- Folder picker + Browse + **Open folder** button
- **Free-disk hint** under the path field: *"1.2 TB free on D:"* — fires
  on `textChanged` through a 200 ms debounce (UNC-share safety) + on
  Browse + on initial load.
- **Max storage**: `AnchoredComboBox` with Medal/ShadowPlay-style
  presets (Unlimited / 10 / 25 / 50 / 100 / 250 / 500 GB / 1 TB /
  Custom…). Custom reveals a spinner row beneath. When exceeded,
  `momento/core/storage_cleanup.py` deletes the oldest top-level `.mkv`
  files + their sidecars. Clips and bookmarks are **always preserved**.
  Runs at startup and after every recording stops.
- **Low-disk warning at**: same preset pattern (Off / 5 / 10 / 25 / 50 /
  100 GB / Custom…). At startup, if free space is below the watermark,
  shows the amber warning toast.
- **Folder change → migration prompt**: when the user changes
  `output_folder` and the previous folder has recordings or clips, a
  three-button dialog (Move / Leave them / Cancel) fires before the
  config save. Move runs on a `QThread` driven by `MigrationWorker`
  (in `momento/core/storage_cleanup.py`) with a `QProgressDialog`
  showing per-file count and current filename — UI stays responsive
  the whole time. Sidecars (`.thumb.jpg`, `.bookmarks.json`) follow
  their parent media file automatically.
- **Browse dialog** remembers its size between sessions (persisted in
  `window_state.ini` under `dialogs/output_folder/geometry`) and lists
  every mounted drive in the sidebar via `GetLogicalDrives` —
  necessary because Qt's non-native picker has no "This PC" shell root.

**Bookmarks:**
- Hotkey field (default `F8`)
- "Play a soft chime when a bookmark is added" toggle
- "How bookmarks work" tips card

**Games:**
- "Record fullscreen apps not in the game list" toggle + subtext "May
  record non-game apps by mistake."
- Search box + filter combo (All / Auto-record on / Auto-record off)
- 3-column QTableWidget: **Game** (humanised name, italic + muted when
  disabled) · **Executable** · **Auto-record** (pill toggle styled with
  quiet "On" outline and prominent amber "Off" so disabled rows pop)
- Pill is `setFixedSize(60, 24)`; rows are 38 px (`Fixed` resize mode +
  per-row `setRowHeight`) so the rounded edges never clip
- Bottom row buttons (single line, left/right split):
  - Left: Add game… / Scan running apps… / Remove selected
  - Right: Restore defaults (merge) / Import… / Export… (JSON)

**Notifications:**
- Toggles: "Game detected — show Recording started" / "Show Recording
  saved when a game exits" / "Show Bookmark added when the hotkey lands"
  / "Show Couldn't record when recording fails to start"
- Position dropdown: Top-left / Top-right / Bottom-left / Bottom-right

**Startup:**
- "Start Momento with Windows"
- "Begin monitoring games on launch"
- "Close button minimises to tray"
- Hint: "When enabled, Momento starts in the system tray without
  opening the main window."

### Editor window (`EditorWindow`)
- `QStackedWidget`:
  - Page 0: the editor (recordings/clips + preview + timeline)
  - Page 1: the settings panel
- **Window geometry persistence**: position + size + splitter state
  saved on close (and on `QApplication.aboutToQuit` so tray-Quit also
  captures the final state) to `%APPDATA%/Momento/window_state.ini` via
  `QSettings`. Restored in `__init__`. The horizontal splitter is also
  exposed as `self._main_splitter` so post-dialog restoration logic can
  re-apply sizes if a wide modal nudged the handle.
- **Top status panel** (`StatusPanel`):
  - Coloured **pill** with dot + label — three states:
    - Recording {game} (red)
    - Monitoring for games (green)
    - Idle (grey — only when monitoring is paused)
  - Live elapsed time during recording (1 Hz QTimer when visible;
    slows to 5 s when hidden via close-to-tray or when the user is on
    the settings page)
  - Right-hand chips: **Mic: On/Off**, **System audio: On/Off**, **Free
    space: 56.4 GB** (`free_bytes_for` in `momento/util/format.py`)
- **Left pane**:
  - Tabs **Recordings (N)** / **Clips (N)**
  - Search box (matches stem + humanised game title)
  - Filter by game combo (humanised names + counts) — uses `_AnchoredComboBox`
    so the popup never jumps based on the selected item
  - Sort combo: Newest first / Oldest first / Longest / Largest / Game name
  - Card-style `QListView` (`RecordingsList`) with thumbnails, duration
    badges, friendly game-name title, UK date format meta line "date ·
    duration · size" with bullet separators (duration always renders,
    shows "—" while probe pending)
  - Empty-state placeholder via `QStackedWidget` ("No recordings yet…" /
    "No clips yet…" / "No matches…")
  - Auto-selects row 0 on tab switch so the preview never sits black
  - Right-click context: **Play** / Rename… / Open file location / Export
    clip… / Repair recording… / Delete
  - Refresh list + Delete selected buttons
- **Right pane**:
  - **Preview** (`VideoPreview`) — `QMediaPlayer + QVideoWidget`. Single
    click toggles play/pause (YouTube-style: press fires immediately,
    double-click cancels via second-toggle so fullscreen preserves play
    state). Double-click toggles fullscreen (via a top-level `_VideoArea`
    host, NOT reparenting). Empty-state placeholder when no clip loaded.
    `_VideoArea.sizeHint()` is fixed at `QSize(320, 180)` so splitter
    layout doesn't reflow on every load.
  - Controls row: `[▶] 0:00 / 15:06`  <stretch>  `[🔇] [volume]`. No
    standalone scrubber — the timeline is the single source of truth.
  - **Fullscreen overlay** (`_FullscreenOverlay`): Medal-style
    floating bar that appears over the fullscreen video. Top-level
    frameless `Tool` window with `WindowStaysOnTopHint` (child widgets
    of `QVideoWidget` get painted behind the native sub-window on
    Windows — only a separate top-level survives). Contents: a 4 px
    progress bar (with white playhead ball, hover-swell to 7 px via a
    `pyqtProperty` `swell` driven by `QPropertyAnimation`) over an
    icon row (▶/⏸, time, 🔇, volume slider, ⛶ exit-fullscreen). Auto-
    hides after 2.5 s cursor inactivity; fades via `QGraphicsOpacityEffect`
    on a `_content` child wrapper with `QEasingCurve.OutCubic`. Cursor
    polled at 60 ms via `QCursor.pos()` (the host's `mouseMoveEvent`
    isn't reliable — QVideoWidget's native sub-window swallows events).
    Activity poll is restricted to `host.geometry().contains(pos)` so
    movement on another monitor doesn't wake the overlay. Custom-painted
    monochrome white icons (Qt's `standardIcon` glyphs disappear on the
    dark gradient). Focus bounces back to the host after every overlay
    click via `QApplication.focusWindowChanged` listener — keeps F/Esc/
    Space/M shortcuts firing. Backup `QShortcut`s on the overlay itself
    cover the brief window where focus is still on us. Aspect ratio
    defaults to `KeepAspectRatioByExpanding` (fill) so matched-aspect
    recordings fill the screen edge-to-edge with no sub-pixel
    letterbox gap at fractional DPI scaling; the host is anchored to
    the editor's current screen via `setScreen()` for multi-monitor
    correctness.
  - Bottom panel (170–260 px, sizes to content; was previously a flat
    280 px which left ~110 px of empty space below the timeline):
    - **Clip controls row**: `Start [00:00] End [15:06] Length 15:06`
      <stretch> Reset zoom. Time inputs parse `M:SS` or `H:MM:SS` via
      `momento.util.time_format.parse_time`.
    - **Timeline** (`Timeline`):
      - Time ruler at top with adaptive "nice" intervals: `1s · 2s · 5s ·
        10s · 15s · 30s · 1m · 2m · 5m · 10m · 15m · 30m · 1h · 2h · 4h ·
        6h · 12h · 24h`. Picks smallest step that keeps majors ≥ 64 px
        apart. Minor ticks at clean sub-units.
      - Mouse wheel zooms 1.25× per notch centred on cursor; horizontal
        scrollbar appears beneath when zoomed in for panning.
      - Trim handles (drag), bookmark ticks (clickable), playhead extends
        up through the ruler.
    - **Bookmark chip strip** — hidden when no bookmarks; otherwise a
      horizontally-scrollable row of clickable "M:SS" pills.
    - **Export row**: Play clip portion / Export selected clip /
      progress bar. *(Quick export ▾ dropdown was removed Phase 9 — it
      duplicated functionality already covered by the trim handles +
      keyboard shortcuts.)*
  - Keyboard shortcuts: Space (play/pause), M (mute), F (fullscreen),
    arrows ±5s, Shift+arrows ±1s, Home/End

### First-run setup wizard
Single class `WelcomeDialog` in `momento/ui/welcome.py`. 8 steps in a
`QStackedWidget` with Back / Skip / Next + Finish:

1. **Welcome** — short explainer, local-only, no cloud
2. **Recordings folder** — `QLineEdit` + Browse
3. **Audio** — mic + system audio combos, connected/missing status, full
   Test mic flow with `MicMonitor` + `LevelMeter` + 3-state caption, Test
   system audio (chime)
4. **Capture** — Resolution / FPS / Quality combos (custom bitrate stays
   Settings-only)
5. **Game monitoring** — "Start watching for games when Momento launches"
6. **Notifications + bookmarks** — three notification toggles + hotkey field
7. **Startup** — autostart / monitor on launch / close to tray
8. **Final check** — 6-row checklist rendered from merged pending+config
   state, then Finish

On Finish: builds Config via `dataclasses.replace`, calls `save_config`,
emits `settings_saved(Config)` (same shape as `SettingsPanel`) → tray's
`_apply_new_config` reloads the session. Skip jumps straight to the final
page so the user can still review + Finish.

Auto-launches on first run (`is_first_run = not config_path().exists()`).
Re-openable any time from **File → Run setup tutorial…** in the editor.

### Bookmarks
- Global hotkey (Win32 `RegisterHotKey` via ctypes + native event filter)
- Sidecar `<recording>.<ext>.bookmarks.json`
- Timeline ticks + bookmark chip strip + chip-on-click seek
- Orange toast "Bookmark added @ M:SS" (gated by `show_bookmark_toast`)
- Soft chime (gated by `bookmark_sound`)

### Branded toasts (`RecordingToast`)
Four states with cross-fade swap between content:
- **Red** (`#dc3c40`) "Recording started"
- **Violet** (`#8b5cf6`) "Recording saved" *(was blue pre-Phase 9; now
  tracks brand accent)*
- **Amber** (`#ecb14c`) "Couldn't record" / low-disk warning
- **Orange** (`#ffaa3c`) "Bookmark added"

Position configurable: top-left / top-right / bottom-left / bottom-right
(`Config.notification_position`). Click to dismiss; ~4 s auto-dismiss (8 s
warnings, 2 s bookmarks).

### Brand colour
- Accent: **`#8b5cf6` (violet)** with `ACCENT_HOVER = #a78bf9`,
  `ACCENT_PRESS = #7245d8`. Tokens live in `momento/ui/theme.py` and
  drive the global QSS template, the `QPalette.Highlight` role, and
  every accent surface that derives from theme at paint time.
- All accent-coloured surfaces read `theme.ACCENT` at render time so
  future swaps are a single edit:
  - `RecordingsList` selected-card border + tinted background
    (HSL-derived from accent in `recordings_list.py`)
  - `Timeline` clip-selection bar (alpha 200)
  - `_ProgressBar` fill in the fullscreen overlay (read in `_COLOUR_FILL`
    property at paint time)
  - Bookmark chips on the timeline (border + tinted background)
  - "Recording saved" toast accent
- The toast palette intentionally keeps semantic colours (red / amber /
  orange) for status meaning — only the "saved" accent tracks brand.
- **First-frame priming**: brief play→pause once `durationChanged` fires
  so preview opens on real content
- **Seek-time mute** (~150 ms): swallows H.264 decoder catch-up artifacts
- **Fullscreen via separate top-level host**: no reparenting
- **ffprobe duration fallback**: `MetadataProbe` provides duration when
  QMediaPlayer reports 0 for unfinalised MKVs

### Crash safety & recovery
- Matroska muxer with `cluster_time_limit=1000ms`, `cluster_size_limit=
  2 MiB`, `reserve_index_space=64 KiB`. Hard kill loses ≤ 1 s of metadata.
- **Startup recovery**: `media_probe.find_broken_recordings()` finds MKVs
  with N/A duration, queues async `repair_async()` (ffmpeg
  `-fflags +genpts+igndts -c copy`).
- Manual right-click → **Repair recording…**
- Orphan ffmpeg kill at session start (legacy carryover; live recording
  spawns no subprocesses, so usually a no-op).
- **One-shot clip migration** at startup: moves legacy clip-shaped files
  from the root recordings folder into `clips/`. Idempotent.
- Uncaught `sys.excepthook` + `threading.excepthook` route to the log file.

### Game-tag persistence
`InProcessEncoder` writes `container.metadata["MOMENTO_GAME"] = slug` at
record start. The editor's `MetadataProbe` (combined duration + tag probe,
one ffprobe call per file) reads it back; `_game_slug_cache: dict[str, str
| None]` keys path → slug, with `None` as the in-flight sentinel so we
never double-submit. When the embedded slug disagrees with the
filename-derived fallback, `QTimer.singleShot(0, ...)` coalesces N
probe-completion events into one combo rebuild.

The Games filter dropdown, list grouping, and the "Filter by game"
selection all use this — so a recording renamed via Explorer or Momento's
right-click → Rename still groups under its original game.

Trim export carries the tag via `-map_metadata 0 -movflags
+faststart+use_metadata_tags`.

---

## Out of scope (do not build, even if it seems easy)

- Live preview during recording
- Ring buffer / "last N minutes" continuous recording
- Per-application audio capture (cannot separate game audio from Discord;
  accept this)
- Cloud anything (except the **explicit, opt-in YouTube upload bridge** in
  Phase 11 — that's a right-click → dialog → progress flow on a single
  clip, not a background sync); accounts (Momento has no account of its
  own — the YouTube feature uses the user's existing Google account);
  telemetry; auto-update
- Frame-accurate trim
- HDR capture
- Webcam capture
- Streaming
- Mic noise suppression, audio post-processing
- Hotkeys for manual record start/stop (auto-only; **bookmark** hotkey is
  fine because it's a different concept)
- **Mic-disconnected notification** — no device-disconnect detector
  currently implemented; would need its own monitor service

---

## Config schema (`momento/config.py`)

All fields with their current defaults:

| Field | Default | Notes |
|---|---|---|
| `mic_device` | `""` | WASAPI device id |
| `system_audio_device` | `""` | WASAPI device id |
| `mic_volume_pct` | `100` | 0–200 |
| `system_volume_pct` | `100` | 0–200 |
| `output_folder` | `default_output_folder()` | `%USERPROFILE%/Videos/Momento` |
| `autostart_with_windows` | `False` | HKCU Run key |
| `known_games` | `DEFAULT_KNOWN_GAMES` | ~650 exes |
| `disabled_games` | `[]` | Subset of `known_games` toggled off in Games tab |
| `max_storage_gb` | `0` | 0 = unlimited |
| `low_disk_warning_gb` | `5` | 0 = off |
| `framerate` | `120` | Manual fallback |
| `framerate_auto` | `True` | Match display refresh rate |
| `bookmark_hotkey` | `"F8"` | |
| `record_any_fullscreen` | `False` | Risky fallback for unknown games |
| `show_recording_started_toast` | `True` | |
| `show_recording_saved_toast` | `True` | |
| `show_failure_toast` | `True` | Failures need action; opt-out |
| `audio_offset_ms` | `-50` | Config-only, no UI |
| `bookmark_sound` | `True` | Soft chime through default speaker |
| `show_bookmark_toast` | `True` | Orange overlay |
| `notification_position` | `"top-left"` | top-left / top-right / bottom-left / bottom-right |
| `close_to_tray` | `True` | Editor close → hide |
| `start_monitoring_on_launch` | `True` | When False, user resumes via tray |
| `target_resolution` | `"source"` | source / 1080p / 1440p / 4k |
| `quality_preset` | `"high"` | low / medium / high / custom |
| `custom_bitrate_kbps` | `12_000` | Used only when `quality_preset == "custom"` |
| `youtube_default_privacy` | `"unlisted"` | Phase 11 — public / unlisted / private |
| `youtube_default_category` | `20` | YouTube category ID. 20 = Gaming |
| `youtube_default_tags` | `""` | Comma-separated default tags appended to every upload |
| `youtube_channel_name` | `""` | Cached display name from last successful auth — UI only |
| `youtube_channel_id` | `""` | Cached channel ID from last successful auth — UI only |

`Config.from_dict()` validates ranges, auto-prunes launcher exes from
older `known_games` lists, falls back from a legacy `show_recording_toast`
single flag to the split toggles.

---

## Project structure (current, as it exists on disk)

```
Momento/
├── CLAUDE.md                     this file
├── LICENSE                       MIT + third-party (ffmpeg) notice
├── README.md
├── pyproject.toml
├── .gitignore
├── .venv/
│
├── momento/                      app package (editable-installed)
│   ├── __init__.py
│   ├── __main__.py               entry: single-instance → QApp → theme →
│   │                             load config → SessionManager → Tray →
│   │                             HotkeyService → migrations → storage
│   │                             cleanup → low-disk warning → first-run
│   │                             welcome wizard
│   ├── config.py                 schema (28+ fields), DEFAULT_KNOWN_GAMES,
│   │                             from_dict validation, auto-prune
│   │
│   ├── core/
│   │   ├── audio_loopback.py     WASAPI loopback capture via soundcard
│   │   ├── bookmarks.py          .bookmarks.json sidecar persistence
│   │   ├── encoders.py           Phase 12 — H.264 encoder probe + select.
│   │                             detect_available() opens each backend
│   │                             (NVENC/AMF/QSV/MF/libx264) at 320x240 to
│   │                             see what the hardware can run. Cached.
│   │                             pick_encoder() returns the highest
│   │                             priority working one. quality_options_for
│   │                             maps preset → per-backend options dict.
│   ├── encoder.py            InProcessEncoder: PyAV-backed video +
│   │   │                         audio encoder/muxer writing MKV.
│   │   │                         Accepts `game_slug` (writes MOMENTO_GAME
│   │   │                         tag), `target_width/height` (frame
│   │   │                         reformat to downscale), and
│   │   │                         `video_options` (quality preset).
│   │   ├── game_names.py         ~200-entry display-name lookup +
│   │   │                         humanise_game_name + filename helpers
│   │   ├── game_watcher.py       psutil polling + fullscreen fallback +
│   │   │                         block-list + sustained-fullscreen check.
│   │   │                         `is_running` property for tray sync.
│   │   ├── media_probe.py        MOMENTO_GAME_TAG constant.
│   │   │                         DurationProbe / probe_duration_async,
│   │   │                         MetadataProbe / probe_metadata_async
│   │   │                         (combined duration + tag in ONE ffprobe
│   │   │                         call — what the editor uses for the
│   │   │                         listing), RepairJob / repair_async,
│   │   │                         find_broken_recordings.
│   │   ├── mic_capture.py        Mic streamer for the encoder pipeline.
│   │   ├── mic_monitor.py        QObject-based mic monitor for the
│   │   │                         settings + wizard Test mic flow.
│   │   │                         Emits level_changed + optional speaker
│   │   │                         playback.
│   │   ├── recorder.py           Orchestrates WGC + WASAPI + encoder.
│   │   │                         _resolve_target_dims (resolution preset
│   │   │                         → concrete dims, never upscales),
│   │   │                         _quality_options (preset → NVENC opts).
│   │   ├── session.py            SessionManager + pause_monitoring /
│   │   │                         resume_monitoring /
│   │   │                         stop_current_recording / is_monitoring.
│   │   │                         _active_known_games filters
│   │   │                         disabled_games out of the watcher set.
│   │   ├── storage_cleanup.py    enforce_storage_limit — deletes oldest
│   │   │                         top-level .mkv + sidecars when over
│   │   │                         max_storage_gb. Clips/ never touched.
│   │   ├── thumbnails.py         QThreadPool-managed ffmpeg frame
│   │   │                         extraction
│   │   └── video_capture.py      WGC capture with settling period before
│   │                             locking encoder dims.
│   │
│   ├── trim/
│   │   └── ffmpeg_trim.py        Stream-copy export: -map_metadata 0 +
│   │                             +use_metadata_tags so MOMENTO_GAME
│   │                             survives MKV → MP4. Writes to clips/.
│   │
│   ├── youtube/                  Phase 11 — opt-in YouTube upload bridge.
│   │   ├── __init__.py
│   │   ├── auth.py               InstalledAppFlow + DPAPI token storage.
│   │   │                         connect_account / disconnect_account /
│   │   │                         is_connected / get_authorized_credentials /
│   │   │                         fetch_channel_info.
│   │   └── uploader.py           UploadOptions dataclass + UploadJob
│   │                             (QObject worker). Resumable
│   │                             MediaFileUpload at 4 MiB chunks; exp
│   │                             backoff on 5xx/network; cooperative
│   │                             cancellation. Quota cost: 1600 units
│   │                             per upload.
│   │
│   ├── ui/
│   │   ├── editor.py             EditorWindow with status panel +
│   │   │                         settings stack + list / preview /
│   │   │                         timeline / clip controls.
│   │   │                         _AnchoredComboBox for non-jumping
│   │   │                         dropdowns. File → "Run setup tutorial…"
│   │   │                         re-opens the wizard.
│   │   ├── level_meter.py        Continuous gradient bar with peak-hold
│   │   │                         tick. Used by Settings + Welcome.
│   │   ├── preview.py            VideoPreview. Empty-state stack, fixed
│   │   │                         sizeHint, click-to-play, play_range
│   │   │                         (preview just the trim portion).
│   │   ├── recordings_list.py    Card delegate (date · duration · size
│   │   │                         bullet meta), select_first / row_count /
│   │   │                         select_by_path. Right-click signals
│   │   │                         (play / rename / reveal / export /
│   │   │                         repair / delete).
│   │   ├── settings_dialog.py    SettingsPanel — 7 tabs. _make_onoff_pill
│   │   │                         (Games column), _tab_with /
│   │   │                         _tab_with_groups (width-capped pages
│   │   │                         with optional max_width), _tips_group
│   │   │                         (sub-cards).
│   │   ├── status_panel.py       Live status strip with rounded pill
│   │   │                         (Recording / Monitoring / Idle) + Mic /
│   │   │                         System audio / Free space chips.
│   │   ├── theme.py              Global dark QSS + palette
│   │   ├── timeline.py           Trim handles + bookmark ticks + time
│   │   │                         ruler + wheel zoom + pan. set_view /
│   │   │                         set_clip_range public APIs for the
│   │   │                         time-input fields.
│   │   ├── toast.py              Four-state branded toast (recording /
│   │   │                         idle / warn / bookmark) with position
│   │   │                         picker.
│   │   ├── tray.py               Expanded tray menu (Open recordings /
│   │   │                         Stop current / Pause⇄Resume monitoring /
│   │   │                         Quit). Gates failure toast on
│   │   │                         show_failure_toast.
│   │   ├── welcome.py            8-step setup wizard.
│   │   ├── youtube_upload_dialog.py    Phase 11 — modal collecting
│   │   │                                title/desc/tags/privacy/thumbnail
│   │   │                                with pre-fill from
│   │   │                                friendly_recording_title +
│   │   │                                Config defaults.
│   │   └── youtube_upload_progress.py  Phase 11 — modal owning a worker
│   │                                    QThread driving an UploadJob.
│   │                                    Progress / speed / ETA, Cancel,
│   │                                    on-success "View on YouTube"
│   │                                    button opens watch URL.
│   │
│   └── util/
│       ├── autostart.py          HKCU Run-key add/remove. Prefers the
│       │                         bundled exe at dist/Momento/Momento.exe
│       │                         when present so dev-mode toggling never
│       │                         registers the pythonw path.
│       ├── dpapi.py              Phase 11 — ctypes wrapper over crypt32
│       │                         CryptProtectData / CryptUnprotectData.
│       │                         protect(bytes) / unprotect(bytes). Used
│       │                         by youtube/auth.py for refresh-token
│       │                         storage.
│       ├── ffmpeg_path.py        ffmpeg + ffprobe resolution
│       ├── hotkey.py             RegisterHotKey + WM_HOTKEY filter
│       ├── logging_setup.py      rotating file handler + crash hook
│       ├── paths.py              %APPDATA%/Momento paths. Includes
│       │                         youtube_token_path() (DPAPI blob).
│       ├── resources.py          dev/frozen-aware resource paths.
│       │                         Includes youtube_client_secrets_path()
│       │                         which returns None when not bundled.
│       ├── screen.py             primary_refresh_rate()
│       ├── single_instance.py    msvcrt.locking lock file
│       ├── time_format.py        Single shared fmt_time + parse_time
│       └── windows_api.py        ctypes user32 helpers
│
├── resources/
│   ├── ffmpeg/  (ffmpeg.exe + ffprobe.exe)
│   ├── icons/momento.ico
│   ├── sounds/bookmark.wav
│   ├── known_games.json
│   └── youtube/
│       ├── README.md             How to produce client_secrets.json
│       └── client_secrets.json   GITIGNORED — Google Cloud OAuth client
│                                 ID/secret for the Desktop OAuth flow.
│                                 Drop yours here before building the exe.
│
├── tests/                        smoke + diagnostic scripts (not pytest)
│   ├── check_ffmpeg.py
│   ├── check_game_names.py
│   ├── check_games_list.py
│   ├── check_mkv_playback.py
│   ├── check_pyav_nvenc.py
│   ├── make_bookmark_sound.py
│   ├── make_icon.py
│   ├── seed_config.py
│   ├── smoke_editor.py
│   ├── smoke_encoder.py
│   ├── smoke_recorder.py
│   ├── smoke_settings.py
│   ├── smoke_single_instance.py
│   ├── smoke_thumbpool.py
│   ├── smoke_toast.py
│   ├── smoke_tray.py
│   ├── smoke_trim.py
│   ├── smoke_watcher.py
│   ├── smoke_wgc.py
│   ├── test_quick_capture.py
│   ├── test_session.py
│   ├── test_wasapi_capture.py
│   └── test_window_recording.py
│
├── build/
│   ├── pyinstaller.spec
│   └── pyinstaller_work/
│
└── dist/
    └── Momento/                  ~748 MB bundle
        ├── Momento.exe
        └── _internal/
```

---

## Recording pipeline (current — in-process via PyAV)

```
WGC capture (BGRA, locked size from first frame)
        │  on_frame -> stash latest under lock
        ▼
[video sender thread, 1/framerate clock]
        │  submit_video(bgra, pts_seconds)
        ▼
                          InProcessEncoder (momento/core/encoder.py)
                          ┌────────────────────────────────────────┐
WASAPI mic                │  video_q (drop-oldest, 8 deep)         │
        │  submit_mic     │      └─> (optional reformat to target) │
        ▼   →→→→→→→→→→→→→ │      └─> encode -> mux                 │
                          │                                        │
                          │  mic_q (32) ─┐                         │
WASAPI loopback           │              ├─> filter graph (amix=   │
        │  submit_sys     │  sys_q (32) ─┘   normalize=0) -> AAC   │
        ▼   →→→→→→→→→→→→→ │                                        │
                          │  -> mux ─────────────────────> .mkv    │
                          │                                        │
                          │  container.metadata["MOMENTO_GAME"]    │
                          │    = slug                              │
                          └────────────────────────────────────────┘
```

Encoder defaults (overridable per recording from Config):
- `h264_nvenc` with `preset=p4 tune=hq spatial-aq=1 temporal-aq=1`. Rate
  mode + `cq` / `b` driven by `quality_preset`.
- AAC 192 kbps stereo 48 kHz
- Stream `time_base = 1/1000` (ms), PTS = wallclock-since-start * 1000
- `pix_fmt` declared as `bgra` at submission, libav swscales to `yuv420p`
- For non-source resolution preset: frame is `frame.reformat(width=tw,
  height=th, format="yuv420p")` before `stream.encode(frame)` — one
  swscale step rather than two.

Trim export (offline, on closed file):
```
ffmpeg -ss S -to E -i in.mkv -map_metadata 0 -c copy
       -movflags +faststart+use_metadata_tags out.mp4
```

---

## Architectural deviations from original spec

These are intentional. A future session should NOT try to "fix" them back
without explicit user direction.

### 1. WGC replaces ddagrab for video
Per-window capture, not desktop. WGC targets one HWND.

### 2. WASAPI replaces dshow for mic AND system audio
Same library + ndarray format for both.

### 3. In-process libav (PyAV) replaces ffmpeg subprocess for live encode
No subprocess, no TCP, no stdin handshake, no MP4-moov-at-end risk on the
live path. Errors are synchronous Python exceptions on the worker thread.

### 4. MKV is canonical (matches OBS)
No auto-remux. Trim export is the explicit "share-out" path that emits MP4
+ faststart.

### 5. Settings is an in-window panel
`SettingsPanel` (QWidget) lives inside `EditorWindow.QStackedWidget`. Both
the embedded panel and the welcome wizard emit `settings_saved(Config)`
that the tray's `_apply_new_config` handles identically.

### 6. Bookmark hotkey is a feature
Distinct from "manual record start/stop" (out of scope).

### 7. Friendly game display names
`game_names.py` maps ~200 exes; camelCase fallback otherwise.

### 8. Default framerate matches the monitor
`framerate_auto=True`. Read once on `SessionManager` construction (Qt
thread) and cached for the watcher thread.

### 9. Game-tag persistence via container metadata
`MOMENTO_GAME=<slug>` written at record time, read async via
`MetadataProbe`. Survives Explorer rename + survives MKV → MP4 trim. The
editor's filter dropdown and grouping use the embedded tag, with the
filename-prefix regex as a fallback for legacy files.

### 10. Clips live in `clips/`
`<output>/clips/<name>.mp4`. Classification is pure path. Migration moves
legacy root-folder clips on startup.

### 11. Game-detection: curated allowlist + opt-in fallback + blocklist
- ~650-entry default list
- Opt-in `record_any_fullscreen` for unknown games, with ~150-name block
  list + 2-tick sustained-fullscreen requirement + own-pid skip

### 12. Editor preview robustness
First-frame priming + seek-time mute + fullscreen via separate top-level
host + ffprobe duration fallback + fixed `_VideoArea.sizeHint()` (so
splitter doesn't reflow on every load).

### 13. Quality / resolution / FPS presets
Resolution preset reformats frames before encode. Quality preset maps to
NVENC `cq` (CQ) or `b` (CBR). FPS combo collapses match-display + presets
+ custom into one row.

### 14. 8-step setup wizard
Single `WelcomeDialog` class. Auto-shows when `config.json` doesn't exist;
re-openable from File menu. Reuses the live `MicMonitor` + `LevelMeter` so
Test mic in the wizard works identically to the Settings tab.

### 15. Storage cleanup
`storage_cleanup.enforce_storage_limit` runs at app startup and after
every recording stops. Deletes oldest `.mkv` recordings + sidecars to fit
`max_storage_gb`. Clips folder is never touched.

### 16. UK spelling in user-facing strings
`"minimise"`, `"colour"` in comments. CSS / Qt API spelling stays US
(`color`, `setStyleSheet`) — that's the API, not user-facing text.

---

## Decision rules

**Proceed without asking when:**
- Naming of internal classes / variables / files
- Library minor version choices
- Defaults for things not specified above
- UI layout + styling within reason
- Bug fixes the user would obviously want
- Adding helper utilities small later work will need

**Pause and ask when:**
- A milestone is complete and ready for verification
- An external tool behaves differently than docs say
- A scope question isn't answered above
- About to install something heavy (>50 MB native dep)
- About to delete or rewrite a significant chunk of code

---

## Code style

- Modern Python 3.12: type hints, dataclasses, `pathlib.Path`, f-strings
- Black, 100-char lines
- `subprocess` always with list args, **never `shell=True`**
- All paths absolute via `pathlib`
- Brief inline comments only where *why* is non-obvious; never narrate
  *what* the code does
- Threading: prefer `QThreadPool` + `QRunnable` over raw QThreads
- COM-using libs (soundcard, windows_capture) init their own apartments
- Long-running daemons use `Event.wait(interval)` so `stop()` interrupts
- UK spelling in user-facing strings; US spelling in CSS / Qt API

---

## Current status — what's shipped

**All 12 original milestones shipped + Tier-3 PyAV rewrite + multiple
polish passes complete.** Build produces ~749 MB bundles at
`dist/Momento/Momento.exe`.

### Latest landed work (Phase 12 — Multi-vendor GPU encoder support, 2026-05-27)

**Why:** Previously NVIDIA-only via hardcoded `h264_nvenc`. CLAUDE.md
explicitly listed "Hardware encoders other than NVENC" as out of scope
for v1 — that line is now gone. Adding YouTube distribution made the
audience cap untenable; ~20% of gaming PCs run AMD, plus every laptop
without a dGPU runs Intel iGPU.

**New module: `momento/core/encoders.py`**

- `detect_available()` probes h264_nvenc → h264_amf → h264_qsv →
  h264_mf → libx264 by opening a real CodecContext at 320×240 yuv420p.
  Caches result module-level — one probe per process. Failed probes log
  at DEBUG (so a NVIDIA-only box doesn't spam INFO with AMF/QSV "device
  not present" messages); successes log at INFO.
- `pick_encoder(preferred=None)` returns the first available from the
  priority list, or honours an explicit pin. Raises if even libx264
  fails (which would mean the libav build is broken — we'd rather
  refuse to start than fall through silently).
- `quality_options_for(encoder, preset, custom_bitrate_kbps)` returns
  the encoder-specific options dict. Each backend uses its own option
  vocabulary; this is the single point that knows the differences:
  - NVENC: `rc=vbr cq=N` (constant-quality) or `rc=cbr b=Nk`; common
    `preset=p4 tune=hq spatial-aq=1 temporal-aq=1`
  - AMF: `rc=cqp qp_i/qp_p/qp_b=N` or `rc=cbr b=Nk`; common
    `usage=transcoding quality={speed|balanced|quality}`
  - QSV: `global_quality=N preset={faster|medium|slow} look_ahead=0`
    (lookahead off to avoid capture latency)
  - Media Foundation: `quality_vs_speed=0-100 rate_control=u_vbr|cbr`
  - libx264: `crf=N preset=ultrafast tune=zerolatency` — `ultrafast` is
    essentially mandatory for live capture; anything slower drops
    frames at 1080p60 even on fast CPUs.
- `display_name_for(encoder)` returns the human-readable label that
  the recorder logs and (in a future Phase) the Capture settings tab
  surfaces.

**Wiring in `momento/core/recorder.py`:**

- `_QUALITY_CQ` dict + `_quality_options()` deleted — replaced by the
  delegation to `encoders.quality_options_for()`.
- Recorder picks the encoder at the start of each recording via
  `encoders.pick_encoder()`. Logs "Selected video encoder: X (preset=Y)"
  so a failed recording is diagnosable.
- The InProcessEncoder constructor already accepted `video_codec` +
  `video_options` independently; recorder now passes both rather than
  relying on the NVENC default.

**Quality semantics:**

The constant-quality value (cq / qp / global_quality / crf) is held
constant across backends at `_QUALITY_FACTOR = {low: 28, medium: 23,
high: 19}`. Visual quality at the same factor is approximately
comparable across NVENC / AMF / QSV / x264 (within ~10% bitrate at
matched perceptual quality). libx264's `ultrafast` preset reaches
the same CRF but with higher bitrate to hit the quality — file sizes
on the software path will be somewhat larger than hardware-encoded
output at the same preset.

**Testing reality:**

NVENC path verified bit-identical to pre-Phase-12 output (same options
emitted, same encoder, same file). AMF + QSV paths are written
correctly per FFmpeg docs but **not yet runtime-validated on real
hardware** — the dev machine is NVIDIA-only. Treat reports from AMD /
Intel-iGPU friends as the test pass. Media Foundation + libx264 paths
verified open cleanly during probe; libx264 produces playable output
in a smoke test.

### Latest landed work (Phase 11 — YouTube upload bridge, 2026-05-26)

**The "no cloud" line in the overview was rewritten** to reflect that
Momento is local-first but now has one explicit, opt-in network feature:
a right-click → upload-to-YouTube flow on any recording or clip.

**New package: `momento/youtube/`**

- `auth.py` — InstalledAppFlow (Google's standard OAuth Desktop flow).
  The loopback redirect server is short-lived (default port 0 → OS picks
  free port). User signs in on Google's site in their browser — Momento
  never sees the password. Refresh token serialised via
  `creds.to_json()`, encrypted with DPAPI, written atomically to
  `%APPDATA%/Momento/youtube_token.dat`. Public API:
  `is_connected() / connect_account() / disconnect_account() /
  get_authorized_credentials() / fetch_channel_info(creds)`.
  `get_authorized_credentials()` auto-refreshes expired access tokens
  and clears the local blob on `RefreshError` (revoked).
- `uploader.py` — `UploadOptions` (file_path, title, desc, tags,
  category_id, privacy, thumbnail_path, made_for_kids) + `UploadJob`
  (QObject). Job emits `progress(int)`, `speed(float bytes/s)`,
  `state_changed(str)`, terminal `finished(video_id, watch_url)` or
  `failed(error_msg)`. Resumable upload via `MediaFileUpload(chunksize=
  4MiB, resumable=True)`. Exponential backoff on 5xx + network errors
  (5 retries, cap 30 s, with jitter). Cooperative `cancel()` checked
  at every chunk boundary. `_format_http_error` translates common
  YouTube reasons (`quotaExceeded`, `youtubeSignupRequired`,
  `forbidden`, …) into actionable user messages.

**New util: `momento/util/dpapi.py`**

- Thin ctypes wrapper over `crypt32.CryptProtectData /
  CryptUnprotectData`. `protect(bytes) -> bytes` /
  `unprotect(bytes) -> bytes`. Raises `DPAPIError(OSError)`. Bound to
  current Windows user — copying the token blob to another user account
  or another machine produces a clean decryption failure rather than a
  silent compromise.

**New UI: two dialogs + a settings tab**

- `momento/ui/youtube_upload_dialog.py` — `YouTubeUploadDialog`. Pre-fills
  title from `friendly_recording_title(clip.name)`, tags from
  `Config.youtube_default_tags`, privacy/category from saved defaults.
  Live character counters (red when over limit). Validates: title 1..100,
  description ≤5000, tags-joined ≤500. `get_options()` returns
  `UploadOptions`; `updated_config_defaults()` returns a new `Config`
  with the chosen privacy/category/tags baked in as the new defaults
  (caller decides whether to persist).
- `momento/ui/youtube_upload_progress.py` — `YouTubeUploadProgressDialog`.
  Owns the `QThread` + `UploadJob` lifecycle. Shows percentage / MB·s
  speed / ETA / state-string. Cancel button + close-while-uploading
  guard. On `finished`, swaps to "Upload complete." + "View on YouTube"
  button that opens the watch URL in the default browser.
- New **YouTube** tab in `SettingsPanel`. Account group:
  Connect / Switch / Disconnect buttons + "Signed in as: X" status.
  Connect runs on a `_YouTubeConnectWorker(QObject)` moved to a
  `QThread` so the OAuth flow's browser-blocking call doesn't freeze
  the settings UI. Defaults group: privacy / category / tags. Channel
  name / id are managed by the Connect/Disconnect handlers (which call
  `save_config` directly) and preserved verbatim through a normal Save
  so a defaults edit can't blow away an active sign-in.

**Editor wiring**

- `RecordingsList` gained a `upload_to_youtube_requested = pyqtSignal
  (object)` and an "Upload to YouTube…" entry in the single-selection
  right-click menu (slotted between "Export clip…" and "Repair
  recording…"). Available on both `.mkv` recordings and `.mp4` clips —
  YouTube accepts both.
- `EditorWindow._on_upload_to_youtube_requested(path)` is the gate:
  prompts "Connect YouTube account in Settings?" if not connected,
  surfaces a clear re-auth message if `get_authorized_credentials()`
  returns None, otherwise opens the upload dialog → progress dialog.
  Persists the user's chosen defaults via
  `dialog.updated_config_defaults()` immediately after Accept so the
  next upload starts from the right place.

**Config schema additions**

- `youtube_default_privacy: str = "unlisted"`
- `youtube_default_category: int = 20` (Gaming)
- `youtube_default_tags: str = ""`
- `youtube_channel_name: str = ""` (cached for "Signed in as:" UI)
- `youtube_channel_id: str = ""`

All validated in `Config.from_dict`.

**The platform reality (kept here so a future session doesn't relearn it)**

- Quota cost: `videos.insert` = **1600 units**. Default project quota
  10000 units/day = **6 uploads/day across ALL users of one Google
  Cloud project**. Quota-increase request is easier post-Google-
  verification + with real usage data.
- Test mode: ≤100 specific Google accounts in the Cloud Console's
  "Test users" list. They see a one-time "Google hasn't verified this
  app" warning on first connect — dismissable. Public use requires
  Google OAuth verification (privacy policy URL, landing page, demo
  video, manual review, 2-6 weeks typical).
- `client_secrets.json` lives at `resources/youtube/client_secrets.json`
  — **gitignored**. Each Momento build identifies itself to Google by
  whichever OAuth client is shipped in that JSON. The dev tree won't
  have one until the developer drops it in; the YouTube tab gracefully
  surfaces "this build wasn't shipped with YouTube credentials" when
  `youtube_client_secrets_path()` returns None.

### Latest landed work (Phase 9 — fullscreen overlay, migration UX, violet rebrand, perf, 2026-05-23)

Roughly chronological across this session:

**Editor window geometry persistence:**
- New `momento/util/paths.py::window_state_path()` returning
  `%APPDATA%/Momento/window_state.ini`.
- `EditorWindow._save_window_state` / `_restore_window_state` /
  `_window_state_settings` use `QSettings` (INI format) to round-trip
  `saveGeometry()` + `saveState()`. Save fires from `closeEvent`
  (covers X-click → hide-to-tray, plus full close) AND
  `QApplication.aboutToQuit` (covers tray Quit, which bypasses
  closeEvent). Restore runs in `__init__` after `setCentralWidget`.

**Quick export removed:**
- The "Quick export ▾" dropdown (Last 30 s / Last 60 s / Full
  recording) and its three helper methods (`_quick_export_last`,
  `_quick_export_full`, `_quick_export_with_suffix`) removed — the
  trim handles + keyboard nav already cover the same flows.

**Fullscreen overlay (the big one — new file `momento/ui/preview.py`):**
- `_VideoArea.mouseMoveEvent` overridden to emit a `mouse_moved`
  signal so the overlay can listen for activity over the video.
- New `_ClickJumpSlider` — overrides press/move to set value from
  pixel position, so groove clicks jump rather than stepping by
  `pageStep`. Applied to both the embedded preview and the overlay
  volume sliders.
- New `_ProgressBar` custom widget — paints a 5 px track with violet
  fill and a white playhead ball. Click-anywhere-to-seek. Hover and
  scrub-active interpolate a `swell` `pyqtProperty` from 0 → 1 over
  150 ms ease-out, growing bar to 7 px and handle to 7 px radius
  together.
- New `_FullscreenOverlay(QWidget)` — top-level frameless `Tool`
  window with `WindowStaysOnTopHint`, anchored to the host's bottom
  edge in global coordinates. Contains a progress bar over a control
  row (▶/⏸, time, 🔇, volume, ⛶ exit). Custom-painted white icons
  (`_paint_play` / `_paint_pause` / `_paint_volume` / `_paint_mute` /
  `_paint_exit_fullscreen`). Fade via `QGraphicsOpacityEffect` on a
  `_content` child widget animated by `QPropertyAnimation` with
  `QEasingCurve.OutCubic`. Initial opacity 0 → animates to 1 on the
  `on_activity()` call at end of `__init__`, so entering fullscreen
  feels like the bar arrives rather than pops.
- **Cursor poll**: 60 ms `QTimer` polling `QCursor.pos()` because
  `QVideoWidget`'s native sub-window swallows mouseMoveEvent. Restricted
  to `host.geometry().contains(pos)` so movement on another monitor
  doesn't wake the overlay.
- **Hide timer**: 2500 ms idle → fade out + `setCursor(BlankCursor)`.
  Restarts on every activity (matches YouTube / Medal).
- **Focus bouncing**: connected to `QApplication.focusWindowChanged` —
  when focus lands on the overlay (from any control click), schedules
  `_return_focus_to_host` via `QTimer.singleShot(0)`. Buttons have
  `FocusPolicy.NoFocus` to avoid grabbing focus in the first place.
  Backup `Esc/F/Space/M` `QShortcut`s installed on the overlay cover
  the millisecond window before the bounce.
- **Multi-monitor fullscreen**: `_enter_fullscreen` now uses
  `self._video_widget.screen()` (where the editor currently lives)
  via explicit `host.setScreen(target_screen)` before
  `showFullScreen()` — fixes the case where the editor is on monitor
  2 but `QApplication.primaryScreen()` returns monitor 1.
- **Aspect ratio default**: `KeepAspectRatioByExpanding` (Fill mode).
  For matched-aspect recordings (recorder native res = monitor res,
  the universal case) this produces edge-to-edge fill with no
  sub-pixel letterbox gap at fractional DPI scaling. We briefly added
  a Fit/Fill toggle button and removed it — over-engineering for an
  edge case that doesn't exist in practice for this app.

**Threaded folder-change migration (`momento/core/storage_cleanup.py`):**
- New `MigrationWorker` class with `run(pairs=None, progress_callback=None)`
  and `collect_media_pairs()`. Iterates `(src, dst)` media pairs and
  moves each via `shutil.move` (rename within drive; copy-then-delete
  across drives). Sidecars (`.thumb.jpg` / `.bookmarks.json`) follow
  their parent.
- `migrate_to_folder` collapsed from ~70 lines of parallel iteration
  to a one-liner that delegates to the worker. Used by tests / sync
  callers.
- Settings-panel `_run_migration_with_progress` spins up a `QThread`,
  moves a module-level `_MigrationDriver(QObject)` adapter to it that
  exposes `progress_changed(int, int, str)` and `finished(int, int)`
  signals. Drives a `QProgressDialog` titled *"Momento — Transferring
  files"* with per-file count + filename label. UI stays responsive;
  no more "Not Responding" freeze on cross-drive moves.
- Pre-flight `collect_media_pairs` walk feeds both the dialog max
  and the worker's iteration — no double iterdir.

**Threaded repair (`momento/ui/editor.py::_on_repair_requested`):**
- `repair_async` was already async (runs in `_POOL`), but the
  feedback was a single status-bar message and the UI was idle the
  whole time the user worried.
- Now wrapped in an **indeterminate** `QProgressDialog` (busy bar
  via `setRange(0, 0)`) titled *"Momento — Repairing recording"*
  with an `QElapsedTimer`-driven label: *"Repairing X.mkv\nElapsed:
  1m 23s"*. Modal so the user can't break state by clicking elsewhere.
- Failure dialog now uses `QMessageBox.setDetailedText(err)` so the
  400-char ffmpeg stderr lives in a "Show Details…" collapsible
  instead of bursting the dialog width.
- Snapshots `self._main_splitter.sizes()` before repair, restores
  after — defensive against wide dialogs nudging the layout on Windows.

**Settings polish:**
- `_refresh_disk_free_hint` debounced (200 ms `QTimer`) and connected
  to `textChanged` only — drops the duplicate `editingFinished` and
  protects UNC-share paths where `shutil.disk_usage` can block.
- Browse dialog: lists every mounted drive via
  `momento/util/windows_api.py::logical_drives()` (uses
  `GetLogicalDrives` bitmask, never pokes a non-existent letter); size
  persists in `window_state.ini` under `dialogs/output_folder/geometry`,
  default 900×600 on first open.
- Resolution combo labels carry the pixel dimensions inline.
- Quality combo has a dynamic per-option description label beneath.
- Inline rename editor inside `QAbstractItemView` (file picker's New
  Folder, recordings rename) — global QSS reset so the editor sits
  cleanly inside the cell instead of bursting the row.
- Volume slider in fullscreen overlay: explicit `border: none` on the
  handle to drop the inherited 2 px global ring (was clipping the
  top); explicit `setFixedHeight(22)` + 6 px QSS padding so the
  handle pill never bleeds past the slider's edges. Same height fix
  applied to the embedded preview's volume slider (24 px).

**Recordings list duration:**
- `_fmt_duration` deleted (was duplicating
  `momento.util.time_format.fmt_time`). Card delegate imports
  `fmt_time` directly.
- Duration only displays on the YouTube-style thumbnail badge, not
  in the meta line — meta is now `date · size` only.

**Violet rebrand (Phase 9 climax):**
- `theme.ACCENT` swapped from `#5b8cff` (default-blue) to **`#8b5cf6`
  (violet)** with matching `ACCENT_HOVER` and `ACCENT_PRESS`.
- All previously-hardcoded accent-blue references rewired to read
  `theme.ACCENT` at runtime: `recordings_list.py` selected card,
  `preview.py` progress bar fill, `timeline.py` clip-selection bar,
  `editor.py` bookmark chips, `toast.py` "Recording saved" accent.
- The dark BG tints (`#15171c` / `#1d2027` / `#262a33`) are still
  cool-blue tinted; the chroma is single-digit so it doesn't fight
  the dominant violet accent. Worth re-tinting toward violet in a
  future pass if cohesion needs tightening.

**Optimisation pass:**
- `GameWatcher._find_first_known` no longer fetches `"exe"` in the
  broad `process_iter` — that attr was opening every process with
  `PROCESS_QUERY_LIMITED_INFORMATION` to read its image path, the
  dominant per-poll cost. Now resolves `exe` lazily for the single
  matched process only. On a machine with ~400 processes the scan
  dropped from ~50-100 ms to ~15 ms.
- `StatusPanel._timer` slows from 1 Hz to 0.2 Hz (5 s) when the
  panel is hidden (close-to-tray or settings page active).

**Code health:**
- New module: `momento/util/format.py` — `format_bytes(n)` and
  `free_bytes_for(path)` (free-disk helper with bounded walk-up via
  `disk_usage` failures, no `Path.exists` probing — important for
  UNC paths that stall on SMB).
- New module: `momento/ui/widgets.py` — `AnchoredComboBox` shared
  across editor, settings, and welcome wizard.
- `momento/ui/recordings_list.py::_human_size` deleted (use
  `format_bytes`).
- `momento/ui/status_panel.py` — uses `free_bytes_for` + `format_bytes`,
  diagnostic `logger.info` from set_config removed after stability
  confirmed.
- `momento/__main__.py` — uses `free_bytes_for`, GB→GiB unit bug in
  low-disk-warning text fixed.
- `momento/core/storage_cleanup.py` — `_move_media_files` collapsed
  into `MigrationWorker.run`; `migrate_to_folder` is now a
  one-liner.
- The "already running" QMessageBox sets `app.setWindowIcon` **before**
  constructing the dialog so the title-bar icon is Momento's, not Qt's
  default.

### Latest landed work (Phase 8 — UX polish, 2026-05-22)

Roughly chronological across multiple polish passes within this date:

**Bug fixes:**
- Preview pane no longer reflows on right-click — `_VideoArea.sizeHint()`
  fixed at `QSize(320, 180)`
- Editor window title "Momento — Editor" → "Momento"
- Games table row height locked to 38 px with `setSectionResizeMode(Fixed)`
  + per-row `setRowHeight(row, 38)` so the auto-record pill can't get
  vertically clipped

**Editor improvements:**
- Top scrubber removed from preview (timeline is the single seek bar)
- Time ruler on timeline with adaptive nice intervals
- Mouse-wheel zoom + scrollbar pan
- "Play clip portion" / Quick export ▾ (Last 30s / 60s / Full)
- Precise Start / End / Length time-input fields (`M:SS` / `H:MM:SS`)
- Bookmark chip strip
- List search box + sort combo
- Auto-select first row on tab switch + empty states
- Click-to-play on preview (YouTube-style)
- Status panel pinned to top with state pill + device + disk chips
- Recordings right-click menu expanded (Play / Open file location /
  Export clip)
- Card meta line: `date · duration · size` bullet separators, duration
  always rendered (placeholder while pending)
- Bottom panel 280 px for clip controls

**Settings improvements:**
- Per-tab width caps (920 default, 1280 Games)
- Games tab: search + filter combo + On/Off pill toggles (quiet On,
  amber Off) + single button row (left/right split)
- Audio tab: connected/missing status, Test mic with 3-state caption
  ("Mic input level" label + Listening… / Input detected / No input
  detected), Test system audio chime button
- Capture tab: collapsed FPS UI (single combo, custom row reveals
  conditionally), Resolution + Quality presets, "Recommended for most
  users" sub-card
- Output tab: max storage + low-disk threshold + Open folder button + GB
  units + clearer storage wording
- Notifications tab: position picker + extra toggles
- Startup tab: monitoring-on-launch + close-to-tray + clearer hint
- Bookmarks tab: tips card
- `_AnchoredComboBox` everywhere — popups always anchor below

**Tray:**
- Expanded menu (Open recordings folder / Stop current recording /
  Pause⇄Resume monitoring)
- `show_failure_toast` gating
- Failure toast tied to `_apply_status` recording-state changes

**Onboarding:**
- `WelcomeDialog` converted from static explainer to 8-step wizard
- Reuses `MicMonitor` + `LevelMeter` for the Test mic step
- "Run setup tutorial…" in editor File menu

**Code health:**
- `MetadataProbe` combines duration + game tag in one ffprobe call
  (halves subprocess spawns)
- Old `_DurationProbeWorker` (raw QThread per file) removed
- Three time formatters collapsed to `momento.util.time_format.fmt_time`
- `_track_rect()` hoisted in timeline ruler hot path
- Filter-rebuild storm coalesced via `QTimer.singleShot(0, ...)`
- `_game_tag_submitted` set removed in favour of `None` sentinel in cache
- `Counter`-based slug-rebuild in the filter combo
- `wheelEvent` clamp ordering fixed (was leaving `new_start < 0` in edge
  cases)
- Cache pruning on delete / rename
- New module: `momento/util/time_format.py`
- New module: `momento/core/storage_cleanup.py`
- New module: `momento/core/mic_monitor.py`
- New module: `momento/ui/level_meter.py`
- New module: `momento/ui/status_panel.py`

### Build commands

```powershell
# Dev:
C:\dev\Momento\.venv\Scripts\python.exe -m momento

# Detached (no console window):
Start-Process -FilePath 'C:\dev\Momento\.venv\Scripts\pythonw.exe' `
              -ArgumentList '-m','momento'

# Rebuild the exe:
taskkill /F /IM Momento.exe 2>&1 | Out-Null
Set-Location C:\dev\Momento
C:\dev\Momento\.venv\Scripts\python.exe -m PyInstaller `
  build\pyinstaller.spec --noconfirm `
  --distpath C:\dev\Momento\dist `
  --workpath C:\dev\Momento\build\pyinstaller_work
```

### Standing known-rough edges (none blocking)

- **Idle CPU ~4-6%** on a typical machine — Python interpreter
  baseline (~1-2%) plus the GameWatcher's `psutil.process_iter` poll
  every 2 s (~1-2%) plus Qt/PyQt6 event loop overhead. Medal sits at
  0.1-3% native C++; see "Future direction" below for the candidate
  paths to close that gap.
- **Encoder ramps up for ~1-2s at recording start** — NVENC steady-state
  takes a moment. Negligible on multi-minute clips.
- **Bundle size 749 MB** — PyAV libav DLLs + bundled ffmpeg/ffprobe +
  PyQt6 + Qt6 binaries.
- **Game-window resize *during* recording** (not startup) is still cropped
  to the first locked size. Startup-resize is handled by the WGC settling
  period. Mid-record resize is rare.
- **FFXIV-style elevated-process re-fire**: `psutil.AccessDenied` →
  treated as "dead" → watcher re-fires. Possible fix via `OpenProcess +
  GetExitCodeProcess`. Not promised.
- **Mic device disconnect mid-recording**: no detector → no toast. Listed
  as out-of-scope above.
- **Genuinely broken MKVs**: a recording whose Matroska segment header
  is truncated mid-cluster (BSOD, hard kill before
  `cluster_time_limit` flushed) can still defeat both `repair_async`
  (`ffmpeg -c copy -fflags +genpts+igndts` returns non-zero) and
  QMediaPlayer's WMF seek. In that case the file plays in the editor
  but seeking is broken; the failure dialog now surfaces the ffmpeg
  stderr in a collapsible detail section.
- **Dark BG tints still cool-blue** — `#15171c` / `#1d2027` / `#262a33`
  carry a single-digit-chroma shift toward the old accent. Cohesion
  could be tightened by re-tinting toward violet, but the chromatic
  shift is small enough to be invisible against the dominant violet
  accent — flag, don't rush.

---

## How to verify the build is healthy

```powershell
# Core pipeline
C:\dev\Momento\.venv\Scripts\python.exe C:\dev\Momento\tests\check_pyav_nvenc.py
C:\dev\Momento\.venv\Scripts\python.exe C:\dev\Momento\tests\check_ffmpeg.py
C:\dev\Momento\.venv\Scripts\python.exe C:\dev\Momento\tests\smoke_encoder.py
C:\dev\Momento\.venv\Scripts\python.exe C:\dev\Momento\tests\smoke_recorder.py
C:\dev\Momento\.venv\Scripts\python.exe C:\dev\Momento\tests\check_mkv_playback.py
C:\dev\Momento\.venv\Scripts\python.exe C:\dev\Momento\tests\smoke_trim.py

# UI
C:\dev\Momento\.venv\Scripts\python.exe C:\dev\Momento\tests\smoke_editor.py
C:\dev\Momento\.venv\Scripts\python.exe C:\dev\Momento\tests\smoke_settings.py --auto
C:\dev\Momento\.venv\Scripts\python.exe C:\dev\Momento\tests\smoke_toast.py --force
C:\dev\Momento\.venv\Scripts\python.exe C:\dev\Momento\tests\smoke_tray.py

# Built exe
Start-Process C:\dev\Momento\dist\Momento\Momento.exe
```

Logs at `%APPDATA%\Momento\logs\momento.log`. Recordings at
`<output>/<game>_<ts>.mkv`. Clips at `<output>/clips/<name>.mp4`.
Bookmarks at `<recording>.<ext>.bookmarks.json`. Thumbnails at
`<recording>.<ext>.thumb.jpg`.

---

## Quick orientation for a fresh Claude session

If you've just been handed this file in a new chat, here are the
high-leverage places to look:

- **What's the live state of the recording loop?** → `session.py`
  (`SessionManager`), `recorder.py` (`Recorder`), `encoder.py`
  (`InProcessEncoder`)
- **How does the editor wire its panels?** → `editor.py`
  (`EditorWindow._build_editor_view` builds the StatusPanel + splitter +
  bottom panel)
- **What state does the user control?** → `config.py` (`Config` dataclass)
  is the only place new settings should be defined; `settings_dialog.py`
  exposes them; `welcome.py` mirrors the most important ones for the
  wizard
- **How does the editor talk to the tray?** → both share `Config`; the
  tray owns the editor and listens to `editor.settings_saved` which
  routes through `_apply_new_config`
- **Where do recordings come from?** → `_on_game_start` in `session.py`
  computes the slug, builds the output path, and calls `recorder.start()`
  with the quality / resolution / FPS values from Config
- **Where does the editor read recordings?** → `_list_recordings()` in
  `editor.py` (scans output folder + clips/), `RecordingsList` (the card
  delegate), `MetadataProbe` async (duration + game tag)
- **Time formatting** → `momento.util.time_format.fmt_time` / `parse_time`
  is the only place; do NOT add a fourth copy
- **Bytes formatting + free-disk** → `momento.util.format.format_bytes` /
  `free_bytes_for`. Don't roll a fourth copy of `disk_usage()` either.
- **Brand colour** → `momento.ui.theme.ACCENT` is the single source. Any
  paint code wanting violet reads from it at paint time, not as a hard-
  coded literal.
- **Window state / dialog state** → `momento.util.paths.window_state_path()`
  returns the INI file; `QSettings` with `IniFormat` is the read/write
  API. Used by editor geometry + Browse-dialog size.

---

## Future direction (snapshot as of 2026-05-23)

### Where the user's head is

Momento is functionally complete for a personal game-recorder. The
remaining gap vs. commercial tools is:

1. **Idle CPU footprint**: ~4-6% sustained vs. Medal's 1-3%. Tracked
   primarily to the `psutil.process_iter` polling loop (was ~7-10%
   pre-optimisation; the GameWatcher `exe`-attr fix in Phase 9
   trimmed the worst of it). The Python interpreter + Qt6 event loop
   add another ~1-2% baseline that no amount of polling change can
   fix.
2. **No signature visual moment**. The violet rebrand (Phase 9)
   shifted the app off the default-blue category reflex, but there's
   still no "this is unmistakably Momento" beat — empty states,
   welcome wizard, "Recording saved" toast are all functional and
   forgettable. Flagged in the Phase 9 design critique; not yet acted
   on.
3. **Keyboard shortcuts invisible**. F / Space / Esc / M / arrows in
   the editor are bound but un-surfaced. The bookmark hotkey lives
   buried in Settings → Bookmarks. Power users (the primary audience
   for a recorder tool) lose discoverability.

The user has explicitly said they're **leaning toward a full C++
rewrite** to get into Medal's CPU range, but wants to sit with the
decision. The honest assessment is below.

### Considered paths to lower idle CPU

Ranked by effort × impact:

| Option | Effort | Expected gain | Risk |
|---|---|---|---|
| **A. WMI process events + slow-poll fallback** | 0.5–1 day | -1 to -2% (target ~3-4% idle) | Low — `wmi` Python wrapper is stable; service is always running |
| **B. Bump poll interval 2 s → 5 s** | 5 min | -0.4 to -0.7% | None; +3 s avg game-detection latency (invisible) |
| **C. Unload `QMediaPlayer` when editor hides** | ~30 min | -0.5% | Low; brief load on re-show |
| **D. Rust watcher binary via stdout/JSON** | 1-2 days | -1 to -2% (same target as A) | Adds Rust toolchain to dev setup |
| **E. Full C++ / Qt6 rewrite** | 6-12 weeks | -2 to -4% (Medal floor) | High; multi-session memory budget concern for Claude |

**Author's recommendation (mine, repeated for posterity):** A + B in the same
change. ~3-4% idle, half-day of work, no dependency footprint to speak
of, no UX trade-offs. Leaves the violet brand polish + the editor
keyboard-shortcut visibility for separate Phase 10 work.

**User's current lean:** the full rewrite (E), partly on principle.
Not yet committed.

### What a C++ rewrite actually looks like

If E is the direction picked:

- **Stack swap**: PyQt6 → Qt 6 C++ (almost line-for-line). PyAV →
  libavformat / libavcodec direct (C API). soundcard → WASAPI direct
  (`IAudioClient` COM). windows-capture (Rust) → WGC COM API direct,
  OR keep the Rust crate and FFI to it.
- **Code volume**: ~12-18 k lines C++ for the same features (vs.
  ~7-8 k Python). Boilerplate is in headers + Q_OBJECT macros +
  COM/libav verbosity.
- **Build system**: CMake + vcpkg or vendored deps. `windeployqt` for
  distribution. Bundling size should drop *somewhat* (Qt6 DLLs only,
  no Python runtime) but libav still dominates.
- **Iteration loop**: every change is compile + link + run. Claude's
  per-change feedback loop is 3-5× slower than Python's. Session
  context budget is the binding constraint, not capability.
- **Timeline**: 6-12 weeks calendar (~12-18 focused Claude sessions
  by my estimate), assuming nothing pathological in COM threading on
  the WGC side.
- **Result**: idle CPU 1-3% (Medal range). Native performance for
  the recording pipeline (already mostly native via libav anyway, so
  the recording itself doesn't measurably speed up). Same feature
  set as today.

### The hybrid alternative

A middle path the user should consider:

- Keep the Python UI / editor / settings as-is (where iteration is
  fast and the UX is now well-polished).
- Rewrite ONLY the hot bits (game watcher) in Rust or C, exposed as
  a small subprocess emitting JSON over stdout. Python subscribes
  via line-buffered stdin.
- ~1-2 sessions of Rust work; UI / editor / migration / repair /
  fullscreen / settings all stay as Python.
- Expected idle: ~3-4% (close to Medal). Without the multi-month
  rewrite cost.

### Phase 10+ candidates (non-perf)

Lower-priority polish items flagged in the design critique:

1. **Surface keyboard shortcuts** — `?` overlay listing every binding,
   F-key hint badge on the preview, bookmark hotkey echoed in the
   status panel.
2. **Recycle-bin storage cleanup** — `send2trash` instead of permanent
   delete, plus a one-time amber toast when auto-delete fires: *"Momento
   removed N old recordings to stay under X GB."*
3. **Signature visual moment** — pick one: empty state that teaches,
   "Recording saved" toast with a slow pulsing thumbnail, or welcome
   wizard finish beat. Per the impeccable critique, the violet rebrand
   moved the brand off the default-blue reflex but didn't add a memorable
   moment.
4. **Settings tab collapse** — fold seven tabs into ~six (merge
   Bookmarks into a future Hotkeys tab; merge Notifications into
   Startup → "Behaviour").
5. **Em-dash audit** — UI copy still has em-dashes in places (e.g.
   "Match game (native — no scaling)"). Replace with colons /
   parentheses per the design philosophy in `.agents/skills/`.
6. **Bottom-panel labelling** — first-session inline hint *"Drag the
   yellow handles to trim"* on the timeline; move Reset-zoom out of
   the time-inputs row.
7. **Dark BG re-tint toward violet** — fine-tune `BG_WINDOW` /
   `BG_PANEL` / `BG_INPUT` to lean a few degrees toward violet
   instead of the leftover blue cast.

### Decisions still pending

- C++ rewrite vs. WMI + Rust-watcher hybrid vs. status-quo + polish only.
- Whether to surface keyboard shortcuts at all (user pushed back on
  this in Phase 9 — "they're universal").
- Whether to give the recording-saved toast a signature animation
  beyond the current cross-fade.

### Notes for whichever Claude picks this up next

- Don't restart this whole discussion. The user has thought about it.
  If they bring it up again, they're at the decision point — ask which
  of the four paths (rewrite / hybrid / minimal-fix / status quo) and
  start that immediately.
- The `.agents/skills/impeccable/` and `.agents/skills/emil-design-eng/`
  skill folders are checked into the project. Use them for design
  reviews if asked — they give Momento's UI a properly framed critique
  vs. ad-hoc opinions.
- The screenshot helper `tests/screenshot_editor.py` is the fastest way
  to validate visual changes — outputs a PNG of the editor headlessly,
  no display required.
- The dev launch + rebuild + relaunch cycle is well-trodden:
  ```powershell
  Get-Process -Name pythonw,python,Momento -ErrorAction SilentlyContinue | Stop-Process -Force
  Start-Process -FilePath 'C:\dev\Momento\.venv\Scripts\pythonw.exe' -ArgumentList '-m','momento'
  ```
- A fresh PyInstaller build takes ~20 seconds; the violet release exe
  is at `dist/Momento/Momento.exe`, 4.9 MB exe, 749 MB bundle, brand
  icon embedded.
