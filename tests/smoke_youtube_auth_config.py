"""OAuth authentication must use only the active validated client document."""

from __future__ import annotations

import sys
import tempfile
import hashlib
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.youtube import auth, client_config  # noqa: E402


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"PASS - {label}")


def _configuration() -> client_config.OAuthClientConfig:
    installed = {
        "client_id": "123456789012-friend.apps.googleusercontent.com",
        "project_id": "friend-project",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_secret": "friend-example-secret",
        "redirect_uris": ["http://localhost"],
    }
    return client_config.OAuthClientConfig(
        source="user",
        client_id=installed["client_id"],
        project_id=installed["project_id"],
        document={"installed": installed},
    )


def main() -> int:
    originals = {
        "load_client": client_config.load_active_client_config,
        "from_config": auth.InstalledAppFlow.from_client_config,
        "save": auth._save_credentials,
        "fetch": auth.fetch_channel_info,
        "load_credentials": auth._load_credentials,
        "disconnect": auth.disconnect_account,
        "token_path": auth.youtube_token_path,
    }
    with tempfile.TemporaryDirectory(prefix="momento-youtube-auth-") as folder:
        token = Path(folder) / "token.dat"
        token.write_bytes(b"encrypted")
        auth.youtube_token_path = lambda: token
        try:
            client_config.load_active_client_config = lambda: None
            try:
                auth.connect_account()
            except auth.YouTubeAuthError as exc:
                missing_message = str(exc)
            else:
                raise AssertionError("missing OAuth setup blocks account connection")
            check(
                "missing OAuth setup blocks account connection with a setup action",
                "import" in missing_message.lower() and "settings" in missing_message.lower(),
            )
            check("token alone is not connected without an active client", not auth.is_connected())

            configuration = _configuration()
            client_config.load_active_client_config = lambda: configuration
            captured: dict[str, object] = {}
            credentials = SimpleNamespace(
                client_id=configuration.client_id,
                client_secret=configuration.document["installed"]["client_secret"],
                expired=False,
                refresh_token="refresh",
            )

            class _Flow:
                def run_local_server(self, **_kwargs):
                    return credentials

            def from_config(document, scopes):
                captured["document"] = document
                captured["scopes"] = scopes
                return _Flow()

            auth.InstalledAppFlow.from_client_config = staticmethod(from_config)
            auth._save_credentials = lambda _creds: None
            auth.fetch_channel_info = lambda _creds: auth.ChannelInfo("channel", "Channel")
            info = auth.connect_account()
            check(
                "OAuth flow consumes the validated in-memory client document",
                captured["document"] == configuration.document
                and captured["scopes"] == auth.SCOPES
                and info.name == "Channel",
            )

            mismatched = SimpleNamespace(
                client_id="different.apps.googleusercontent.com",
                client_secret="different-secret",
                expired=False,
                refresh_token="refresh",
            )
            disconnected = {"count": 0}
            auth._load_credentials = lambda: mismatched
            auth.disconnect_account = lambda: disconnected.__setitem__(
                "count", disconnected["count"] + 1
            )
            check(
                "credentials tied to another OAuth client are discarded",
                auth.get_authorized_credentials() is None and disconnected["count"] == 1,
            )

            active = {"configuration": configuration}
            saved_after_refresh: list[object] = []

            class _RefreshingCredentials:
                client_id = configuration.client_id
                client_secret = configuration.document["installed"]["client_secret"]
                expired = True
                refresh_token = "refresh"

                def refresh(self, _request) -> None:
                    # Model setup removal while Google's refresh call is in flight.
                    token.unlink(missing_ok=True)
                    active["configuration"] = None

            auth.disconnect_account = originals["disconnect"]
            auth._load_credentials = _RefreshingCredentials
            auth._save_credentials = lambda creds, **_kwargs: saved_after_refresh.append(
                creds
            )
            client_config.load_active_client_config = lambda: active["configuration"]
            check(
                "setup removal during refresh cannot resurrect the old token",
                auth.get_authorized_credentials() is None
                and not saved_after_refresh
                and not token.exists(),
            )

            active["configuration"] = configuration
            token.write_bytes(b"original-token")

            class _RefreshCompletesBeforeRemoval:
                client_id = configuration.client_id
                client_secret = configuration.document["installed"]["client_secret"]
                expired = True
                refresh_token = "refresh"

                def refresh(self, _request) -> None:
                    pass

            def remove_immediately_after_save(_creds, **_kwargs):
                refreshed_blob = b"refreshed-token"
                token.write_bytes(refreshed_blob)
                active["configuration"] = None
                return hashlib.sha256(refreshed_blob).digest()

            auth._load_credentials = _RefreshCompletesBeforeRemoval
            auth._save_credentials = remove_immediately_after_save
            check(
                "setup removal after the refresh precheck deletes the just-written token",
                auth.get_authorized_credentials() is None and not token.exists(),
            )

            class _FailingFlow:
                def run_local_server(self, **_kwargs):
                    raise RuntimeError("123456789012-friend.apps.googleusercontent.com")

            client_config.load_active_client_config = lambda: configuration
            auth.InstalledAppFlow.from_client_config = staticmethod(
                lambda *_args, **_kwargs: _FailingFlow()
            )
            try:
                auth.connect_account()
            except auth.YouTubeAuthError as exc:
                safe_error = str(exc)
            else:
                raise AssertionError("OAuth failure is surfaced")
            check(
                "OAuth failures never echo client identity values",
                "123456789012" not in safe_error and "couldn't complete" in safe_error.lower(),
            )
        finally:
            client_config.load_active_client_config = originals["load_client"]
            auth.InstalledAppFlow.from_client_config = originals["from_config"]
            auth._save_credentials = originals["save"]
            auth.fetch_channel_info = originals["fetch"]
            auth._load_credentials = originals["load_credentials"]
            auth.disconnect_account = originals["disconnect"]
            auth.youtube_token_path = originals["token_path"]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
