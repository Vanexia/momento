"""Validate and protect a user's Google Desktop OAuth client configuration."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from momento.util.dpapi import DPAPIError, protect, unprotect
from momento.util.paths import youtube_oauth_client_path
from momento.util.resources import youtube_dir

logger = logging.getLogger(__name__)

MAX_CLIENT_CONFIG_BYTES = 64 * 1024
_MAX_PROTECTED_CONFIG_BYTES = 128 * 1024
_MAX_CLIENT_VALUE_CHARS = 512
_MAX_PROJECT_ID_CHARS = 256
_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_CERT_URI = "https://www.googleapis.com/oauth2/v1/certs"
_CLIENT_CONFIG_ENTROPY = b"Momento/youtube_oauth_client/v1"


class OAuthClientConfigError(ValueError):
    """A configured file is not a safe Google Desktop OAuth client."""


@dataclass(frozen=True)
class OAuthClientConfig:
    """A validated client document ready for Google's installed-app flow."""

    source: Literal["user", "developer"]
    client_id: str
    project_id: str
    document: dict[str, Any]


def load_active_client_config() -> OAuthClientConfig | None:
    """Load the protected user client or a source-only developer fallback.

    An invalid user file is an explicit error and never falls through to a
    different developer identity. Frozen builds always ignore resource clients.
    """
    user_path = youtube_oauth_client_path()
    if user_path.is_file():
        try:
            encrypted = _read_bounded(user_path, _MAX_PROTECTED_CONFIG_BYTES)
            plaintext = unprotect(encrypted, entropy=_CLIENT_CONFIG_ENTROPY)
            document = _normalize_document(plaintext)
        except (OAuthClientConfigError, DPAPIError) as exc:
            raise OAuthClientConfigError(
                "The saved Google OAuth setup can't be read. Replace or remove it in Settings."
            ) from exc
        return _client_config("user", document)

    if getattr(sys, "frozen", False):
        return None
    developer_path = youtube_dir() / "client_secrets.json"
    if not developer_path.is_file():
        return None
    try:
        document = _normalize_document(
            _read_bounded(developer_path, MAX_CLIENT_CONFIG_BYTES)
        )
    except OAuthClientConfigError as exc:
        raise OAuthClientConfigError(
            "The local developer OAuth setup is invalid. Replace or remove it."
        ) from exc
    return _client_config("developer", document)


