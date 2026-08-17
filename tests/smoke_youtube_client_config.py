"""Executable contract for user-owned YouTube OAuth client configuration."""

from __future__ import annotations

import io
import json
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.youtube import client_config  # noqa: E402


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"PASS - {label}")


def _desktop_config(**installed_overrides: object) -> dict[str, object]:
    installed: dict[str, object] = {
        "client_id": "123456789012-example.apps.googleusercontent.com",
        "project_id": "example-project",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": "example-secret",
        "redirect_uris": ["http://localhost"],
    }
    installed.update(installed_overrides)
    return {"installed": installed}


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _must_reject(source: Path, target: Path, label: str) -> None:
    target.unlink(missing_ok=True)
    try:
        client_config.import_user_client_config(source)
    except client_config.OAuthClientConfigError:
        pass
    else:
        raise AssertionError(label)
    check(label, not target.exists())


def main() -> int:
    original_user_path = client_config.youtube_oauth_client_path
    original_youtube_dir = client_config.youtube_dir
    with tempfile.TemporaryDirectory(prefix="momento-youtube-client-test-") as folder:
        root = Path(folder)
        target = root / "appdata" / "youtube_oauth_client.dat"
        legacy_dir = root / "resources" / "youtube"
        legacy_dir.mkdir(parents=True)
        client_config.youtube_oauth_client_path = lambda: target
        client_config.youtube_dir = lambda: legacy_dir

        try:
            source = root / "downloaded.json"
            _write_json(source, _desktop_config())
            imported = client_config.import_user_client_config(source)
            check(
                "valid Desktop OAuth JSON imports to AppData",
                imported.source == "user" and target.is_file(),
            )
            check(
                "imported OAuth values are DPAPI-protected at rest",
                b"123456789012" not in target.read_bytes()
                and b"example-project" not in target.read_bytes()
                and b"example-secret" not in target.read_bytes(),
            )
            loaded = client_config.load_active_client_config()
            check(
                "AppData OAuth config resolves first",
                loaded is not None
                and loaded.source == "user"
                and loaded.client_id == "123456789012-example.apps.googleusercontent.com"
                and loaded.document == imported.document,
            )

            legacy = legacy_dir / "client_secrets.json"
            _write_json(legacy, _desktop_config(client_id="987654321098-legacy.apps.googleusercontent.com"))
            check(
                "AppData config takes precedence over legacy local config",
                client_config.load_active_client_config().source == "user",  # type: ignore[union-attr]
            )
            target.unlink()
            legacy_loaded = client_config.load_active_client_config()
            check(
                "legacy local config remains compatible for source runs",
                legacy_loaded is not None and legacy_loaded.source == "developer",
            )

            setattr(client_config.sys, "frozen", True)
            try:
                check(
                    "frozen builds ignore legacy developer credentials",
                    client_config.load_active_client_config() is None,
                )
            finally:
                delattr(client_config.sys, "frozen")

            target.write_bytes(b"corrupt encrypted user configuration")
            try:
                client_config.load_active_client_config()
            except client_config.OAuthClientConfigError:
                invalid_blocked_fallback = True
            else:
                invalid_blocked_fallback = False
            check("invalid AppData configuration blocks developer fallback", invalid_blocked_fallback)
            target.unlink()

            invalid_cases: list[tuple[str, object]] = [
                ("malformed JSON is rejected", "{not json"),
                ("web OAuth clients are rejected", {"web": _desktop_config()["installed"]}),
                (
                    "extra top-level OAuth client types are rejected",
                    {"installed": _desktop_config()["installed"], "web": {}},
                ),
                ("missing client IDs are rejected", _desktop_config(client_id="")),
                ("non-Google auth endpoints are rejected", _desktop_config(auth_uri="https://example.test/auth")),
                ("credentialed token URLs are rejected", _desktop_config(token_uri="https://user:pass@oauth2.googleapis.com/token")),
                ("non-local redirect URIs are rejected", _desktop_config(redirect_uris=["https://example.test/callback"])),
                ("malformed localhost ports are rejected safely", _desktop_config(redirect_uris=["http://localhost:notaport"])),
                ("oversized client secrets are rejected", _desktop_config(client_secret="x" * 513)),
            ]
            for number, (label, value) in enumerate(invalid_cases):
                candidate = root / f"invalid-{number}.json"
                if isinstance(value, str):
                    candidate.write_text(value, encoding="utf-8")
                else:
                    _write_json(candidate, value)
                _must_reject(candidate, target, label)

            oversized = root / "oversized.json"
            oversized.write_bytes(b" " * (client_config.MAX_CLIENT_CONFIG_BYTES + 1))
            _must_reject(oversized, target, "files over 64 KiB are rejected before parsing")

            _write_json(source, _desktop_config())
            client_config.import_user_client_config(source)
            good_ciphertext = target.read_bytes()
            invalid_replacement = root / "invalid-replacement.json"
            _write_json(invalid_replacement, {"web": {}})
            try:
                client_config.import_user_client_config(invalid_replacement)
            except client_config.OAuthClientConfigError:
                pass
            check(
                "failed replacement preserves the previous valid client",
                target.read_bytes() == good_ciphertext,
            )

            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            logger = logging.getLogger("momento.youtube.client_config")
            logger.addHandler(handler)
            try:
                client_config.import_user_client_config(source)
                check("first removal reports a change", client_config.remove_user_client_config())
                check("repeated removal is harmless", not client_config.remove_user_client_config())
            finally:
                logger.removeHandler(handler)
            output = stream.getvalue()
            check(
                "client and project values never enter logs",
                "123456789012" not in output and "example-project" not in output and "example-secret" not in output,
            )
            check("removing imported config leaves legacy developer config untouched", not target.exists() and legacy.exists())
        finally:
            client_config.youtube_oauth_client_path = original_user_path
            client_config.youtube_dir = original_youtube_dir

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
