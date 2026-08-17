# Friend-ready YouTube and release hardening implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Release Momento 0.2.5 with bring-your-own Google OAuth setup, share-safe diagnostics, hardened release tooling, accurate public documentation, and sanitized GitHub-hosted history.

**Architecture:** A standard-library-only client configuration boundary stores validated Desktop OAuth JSON in AppData and retains a legacy local-resource fallback without ever bundling it. Existing account and upload modules consume the resolved client path, while Settings provides the setup state machine and guide. Independent security changes remain narrowly scoped and receive their own executable regressions.

**Tech Stack:** Python 3.12, PyQt6, Google OAuth installed-app flow, PyInstaller, Inno Setup, PowerShell, GitHub CLI/API.

## Global Constraints

- Build only from the independent sanitized clone.
- Never commit, bundle, print, or transmit an OAuth client value or personal identity value.
- Preserve the maintainer's ignored local `resources/youtube/client_secrets.json` and legacy runtime compatibility.
- Keep all network activity opt-in except the documented installed-build startup update check.
- Use tests first for every production behavior change.
- Version the finished release as 0.2.5.

---

### Task 1: OAuth client configuration boundary

**Files:**
- Create: `momento/youtube/client_config.py`
- Modify: `momento/util/paths.py`
- Modify: `momento/util/resources.py`
- Test: `tests/smoke_youtube_client_config.py`

**Interfaces:**
- Produces `OAuthClientConfigError`, `OAuthClientConfig`,
  `load_active_client_config() -> OAuthClientConfig | None`,
  `has_configured_client() -> bool`, `import_user_client_config(Path) ->
  OAuthClientConfig`, and `remove_user_client_config() -> bool`.
- Produces `youtube_oauth_client_path() -> Path` in `momento.util.paths`.
- Keeps `youtube_client_secrets_path()` as a compatibility wrapper around the
  resolved client path and makes `youtube_upload_available()` describe feature
  support rather than the presence of a distributor credential.

- [ ] Write a smoke test using temporary APPDATA/resources paths that asserts a
  valid Google Desktop JSON is normalized, DPAPI-protected, and atomically
  stored; AppData wins over a source-only legacy resource; invalid AppData
  blocks fallback; frozen builds ignore the fallback; and no client value
  appears in logs or stored plaintext.
- [ ] Add cases that reject files over 64 KiB, malformed JSON, `web` clients,
  missing fields, non-Google endpoints, credentialed URLs, non-local redirects,
  and unsafe string sizes without publishing a target file.
- [ ] Run `python tests/smoke_youtube_client_config.py` and observe failure
  because the module/API does not exist.
- [ ] Implement the bounded validator, normalized writer, resolver, remover,
  and paths with no Google imports.
- [ ] Re-run the smoke test and the public-release/privacy tests until green.

### Task 2: Discoverable YouTube setup and upload routing

**Files:**
- Create: `momento/ui/youtube_setup_dialog.py`
- Modify: `momento/ui/settings_dialog.py`
- Modify: `momento/ui/editor.py`
- Modify: `momento/ui/recordings_list.py`
- Modify: `momento/youtube/auth.py`
- Test: `tests/smoke_youtube_setup.py`
- Test: `tests/smoke_youtube_auth_async.py`
- Test: `tests/smoke_settings.py`

**Interfaces:**
- The setup dialog emits/returns an import result through the client-config
  API and opens only allowlisted HTTPS documentation links.
- Settings exposes a setup group, keeps connection controls disabled until a
  client exists, and refreshes account/setup state after import/remove.
- Editor upload requests route an unconfigured user to Settings > YouTube.

- [ ] Write GUI smoke tests asserting YouTube navigation and upload actions are
  always visible, the unconfigured upload action opens setup, connect is
  disabled before import, valid import enables it, and replacement/removal
  disconnects existing local account state.
- [ ] Run the focused tests and observe failures against the hidden/dead current
  UI.
- [ ] Implement the accessible guide dialog with keyboard-focusable buttons,
  word-wrapped status, descriptive accessible names, official links, import,
  and close actions.
- [ ] Update Settings state handling and concise error/success copy; escape all
  rich-text channel names before rendering.
- [ ] Route editor/context-menu upload actions through setup when necessary and
  preserve the existing asynchronous credential and upload flow after setup.
