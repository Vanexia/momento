# Security policy

## Supported releases

Security fixes target the latest stable Momento release. Install the latest
release before reporting a problem that may already have been fixed.

## Report a vulnerability

Use GitHub's private
[Report a vulnerability](https://github.com/Vanexia/momento/security/advisories/new)
form. GitHub keeps the report and follow-up discussion inside a private
security advisory.

Do not open a public issue for a vulnerability before a fix is available. Do
not include Google OAuth JSON, refresh tokens, recordings, private file paths,
or unredacted logs in the report. If evidence contains personal data, describe
it first and wait for a safe transfer method.

Include:

- the Momento version and Windows version;
- the affected feature and steps to reproduce;
- the security impact you observed;
- whether the issue works against the current stable installer or a source
  build.

Use the public [issue tracker](https://github.com/Vanexia/momento/issues) for
ordinary bugs that do not expose data, execute unwanted code, bypass a trust
check, or cross a security boundary.

## Release integrity

Momento's Windows installer does not yet carry a code-signing certificate, so
Windows can show **Unknown Publisher**. Each GitHub Release includes a
`SHA256SUMS-<version>.txt` file. Compare the installer's SHA-256 with that file
before running it. Download both files from the same official
[Momento release](https://github.com/Vanexia/momento/releases/latest).

Installed builds accept automatic updates only after verifying signed release
metadata, the installer size, and the installer SHA-256.
