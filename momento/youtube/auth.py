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
- ``client_secrets.json`` is the *app's* identity to Google, not the user's.
  Google's docs explicitly support shipping it inside distributed desktop
  binaries (the "installed application" OAuth flow).

Until Momento clears Google's OAuth verification, the consent screen will
show an "unverified app" warning. Friends added under "Test users" in the
Cloud Console can dismiss it; the public can't. See CLAUDE.md Phase 11
notes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from momento.util.dpapi import DPAPIError, protect, unprotect
from momento.util.paths import youtube_token_path
from momento.util.resources import youtube_client_secrets_path

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
# would otherwise sit listening forever — bin it after 5 min.
_FLOW_TIMEOUT_SECONDS = 300


class YouTubeAuthError(RuntimeError):
    """Surfaced to the UI as a user-facing error message."""


@dataclass(frozen=True)
class ChannelInfo:
    """Display info for the user's connected YouTube channel."""

    id: str
    name: str
    custom_url: str = ""        # @handle if the channel has one
    thumbnail_url: str = ""     # 88x88 avatar URL, useful for the Settings chip


# ---------- Public API -----------------------------------------------------

def is_connected() -> bool:
    """Cheap probe — does an encrypted token blob exist on disk?

    True doesn't guarantee the token still works (it might have been revoked
    on Google's side, or the user might have changed their Google password).
    Callers that need a working credential should use
    ``get_authorized_credentials()`` and handle its ``None`` return.
    """
    return youtube_token_path().is_file()


def connect_account() -> ChannelInfo:
    """Run the OAuth Desktop flow and persist an encrypted refresh token.

    BLOCKS until the user completes the consent in their browser (typically
    20-60 seconds) or the flow times out at 5 min. On success, writes the
    token to ``%APPDATA%/Momento/youtube_token.dat`` and returns the
    connected channel's display info.

    Raises ``YouTubeAuthError`` if:
      - ``client_secrets.json`` is missing from the bundle
      - The user cancels / closes the browser before consent
      - The token exchange fails
      - The follow-up channels.list call fails
    """
    secrets = youtube_client_secrets_path()
    if secrets is None:
        raise YouTubeAuthError(
            "YouTube upload is unavailable in this build — "
            "client_secrets.json is missing from resources/youtube/. "
            "Reinstall Momento or contact whoever shipped you this build."
        )

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
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
        logger.exception("OAuth flow failed")
        raise YouTubeAuthError(f"Sign-in did not complete: {exc}") from exc

    # Persist before fetching channel info — if channels.list fails we still
    # want the token saved so the user doesn't have to re-auth.
    _save_credentials(creds)

    try:
        info = fetch_channel_info(creds)
    except YouTubeAuthError:
        # Already logged. Token is saved; UI can prompt a manual refresh.
        raise

    logger.info("YouTube account connected: %s (%s)", info.name, info.id)
    return info


def disconnect_account() -> None:
    """Delete the local token blob. Best-effort, never raises."""
    path = youtube_token_path()
    try:
        if path.is_file():
            path.unlink()
            logger.info("YouTube token deleted: %s", path)
    except OSError:
        logger.exception("Could not delete YouTube token at %s", path)


def get_authorized_credentials() -> Optional[Credentials]:
    """Load saved credentials, refreshing the access token if expired.

    Returns ``None`` when there is no saved token, the blob is corrupt, the
    refresh token has been revoked, or the client_secrets file is missing.
    Callers should treat ``None`` as "user is not connected, surface the
    Connect button" — never as a retryable error.
    """
    creds = _load_credentials()
    if creds is None:
        return None

    # Refresh if expired (or about to expire). Credentials.expired considers
    # tokens within a small window of expiry as expired, so this catches
    # the "we're about to upload, don't fail mid-call" case.
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_credentials(creds)  # persists the new access token + expiry
        except RefreshError:
            logger.warning(
                "Refresh token rejected — user likely revoked access. "
                "Deleting local token."
            )
            disconnect_account()
            return None
        except Exception:  # noqa: BLE001
            logger.exception("Token refresh failed unexpectedly")
            return None

    return creds


def fetch_channel_info(creds: Credentials) -> ChannelInfo:
    """One youtube.channels.list call to fetch the connected channel's name.

    Costs 1 quota unit (vs 1600 for an upload), so cheap to call on Settings
    open. Raises ``YouTubeAuthError`` on API failure; the caller decides
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
        logger.warning("channels.list failed: %s", exc)
        raise YouTubeAuthError(
            f"Could not fetch your channel info: HTTP {exc.resp.status}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("channels.list failed unexpectedly")
        raise YouTubeAuthError(f"Could not reach YouTube: {exc}") from exc

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
        custom_url=snippet.get("customUrl", ""),
        thumbnail_url=thumb,
    )


# ---------- Internals ------------------------------------------------------

def _save_credentials(creds: Credentials) -> None:
    """Serialise → DPAPI encrypt → write atomically."""
    data = creds.to_json().encode("utf-8")
    try:
        encrypted = protect(data)
    except DPAPIError:
        logger.exception("DPAPI encrypt failed — token NOT persisted")
        raise YouTubeAuthError(
            "Windows DPAPI refused to encrypt the YouTube token. "
            "Your sign-in worked but Momento can't remember it across restarts."
        )

    path = youtube_token_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(encrypted)
        tmp.replace(path)  # atomic on Windows when both paths are on same vol
    except OSError as exc:
        logger.exception("Could not write YouTube token to %s", path)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise YouTubeAuthError(
            f"Could not save the YouTube token to disk: {exc}"
        ) from exc


def _load_credentials() -> Optional[Credentials]:
    """Read → DPAPI decrypt → reconstitute Credentials. None on any failure."""
    path = youtube_token_path()
    if not path.is_file():
        return None

    try:
        encrypted = path.read_bytes()
    except OSError:
        logger.exception("Could not read YouTube token at %s", path)
        return None

    try:
        decrypted = unprotect(encrypted)
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
        return Credentials.from_authorized_user_info(info, scopes=SCOPES)
    except Exception:  # noqa: BLE001
        logger.exception("Could not reconstruct Credentials from saved token")
        disconnect_account()
        return None
