"""OAuth 2.0 Desktop flow + DPAPI-encrypted refresh token persistence.

Threading: ``connect_account()`` blocks the calling thread for the duration
of the OAuth flow (the user is in their browser, the UI isn't doing anything
useful in the meantime). Settings is expected to gate the button so it can't
be clicked twice. Token load / refresh / channel fetch are all non-blocking.

Security posture:

- We never see the user's Google password. The browser-based consent flow
  exchanges an authorization code for an access + refresh token directly
  between the user's browser and Google.
- The refresh token (long-lived, the actual sensitive credential) is
  encrypted with Windows DPAPI before writing to disk — bound to the
  current Windows user account, undecryptable from another account or
  another machine.
- Each installation uses the Desktop OAuth client imported by its Windows
  user. Frozen public builds never load a bundled distributor identity.

Until a distributor's OAuth project clears Google's verification, its consent
screen can show an "unverified app" warning. Accounts registered as test users
can complete the flow while that project remains in Testing mode.
"""

from __future__ import annotations

import json
import hashlib
import hmac
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from momento.util.dpapi import DPAPIError, protect, unprotect
from momento.util.paths import youtube_token_path
from momento.youtube import client_config

logger = logging.getLogger(__name__)

# Scopes we request from the user. ``youtube.upload`` is the actual upload
# capability; ``youtube.readonly`` lets us call ``channels.list(mine=True)``
# to show the user *which* channel they signed in as in the Settings tab.
# Both scopes are flagged "sensitive" by Google — required to be listed on
# the OAuth consent screen but covered by the same verification process.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

# Browser-flow timeout. If the user signs in correctly this resolves almost
# immediately. If they close the tab without completing, the loopback server
# blocks the worker thread (and disables the Settings buttons) until it fires,
# so keep it short — 90s is ample to complete a real sign-in. (Was 300s, which
# left the UI stuck for 5 min if the user abandoned the consent page.)
_FLOW_TIMEOUT_SECONDS = 90
_MAX_CHANNEL_AVATAR_BYTES = 5 * 1024 * 1024
_MAX_TOKEN_BLOB_BYTES = 256 * 1024
_TOKEN_FILE_LOCK = threading.RLock()
_TOKEN_GENERATION = 0


class YouTubeAuthError(RuntimeError):
    """Surfaced to the UI as a user-facing error message."""


@dataclass(frozen=True)
class ChannelInfo:
    """Display info for the user's connected YouTube channel."""

    id: str
    name: str
    thumbnail_url: str = ""     # 88x88 avatar URL, useful for the Settings chip


# ---------- Public API -----------------------------------------------------

def is_connected() -> bool:
    """Cheap probe — does an encrypted token blob exist on disk?

    True doesn't guarantee the token still works (it might have been revoked
    on Google's side, or the user might have changed their Google password).
    Callers that need a working credential should use
    ``get_authorized_credentials()`` and handle its ``None`` return.
    """
    try:
        configured = client_config.load_active_client_config() is not None
    except client_config.OAuthClientConfigError:
        return False
    return configured and youtube_token_path().is_file()


