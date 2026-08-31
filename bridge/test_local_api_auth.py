from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import local_api_auth
from local_api_auth import (
    LocalApiAuthError,
    ensure_install_auth,
    issue_internal_session_token,
    issue_session_token,
    provision_local_api_trust,
    read_trusted_extension_ids,
    repair_local_api_trust,
    validate_session_token,
)


BRIDGE_DIR = str(Path(__file__).resolve().parent)
TEST_EXTENSION_VERSION = "4.14.0"


class LocalApiAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(
            json.dumps({"key": "ZGlhbi1hZ2VudC10ZXN0LWtleQ=="}), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def wait_for_path(self, path: Path, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.02)
        self.fail(f"timed out waiting for {path.name}")

    def test_install_secret_is_random_persistent_and_not_returned_in_session(self) -> None:
        first = ensure_install_auth(self.root)
        second = ensure_install_auth(self.root)
        self.assertEqual(first, second)
        self.assertEqual(32, len(first["install_id"]))
        self.assertGreaterEqual(len(first["secret"]), 40)

        session = issue_session_token(
            self.root,
            "a" * 32,
            extension_version=TEST_EXTENSION_VERSION,
            now=1000,
        )
        self.assertNotIn("secret", session)
        self.assertNotIn(first["secret"], json.dumps(session))
        validated = validate_session_token(
            self.root,
            session["access_token"],
            expected_subject="a" * 32,
            expected_extension_version=TEST_EXTENSION_VERSION,
            now=1001,
        )
        self.assertEqual("a" * 32, validated["subject"])

    def test_concurrent_install_uses_the_winning_complete_auth_record(self) -> None:
        winner = local_api_auth._new_install_auth()

        def install_winner(path: Path, _candidate: dict) -> bool:
            local_api_auth._atomic_json_write(path, winner)
            return False

        with patch.object(local_api_auth, "_atomic_json_create", side_effect=install_winner):
            result = ensure_install_auth(self.root)

        self.assertEqual(winner, result)
        self.assertEqual(
            winner,
            json.loads((self.root / "config" / "local_api_auth.json").read_text(encoding="utf-8")),
        )

    def test_cross_process_provision_merges_both_extension_ids(self) -> None:
        second_manifest = self.root / "manifest-second.json"
        second_manifest.write_text(
            json.dumps({"key": "c2Vjb25kLWRpYW4tYWdlbnQtdGVzdC1rZXk="}),
            encoding="utf-8",
        )
        expected_ids = {
            local_api_auth.chromium_extension_id(self.manifest),
            local_api_auth.chromium_extension_id(second_manifest),
        }
        worker = textwrap.dedent(
            """
            import sys
            import time
            from pathlib import Path
            import local_api_auth as module

            root = Path(sys.argv[1])
            manifest = Path(sys.argv[2])
            marker = Path(sys.argv[3])
            release = Path(sys.argv[4])
            original_write = module._atomic_json_write

            def controlled_write(path, value):
                marker.write_text("ready", encoding="utf-8")
                deadline = time.monotonic() + 8
                while not release.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                if not release.exists():
                    raise RuntimeError("test release timed out")
                original_write(path, value)

            module._atomic_json_write = controlled_write
            module.provision_local_api_trust(root, manifest)
            """
        )
        marker_a = self.root / "trust-a.ready"
        marker_b = self.root / "trust-b.ready"
        release_a = self.root / "trust-a.release"
        release_b = self.root / "trust-b.release"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, [BRIDGE_DIR, environment.get("PYTHONPATH", "")])
        )
        process_a = subprocess.Popen(
            [
                sys.executable,
                "-c",
                worker,
                str(self.root),
                str(self.manifest),
                str(marker_a),
                str(release_a),
            ],
            cwd=BRIDGE_DIR,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        process_b = None
        try:
            self.wait_for_path(marker_a)
            process_b = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    worker,
                    str(self.root),
                    str(second_manifest),
                    str(marker_b),
                    str(release_b),
                ],
                cwd=BRIDGE_DIR,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            time.sleep(0.4)
            self.assertFalse(
                marker_b.exists(),
                "the second process entered the trust write before the first transaction released",
            )
            release_a.write_text("go", encoding="utf-8")
            self.wait_for_path(marker_b)
            release_b.write_text("go", encoding="utf-8")
            stdout_a, stderr_a = process_a.communicate(timeout=10)
            stdout_b, stderr_b = process_b.communicate(timeout=10)
            self.assertEqual(0, process_a.returncode, stderr_a or stdout_a)
            self.assertEqual(0, process_b.returncode, stderr_b or stdout_b)
        finally:
            release_a.touch(exist_ok=True)
            release_b.touch(exist_ok=True)
            for process in (process_a, process_b):
                if process is not None and process.poll() is None:
                    process.kill()
                    process.communicate()

        registry = json.loads(
            (self.root / "config" / "trusted_extension_ids.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(expected_ids, set(registry["extension_ids"]))
        auth = json.loads(
            (self.root / "config" / "local_api_auth.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(local_api_auth._valid_config(auth))

    def test_session_is_bound_to_subject_and_signature(self) -> None:
        session = issue_session_token(
            self.root,
            "a" * 32,
            extension_version=TEST_EXTENSION_VERSION,
            now=1000,
        )
        encoded_payload = session["access_token"].split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(encoded_payload + "=" * (-len(encoded_payload) % 4)))
        self.assertEqual("dian-agent-local-api", payload["a"])
        self.assertEqual(1, payload["v"])
        self.assertEqual(TEST_EXTENSION_VERSION, payload["ev"])
        self.assertEqual(TEST_EXTENSION_VERSION, session["extension_version"])
        self.assertEqual(16, len(base64.urlsafe_b64decode(payload["n"] + "=" * (-len(payload["n"]) % 4))))
        with self.assertRaisesRegex(LocalApiAuthError, "another client") as subject:
            validate_session_token(
                self.root,
                session["access_token"],
                expected_subject="b" * 32,
                expected_extension_version=TEST_EXTENSION_VERSION,
                now=1001,
            )
        self.assertEqual("agent_session_invalid", subject.exception.code)

        token = session["access_token"]
        forged = token[:-1] + ("a" if token[-1] != "a" else "b")
        with self.assertRaises(LocalApiAuthError) as signature:
            validate_session_token(
                self.root,
                forged,
                expected_subject="a" * 32,
                expected_extension_version=TEST_EXTENSION_VERSION,
                now=1001,
            )
        self.assertEqual("agent_session_invalid", signature.exception.code)

        with self.assertRaises(LocalApiAuthError) as arbitrary_subject:
            issue_session_token(
                self.root,
                "browser-client",
                extension_version=TEST_EXTENSION_VERSION,
                now=1000,
            )
        self.assertEqual("agent_session_subject_invalid", arbitrary_subject.exception.code)

    def test_extension_session_requires_and_enforces_runtime_version(self) -> None:
        with self.assertRaises(LocalApiAuthError) as missing:
            issue_session_token(self.root, "a" * 32, now=1000)
        self.assertEqual(
            "agent_session_extension_version_required", missing.exception.code
        )

        session = issue_session_token(
            self.root,
            "a" * 32,
            extension_version=TEST_EXTENSION_VERSION,
            now=1000,
        )
        with self.assertRaises(LocalApiAuthError) as upgraded:
            validate_session_token(
                self.root,
                session["access_token"],
                expected_subject="a" * 32,
                expected_extension_version="4.15.0",
                now=1001,
            )
        self.assertEqual(
            "agent_session_extension_version_mismatch", upgraded.exception.code
        )

    def test_expired_session_and_rotated_install_secret_fail_closed(self) -> None:
        session = issue_session_token(
            self.root,
            "a" * 32,
            extension_version=TEST_EXTENSION_VERSION,
            now=1000,
            ttl_seconds=60,
        )
        with self.assertRaises(LocalApiAuthError) as expired:
            validate_session_token(
                self.root,
                session["access_token"],
                expected_subject="a" * 32,
                expected_extension_version=TEST_EXTENSION_VERSION,
                now=1060,
            )
        self.assertEqual("agent_session_expired", expired.exception.code)

        auth_path = self.root / "config" / "local_api_auth.json"
        auth_path.unlink()
        ensure_install_auth(self.root)
        with self.assertRaises(LocalApiAuthError) as rotated:
            validate_session_token(
                self.root,
                session["access_token"],
                expected_subject="a" * 32,
                expected_extension_version=TEST_EXTENSION_VERSION,
                now=1001,
            )
        self.assertEqual("agent_session_invalid", rotated.exception.code)

    def test_internal_client_helper_uses_separate_short_lived_subject(self) -> None:
        session = issue_internal_session_token(self.root, now=1000)
        validated = validate_session_token(self.root, session["access_token"], now=1001)
        self.assertEqual("local-client", validated["subject"])
        with self.assertRaises(LocalApiAuthError):
            validate_session_token(
                self.root, session["access_token"], expected_subject="a" * 32, now=1001
            )

    def test_corrupt_install_auth_is_not_silently_replaced(self) -> None:
        path = self.root / "config" / "local_api_auth.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"schema_version": 1, "secret": "broken"}', encoding="utf-8")
        with self.assertRaises(LocalApiAuthError) as context:
            ensure_install_auth(self.root)
        self.assertEqual("agent_install_auth_corrupt", context.exception.code)
        self.assertEqual('{"schema_version": 1, "secret": "broken"}', path.read_text(encoding="utf-8"))

    def test_explicit_repair_is_idempotent_for_valid_configuration(self) -> None:
        provisioned = provision_local_api_trust(self.root, self.manifest)
        auth_path = self.root / "config" / "local_api_auth.json"
        trust_path = self.root / "config" / "trusted_extension_ids.json"
        original_auth = auth_path.read_bytes()
        original_trust = trust_path.read_bytes()

        first = repair_local_api_trust(self.root, self.manifest)
        second = repair_local_api_trust(self.root, self.manifest)

        self.assertFalse(first["repaired"])
        self.assertFalse(first["rotated"])
        self.assertIsNone(first["backup_path"])
        self.assertEqual([], first["repaired_files"])
        self.assertEqual(provisioned["install_id"], first["install_id"])
        self.assertEqual(provisioned["trusted_extension_ids"], first["trusted_extension_ids"])
        self.assertEqual(first["install_id"], second["install_id"])
        self.assertEqual(original_auth, auth_path.read_bytes())
        self.assertEqual(original_trust, trust_path.read_bytes())
        self.assertFalse((self.root / "config" / "repair-backup").exists())

    def test_explicit_repair_backs_up_both_records_rotates_and_rebuilds_trust(self) -> None:
        provisioned = provision_local_api_trust(self.root, self.manifest)
        auth_path = self.root / "config" / "local_api_auth.json"
        trust_path = self.root / "config" / "trusted_extension_ids.json"
        old_auth = auth_path.read_bytes()
        old_session = issue_session_token(
            self.root,
            "a" * 32,
            extension_version=TEST_EXTENSION_VERSION,
            now=1000,
        )
        damaged_trust = b'{"schema_version": 999, "extension_ids": ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]}'
        trust_path.write_bytes(damaged_trust)

        repaired = repair_local_api_trust(self.root, self.manifest)

        self.assertTrue(repaired["repaired"])
        self.assertTrue(repaired["rotated"])
        self.assertNotEqual(provisioned["install_id"], repaired["install_id"])
        self.assertEqual(
            [provisioned["extension_id"]], repaired["trusted_extension_ids"]
        )
        self.assertEqual(
            {"local_api_auth.json", "trusted_extension_ids.json"},
            set(repaired["repaired_files"]),
        )
        backup = Path(repaired["backup_path"])
        self.assertEqual(self.root / "config" / "repair-backup", backup.parent)
        self.assertEqual(old_auth, (backup / "local_api_auth.json").read_bytes())
        self.assertEqual(damaged_trust, (backup / "trusted_extension_ids.json").read_bytes())
        backup_receipt = json.loads((backup / "repair-receipt.json").read_text(encoding="utf-8"))
        self.assertTrue(backup_receipt["credentials_rotated"])
        self.assertNotIn("secret", json.dumps(backup_receipt))
        self.assertNotIn("access_token", json.dumps(backup_receipt))
        self.assertEqual(
            frozenset({provisioned["extension_id"]}), read_trusted_extension_ids(self.root)
        )
        with self.assertRaises(LocalApiAuthError) as old_token:
            validate_session_token(
                self.root,
                old_session["access_token"],
                expected_subject="a" * 32,
                expected_extension_version=TEST_EXTENSION_VERSION,
                now=1001,
            )
        self.assertEqual("agent_session_invalid", old_token.exception.code)

    def test_repair_rejects_invalid_manifest_without_touching_corrupt_state(self) -> None:
        auth_path = self.root / "config" / "local_api_auth.json"
        auth_path.parent.mkdir(parents=True)
        damaged = b"{not-json"
        auth_path.write_bytes(damaged)
        invalid_manifest = self.root / "invalid-manifest.json"
        invalid_manifest.write_text("{}", encoding="utf-8")

        with self.assertRaises(LocalApiAuthError) as context:
            repair_local_api_trust(self.root, invalid_manifest)

        self.assertEqual("agent_extension_manifest_invalid", context.exception.code)
        self.assertEqual(damaged, auth_path.read_bytes())
        self.assertFalse((self.root / "config" / "repair-backup").exists())

    def test_manifest_trust_anchor_rejects_nonregular_and_symlink_paths(self) -> None:
        manifest_directory = self.root / "manifest-directory"
        manifest_directory.mkdir()
        with self.assertRaises(LocalApiAuthError) as nonregular:
            provision_local_api_trust(self.root, manifest_directory)
        self.assertEqual("agent_extension_manifest_invalid", nonregular.exception.code)
        self.assertFalse((self.root / "config").exists())

        manifest_link = self.root / "manifest-link.json"
        try:
            manifest_link.symlink_to(self.manifest)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink creation is unavailable: {error}")
        with self.assertRaises(LocalApiAuthError) as symlink:
            provision_local_api_trust(self.root, manifest_link)
        self.assertEqual("agent_extension_manifest_invalid", symlink.exception.code)
        self.assertFalse((self.root / "config").exists())

    def test_repair_rejects_symlinked_backup_root_without_moving_state(self) -> None:
        auth_path = self.root / "config" / "local_api_auth.json"
        auth_path.parent.mkdir(parents=True)
        damaged = b"{not-json"
        auth_path.write_bytes(damaged)
        outside = self.root / "outside"
        outside.mkdir()
        backup_root = auth_path.parent / "repair-backup"
        try:
            backup_root.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink creation is unavailable: {error}")

        with self.assertRaises(LocalApiAuthError) as context:
            repair_local_api_trust(self.root, self.manifest)

        self.assertEqual("agent_repair_path_unsafe", context.exception.code)
        self.assertEqual(damaged, auth_path.read_bytes())
        self.assertEqual([], list(outside.iterdir()))


if __name__ == "__main__":
    unittest.main()
