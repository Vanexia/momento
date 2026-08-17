# YouTube OAuth resources

Installed Momento builds do not read an OAuth identity from this directory.
Use **Settings > YouTube > Import OAuth JSON...** and follow
[the user setup guide](../../docs/youtube-setup.md).

Source runs retain a developer fallback for local testing. If no imported
AppData client exists, a non-frozen source run can read
`resources/youtube/client_secrets.json`. The file must use Google's Desktop app
OAuth format.

`client_secrets.json` is gitignored. Do not commit it, include it in an
archive, paste it into logs, or share it in an issue. The public PyInstaller
recipe refuses to bundle it, and frozen builds ignore the fallback path.

The Settings import path is preferable for source testing because it exercises
the same validation, DPAPI storage, replacement, and removal flow as the
installer.

Momento requests:

- `https://www.googleapis.com/auth/youtube.upload`
- `https://www.googleapis.com/auth/youtube.readonly`

Google's [installed-app OAuth guide](https://developers.google.com/youtube/v3/guides/auth/installed-apps)
documents the Desktop client format.
