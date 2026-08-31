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

from oceanengine_account_center import OceanEngineAccountCenter


ACCOUNT_KEY = "adacct_v1_0123456789abcdef0123456789"
SECOND_ACCOUNT_KEY = "adacct_v1_fedcba9876543210fedcba9876"
RAW_ACCOUNT_ID = "1234567890123456"
BRIDGE_DIR = str(Path(__file__).resolve().parent)


class OceanEngineAccountCenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name)
        self.now = 2_000_000_000
        self.service = OceanEngineAccountCenter(self.data_dir, now=self.now)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def wait_for_path(self, path: Path, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.02)
        self.fail(f"timed out waiting for {path.name}")

    def fixtures(self, *, connected: bool = True, synced: bool = True):
        oauth = {
            "connected": connected,
            "refresh_token_expires_at": self.now + 30 * 24 * 60 * 60,
            "accounts": [
                {
                    "account_key": ACCOUNT_KEY,
                    "account_id": RAW_ACCOUNT_ID,
                    "account_name": "华东主账号",
                    "account_role": "SHOP",
                    "valid": True,
                    "advertiser_count": 2,
                }
            ],
        }
        sync_status = {
            "synced_at": self.now - 60 if synced else None,
            "accounts": [
                {
                    "account_key": ACCOUNT_KEY,
                    "advertiser_count": 2,
                    "endpoints": [
                        {"name": "关联广告账户", "ok": True, "count": 2},
                        {"name": "直播计划", "ok": True, "count": 3},
                        {"name": "直播经营报表", "ok": True, "count": 3},
                        {"name": "素材投放报表", "ok": True, "count": 6},
                        {"name": "视频素材库", "ok": True, "count": 6},
                    ],
                }
            ] if synced else [],
        }
        catalog = {
            "selected_account_key": ACCOUNT_KEY,
            "stores": [
                {"key": "store_v1_abcdef", "label": "华东店铺", "account_keys": [ACCOUNT_KEY]}
            ],
        }
        return oauth, sync_status, catalog

    def test_ready_account_exposes_capabilities_without_raw_identifier(self) -> None:
        center = self.service.build(*self.fixtures())
        account = center["accounts"][0]
        self.assertEqual(account["state"], "ready")
        self.assertEqual(account["masked_id"], "•••• 3456")
        self.assertEqual(center["summary"]["advertisers"], 2)
        self.assertTrue(account["selected"])
        capabilities = {item["id"]: item for item in account["capabilities"]}
        self.assertTrue(capabilities["plan_read"]["verified"])
        self.assertTrue(capabilities["report_read"]["verified"])
        self.assertFalse(capabilities["production_write"]["verified"])
        self.assertFalse(center["platform_write_enabled"])
        self.assertNotIn(RAW_ACCOUNT_ID, json.dumps(center, ensure_ascii=False))

    def test_preferences_are_local_safe_and_sync_pause_disables_management(self) -> None:
        saved = self.service.update_preference(
            ACCOUNT_KEY,
            {
                "alias": "北区核心账户",
                "group_name": "直播组",
                "sync_enabled": False,
                "managed": True,
            },
            {ACCOUNT_KEY},
        )
        self.assertFalse(saved["managed"])
        self.assertEqual(saved["revision"], 1)
        persisted = self.service.path.read_text(encoding="utf-8")
        self.assertNotIn(RAW_ACCOUNT_ID, persisted)
        center = self.service.build(*self.fixtures())
        account = center["accounts"][0]
        self.assertEqual(account["display_name"], "北区核心账户")
        self.assertEqual(account["state"], "paused")

    def test_corrupt_or_unreadable_preferences_are_not_replaced(self) -> None:
        self.service.path.write_bytes(b"{damaged-json")
        damaged = self.service.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "unreadable"):
            self.service.update_preference(
                ACCOUNT_KEY, {"managed": True}, {ACCOUNT_KEY}
            )
        self.assertEqual(damaged, self.service.path.read_bytes())

        valid = {
            "schema_version": 1,
            "updated_at": self.now,
            "accounts": {},
        }
        self.service.path.write_text(json.dumps(valid), encoding="utf-8")
        preserved = self.service.path.read_bytes()
        with patch.object(Path, "read_text", side_effect=OSError("read denied")):
            with self.assertRaisesRegex(ValueError, "unreadable"):
                self.service.update_preference(
                    ACCOUNT_KEY, {"managed": True}, {ACCOUNT_KEY}
                )
        self.assertEqual(preserved, self.service.path.read_bytes())

    def test_cross_process_updates_preserve_both_accounts(self) -> None:
        worker = textwrap.dedent(
            """
            import sys
            import time
            from pathlib import Path
            import oceanengine_account_center as module

            root = Path(sys.argv[1])
            account_key = sys.argv[2]
            alias = sys.argv[3]
            marker = Path(sys.argv[4])
            release = Path(sys.argv[5])
            original_write = module._atomic_write

            def controlled_write(path, value):
                marker.write_text("ready", encoding="utf-8")
                deadline = time.monotonic() + 8
                while not release.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                if not release.exists():
                    raise RuntimeError("test release timed out")
                original_write(path, value)

            module._atomic_write = controlled_write
            module.OceanEngineAccountCenter(root).update_preference(
                account_key,
                {"alias": alias, "sync_enabled": True, "managed": False},
                {account_key},
            )
            """
        )
        marker_a = self.data_dir / "account-a.ready"
        marker_b = self.data_dir / "account-b.ready"
        release_a = self.data_dir / "account-a.release"
        release_b = self.data_dir / "account-b.release"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, [BRIDGE_DIR, environment.get("PYTHONPATH", "")])
        )
        process_a = subprocess.Popen(
            [
                sys.executable,
                "-c",
                worker,
                str(self.data_dir),
                ACCOUNT_KEY,
                "Account A",
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
                    str(self.data_dir),
                    SECOND_ACCOUNT_KEY,
                    "Account B",
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
                "the second process entered the write before the first transaction released",
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

        payload = json.loads(self.service.path.read_text(encoding="utf-8"))
        self.assertEqual(
            {ACCOUNT_KEY, SECOND_ACCOUNT_KEY}, set(payload["accounts"])
        )
        self.assertEqual("Account A", payload["accounts"][ACCOUNT_KEY]["alias"])
        self.assertEqual(
            "Account B", payload["accounts"][SECOND_ACCOUNT_KEY]["alias"]
        )

    def test_unknown_account_cannot_create_preference(self) -> None:
        with self.assertRaisesRegex(ValueError, "账户不存在"):
            self.service.update_preference(
                ACCOUNT_KEY, {"managed": True}, set()
            )

    def test_string_boolean_preferences_are_rejected(self) -> None:
        for payload in ({"sync_enabled": "false"}, {"managed": "false"}, {"managed": 1}):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, "must be true or false"):
                    self.service.update_preference(ACCOUNT_KEY, payload, {ACCOUNT_KEY})
        self.assertFalse(self.service.path.exists())

    def test_batch_preview_fails_closed_and_never_enables_platform_write(self) -> None:
        self.service.update_preference(
            ACCOUNT_KEY,
            {"sync_enabled": True, "managed": False},
            {ACCOUNT_KEY},
        )
        center = self.service.build(*self.fixtures())
        blocked = self.service.preview([ACCOUNT_KEY], "budget_decrease", center)
        self.assertEqual(blocked["eligible_count"], 0)
        self.assertIn("尚未开启本地托管", blocked["blocked"][0]["reasons"])
        self.assertFalse(blocked["platform_write_enabled"])
        self.assertFalse(blocked["automatic_submit"])

        self.service.update_preference(
            ACCOUNT_KEY,
            {"sync_enabled": True, "managed": True},
            {ACCOUNT_KEY},
        )
        center = self.service.build(*self.fixtures())
        preview = self.service.preview([ACCOUNT_KEY], "budget_decrease", center)
        self.assertEqual(preview["eligible_count"], 1)
        self.assertEqual(preview["execution_kind"], "preview_only")
        self.assertTrue(preview["requires_human_review"])
        self.assertFalse(preview["platform_write_enabled"])

    def test_expired_authorization_blocks_even_read_only_sync(self) -> None:
        oauth, sync_status, catalog = self.fixtures(connected=False)
        center = self.service.build(oauth, sync_status, catalog)
        self.assertEqual(center["accounts"][0]["state"], "needs_auth")
        preview = self.service.preview([ACCOUNT_KEY], "sync", center)
        self.assertEqual(preview["eligible_count"], 0)
        self.assertIn("官方授权不可用", preview["blocked"][0]["reasons"])


if __name__ == "__main__":
    unittest.main()