def connect_account() -> ChannelInfo:
    """Run the OAuth Desktop flow and persist an encrypted refresh token.

    BLOCKS until the user completes the consent in their browser (typically
    20-60 seconds) or the flow times out at 5 min. On success, writes the
    token to ``%APPDATA%/Momento/youtube_token.dat`` and returns the
    connected channel's display info.

    Raises ``YouTubeAuthError`` if:
      - A Google Desktop OAuth file has not been imported
      - The user cancels / closes the browser before consent
      - The token exchange fails
      - The follow-up channels.list call fails
    """
    try:
        active_client = client_config.load_active_client_config()
    except client_config.OAuthClientConfigError as exc:
        raise YouTubeAuthError(
            "The saved Google OAuth setup needs attention. Replace or remove it "
            "in Settings > YouTube."
        ) from exc
    if active_client is None:
        raise YouTubeAuthError(
            "Import a Google Desktop OAuth JSON file in Settings > YouTube "
            "before connecting an account."
        )

    try:
        flow = InstalledAppFlow.from_client_config(active_client.document, SCOPES)
        # port=0 → OS picks a free port. open_browser=True spawns the user's
        # default browser. prompt='consent' forces the refresh-token grant
        # even if the user has previously authorized — without this,
        # subsequent connects sometimes return only an access token.
        creds = flow.run_local_server(
            port=0,
            open_browser=True,
            prompt="consent",
            timeout_seconds=_FLOW_TIMEOUT_SECONDS,
            success_message=(
                "Momento is connected. You can close this tab and return "
                "to the app."
            ),
        )
    except Exception as exc:  # noqa: BLE001 — third-party can raise anything
        logger.warning("OAuth flow failed (%s)", type(exc).__name__)
        raise YouTubeAuthError(
            "Couldn't complete YouTube sign-in. Return to Momento and try again."
        ) from None

    try:
        current_client = client_config.load_active_client_config()
    except client_config.OAuthClientConfigError:
        current_client = None
    if current_client is None or current_client.document != active_client.document:
        raise YouTubeAuthError(
            "The Google OAuth setup changed during sign-in. Start the connection again."
        ) from None

    # Persist before fetching channel info — if channels.list fails we still
    # want the token saved so the user doesn't have to re-auth.
    _save_credentials(creds)

    try:
        info = fetch_channel_info(creds)
    except YouTubeAuthError:
        # Already logged. Token is saved; UI can prompt a manual refresh.
        raise

    logger.info("YouTube account connected")
    return info


def disconnect_account() -> None:
    """Delete the local token blob. Best-effort, never raises."""
    global _TOKEN_GENERATION

    path = youtube_token_path()
    with _TOKEN_FILE_LOCK:
        # Increment even when the file is already absent. An in-flight refresh
        # may have loaded it just before this call and must not recreate it.
        _TOKEN_GENERATION += 1
        try:
            if path.is_file():
                path.unlink()
                logger.info("YouTube token deleted")
        except OSError:
            logger.exception("Could not delete the YouTube token")


def get_authorized_credentials() -> Optional[Credentials]:
    """Load saved credentials, refreshing the access token if expired.

    Returns ``None`` when there is no saved token, the blob is corrupt, the
    refresh token has been revoked, or the active client configuration is missing.
    Callers should treat ``None`` as "user is not connected, surface the
    Connect button" — never as a retryable error.
    """
    try:
        active_client = client_config.load_active_client_config()
    except client_config.OAuthClientConfigError:
        logger.warning("Saved YouTube OAuth setup is invalid")
        return None
    if active_client is None:
        return None

    creds = _load_credentials()
    if creds is None:
        return None
    if not _credentials_match_client(creds, active_client):
        logger.info("Discarding YouTube token tied to a different OAuth client")
        disconnect_account()
        return None

    # Refresh if expired (or about to expire). Credentials.expired considers
    # tokens within a small window of expiry as expired, so this catches
    # the "we're about to upload, don't fail mid-call" case.
    if creds.expired and creds.refresh_token:
        with _TOKEN_FILE_LOCK:
            refresh_generation = _TOKEN_GENERATION
        try:
            creds.refresh(Request())
            try:
                current_client = client_config.load_active_client_config()
            except client_config.OAuthClientConfigError:
                current_client = None
            if (
                current_client is None
                or current_client.document != active_client.document
                or not _credentials_match_client(creds, current_client)
            ):
                logger.info(
                    "Discarding refreshed credentials because the OAuth setup changed"
                )
                return None
            saved_fingerprint = _save_credentials(
                creds, expected_generation=refresh_generation
            )
            if saved_fingerprint is None:
                logger.info("Discarding refreshed credentials after account disconnect")
                return None

            try:
                current_client = client_config.load_active_client_config()
            except client_config.OAuthClientConfigError:
                current_client = None
            if (
                current_client is None
                or current_client.document != active_client.document
                or not _credentials_match_client(creds, current_client)
            ):
                _delete_credentials_if_current(saved_fingerprint)
                logger.info(
                    "Discarding refreshed credentials because the OAuth setup changed"
                )
                return None
        except RefreshError:
            logger.warning(
                "Refresh token rejected — user likely revoked access. "
                "Deleting local token."
            )
            disconnect_account()
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Token refresh failed unexpectedly (%s)", type(exc).__name__)
            return None

    return creds


