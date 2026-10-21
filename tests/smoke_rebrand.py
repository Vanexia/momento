"""Rebrand compatibility checks using isolated profiles, real locks and signatures."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from momento.config import load_config, save_config  # noqa: E402
from momento.util import paths, autostart  # noqa: E402
from momento.util.single_instance import AlreadyRunningError, SingleInstance  # noqa: E402
from momento.updater.metadata import (  # noqa: E402
    UpdatePolicyError, canonical_manifest_bytes, parse_signed_manifest,
)
from momento.updater.cache import UpdateCache  # noqa: E402
from momento.updater.client import UpdateClient, UpdateStatus, GITHUB_LATEST_API  # noqa: E402
from smoke_update_client import FakeResponse, FakeSession, _fixture, _public_pem  # noqa: E402


class RebrandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mementos_rebrand_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.roaming = self.root / "roaming"
        self.local = self.root / "local"
        self.env = patch.dict(os.environ, APPDATA=str(self.roaming), LOCALAPPDATA=str(self.local))
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_fresh_profile_uses_new_name_and_roundtrips_settings(self):
        config = load_config()
        config.framerate = 30
        save_config(config)
        self.assertTrue((self.roaming / "Mementos" / "config.json").is_file())
        self.assertEqual(load_config().framerate, 30)
        self.assertEqual(paths.update_cache_dir(), self.local / "Mementos" / "updates")
        self.assertEqual(paths.default_output_folder().name, "Mementos")

    def test_existing_profile_keeps_settings_and_encrypted_account_files(self):
        old = self.roaming / "Momento"
        old.mkdir(parents=True)
        output = str(self.root / "recordings")
        (old / "config.json").write_text(json.dumps({"framerate": 30, "output_folder": output}))
        (old / "youtube_token.dat").write_bytes(b"encrypted legacy account fixture")
        config = load_config()
        self.assertEqual(config.output_folder, Path(output))
        self.assertEqual(config.framerate, 30)
        save_config(config)
        self.assertEqual(paths.youtube_token_path().read_bytes(), b"encrypted legacy account fixture")
        self.assertFalse((self.roaming / "Mementos").exists())

    def test_legacy_updater_trust_is_not_reset(self):
        old = self.local / "Momento" / "updates"
        old.mkdir(parents=True)
        (old / "trusted-state.json").write_bytes(b"persisted replay protection")
        self.assertEqual(paths.update_cache_dir(), old)
        self.assertEqual((paths.update_cache_dir() / "trusted-state.json").read_bytes(), b"persisted replay protection")

    def test_lock_only_legacy_directory_does_not_claim_a_fresh_profile(self):
        old = self.roaming / "Momento"
        old.mkdir(parents=True)
        (old / "momento.lock").write_text("0")
        self.assertEqual(paths.appdata_dir(), self.roaming / "Mementos")

    def test_empty_new_profile_does_not_hide_existing_settings(self):
        old = self.roaming / "Momento"
        old.mkdir(parents=True)
        (old / "config.json").write_text(json.dumps({"framerate": 30}))
        (old / "youtube_token.dat").write_bytes(b"encrypted legacy account fixture")
        new = self.roaming / "Mementos"
        new.mkdir()
        for log_only in (False, True):
            with self.subTest(log_only=log_only):
                if log_only:
                    (new / "logs").mkdir()
                    (new / "logs" / "mementos.log").write_text("interrupted startup")
                self.assertEqual(load_config().framerate, 30)
                self.assertEqual(paths.youtube_token_path().read_bytes(),
                                 b"encrypted legacy account fixture")
                self.assertEqual(paths.appdata_dir(), old)

    def test_populated_new_profile_remains_authoritative(self):
        for name, fps in (("Momento", 30), ("Mementos", 60)):
            profile = self.roaming / name
            profile.mkdir(parents=True)
            (profile / "config.json").write_text(json.dumps({"framerate": fps}))
        self.assertEqual(load_config().framerate, 60)
        self.assertEqual(paths.appdata_dir(), self.roaming / "Mementos")

    def test_new_update_trust_survives_empty_or_populated_legacy_cache(self):
        private = Ed25519PrivateKey.generate()
        public = _public_pem(private)
        new = self.local / "Mementos" / "updates"
        cache = UpdateCache(new, public)
        _, data, signature, _, _ = _fixture(private, version="0.2.10", metadata_version=10)
        manifest = parse_signed_manifest(data, signature, public, current_version="0.2.8")
        cache.accept_metadata(manifest, data)
        old = self.local / "Momento" / "updates"
        old.mkdir(parents=True)
        _, lower_data, lower_signature, _, _ = _fixture(
            private, version="0.2.9", metadata_version=9)
        lower = parse_signed_manifest(lower_data, lower_signature, public,
                                      current_version="0.2.8")
        for populated in (False, True):
            with self.subTest(populated=populated):
                if populated:
                    UpdateCache(old, public).accept_metadata(lower, lower_data)
                selected = UpdateCache(paths.update_cache_dir(), public)
                self.assertEqual(selected.root, new)
                with self.assertRaisesRegex(ValueError, "rolled back"):
                    selected.accept_metadata(lower, lower_data)

    def test_empty_new_cache_keeps_existing_legacy_trust(self):
        old = self.local / "Momento" / "updates"
        old.mkdir(parents=True)
        (old / "trusted-state.json").write_bytes(b"legacy trust fixture")
        new = self.local / "Mementos" / "updates"
        new.mkdir(parents=True)
        self.assertEqual(paths.update_cache_dir(), old)

    def test_corrupt_new_trust_does_not_fall_back_to_legacy(self):
        private = Ed25519PrivateKey.generate()
        public = _public_pem(private)
        for name in ("Momento", "Mementos"):
            directory = self.local / name / "updates"
            directory.mkdir(parents=True)
        new = self.local / "Mementos" / "updates"
        (new / "trusted-state.json").write_bytes(b"corrupt")
        _, data, signature, _, _ = _fixture(private, version="0.2.9")
        manifest = parse_signed_manifest(data, signature, public, current_version="0.2.8")
        selected = UpdateCache(paths.update_cache_dir(), public)
        self.assertEqual(selected.root, new)
        with self.assertRaisesRegex(ValueError, "corrupt"):
            selected.accept_metadata(manifest, data)

    def test_new_and_old_apps_cannot_record_concurrently(self):
        old = self.roaming / "Momento"
        old.mkdir(parents=True)
        options = dict(mutex_name=f"Mementos.Rebrand.Test.{os.getpid()}",
                       update_gate_name=f"Mementos.Rebrand.Gate.Test.{os.getpid()}")
        legacy = SingleInstance(old / "momento.lock", **options)
        legacy.acquire()
        self.addCleanup(legacy.release)
        # A fresh Mementos profile must still lock out a running old app.
        (self.roaming / "Mementos").mkdir()
        new = SingleInstance(**options)
        with self.assertRaises(AlreadyRunningError):
            new.acquire()
        self.assertTrue(legacy.is_acquired)

    @unittest.skipUnless(sys.platform == "win32", "Windows registry")
    def test_autostart_replaces_old_entry_and_disable_removes_both(self):
        import winreg
        key_path = rf"Software\MementosRebrandTest\{os.getpid()}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, "Momento", 0, winreg.REG_SZ, "old.exe")
        self.addCleanup(winreg.DeleteKey, winreg.HKEY_CURRENT_USER, key_path)
        with patch.object(autostart, "REG_KEY", key_path):
            autostart.set_autostart(True)
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                self.assertEqual(winreg.QueryInfoKey(key)[1], 1)
                self.assertEqual(winreg.EnumValue(key, 0)[0], "Mementos")
                self.assertTrue(winreg.QueryValueEx(key, "Mementos")[0])
                with self.assertRaises(FileNotFoundError):
                    winreg.QueryValueEx(key, "Momento")
            autostart.set_autostart(False)
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                self.assertEqual(winreg.QueryInfoKey(key)[1], 0)

    def test_signed_new_installer_download_and_legacy_contract(self):
        private = Ed25519PrivateKey.generate()
        public = _public_pem(private)
        for brand in ("Mementos", "Momento"):
            with self.subTest(brand=brand):
                responses, metadata, _, installer, old_url = _fixture(private, version="0.2.9")
                payload = json.loads(metadata)
                new_url = old_url.replace("MomentoSetup", f"{brand}Setup")
                payload["installer"]["name"] = f"{brand}Setup-0.2.9.exe"
                payload["installer"]["url"] = new_url
                data = canonical_manifest_bytes(payload)
                signature = base64.b64encode(private.sign(data))
                release = json.loads(responses[GITHUB_LATEST_API]._body)
                for asset in release["assets"]:
                    asset["name"] = asset["name"].replace("Momento", brand)
                    asset["browser_download_url"] = asset["browser_download_url"].replace("Momento", brand)
                    if asset["name"].endswith(".json"):
                        body = data
                    elif asset["name"].endswith(".sig"):
                        body = signature
                    else:
                        body = installer
                    asset["size"] = len(body)
                    responses[asset["browser_download_url"]] = FakeResponse(body, url=asset["browser_download_url"])
                responses[GITHUB_LATEST_API] = FakeResponse(json.dumps(release).encode(), url=GITHUB_LATEST_API)
                result = UpdateClient(session=FakeSession(responses), public_key=public,
                                      cache=UpdateCache(self.root / brand, public)).check(current_version="0.2.8")
                self.assertEqual(result.status, UpdateStatus.AVAILABLE)
                self.assertEqual(hashlib.sha256(result.staged.installer_path.read_bytes()).hexdigest(),
                                 hashlib.sha256(installer).hexdigest())
                self.assertEqual(result.staged.manifest.installer.name, f"{brand}Setup-0.2.9.exe")

    def test_bridge_upgrade_preserves_trust_across_brand_and_next_release(self):
        private = Ed25519PrivateKey.generate()
        public = _public_pem(private)
        cache = UpdateCache(self.root / "bridge-cache", public)

        def release_contract(version, brand, metadata_version,
                             expires_at="2027-02-13T12:00:00Z"):
            responses, metadata, _, installer, _ = _fixture(
                private, version=version, metadata_version=metadata_version,
                expires_at=expires_at)
            payload = json.loads(metadata)
            payload["installer"]["name"] = f"{brand}Setup-{version}.exe"
            payload["installer"]["url"] = payload["installer"]["url"].replace(
                "MomentoSetup", f"{brand}Setup")
            data = canonical_manifest_bytes(payload)
            signature = base64.b64encode(private.sign(data))
            release = json.loads(responses[GITHUB_LATEST_API]._body)
            old_assets = list(release["assets"]) if brand == "Mementos" else []
            new_assets = []
            for original in release["assets"]:
                asset = dict(original)
                asset["name"] = asset["name"].replace("Momento", brand)
                asset["browser_download_url"] = asset["browser_download_url"].replace("Momento", brand)
                body = (data if asset["name"].endswith(".json") else
                        signature if asset["name"].endswith(".sig") else installer)
                asset["size"] = len(body)
                responses[asset["browser_download_url"]] = FakeResponse(
                    body, url=asset["browser_download_url"])
                new_assets.append(asset)
            release["assets"] = old_assets + new_assets
            responses[GITHUB_LATEST_API] = FakeResponse(json.dumps(release).encode(),
                                                       url=GITHUB_LATEST_API)
            return responses

        current = "0.2.8"
        for version, old_sequence, new_sequence in (
            ("0.2.9", 4_000_018, 4_000_019),
            ("0.2.10", 4_000_020, 4_000_021),
            ("0.2.11", 4_000_022, 4_000_023),
            # 0.2.12 onwards reserves 1000 re-sign revisions per identity.
            ("0.2.12", 4_000_024_000, 4_000_025_000),
        ):
            with self.subTest(version=version):
                old = UpdateClient(
                    session=FakeSession(release_contract(version, "Momento", old_sequence)),
                    public_key=public, cache=cache).check(current_version=current)
                self.assertEqual(old.status, UpdateStatus.AVAILABLE)
                self.assertEqual(old.staged.manifest.metadata_version, old_sequence)
                # A successful startup removes the installer, but retains trust.
                cache.discard_version(version)
                current = version
                collision = UpdateClient(
                    session=FakeSession(release_contract(version, "Mementos", old_sequence)),
                    public_key=public, cache=cache).check(current_version=current)
                self.assertEqual(collision.status, UpdateStatus.FAILED)
                new = UpdateClient(
                    session=FakeSession(release_contract(version, "Mementos", new_sequence)),
                    public_key=public, cache=cache).check(current_version=current)
                self.assertEqual(new.status, UpdateStatus.CURRENT)
                trust = json.loads((cache.root / "trusted-state.json").read_text())
                self.assertEqual(trust["metadata_version"], new_sequence)

        # Re-signing the same release to extend its expiry: reusing the number
        # with new bytes is rejected (the trust store working as designed), so
        # the re-sign takes the next revision, which the 0.2.12 scheme leaves
        # room for.
        extended = "2027-08-13T12:00:00Z"
        reused = UpdateClient(
            session=FakeSession(release_contract(
                "0.2.12", "Mementos", 4_000_025_000, expires_at=extended)),
            public_key=public, cache=cache).check(current_version="0.2.12")
        self.assertEqual(reused.status, UpdateStatus.FAILED)
        resigned = UpdateClient(
            session=FakeSession(release_contract(
                "0.2.12", "Mementos", 4_000_025_001, expires_at=extended)),
            public_key=public, cache=cache).check(current_version="0.2.12")
        self.assertEqual(resigned.status, UpdateStatus.CURRENT)
        trust = json.loads((cache.root / "trusted-state.json").read_text())
        self.assertEqual(trust["metadata_version"], 4_000_025_001)

    def test_new_name_does_not_allow_other_hosts_versions_or_paths(self):
        private = Ed25519PrivateKey.generate()
        _, metadata, _, _, _ = _fixture(private, version="0.2.9")
        payload = json.loads(metadata)
        allowed_url = "https://github.com/Vanexia/momento/releases/download/v0.2.9/MementosSetup-0.2.9.exe"
        for filename, url in (
            ("MementosSetup-0.2.9.exe", allowed_url.replace("github.com", "example.com")),
            ("MementosSetup-0.2.8.exe", allowed_url),
            ("../MementosSetup-0.2.9.exe", allowed_url),
            ("MementosSetup-0.2.9.exe", allowed_url.replace("Vanexia", "someone-else")),
            ("MementosSetup-0.2.9.exe", allowed_url.replace("v0.2.9", "v0.2.8")),
        ):
            with self.subTest(filename=filename, url=url):
                payload["installer"].update(name=filename, url=url)
                data = canonical_manifest_bytes(payload)
                with self.assertRaises(UpdatePolicyError):
                    parse_signed_manifest(data, base64.b64encode(private.sign(data)),
                                          _public_pem(private), current_version="0.2.8")


if __name__ == "__main__":
    unittest.main(verbosity=2)