- [ ] Update auth errors and current quota wording without exposing client
  details.
- [ ] Run the YouTube, Settings, worker-lifecycle, and editor suites until green.

### Task 3: Public bundle contract

**Files:**
- Modify: `build/pyinstaller.spec`
- Modify: `scripts/build_installer.ps1`
- Modify: `tests/smoke_public_release.py`
- Modify: `tests/smoke_installer_contract.py`
- Modify: `tests/smoke_update_client.py`

**Interfaces:**
- The frozen app always includes pinned Google/requests libraries.
- No environment flag can bundle `client_secrets.json`.

- [ ] Change packaging tests first to require Google hidden imports, reject any
  OAuth-bundling flag/path, reject client JSON in source/bundle, and require the
  new AppData runtime file in purge/privacy lists.
- [ ] Run the packaging tests and observe the old opt-in bundle contract fail.
- [ ] Remove the distributor OAuth build profile, always collect the Google
  runtime, and keep the OAuth identity absent from datas.
- [ ] Update installer purge handling for the imported JSON and temporary file.
- [ ] Run the packaging/public-source gates until green.

### Task 4: PyAV verifier trust boundary

**Files:**
- Modify: `tests/smoke_pyav_runtime_contract.py`
- Modify: `scripts/verify_pyav_runtime.py`

**Interfaces:**
- `verify_runtime()` returns a report containing only the failed artifact hash
  check for a mismatched wheel and performs no ZIP open/extraction/functional
  import.

- [ ] Add a regression with a deliberately mismatched non-ZIP `.whl` and patch
  `_functional_checks` to fail if reached.
- [ ] Run the unit-contract path and observe the current implementation attempt
  to open the file.
- [ ] Return immediately after a missing/mismatched expected hash.
- [ ] Re-run the focused contract test and verified runtime test until green.

### Task 5: Share-safe bounded diagnostics

**Files:**
- Create: `tests/smoke_log_privacy.py`
- Modify: `momento/trim/ffmpeg_trim.py`
- Modify: `momento/core/recorder.py`
- Modify: `momento/core/session.py`
- Modify: `momento/core/encoder.py`
- Modify: `momento/core/bookmarks.py`
- Modify: `momento/core/media_probe.py`
- Modify: `momento/core/audio_loopback.py`
- Modify: `momento/core/audio_devices.py`
- Modify: `momento/core/mic_capture.py`
- Modify: `momento/core/mic_monitor.py`
- Modify: `momento/util/single_instance.py`

**Interfaces:**
- Trim diagnostics use `trim_<timestamp>_<nonce>.log`, retain at most five
  files, and contain operation/timing/exit details without input/output paths.
- Persistent log arguments use operation labels, safe counts, or generic roles,
  never full media paths, endpoint IDs, or configured device keys.

- [ ] Add a source-and-runtime regression using a sentinel Windows username,
  media title, and endpoint ID; assert the generated trim log name/content and
  captured logger records contain none of them and old trim logs are bounded.
- [ ] Run it and observe the current path/device leaks.
- [ ] Redact the exact audited log calls, use generic trim diagnostic names,
  remove the raw FFmpeg argument line, and prune old trim logs after opening a
  new one.
- [ ] Preserve actionable user-facing errors without writing identity-bearing
  paths to the persistent logger.
- [ ] Run recording, trim, audio, repair, bookmark, and single-instance suites.

### Task 6: Bounded custom thumbnails

**Files:**
- Create: `tests/smoke_youtube_thumbnail.py`
- Modify: `momento/ui/youtube_upload_dialog.py`
- Modify: `momento/youtube/uploader.py`

**Interfaces:**
- `validate_thumbnail(Path) -> tuple[str, bytes]` accepts only JPEG/PNG magic,
  a matching supported suffix, non-empty content, and at most 2 MiB using a
  2 MiB + 1 bounded read.

- [ ] Add tests for valid JPEG/PNG, wrong magic, unsupported suffix, empty,
  missing, exactly 2 MiB, over 2 MiB, and replacement between UI selection and
  upload.
- [ ] Run the focused test and observe unbounded acceptance/read behavior.
- [ ] Implement one shared bounded validator used by the dialog and uploader;
  send the returned bytes only after validation.
- [ ] Run upload-resume, cancellation, dialog, and thumbnail tests until green.