def import_user_client_config(source: Path) -> OAuthClientConfig:
    """Validate, DPAPI-protect, and atomically store a Desktop OAuth JSON file."""
    document = _normalize_document(
        _read_bounded(Path(source), MAX_CLIENT_CONFIG_BYTES)
    )
    configuration = _client_config("user", document)
    plaintext = (json.dumps(document, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        encrypted = protect(plaintext, entropy=_CLIENT_CONFIG_ENTROPY)
    except DPAPIError as exc:
        raise OAuthClientConfigError(
            "Windows couldn't protect the Google OAuth setup. Nothing was imported."
        ) from exc

    target = youtube_oauth_client_path()
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(encrypted)
        os.replace(temporary, target)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise OAuthClientConfigError(
            "Momento couldn't save the Google OAuth setup. Check AppData access and try again."
        ) from exc
    logger.info("YouTube OAuth client configuration imported")
    return configuration


def remove_user_client_config() -> bool:
    """Remove only the protected AppData client, leaving source files alone."""
    target = youtube_oauth_client_path()
    temporary = target.with_suffix(target.suffix + ".tmp")
    changed = target.exists() or temporary.exists()
    try:
        target.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
    except OSError as exc:
        raise OAuthClientConfigError(
            "Momento couldn't remove the imported Google OAuth setup."
        ) from exc
    if changed:
        logger.info("YouTube OAuth client configuration removed")
    return changed


def has_configured_client() -> bool:
    """Return whether a valid OAuth client is ready, including source fallback."""
    try:
        return load_active_client_config() is not None
    except OAuthClientConfigError:
        return False


def _client_config(
    source: Literal["user", "developer"], document: dict[str, Any]
) -> OAuthClientConfig:
    installed = document["installed"]
    return OAuthClientConfig(
        source=source,
        client_id=installed["client_id"],
        project_id=installed["project_id"],
        document=document,
    )


def _read_bounded(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
    except OSError as exc:
        raise OAuthClientConfigError(
            "Momento couldn't read that file. Choose the JSON downloaded from Google Cloud."
        ) from exc
    if not raw:
        raise OAuthClientConfigError("The selected Google OAuth file is empty.")
    if len(raw) > limit:
        raise OAuthClientConfigError("The selected Google OAuth file is larger than 64 KiB.")
    return raw


def _normalize_document(raw: bytes) -> dict[str, Any]:
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise OAuthClientConfigError("The selected file isn't valid JSON.") from exc
    if not isinstance(document, dict) or set(document) != {"installed"}:
        raise OAuthClientConfigError(
            "Choose a Desktop app OAuth file. Web application credentials aren't supported."
        )
    installed = document["installed"]
    if not isinstance(installed, dict):
        raise OAuthClientConfigError(
            "Choose a Desktop app OAuth file downloaded from Google Cloud."
        )

    client_id = _required_string(installed, "client_id", _MAX_CLIENT_VALUE_CHARS)
    if not client_id.endswith(".apps.googleusercontent.com"):
        raise OAuthClientConfigError("The OAuth client ID isn't a Google Desktop client ID.")
    client_secret = _required_string(installed, "client_secret", _MAX_CLIENT_VALUE_CHARS)
    project_id = _required_string(installed, "project_id", _MAX_PROJECT_ID_CHARS)
    auth_uri = _required_string(installed, "auth_uri", _MAX_CLIENT_VALUE_CHARS)
    token_uri = _required_string(installed, "token_uri", _MAX_CLIENT_VALUE_CHARS)
    if auth_uri != _AUTH_URI or not _safe_https_url(auth_uri):
        raise OAuthClientConfigError("The OAuth authorization endpoint isn't Google's endpoint.")
    if token_uri != _TOKEN_URI or not _safe_https_url(token_uri):
        raise OAuthClientConfigError("The OAuth token endpoint isn't Google's endpoint.")

    redirects = installed.get("redirect_uris")
    if not isinstance(redirects, list) or not redirects:
        raise OAuthClientConfigError("The Desktop OAuth file has no localhost redirect URI.")
    normalized_redirects: list[str] = []
    for redirect in redirects:
        if not isinstance(redirect, str) or not _safe_local_redirect(redirect):
            raise OAuthClientConfigError(
                "The Desktop OAuth file contains a non-local redirect URI."
            )
        normalized_redirects.append(redirect)

    normalized_installed: dict[str, Any] = {
        "client_id": client_id,
        "project_id": project_id,
        "auth_uri": auth_uri,
        "token_uri": token_uri,
        "client_secret": client_secret,
        "redirect_uris": normalized_redirects,
    }
    certificate_uri = installed.get("auth_provider_x509_cert_url")
    if certificate_uri is not None:
        certificate_uri = _validated_string(
            certificate_uri, "auth_provider_x509_cert_url", _MAX_CLIENT_VALUE_CHARS
        )
        if certificate_uri != _CERT_URI or not _safe_https_url(certificate_uri):
            raise OAuthClientConfigError("The OAuth certificate endpoint isn't Google's endpoint.")
        normalized_installed["auth_provider_x509_cert_url"] = certificate_uri
    return {"installed": normalized_installed}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _required_string(source: dict[str, Any], key: str, limit: int) -> str:
    if key not in source:
        raise OAuthClientConfigError(f"The Desktop OAuth file is missing {key}.")
    return _validated_string(source[key], key, limit)


def _validated_string(value: Any, key: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise OAuthClientConfigError(f"The Desktop OAuth file has an invalid {key}.")
    if any(ord(character) < 0x20 for character in value):
        raise OAuthClientConfigError(f"The Desktop OAuth file has an invalid {key}.")
    return value


def _safe_https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
    )


def _safe_local_redirect(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname == "localhost"
        and parsed.username is None
        and parsed.password is None
        and port is None
        and parsed.path in {"", "/"}
        and parsed.query == ""
        and parsed.fragment == ""
    )
