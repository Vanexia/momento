# Friend-ready YouTube and release-hardening design

## Goal

Ship Momento 0.2.5 as a public Windows installer that contains no developer
identity or Google OAuth project, but gives every user a complete, discoverable
route to configure their own Google project and then use the existing YouTube
upload experience.

## Product contract

- YouTube remains visible in Settings, the editor action bar, and recording
  context menus in every public build.
- An upload request never fails silently. Without an imported OAuth client, it
  explains the requirement and opens the YouTube setup page in Settings.
- Settings shows whether a Google OAuth file is configured separately from
  whether a YouTube account is connected.
- Users can open an in-app setup guide, import or replace a Google Desktop app
  OAuth JSON file, remove the imported file, connect or switch accounts, and
  disconnect.
- Importing, replacing, or removing the OAuth client deletes the old local
  YouTube token and cached avatar because those belong to the old client.
- Once configured and connected, the upload dialog and progress experience are
  the existing Momento experience; no source checkout or custom build is
  required.

## OAuth client storage and compatibility

- A new standard-library-only module, `momento.youtube.client_config`, owns
  bounded parsing, validation, atomic persistence, removal, and path
  resolution.
- Imported configuration is normalized, encrypted with Windows DPAPI, and
  stored at `%APPDATA%\Momento\youtube_oauth_client.dat`. It is runtime data
  and is excluded from source archives, bundles, logs, and diagnostics.
- Import accepts at most 64 KiB and requires the Google Desktop application
  `installed` schema, a Google client ID, a non-empty client secret, Google's
  HTTPS authorization/token endpoints, and localhost redirect URIs. Only an
  allowlisted normalized structure is persisted.
- Resolution prefers the AppData file. For compatibility with the maintainer's
  existing local source setup, a legacy `resources/youtube/client_secrets.json`
  is accepted only in non-frozen runs. An invalid AppData file blocks fallback.
  The public builder never bundles or reads that legacy file.
- The public frozen build always carries the pinned Google client libraries.
  It carries setup documentation, never an OAuth identity.
- Existing local ignored OAuth files are not modified by repository work.

## Setup experience

- The YouTube Settings page is always present.
- A setup group contains status text and `Import OAuth JSON...`,
  `Open setup guide`, and context-appropriate replace/remove actions.
- The guide is an accessible Momento dialog with plain-language ordered steps,
  safe clickable links to Google Cloud and official documentation, the exact
  required scopes, Testing-mode expiry, privacy implications, revocation, and
  an import action. A matching written guide lives in the repository and is
  linked from the README.
- Account controls stay disabled until a valid OAuth file exists. Upload
  defaults remain editable so users can prepare them before connecting.
- Imported-project status never displays or logs client IDs, project IDs,
  secrets, user profile paths, or source filenames.

## Security and privacy hardening

- The PyAV runtime verifier stops immediately on a missing or mismatched wheel
  hash, before opening, extracting, importing, or running the wheel.
- Persistent logs redact full paths, recording/media names, and audio endpoint
  identifiers. Trim diagnostics use generic names, a bounded retained count,
  and a redacted command description.
- Custom YouTube thumbnails must be JPEG or PNG and no larger than 2 MiB. The
  UI validates early and the uploader revalidates with a bounded read to cover
  file replacement races.
- Public Pages copy accurately discloses the single installed-build GitHub
  update check and the ordinary request metadata GitHub receives.
- `SECURITY.md` uses GitHub private vulnerability reporting rather than a
  personal contact address.

## Release and hosted-history cleanup

- Version 0.2.5 is built from the independent sanitized clone only after all
  source, history, privacy, security, packaging, and installed-release gates
  pass.
- The old draft 0.2.3 release, affected Actions runs, and removable stale
  deployment/artifact records are deleted from GitHub.
- A GitHub Support request identifies the four server-managed pull-request refs
  and asks GitHub to dereference them, remove cached commit views, and garbage
  collect the unreachable objects. Personal values are not repeated in public
  issues.
- The repository is not handed to friends until an anonymous/public re-audit
  finds no reachable private identity data on branches, tags, pull refs,
  release assets, Pages, and accessible Actions metadata.

## Verification

- Every behavior change follows red-green-refactor with executable smoke tests.
- The final pass includes compile/ruff, dependency audit, all hardware-neutral
  suites, bundle/source/history privacy gates, deterministic release build,
  installer contract, installed-release upgrade/uninstall/purge checks, and a
  manual YouTube setup-path UI exercise.
- The release notes give the installer SHA-256 and friends receive the same
  hash through a separate trusted channel because the installer is not code
  signed.