def credentials_match_active_client(creds: Credentials) -> bool:
    """Return whether credentials still belong to the current OAuth setup."""
    try:
        active_client = client_config.load_active_client_config()
    except client_config.OAuthClientConfigError:
        return False
    return active_client is not None and _credentials_match_client(creds, active_client)


def _credentials_match_client(
    creds: Credentials, active_client: client_config.OAuthClientConfig
) -> bool:
    installed = active_client.document["installed"]
    return (
        getattr(creds, "client_id", None) == installed["client_id"]
        and getattr(creds, "client_secret", None) == installed["client_secret"]
    )


def fetch_channel_info(creds: Credentials) -> ChannelInfo:
    """One youtube.channels.list call to fetch the connected channel's name.

    Costs one quota unit, so it is inexpensive to call on Settings open.
    Raises ``YouTubeAuthError`` on API failure; the caller decides
    whether to surface it or fall back to cached config values.
    """
    try:
        yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
        resp = (
            yt.channels()
            .list(part="snippet", mine=True, maxResults=1)
            .execute()
        )
    except HttpError as exc:
        logger.warning("channels.list failed with HTTP %s", exc.resp.status)
        raise YouTubeAuthError(
            f"Could not fetch your channel info: HTTP {exc.resp.status}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("channels.list failed unexpectedly (%s)", type(exc).__name__)
        raise YouTubeAuthError(
            "Couldn't reach YouTube. Check your connection and try again."
        ) from exc

    items = resp.get("items") or []
    if not items:
        raise YouTubeAuthError(
            "Signed in successfully but the account has no YouTube channel. "
            "Create one at youtube.com first, then reconnect."
        )

    item = items[0]
    snippet = item.get("snippet", {})
    thumbnails = snippet.get("thumbnails", {}) or {}
    thumb = thumbnails.get("default", {}).get("url", "")
    return ChannelInfo(
        id=item.get("id", ""),
        name=snippet.get("title", "(unnamed channel)"),
        thumbnail_url=thumb,
    )


def cache_channel_avatar(thumbnail_url: str) -> Optional[Path]:
    """Download the channel avatar to a local PNG for the Settings chip.

    Best-effort and non-fatal: returns the file path on success, ``None`` on
    any failure (no network, bad URL, write error). A missing avatar is never
    worth failing a sign-in over. Safe to call from a worker thread.
    """
    if not _is_safe_avatar_url(thumbnail_url):
        if thumbnail_url:
            logger.warning("Refusing non-HTTPS YouTube avatar URL")
        return None

    from momento.util.paths import youtube_avatar_path

    try:
        with requests.get(
            thumbnail_url,
            headers={"User-Agent": "Momento"},
            timeout=(5, 10),
            stream=True,
            allow_redirects=True,
        ) as resp:
            resp.raise_for_status()
            if not _is_safe_avatar_url(resp.url):
                logger.warning("Refusing YouTube avatar redirect to a non-HTTPS URL")
                return None
            declared_size = int(resp.headers.get("Content-Length", "0") or 0)
            if declared_size > _MAX_CHANNEL_AVATAR_BYTES:
                logger.warning("YouTube avatar exceeds the %d-byte limit", _MAX_CHANNEL_AVATAR_BYTES)
                return None
            data = bytearray()
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                data.extend(chunk)
                if len(data) > _MAX_CHANNEL_AVATAR_BYTES:
                    logger.warning(
                        "YouTube avatar exceeded the %d-byte limit while downloading",
                        _MAX_CHANNEL_AVATAR_BYTES,
                    )
                    return None
    except (requests.RequestException, OSError, ValueError):
        logger.warning("Could not download YouTube avatar", exc_info=True)
        return None

    path = youtube_avatar_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(bytes(data))
        tmp.replace(path)  # atomic swap
    except OSError:
        logger.warning("Could not write the YouTube avatar cache", exc_info=True)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return path


def _is_safe_avatar_url(value: str) -> bool:
    """Accept only credential-free HTTPS URLs with a real host."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


def delete_cached_avatar() -> None:
    """Remove the cached avatar PNG. Best-effort, never raises."""
    from momento.util.paths import youtube_avatar_path

    path = youtube_avatar_path()
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        logger.warning("Could not delete cached YouTube avatar", exc_info=True)


# ---------- Internals ------------------------------------------------------

# Application-specific DPAPI entropy. Binds the encrypted token blob to Momento
# in addition to the Windows user, so generic same-user secret-stealers can't
# CryptUnprotectData the refresh token without also knowing this value. A blob
# written before this existed (no entropy) is migrated transparently on load.
_TOKEN_ENTROPY = b"Momento/youtube_token/v1"


def _save_credentials(
    creds: Credentials, *, expected_generation: int | None = None
) -> bytes | None:
    """Serialise, protect, and atomically write credentials.

    When ``expected_generation`` is supplied, a disconnect or newer token
    write cancels this save. The returned fingerprint identifies only this
    exact encrypted write so a later cleanup cannot remove a newer sign-in.
    """
    global _TOKEN_GENERATION

    data = creds.to_json().encode("utf-8")
    try:
        encrypted = protect(data, entropy=_TOKEN_ENTROPY)
    except DPAPIError:
        logger.exception("DPAPI encrypt failed — token NOT persisted")
        raise YouTubeAuthError(
            "Windows DPAPI refused to encrypt the YouTube token. "
            "Your sign-in worked but Momento can't remember it across restarts."
        )

    path = youtube_token_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    fingerprint = hashlib.sha256(encrypted).digest()
    with _TOKEN_FILE_LOCK:
        if (
            expected_generation is not None
            and expected_generation != _TOKEN_GENERATION
        ):
            return None
        try:
            tmp.write_bytes(encrypted)
            tmp.replace(path)  # atomic on Windows when both paths are on same vol
            _TOKEN_GENERATION += 1
        except OSError as exc:
            logger.exception("Could not write the YouTube token")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise YouTubeAuthError(
                "Couldn't save the YouTube sign-in securely. "
                "Check AppData access and try again."
            ) from exc
    return fingerprint


def _delete_credentials_if_current(expected_fingerprint: bytes) -> bool:
    """Delete the token only if it is the exact write being invalidated."""
    global _TOKEN_GENERATION

    path = youtube_token_path()
    with _TOKEN_FILE_LOCK:
        try:
            with path.open("rb") as handle:
                encrypted = handle.read(_MAX_TOKEN_BLOB_BYTES + 1)
            if len(encrypted) > _MAX_TOKEN_BLOB_BYTES:
                return False
            if not hmac.compare_digest(
                hashlib.sha256(encrypted).digest(), expected_fingerprint
            ):
                return False
            path.unlink()
            _TOKEN_GENERATION += 1
            return True
        except FileNotFoundError:
            return False
        except OSError:
            logger.warning("Could not remove an invalidated YouTube token")
            return False


def _load_credentials() -> Optional[Credentials]:
    """Read → DPAPI decrypt → reconstitute Credentials. None on any failure."""
    path = youtube_token_path()
    if not path.is_file():
        return None

    try:
        encrypted = path.read_bytes()
    except OSError:
        logger.exception("Could not read the YouTube token")
        return None

    migrated_from_legacy = False
    try:
        decrypted = unprotect(encrypted, entropy=_TOKEN_ENTROPY)
    except DPAPIError:
        # Migration: a blob written before app entropy existed has no entropy.
        # Try the legacy form; if it decrypts, we re-encrypt with entropy below
        # so the user never has to reconnect.
        try:
            decrypted = unprotect(encrypted)
            migrated_from_legacy = True
        except DPAPIError:
            logger.warning(
                "DPAPI decrypt failed — token blob is corrupt or was created "
                "by a different Windows account. Discarding."
            )
            disconnect_account()
            return None

    try:
        info = json.loads(decrypted.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("Token blob decrypted to non-JSON content. Discarding.")
        disconnect_account()
        return None

    try:
        creds = Credentials.from_authorized_user_info(info, scopes=SCOPES)
    except Exception:  # noqa: BLE001
        logger.exception("Could not reconstruct Credentials from saved token")
        disconnect_account()
        return None

    if migrated_from_legacy:
        # Re-encrypt with app entropy so the next load uses the hardened blob.
        # Best-effort: a failure just means we migrate again next launch.
        try:
            _save_credentials(creds)
            logger.info("Migrated YouTube token to app-entropy DPAPI encryption.")
        except YouTubeAuthError:
            logger.warning("Could not re-encrypt token with app entropy; will retry next load.")
    return creds
