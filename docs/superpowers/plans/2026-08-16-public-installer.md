# Public Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a privacy-clean Momento installer that gives a brand-new Windows user a safe first-run setup and can be installed, upgraded, launched, and uninstalled without developer-machine assumptions.

**Architecture:** Keep Momento as a PyInstaller one-folder application and wrap the complete folder with a per-user Inno Setup installer. First-run state remains defined by the absence of `%APPDATA%\Momento\config.json`; device seeding is in-memory until the user finishes setup, and game monitoring remains paused until then. Public packaging excludes the optional OAuth client identity unless the builder deliberately opts in.

**Tech Stack:** Python 3.12, PyQt6, PyInstaller 6, Inno Setup 6.7, PowerShell, Windows Registry/HKCU.

## Global Constraints

- Windows 10 1903 or later, Windows 11, x64 only.
- Per-user install under `%LOCALAPPDATA%\Programs\Momento`; no administrator rights.
- Never package `%APPDATA%`, recordings, logs, OAuth tokens, channel identity, local device selections, or developer paths.
- Public builds omit `resources/youtube/client_secrets.json` unless `MOMENTO_INCLUDE_YOUTUBE_OAUTH=1` is explicitly set.
- Preserve recordings and user settings on ordinary uninstall; remove the Momento autostart entry.
- Keep the one-folder release intact; do not hand-edit `dist` output.
- Default capture for a new user is source resolution, 60 fps, Custom 16 Mbit/s.
- Do not start game monitoring before first-time setup is finished.

---

### Task 1: Fresh-User Startup Contract

**Files:**
- Modify: `momento/config.py`
- Modify: `momento/__main__.py`
- Modify: `momento/ui/welcome.py`
- Test: `tests/smoke_first_run.py`

**Interfaces:**
- Consumes: `config_path()`, `Config`, `_seed_default_devices`, `WelcomeDialog.settings_saved`.
- Produces: `_finish_first_run(dialog_result, tray, session) -> bool`, safe `Config()` defaults, and welcome-driven autostart persistence.

- [ ] **Step 1: Write the failing startup/default tests**

```python
cfg = Config()
check("fresh default: 60 fps", cfg.framerate == 60 and not cfg.framerate_auto)
check("fresh default: 16 Mbit/s quality", cfg.quality_preset == "custom")
check("first run: seeding does not persist config", not config_path.exists())
check("first run: cancelled setup leaves monitoring paused", session.starts == 0)
check("first run: accepted setup starts opted-in monitoring", session.starts == 1)
```

- [ ] **Step 2: Run the test and observe the current defaults/start order fail**

```powershell
.\.venv\Scripts\python.exe tests\smoke_first_run.py
```

Expected: non-zero exit with High/automatic refresh defaults or premature monitoring.

- [ ] **Step 3: Implement safe startup order**

```python
is_first_run = not config_path().exists()
config = load_config()
if is_first_run:
    _seed_default_devices(config)  # in memory only

if not is_first_run and config.start_monitoring_on_launch:
    session.start()

if is_first_run:
    welcome = WelcomeDialog(config)
    welcome.settings_saved.connect(tray._apply_new_config)
    accepted = welcome.exec() == QDialog.DialogCode.Accepted
    if accepted and tray.config.start_monitoring_on_launch:
        session.start()
```

The welcome finish path must call `set_autostart(new_cfg.autostart_with_windows)` after the atomic config save and show a warning if the registry update fails.

- [ ] **Step 4: Run targeted startup and existing settings tests**

```powershell
.\.venv\Scripts\python.exe tests\smoke_first_run.py
.\.venv\Scripts\python.exe tests\smoke_settings_save.py
```

Expected: every printed check passes.

- [ ] **Step 5: Commit the fresh-user behavior**

```powershell
git add momento/config.py momento/__main__.py momento/ui/welcome.py tests/smoke_first_run.py
git commit -m "Harden Momento first-run setup"
```

### Task 2: Public-Build Privacy Boundary

**Files:**
- Modify: `build/pyinstaller.spec`
- Modify: `momento/util/resources.py`
- Modify: `momento/__main__.py`
- Modify: `momento/ui/settings_dialog.py`
- Modify: `momento/ui/editor.py`
- Modify: `momento/ui/recordings_list.py`
- Modify: `docs/index.html`
- Modify: `docs/privacy.html`
- Delete: `docs/archive/CLAUDE_HISTORY_THROUGH_2026-07-04.md`
- Test: `tests/smoke_public_release.py`

**Interfaces:**
- Produces: `youtube_upload_available() -> bool` and an opt-in OAuth bundling flag.
- Consumes: `youtube_client_secrets_path()` and UI construction paths.

- [ ] **Step 1: Write the failing privacy/build tests**

```python
assert "MOMENTO_INCLUDE_YOUTUBE_OAUTH" in spec_text
assert release_identity_is_generic(tracked_text)
assert Config().quality_preset == "custom"
assert Config().custom_bitrate_kbps == 16_000
```

- [ ] **Step 2: Run the privacy test and capture the current failures**

```powershell
.\.venv\Scripts\python.exe tests\smoke_public_release.py
```

Expected: personal contact, historical paths, AppUserModelID, and implicit OAuth bundling fail.

- [ ] **Step 3: Gate optional OAuth resources and UI**

