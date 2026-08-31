from __future__ import annotations

import hashlib
import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import offline_upgrade
from offline_upgrade import (
    ActivationError,
    BundleValidationError,
    DEVELOPMENT_TEST_KEY_ID,
    ManifestValidationError,
    canonical_offline_manifest_bytes,
    inspect_bundle as _inspect_bundle,
    install_bundle as _install_bundle,
    read_current,
    read_installed_version,
    mark_pending_upgrade,
    packaged_agent_self_test,
    prepare_pending_upgrade,
    confirm_pending_upgrade,
    rollback_pending_upgrade,
    _RejectLocalHealthRedirects,
    _read_local_health,
    main,
)


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
TEST_SANDBOX_ROOT = REPOSITORY_ROOT / "dist" / "test-sandboxes"
TEST_PRIVATE_SEED = bytes.fromhex(
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
)
TEST_PUBLIC_KEY = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"


def inspect_bundle(*args, **kwargs):
    kwargs["allow_test_keys"] = True
    return _inspect_bundle(*args, **kwargs)


def install_bundle(*args, **kwargs):
    kwargs["allow_test_keys"] = True
    return _install_bundle(*args, **kwargs)


class OfflineUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="offline-upgrade-", dir=TEST_SANDBOX_ROOT)
        self.sandbox = Path(self.temporary.name)
        self.install_root = self.sandbox / "install"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_windows_maintenance_mutex_name_matches_powershell_contract(self) -> None:
        root = r"C:\Users\Example\AppData\Local\DianAgent"
        canonical = root.rstrip("\\/").upper()
        expected = "Local\\DianAgentMaintenance-" + hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest().upper()[:16]
        self.assertEqual(expected, offline_upgrade._windows_maintenance_mutex_name(root))

    def test_mutating_updater_fails_closed_on_primary_install_journal(self) -> None:
        self.install_root.mkdir()
        journal = self.install_root / ".install-transaction.json"
        journal.write_text('{"state":"activating"}\n', encoding="utf-8")
        with self.assertRaisesRegex(ActivationError, "install or maintenance-tools transaction"):
            offline_upgrade._assert_no_primary_install_transaction(self.install_root)
        self.assertEqual('{"state":"activating"}\n', journal.read_text(encoding="utf-8"))

    def test_mutating_updater_fails_closed_on_release_tools_journal(self) -> None:
        self.install_root.mkdir()
        journal = self.install_root / ".release-tools-transaction.json"
        journal.write_text('{"state":"activating"}\n', encoding="utf-8")
        with self.assertRaisesRegex(ActivationError, "install or maintenance-tools transaction"):
            offline_upgrade._assert_no_primary_install_transaction(self.install_root)
        self.assertEqual('{"state":"activating"}\n', journal.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "nt", "Windows named-mutex contract")
    def test_mutating_updater_cli_refuses_shared_maintenance_lock(self) -> None:
        self.install_root.mkdir()
        sentinel = self.install_root / "must-survive.txt"
        sentinel.write_text("owned by another operation\n", encoding="utf-8")
        with offline_upgrade._windows_installation_maintenance_lock(
            self.install_root, timeout_seconds=1
        ):
            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(offline_upgrade.__file__).resolve()),
                    "cleanup",
                    "--install-root",
                    str(self.install_root),
                    "--apply",
                    "--maintenance-lock-timeout-seconds",
                    "1",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(2, completed.returncode, completed.stderr)
        self.assertIn("already in progress", completed.stderr)
        self.assertEqual("owned by another operation\n", sentinel.read_text(encoding="utf-8"))

    def test_local_health_redirects_are_rejected(self) -> None:
        handler = _RejectLocalHealthRedirects()
        response = mock.Mock()
        with self.assertRaisesRegex(URLError, "do not allow redirects"):
            handler.redirect_request(
                mock.Mock(),
                response,
                302,
                "Found",
                {},
                "https://attacker.invalid/healthy",
            )
        response.close.assert_called_once_with()

    def test_local_health_http_error_closes_response(self) -> None:
        response = mock.Mock()
        error = HTTPError("http://127.0.0.1:8765/health/live", 503, "unavailable", {}, response)
        with mock.patch.object(offline_upgrade._LOCAL_HEALTH_OPENER, "open", side_effect=error):
            self.assertIsNone(_read_local_health("http://127.0.0.1:8765/health/live"))
        response.close.assert_called_once_with()

    def test_packaged_self_test_never_opens_the_production_data_directory(self) -> None:
        release = self.sandbox / "release"
        executable = release / "program" / "DianAgent.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"self-test-fixture")
        captured = {}

        def fake_run(_command, **kwargs):
            captured.update(kwargs)
            data_dir = Path(kwargs["env"]["DIAN_AGENT_DATA_DIR"])
            log_dir = Path(kwargs["env"]["DIAN_AGENT_LOG_DIR"])
            self.assertTrue(data_dir.parent.is_dir())
            self.assertEqual(data_dir.parent, log_dir.parent)
            return mock.Mock(returncode=0)

        with mock.patch("offline_upgrade.subprocess.run", side_effect=fake_run):
            self.assertTrue(packaged_agent_self_test(release, self.install_root))

        production_data = (self.install_root / "data").resolve()
        production_logs = (self.install_root / "logs").resolve()
        self.assertNotEqual(production_data, Path(captured["env"]["DIAN_AGENT_DATA_DIR"]).resolve())
        self.assertNotEqual(production_logs, Path(captured["env"]["DIAN_AGENT_LOG_DIR"]).resolve())

    def _bundle(
        self,
        version: str,
        *,
        files: dict[str, bytes] | None = None,
        manifest_version: int = 1,
        minimum: str = "0.0.0",
        maximum: str | None = None,
        mutate_manifest=None,
        mutate_after_sign=None,
        extra_members: dict[str, bytes] | None = None,
        signed: bool = True,
        distribution: str = "development_test",
        key_id: str = DEVELOPMENT_TEST_KEY_ID,
    ) -> Path:
        payloads = files or {
            "program/DianAgent.exe": f"agent-{version}".encode(),
            "extension/modern/manifest.json": json.dumps({"version": version}).encode(),
        }
        manifest = {
            "manifest_version": manifest_version,
            "product": "DianAgent",
            "version": version,
            "distribution": distribution,
            "compatibility": {
                "platform": "windows",
                "min_current_version": minimum,
                "max_current_version": maximum,
            },
            "files": [
                {
                    "path": path,
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                for path, content in sorted(payloads.items())
            ],
        }
        if mutate_manifest is not None:
            mutate_manifest(manifest)
        if signed:
            private_key = Ed25519PrivateKey.from_private_bytes(TEST_PRIVATE_SEED)
            manifest["signature"] = {
                "algorithm": "ed25519",
                "key_id": key_id,
                "value": base64.b64encode(
                    private_key.sign(canonical_offline_manifest_bytes(manifest))
                ).decode("ascii"),
            }
        if mutate_after_sign is not None:
            mutate_after_sign(manifest)
        bundle = self.sandbox / f"DianAgent-{version}-{len(list(self.sandbox.glob('*.zip')))}.zip"
        with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("offline-manifest.json", json.dumps(manifest))
            for path, content in payloads.items():
                archive.writestr(path, content)
            for path, content in (extra_members or {}).items():
                archive.writestr(path, content)
        return bundle

    def test_signature_is_required_and_verified_before_installation(self) -> None:
        unsigned = self._bundle("3.8.0", signed=False)
        with self.assertRaisesRegex(ManifestValidationError, "signature is required"):
            _inspect_bundle(unsigned, current_version="3.7.0")

        signed = self._bundle("3.8.1")
        with self.assertRaisesRegex(ManifestValidationError, "explicit test-key mode"):
            _inspect_bundle(signed, current_version="3.7.0")
        verified = inspect_bundle(signed, current_version="3.7.0")
        self.assertEqual("3.8.1", verified.manifest["version"])

        tampered = self._bundle(
            "3.8.2",
            mutate_after_sign=lambda manifest: manifest.__setitem__("version", "3.8.3"),
        )
        with self.assertRaisesRegex(ManifestValidationError, "signature verification failed"):
            inspect_bundle(tampered, current_version="3.7.0")
        self.assertFalse((self.install_root / "versions").exists())

    def test_test_key_cannot_claim_a_production_distribution(self) -> None:
        bundle = self._bundle("3.8.0", distribution="production")
        with self.assertRaisesRegex(ManifestValidationError, "cannot use a development test key"):
            inspect_bundle(bundle, current_version="3.7.0")

    def test_pinned_production_public_key_accepts_only_matching_signature(self) -> None:
        bundle = self._bundle(
            "3.8.0",
            distribution="production",
            key_id="production-test-fixture-1",
        )
        with mock.patch(
            "offline_upgrade.PRODUCTION_OFFLINE_PUBLIC_KEYS",
            {"production-test-fixture-1": TEST_PUBLIC_KEY},
        ):
            verified = _inspect_bundle(bundle, current_version="3.7.0")
        self.assertEqual("production", verified.manifest["distribution"])

    def test_valid_bundle_installs_versioned_program_and_extension(self) -> None:
        for name in ("data", "config", "knowledge", "backup", "logs"):
            protected = self.install_root / name
            protected.mkdir(parents=True)
            (protected / "keep.txt").write_text(name, encoding="utf-8")

        result = install_bundle(self._bundle("3.8.0"), self.install_root, current_version="3.7.0")

        self.assertEqual("activated", result["status"])
        current = read_current(self.install_root)
        self.assertEqual("3.8.0", current["version"])
        release = self.install_root / "versions" / "3.8.0"
        self.assertEqual(b"agent-3.8.0", (release / "program" / "DianAgent.exe").read_bytes())
        self.assertTrue((release / "extension" / "modern" / "manifest.json").is_file())
        self.assertTrue((self.install_root / "extension-current" / "manifest.json").is_file())
        self.assertTrue((release / "offline-manifest.json").is_file())
        for name in ("data", "config", "knowledge", "backup", "logs"):
            self.assertEqual(name, (self.install_root / name / "keep.txt").read_text(encoding="utf-8"))

    def test_manifest_version_compatibility_and_hash_fail_closed(self) -> None:
        with self.assertRaisesRegex(ManifestValidationError, "manifest version"):
            inspect_bundle(self._bundle("3.8.0", manifest_version=2), current_version="3.7.0")
        with self.assertRaisesRegex(ManifestValidationError, "older than supported"):
            inspect_bundle(self._bundle("3.8.0", minimum="3.7.1"), current_version="3.7.0")
        with self.assertRaisesRegex(ManifestValidationError, "newer than supported"):
            inspect_bundle(self._bundle("3.8.0", maximum="3.6.9"), current_version="3.7.0")

        def corrupt_hash(manifest):
            manifest["files"][0]["sha256"] = "0" * 64

        with self.assertRaisesRegex(BundleValidationError, "SHA-256"):
            install_bundle(
                self._bundle("3.8.0", mutate_manifest=corrupt_hash),
                self.install_root,
                current_version="3.7.0",
            )
        self.assertIsNone(read_current(self.install_root))
        self.assertFalse((self.install_root / "versions" / "3.8.0").exists())

    def test_path_traversal_and_protected_roots_are_rejected(self) -> None:
        traversal = self._bundle("3.8.0", extra_members={"../data/escaped.txt": b"bad"})
        with self.assertRaisesRegex(BundleValidationError, "traversal"):
            inspect_bundle(traversal, current_version="3.7.0")
        self.assertFalse((self.sandbox / "data" / "escaped.txt").exists())

        protected = self._bundle("3.8.1", files={"data/settings.json": b"bad"})
        with self.assertRaisesRegex(ManifestValidationError, "protected directory"):
            inspect_bundle(protected, current_version="3.7.0")

        absolute = self._bundle("3.8.2", extra_members={"C:\\logs\\escaped.txt": b"bad"})
        with self.assertRaisesRegex(BundleValidationError, "drive-qualified"):
            inspect_bundle(absolute, current_version="3.7.0")

    def test_failed_health_check_restores_exact_previous_pointer(self) -> None:
        install_bundle(self._bundle("3.8.0"), self.install_root, current_version="3.7.0")
        previous = (self.install_root / "current.json").read_bytes()
        previous_extension = (self.install_root / "extension-current" / "manifest.json").read_bytes()

        with self.assertRaisesRegex(ActivationError, "health check"):
            install_bundle(self._bundle("3.9.0"), self.install_root, health_check=lambda path, manifest: False)

        self.assertEqual(previous, (self.install_root / "current.json").read_bytes())
        self.assertEqual("3.8.0", read_current(self.install_root)["version"])
        self.assertEqual(
            previous_extension,
            (self.install_root / "extension-current" / "manifest.json").read_bytes(),
        )
        self.assertTrue((self.install_root / "versions" / "3.8.0").is_dir())
        self.assertFalse((self.install_root / "versions" / "3.9.0").exists())

    def test_real_start_failure_can_rollback_a_pending_successful_activation(self) -> None:
        install_bundle(self._bundle("3.8.0"), self.install_root, current_version="3.7.0")
        previous_extension = (self.install_root / "extension-current" / "manifest.json").read_bytes()
        prepare_pending_upgrade(self.install_root)
        result = install_bundle(self._bundle("3.9.0"), self.install_root)
        mark_pending_upgrade(self.install_root, result["version"])

        rolled_back = rollback_pending_upgrade(self.install_root)

        self.assertEqual("3.8.0", rolled_back["version"])
        self.assertEqual("3.8.0", read_current(self.install_root)["version"])
        self.assertEqual(previous_extension, (self.install_root / "extension-current" / "manifest.json").read_bytes())
        self.assertFalse((self.install_root / "versions" / "3.9.0").exists())

    def test_interrupted_rollback_preparation_never_publishes_an_incomplete_transaction(self) -> None:
        install_bundle(self._bundle("3.8.0"), self.install_root, current_version="3.7.0")
        real_atomic_write = offline_upgrade._atomic_write

        def interrupt_before_state(path: Path, content: bytes) -> None:
            if path.name == "state.json":
                raise KeyboardInterrupt("simulated hard interruption")
            real_atomic_write(path, content)

        with mock.patch.object(offline_upgrade, "_atomic_write", side_effect=interrupt_before_state):
            with self.assertRaisesRegex(KeyboardInterrupt, "hard interruption"):
                prepare_pending_upgrade(self.install_root)

        rollback = self.install_root / ".offline-upgrade-rollback"
        self.assertFalse(rollback.exists())
        abandoned = list(self.install_root.glob(".offline-prep-*"))
        self.assertEqual(1, len(abandoned))

        prepared = prepare_pending_upgrade(self.install_root)

        self.assertEqual(rollback, prepared)
        self.assertTrue((rollback / "state.json").is_file())
        self.assertEqual([], list(self.install_root.glob(".offline-prep-*")))

    def test_new_prepare_never_removes_a_published_transaction(self) -> None:
        install_bundle(self._bundle("3.8.0"), self.install_root, current_version="3.7.0")
        rollback = prepare_pending_upgrade(self.install_root)
        result = install_bundle(self._bundle("3.9.0"), self.install_root)
        mark_pending_upgrade(self.install_root, result["version"])
        state_before = (rollback / "state.json").read_bytes()
        pointer_before = (self.install_root / "current.json").read_bytes()

        with self.assertRaisesRegex(ActivationError, "awaiting confirm or rollback"):
            prepare_pending_upgrade(self.install_root)

        self.assertEqual(state_before, (rollback / "state.json").read_bytes())
        self.assertEqual(pointer_before, (self.install_root / "current.json").read_bytes())

    def test_fresh_app_layout_supplies_current_version_for_compatibility(self) -> None:
        app = self.install_root / "app" / "3.7.0"
        app.mkdir(parents=True)
        (app / "DianAgent.exe").write_bytes(b"fresh-agent")
        (self.install_root / "current-version.txt").write_text("3.7.0\n", encoding="ascii")
        old_extension = self.install_root / "extension-current"
        old_extension.mkdir(parents=True)
        (old_extension / "manifest.json").write_text('{"version":"3.7.0"}', encoding="utf-8")

        self.assertEqual("3.7.0", read_installed_version(self.install_root))
        result = install_bundle(self._bundle("3.8.0", minimum="3.7.0"), self.install_root)

        self.assertEqual("3.8.0", result["version"])
        self.assertEqual("3.8.0", read_current(self.install_root)["version"])

    def test_fresh_app_layout_rejects_a_false_explicit_current_version(self) -> None:
        app = self.install_root / "app" / "3.7.0"
        app.mkdir(parents=True)
        (app / "DianAgent.exe").write_bytes(b"fresh-agent")
        (self.install_root / "current-version.txt").write_text("3.7.0\n", encoding="ascii")

        with self.assertRaisesRegex(ActivationError, "current-version.txt"):
            install_bundle(self._bundle("3.8.0"), self.install_root, current_version="3.6.0")

    def test_mark_failure_after_activation_rolls_back_persisted_transaction(self) -> None:
        app = self.install_root / "app" / "3.7.0"
        app.mkdir(parents=True)
        (app / "DianAgent.exe").write_bytes(b"fresh-agent")
        (self.install_root / "current-version.txt").write_text("3.7.0\n", encoding="ascii")
        old_extension = self.install_root / "extension-current"
        old_extension.mkdir(parents=True)
        previous_extension = b'{"version":"3.7.0"}'
        (old_extension / "manifest.json").write_bytes(previous_extension)

        with mock.patch("offline_upgrade.packaged_agent_self_test", return_value=True), mock.patch(
            "offline_upgrade.mark_pending_upgrade", side_effect=ActivationError("injected state write failure")
        ):
            result = main([
                "install",
                str(self._bundle("3.8.0", minimum="3.7.0")),
                "--install-root",
                str(self.install_root),
                "--allow-test-keys",
            ])

        self.assertEqual(2, result)
        self.assertIsNone(read_current(self.install_root))
        self.assertEqual("3.7.0", read_installed_version(self.install_root))
        self.assertEqual(previous_extension, (self.install_root / "extension-current" / "manifest.json").read_bytes())
        self.assertFalse((self.install_root / "versions" / "3.8.0").exists())
        self.assertFalse((self.install_root / ".offline-upgrade-rollback").exists())

    def test_corrupt_previous_pointer_fails_closed_without_changing_active_release(self) -> None:
        install_bundle(self._bundle("3.8.0"), self.install_root, current_version="3.7.0")
        prepare_pending_upgrade(self.install_root)
        result = install_bundle(self._bundle("3.9.0"), self.install_root)
        mark_pending_upgrade(self.install_root, result["version"])
        (self.install_root / ".offline-upgrade-rollback" / "previous-current.json").unlink()
        active_pointer = (self.install_root / "current.json").read_bytes()
        active_extension = (self.install_root / "extension-current" / "manifest.json").read_bytes()

        with self.assertRaisesRegex(ActivationError, "previous active version pointer is missing"):
            rollback_pending_upgrade(self.install_root)

        self.assertEqual(active_pointer, (self.install_root / "current.json").read_bytes())
        self.assertEqual(active_extension, (self.install_root / "extension-current" / "manifest.json").read_bytes())
        self.assertTrue((self.install_root / ".offline-upgrade-rollback").is_dir())

    def test_confirm_refuses_pointer_that_does_not_match_pending_version(self) -> None:
        install_bundle(self._bundle("3.8.0"), self.install_root, current_version="3.7.0")
        prepare_pending_upgrade(self.install_root)
        result = install_bundle(self._bundle("3.9.0"), self.install_root)
        mark_pending_upgrade(self.install_root, result["version"])
        previous = self.install_root / ".offline-upgrade-rollback" / "previous-current.json"
        (self.install_root / "current.json").write_bytes(previous.read_bytes())

        with self.assertRaisesRegex(ActivationError, "does not match"):
            confirm_pending_upgrade(self.install_root)
        self.assertTrue((self.install_root / ".offline-upgrade-rollback").is_dir())

    def test_locked_failed_release_cleanup_does_not_undo_successful_pointer_rollback(self) -> None:
        install_bundle(self._bundle("3.8.0"), self.install_root, current_version="3.7.0")
        prepare_pending_upgrade(self.install_root)
        result = install_bundle(self._bundle("3.9.0"), self.install_root)
        mark_pending_upgrade(self.install_root, result["version"])

        with mock.patch("offline_upgrade._remove_release_dir", side_effect=PermissionError("locked")):
            rolled_back = rollback_pending_upgrade(self.install_root)

        self.assertEqual("3.8.0", rolled_back["version"])
        self.assertEqual("3.8.0", read_current(self.install_root)["version"])
        self.assertFalse((self.install_root / ".offline-upgrade-rollback").exists())
        self.assertTrue((self.install_root / "versions" / "3.9.0").exists())

    def test_existing_version_is_never_overwritten(self) -> None:
        bundle = self._bundle("3.8.0")
        existing = self.install_root / "versions" / "3.8.0"
        existing.mkdir(parents=True)
        marker = existing / "keep.txt"
        marker.write_text("existing", encoding="utf-8")

        with self.assertRaisesRegex(ActivationError, "already exists"):
            install_bundle(bundle, self.install_root, current_version="3.7.0")
        self.assertEqual("existing", marker.read_text(encoding="utf-8"))
        self.assertIsNone(read_current(self.install_root))

    def test_explicit_current_version_cannot_disagree_with_pointer(self) -> None:
        install_bundle(self._bundle("3.8.0"), self.install_root, current_version="3.7.0")

        with self.assertRaisesRegex(ActivationError, "does not match"):
            install_bundle(self._bundle("3.9.0"), self.install_root, current_version="3.7.0")

        self.assertEqual("3.8.0", read_current(self.install_root)["version"])
        self.assertFalse((self.install_root / "versions" / "3.9.0").exists())

    def test_windows_alias_paths_are_rejected(self) -> None:
        reserved = self._bundle("3.8.0", files={"program/CON.txt": b"bad"})
        with self.assertRaisesRegex(BundleValidationError, "reserved device"):
            inspect_bundle(reserved, current_version="3.7.0")

        trailing_dot = self._bundle("3.8.1", files={"extension/settings.json.": b"bad"})
        with self.assertRaisesRegex(BundleValidationError, "Windows-unsafe"):
            inspect_bundle(trailing_dot, current_version="3.7.0")

    def test_updater_rejects_version_forms_the_launcher_cannot_start(self) -> None:
        for version in ("3.8", "3.8.0.1"):
            with self.subTest(version=version), self.assertRaisesRegex(ManifestValidationError, "invalid release version"):
                inspect_bundle(self._bundle(version), current_version="3.7.0")


if __name__ == "__main__":
    unittest.main()
