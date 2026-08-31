from __future__ import annotations

import json
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

BRIDGE_DIR = str(Path(__file__).resolve().parent)
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

import integration_secret_store


class IntegrationSecretStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.install_id = "a" * 32
        self.store = integration_secret_store.IntegrationSecretStore(
            self.temp.name,
            install_id=self.install_id,
            install_root=Path(self.temp.name).parent,
        )
        self.record = {
            "schema_version": 1,
            "revision": "1" * 32,
            "install_id": self.install_id,
            "auto_send_reports": True,
            "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/private-value",
            "dingtalk_webhook": "",
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_windows_dpapi_backend_round_trips_through_neutral_mocks(self) -> None:
        protected: dict[str, dict] = {}

        def fake_store(path: Path, value: dict, _description: str) -> None:
            protected["record"] = json.loads(json.dumps(value))
            Path(path).write_bytes(b"opaque-dpapi-test-envelope")

        def fake_load(_path: Path) -> dict:
            return json.loads(json.dumps(protected["record"]))

        with (
            patch.object(integration_secret_store.sys, "platform", "win32"),
            patch.object(integration_secret_store, "_store_encrypted", side_effect=fake_store),
            patch.object(integration_secret_store, "_load_encrypted", side_effect=fake_load),
        ):
            self.store.store(self.record)
            restarted = integration_secret_store.IntegrationSecretStore(
                self.temp.name,
                install_id=self.install_id,
                install_root=Path(self.temp.name).parent,
            )
            self.assertEqual(self.record, restarted.load(required=True))
            self.assertEqual("windows_dpapi", restarted.label())

        raw = self.store.windows_path.read_bytes()
        self.assertNotIn(b"private-value", raw)

    def test_macos_keychain_backend_round_trips_through_neutral_mocks(self) -> None:
        keychain: dict[tuple[str, str], dict] = {}

        def fake_store(service: str, value: dict, *, account: str) -> None:
            keychain[(service, account)] = json.loads(json.dumps(value))

        def fake_load(service: str, *, account: str) -> dict:
            return json.loads(json.dumps(keychain.get((service, account), {})))

        with (
            patch.object(integration_secret_store.sys, "platform", "darwin"),
            patch.object(integration_secret_store, "_macos_keychain_store", side_effect=fake_store),
            patch.object(integration_secret_store, "_macos_keychain_load", side_effect=fake_load),
        ):
            self.store.store(self.record)
            self.assertEqual(self.record, self.store.load(required=True))
            self.assertEqual("macos_keychain", self.store.label())
        self.assertFalse(self.store.windows_path.exists())

    def test_macos_keychain_isolated_by_install_identity_and_root(self) -> None:
        keychain: dict[tuple[str, str], dict] = {}
        second_root = Path(self.temp.name) / "second-install"
        second_root.mkdir()
        second = integration_secret_store.IntegrationSecretStore(
            second_root / "data",
            install_id="b" * 32,
            install_root=second_root,
        )
        second_record = {
            **self.record,
            "revision": "2" * 32,
            "install_id": "b" * 32,
            "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/second-install",
        }

        def fake_store(service: str, value: dict, *, account: str) -> None:
            keychain[(service, account)] = json.loads(json.dumps(value))

        def fake_load(service: str, *, account: str) -> dict:
            return json.loads(json.dumps(keychain.get((service, account), {})))

        with (
            patch.object(integration_secret_store.sys, "platform", "darwin"),
            patch.object(integration_secret_store, "_macos_keychain_store", side_effect=fake_store),
            patch.object(integration_secret_store, "_macos_keychain_load", side_effect=fake_load),
        ):
            self.store.store(self.record)
            second.store(second_record)
            self.assertEqual(self.record, self.store.load(required=True))
            self.assertEqual(second_record, second.load(required=True))

        self.assertNotEqual(self.store.keychain_service, second.keychain_service)
        self.assertNotEqual(self.store.keychain_account, second.keychain_account)
        self.assertEqual(2, len(keychain))

    def test_macos_legacy_global_item_migrates_only_for_matching_local_revision(self) -> None:
        legacy = {key: value for key, value in self.record.items() if key != "install_id"}
        keychain: dict[tuple[str, str], dict] = {
            (
                integration_secret_store.MACOS_INTEGRATION_SECRET_SERVICE,
                integration_secret_store.MACOS_KEYCHAIN_ACCOUNT,
            ): legacy,
        }

        def fake_store(service: str, value: dict, *, account: str) -> None:
            keychain[(service, account)] = json.loads(json.dumps(value))

        def fake_load(service: str, *, account: str) -> dict:
            return json.loads(json.dumps(keychain.get((service, account), {})))

        with (
            patch.object(integration_secret_store.sys, "platform", "darwin"),
            patch.object(integration_secret_store, "_macos_keychain_store", side_effect=fake_store),
            patch.object(integration_secret_store, "_macos_keychain_load", side_effect=fake_load),
        ):
            migrated = self.store.load(required=True, legacy_revision="1" * 32)
            self.assertEqual(self.install_id, migrated["install_id"])
            self.assertEqual(migrated, self.store.load(required=True))

            unrelated = integration_secret_store.IntegrationSecretStore(
                Path(self.temp.name) / "unrelated-data",
                install_id="c" * 32,
                install_root=Path(self.temp.name) / "unrelated",
            )
            with self.assertRaises(integration_secret_store.IntegrationSecretStoreError):
                unrelated.load(required=True, legacy_revision="9" * 32)

    def test_unsupported_platform_and_locked_keychain_fail_closed(self) -> None:
        with patch.object(integration_secret_store.sys, "platform", "linux"):
            with self.assertRaises(integration_secret_store.IntegrationSecretStoreError):
                self.store.store(self.record)
            with self.assertRaises(integration_secret_store.IntegrationSecretStoreError):
                self.store.load(required=True)
            self.assertEqual({}, self.store.load(required=False))

        with (
            patch.object(integration_secret_store.sys, "platform", "darwin"),
            patch.object(integration_secret_store, "_macos_keychain_load", return_value={}),
        ):
            with self.assertRaises(integration_secret_store.IntegrationSecretStoreError):
                self.store.load(required=True)

    def test_identity_mismatch_fails_closed_before_returning_webhook(self) -> None:
        self.store.windows_path.write_bytes(b"opaque")
        wrong_identity = {**self.record, "install_id": "f" * 32}
        with (
            patch.object(integration_secret_store.sys, "platform", "win32"),
            patch.object(integration_secret_store, "_load_encrypted", return_value=wrong_identity),
        ):
            with self.assertRaisesRegex(
                integration_secret_store.IntegrationSecretStoreError,
                "不属于当前安装",
            ):
                self.store.load(required=True)

    def test_native_errors_and_secrets_are_never_propagated(self) -> None:
        secret_marker = "never-echo-this-webhook-token"
        with (
            patch.object(integration_secret_store.sys, "platform", "win32"),
            patch.object(
                integration_secret_store,
                "_store_encrypted",
                side_effect=OSError(f"native failure https://example.invalid/{secret_marker}"),
            ),
        ):
            with self.assertRaises(integration_secret_store.IntegrationSecretStoreError) as blocked:
                self.store.store(self.record)

        message = str(blocked.exception)
        self.assertNotIn(secret_marker, message)
        self.assertNotIn("example.invalid", message)
        self.assertNotIn("private-value", message)
        self.assertIsNone(blocked.exception.__cause__)
        rendered = "".join(traceback.format_exception(blocked.exception))
        self.assertNotIn(secret_marker, rendered)
        self.assertNotIn("example.invalid", rendered)

    def test_windows_readback_failure_restores_and_verifies_previous_record(self) -> None:
        previous = dict(self.record)
        replacement = {
            **self.record,
            "revision": "2" * 32,
            "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/new-private-value",
        }
        protected = {"record": dict(previous)}
        self.store.windows_path.write_bytes(b"opaque-old-record")
        loads = 0

        def fake_store(path: Path, value: dict, _description: str) -> None:
            protected["record"] = json.loads(json.dumps(value))
            Path(path).write_bytes(b"opaque-updated-record")

        def fake_load(_path: Path) -> dict:
            nonlocal loads
            loads += 1
            if loads == 2:
                raise OSError("simulated readback failure")
            return json.loads(json.dumps(protected["record"]))

        with (
            patch.object(integration_secret_store.sys, "platform", "win32"),
            patch.object(integration_secret_store, "_store_encrypted", side_effect=fake_store),
            patch.object(integration_secret_store, "_load_encrypted", side_effect=fake_load),
        ):
            with self.assertRaises(
                integration_secret_store.IntegrationSecretStoreError
            ) as blocked:
                self.store.store(replacement)

        self.assertEqual("rolled_back", blocked.exception.outcome)
        self.assertEqual(previous, protected["record"])
        self.assertNotIn("new-private-value", str(blocked.exception))

    def test_macos_readback_failure_restores_and_verifies_previous_record(self) -> None:
        previous = dict(self.record)
        replacement = {
            **self.record,
            "revision": "2" * 32,
            "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=new-private-value",
        }
        protected = {"record": dict(previous)}
        loads = 0

        def fake_store(_service: str, value: dict, *, account: str) -> None:
            self.assertEqual(self.store.keychain_account, account)
            protected["record"] = json.loads(json.dumps(value))

        def fake_load(_service: str, *, account: str) -> dict:
            nonlocal loads
            self.assertEqual(self.store.keychain_account, account)
            loads += 1
            if loads == 2:
                raise OSError("simulated readback failure")
            return json.loads(json.dumps(protected["record"]))

        with (
            patch.object(integration_secret_store.sys, "platform", "darwin"),
            patch.object(
                integration_secret_store, "_macos_keychain_store", side_effect=fake_store
            ),
            patch.object(
                integration_secret_store, "_macos_keychain_load", side_effect=fake_load
            ),
        ):
            with self.assertRaises(
                integration_secret_store.IntegrationSecretStoreError
            ) as blocked:
                self.store.store(replacement)

        self.assertEqual("rolled_back", blocked.exception.outcome)
        self.assertEqual(previous, protected["record"])
        self.assertNotIn("new-private-value", str(blocked.exception))

    def test_failed_rollback_reports_unknown_without_exposing_secret(self) -> None:
        previous = dict(self.record)
        replacement = {
            **self.record,
            "revision": "3" * 32,
            "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/unknown-private-value",
        }
        protected = {"record": dict(previous)}
        self.store.windows_path.write_bytes(b"opaque-old-record")
        stores = 0
        loads = 0

        def fake_store(path: Path, value: dict, _description: str) -> None:
            nonlocal stores
            stores += 1
            if stores == 2:
                raise OSError("simulated rollback failure")
            protected["record"] = json.loads(json.dumps(value))
            Path(path).write_bytes(b"opaque-updated-record")

        def fake_load(_path: Path) -> dict:
            nonlocal loads
            loads += 1
            if loads > 1:
                raise OSError("simulated readback failure")
            return json.loads(json.dumps(protected["record"]))

        with (
            patch.object(integration_secret_store.sys, "platform", "win32"),
            patch.object(integration_secret_store, "_store_encrypted", side_effect=fake_store),
            patch.object(integration_secret_store, "_load_encrypted", side_effect=fake_load),
        ):
            with self.assertRaises(
                integration_secret_store.IntegrationSecretStoreError
            ) as blocked:
                self.store.store(replacement)

        self.assertEqual("unknown", blocked.exception.outcome)
        rendered = "".join(traceback.format_exception(blocked.exception))
        self.assertNotIn("unknown-private-value", rendered)
        self.assertNotIn("open.feishu.cn", rendered)
        self.assertEqual(replacement, protected["record"])

    def test_dpapi_symlink_target_is_rejected_without_reading_or_writing(self) -> None:
        target = Path(self.temp.name) / "outside.dpapi"
        target.write_bytes(b"outside")
        try:
            self.store.windows_path.symlink_to(target)
        except OSError as error:  # pragma: no cover - host policy may forbid symlinks
            self.skipTest(f"symlink creation unavailable: {error}")
        with (
            patch.object(integration_secret_store.sys, "platform", "win32"),
            patch.object(integration_secret_store, "_load_encrypted") as loader,
            patch.object(integration_secret_store, "_store_encrypted") as writer,
        ):
            with self.assertRaises(integration_secret_store.IntegrationSecretStoreError):
                self.store.load(required=True)
            with self.assertRaises(integration_secret_store.IntegrationSecretStoreError):
                self.store.store(self.record)
        loader.assert_not_called()
        writer.assert_not_called()
        self.assertEqual(b"outside", target.read_bytes())


if __name__ == "__main__":
    unittest.main()