```python
include_youtube_oauth = os.environ.get("MOMENTO_INCLUDE_YOUTUBE_OAUTH") == "1"
if include_youtube_oauth and youtube_secrets.is_file():
    datas.append((str(youtube_secrets), "resources/youtube"))

def youtube_upload_available() -> bool:
    return youtube_client_secrets_path() is not None
```

Hide or disable connect/upload actions when `youtube_upload_available()` is false and explain that uploads are unavailable in this build without exposing a developer identity.

- [ ] **Step 4: Remove identity-bearing public and archived text**

Replace the AppUserModelID with `Momento.GameRecorder`, remove personal contact links, remove private historical handoff data, and describe device/field evidence generically in `CLAUDE.md`.

- [ ] **Step 5: Run the privacy test and source scan**

```powershell
.\.venv\Scripts\python.exe tests\smoke_public_release.py
Run the public-release privacy regression scan.
```

Expected: test passes and the scan returns no matches.

- [ ] **Step 6: Commit the privacy boundary**

```powershell
git add -A
git commit -m "Prepare privacy-clean public builds"
```

### Task 3: Installer And Versioned Build

**Files:**
- Create: `build/installer.iss`
- Create: `scripts/build_installer.ps1`
- Modify: `momento/__init__.py`
- Modify: `pyproject.toml`
- Modify: `resources/version_info.txt`
- Modify: `momento/util/single_instance.py`
- Test: `tests/smoke_installer_contract.py`

**Interfaces:**
- Produces: `dist/installer/MomentoSetup-0.2.0.exe` and mutex `Momento.GameRecorder.Instance`.
- Consumes: `dist/Momento/**`, Inno Setup `ISCC.exe`, and version 0.2.0 from tracked metadata.

- [ ] **Step 1: Write installer contract tests**

```python
assert versions == {"0.2.0"}
assert "PrivilegesRequired=lowest" in installer
assert "{localappdata}\\Programs\\Momento" in installer
assert "Momento.GameRecorder.Instance" in installer
assert "RegDeleteValue(HKCU" in installer
```

- [ ] **Step 2: Run the contract test and observe missing installer/version failures**

```powershell
.\.venv\Scripts\python.exe tests\smoke_installer_contract.py
```

- [ ] **Step 3: Add the named installer mutex and release metadata**

Keep the existing lock-file enforcement, but create and retain a Windows named mutex after the file lock succeeds and close it during `release()`. Set every release version location to `0.2.0`.

- [ ] **Step 4: Add the per-user Inno installer**

The script installs the complete one-folder package, adds Start Menu and optional desktop shortcuts, migrates an existing Momento HKCU Run entry to the installed executable, removes that entry on uninstall, preserves recordings, and optionally removes `%APPDATA%\Momento` when the user requests it.

- [ ] **Step 5: Add the deterministic build wrapper**

```powershell
.\scripts\build_installer.ps1
```

The wrapper validates a clean source version, builds PyInstaller without OAuth credentials by default, rejects forbidden personal identifiers and runtime-data files in `dist/Momento`, locates Inno Setup, compiles the installer, and prints hashes and sizes.

- [ ] **Step 6: Run contract tests and commit**

```powershell
.\.venv\Scripts\python.exe tests\smoke_installer_contract.py
git add build/installer.iss scripts/build_installer.ps1 momento/__init__.py pyproject.toml resources/version_info.txt momento/util/single_instance.py tests/smoke_installer_contract.py
git commit -m "Add the Momento Windows installer"
```

### Task 4: Installed-Artifact Verification

**Files:**
- Create: `tests/smoke_installed_release.ps1`
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `dist/installer/MomentoSetup-0.2.0.exe`.
- Produces: an isolated install/uninstall log, installed-file scan, first-run-window proof, and final release fingerprint.

- [ ] **Step 1: Add isolated install verification**

Install silently to a temporary per-user directory, verify version/resources and absence of OAuth/config/log/media/personal identifiers, launch with temporary `APPDATA` and `USERPROFILE`, observe the first-time setup window, stop the test process, uninstall silently, and verify shortcuts/uninstall keys/install files are removed.

- [ ] **Step 2: Build the public package and installer**

```powershell
.\scripts\build_installer.ps1
```

Expected: PyInstaller and Inno Setup both exit 0 and emit SHA-256 values.

- [ ] **Step 3: Run installed-artifact verification**

```powershell
.\tests\smoke_installed_release.ps1
```

Expected: install, resource, privacy, first-run, launch, and uninstall checks pass with no test process/window left running.

- [ ] **Step 4: Run the full regression/security suite**

```powershell
.\.venv\Scripts\python.exe -m compileall -q momento tests
.\.venv\Scripts\ruff.exe check momento tests --select F,E9
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\pip-audit.exe --local
```

Run every CI smoke program listed in `.github/workflows/ci.yml`; expected: all pass.

- [ ] **Step 5: Update public and maintainer documentation**

Document installer usage, retention behavior, unsigned SmartScreen expectations, exact version/hash/build time, and that the public installer contains no OAuth client or user data.

- [ ] **Step 6: Commit and clean**

```powershell
git add README.md CLAUDE.md .github/workflows/ci.yml tests/smoke_installed_release.ps1
git commit -m "Verify the Momento 0.2.0 installer"
```

Remove temporary install roots, logs, test media, and helper processes. Leave only the requested installed/release app running.