### Task 7: Friend documentation, disclosures, and reporting

**Files:**
- Create: `docs/youtube-setup.md`
- Create: `SECURITY.md`
- Modify: `README.md`
- Modify: `resources/youtube/README.md`
- Modify: `docs/index.html`
- Modify: `docs/privacy.html`
- Modify: `CLAUDE.md`

**Interfaces:**
- Documentation uses the same terms and ordered Google setup flow as the app.

- [ ] Update documentation checks first to require disclosure of the installed
  startup GitHub request, GitHub-visible IP/User-Agent metadata, bring-your-own
  OAuth storage/removal, Testing-mode seven-day expiry, exact scopes, installer
  hash verification, uninstall/purge, and private vulnerability reporting.
- [ ] Run the public-release and installer-documentation gates and observe the
  missing/false copy fail.
- [ ] Write the complete friend guide with official links, privacy warnings,
  troubleshooting, disconnect/revoke steps, and no personal contact details.
- [ ] Correct Pages privacy/network claims and README installation guidance.
- [ ] Run `stop-slop` review manually: remove filler, vague claims, passive
  voice, and inconsistent terminology, then rerun privacy scans.

### Task 8: Version 0.2.5 and release contracts

**Files:**
- Modify: `momento/__init__.py`
- Modify: `pyproject.toml`
- Modify: `resources/version_info.txt`
- Modify: `build/installer.iss`
- Modify: `build/corresponding_sources.json`
- Modify: `scripts/build_installer.ps1`
- Modify: `scripts/fetch_ffmpeg.ps1`
- Modify: `tests/smoke_installer_contract.py`
- Modify: `tests/smoke_source_archive.py`
- Modify: `tests/smoke_corresponding_source.py`
- Modify: `README.md`
- Create: `docs/releases/0.2.5.md`

**Interfaces:**
- Every release contract uses 0.2.5 and update metadata version 2000005 while
  retaining minimum updater version 0.2.2.

- [ ] Change version-contract expectations first and observe failures.
- [ ] Update every release/version/asset filename reference consistently.
- [ ] Add release notes covering YouTube setup and privacy/security fixes.
- [ ] Run installer, source, corresponding-source, updater, and privacy gates.

### Task 9: Full verification and release build

**Files:**
- Modify only as required by reproducible failures, always test first.

- [ ] Run compileall, Ruff F/E9, `pip check`, local and locked-requirement
  `pip-audit`, and all hardware-independent smoke suites.
- [ ] Run the Git history and public-release privacy scans from the independent
  clone and inspect `git diff --check` plus the complete staged diff.
- [ ] Commit with the approved public no-reply identity and push the sanitized
  `codex/release-0.2.5` branch.
- [ ] Build the deterministic installer and source assets from a clean committed
  tree with the protected update-signing key.
- [ ] Run the installed-release script against the produced 0.2.5 installer,
  including upgrade, ordinary uninstall preservation, and explicit purge.
- [ ] Exercise no-client setup routing, OAuth JSON import with a non-personal
  test project if available, account connect, upload dialog, and removal.
- [ ] Record asset SHA-256 values and verify source/bundle scans again.

### Task 10: GitHub publication and historical cleanup

**Files:**
- No source files unless verification discovers a release-note error.

- [ ] Merge/push sanitized 0.2.5 to `main`, create the signed `v0.2.5` tag, and
  publish installer, update metadata/signature, source bundles, helper, and
  checksum assets.
- [ ] Verify current CI and Pages deployments succeed and the updater discovers
  only the stable 0.2.5 release.
- [ ] Delete the draft 0.2.3 release, the 72 affected Actions runs, removable
  stale artifacts/deployments/build records, and verify they are no longer
  anonymously accessible.
- [ ] Enable private vulnerability reporting, Dependabot alerts/security
  updates, and a protective main-branch ruleset where the repository plan/API
  permits it.
- [ ] Submit the prepared private GitHub Support request for pull refs 1-4,
  cached commit views, and unreachable-object garbage collection.
- [ ] Re-clone anonymously; scan main, all tags, explicitly fetched pull refs,
  release assets, Pages, Actions metadata, commits, and public profile.
- [ ] Keep the handoff verdict on hold until the Support-dependent pull refs no
  longer expose the old objects. Send friends the repository link and installer
  SHA-256 only after that gate passes.
