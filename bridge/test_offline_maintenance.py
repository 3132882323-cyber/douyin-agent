from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import offline_upgrade
from offline_upgrade import cleanup_install_root, recover_pending_upgrade, transaction_status


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
TEST_SANDBOX_ROOT = REPOSITORY_ROOT / "dist" / "test-sandboxes"


class OfflineMaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="offline-maintenance-", dir=TEST_SANDBOX_ROOT)
        self.root = Path(self.temporary.name) / "DianAgent"
        self.root.mkdir()
        self.now = time.time()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _version(self, version: str) -> Path:
        path = self.root / "versions" / version
        (path / "program").mkdir(parents=True)
        (path / "program" / "DianAgent.exe").write_bytes(version.encode())
        return path

    def _pointer(self, version: str, destination: Path | None = None) -> bytes:
        value = {
            "schema_version": 1,
            "version": version,
            "version_path": f"versions/{version}",
            "activated_at": "2026-08-03T00:00:00+00:00",
        }
        content = (json.dumps(value) + "\n").encode()
        (destination or (self.root / "current.json")).write_bytes(content)
        return content

    def _make_old(self, path: Path, hours: int = 500) -> None:
        stamp = self.now - hours * 3600
        for base, directories, files in os.walk(path):
            for name in (*directories, *files):
                os.utime(Path(base) / name, (stamp, stamp))
        os.utime(path, (stamp, stamp))

    def test_cleanup_dry_run_and_apply_preserve_active_recent_and_fresh_layout(self) -> None:
        active = self._version("3.8.0")
        recent = self._version("3.9.0")
        orphan = self._version("3.6.0")
        staging = self.root / "versions" / ".staging-4.0.0-dead"
        staging.mkdir()
        fresh = self.root / "app" / "3.7.0"
        fresh.mkdir(parents=True)
        (fresh / "DianAgent.exe").write_bytes(b"fresh")
        self._pointer("3.8.0")
        for path in (active, recent, orphan, staging, fresh):
            self._make_old(path)

        preview = cleanup_install_root(
            self.root, dry_run=True, keep_recent_versions=1, min_age_hours=24, now=self.now
        )
        self.assertEqual(2, preview["summary"]["would_delete"])
        self.assertTrue(orphan.exists())
        self.assertTrue(staging.exists())

        result = cleanup_install_root(
            self.root, dry_run=False, keep_recent_versions=1, min_age_hours=24, now=self.now
        )
        self.assertEqual(2, result["summary"]["deleted"])
        self.assertTrue(active.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(fresh.exists())
        self.assertFalse(orphan.exists())
        self.assertFalse(staging.exists())
        self.assertTrue((self.root / "logs" / "offline-upgrade-maintenance.jsonl").is_file())

    def test_pending_transaction_protects_both_versions_and_skips_ambiguous_stages(self) -> None:
        previous = self._version("3.8.0")
        active = self._version("3.9.0")
        orphan = self._version("3.7.0")
        staging = self.root / "versions" / ".staging-4.0.0-dead"
        staging.mkdir()
        completed = self.root / ".offline-upgrade-confirmed-old"
        completed.mkdir()
        self._pointer("3.9.0")
        rollback = self.root / ".offline-upgrade-rollback"
        (rollback / "extension-current").mkdir(parents=True)
        self._pointer("3.8.0", rollback / "previous-current.json")
        (rollback / "state.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "had_current": True,
                    "had_extension": True,
                    "previous_version": "3.8.0",
                    "new_version": "3.9.0",
                }
            ),
            encoding="utf-8",
        )
        for path in (previous, active, orphan, staging, completed):
            self._make_old(path)

        result = cleanup_install_root(
            self.root, dry_run=False, keep_recent_versions=0, min_age_hours=24, now=self.now
        )

        self.assertTrue(previous.exists())
        self.assertTrue(active.exists())
        self.assertFalse(orphan.exists())
        self.assertTrue(staging.exists())
        self.assertTrue(completed.exists())
        skipped = {Path(item["path"]).name for item in result["records"] if item["action"] == "skipped"}
        self.assertIn(staging.name, skipped)
        self.assertIn(completed.name, skipped)

    def test_corrupt_pending_state_protects_every_installed_version(self) -> None:
        first = self._version("3.8.0")
        second = self._version("3.9.0")
        self._pointer("3.9.0")
        rollback = self.root / ".offline-upgrade-rollback"
        rollback.mkdir()
        (rollback / "state.json").write_text("not json", encoding="utf-8")
        self._make_old(first)
        self._make_old(second)

        result = cleanup_install_root(
            self.root, dry_run=False, keep_recent_versions=0, min_age_hours=0, now=self.now
        )

        self.assertFalse(result["pending_state_valid"])
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_windows_lock_is_reported_and_skipped(self) -> None:
        orphan = self._version("3.8.0")
        self._make_old(orphan)

        with mock.patch.object(offline_upgrade, "_safe_maintenance_remove", side_effect=PermissionError("locked")):
            result = cleanup_install_root(
                self.root, dry_run=False, keep_recent_versions=0, min_age_hours=0, now=self.now
            )

        self.assertTrue(orphan.exists())
        self.assertEqual("skipped", result["records"][0]["action"])
        self.assertIn("locked", result["records"][0]["detail"])

    def test_transaction_status_reports_pending_versions(self) -> None:
        self._version("3.9.0")
        self._pointer("3.9.0")
        rollback = self.root / ".offline-upgrade-rollback"
        rollback.mkdir()
        (rollback / "state.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "had_current": False,
                    "had_extension": False,
                    "previous_version": "3.7.0",
                    "new_version": "3.9.0",
                }
            ),
            encoding="utf-8",
        )

        status = transaction_status(self.root)

        self.assertTrue(status["pending"])
        self.assertEqual("3.9.0", status["current_version"])
        self.assertEqual("3.7.0", status["previous_version"])

    def test_healthy_pending_version_is_confirmed_without_rollback(self) -> None:
        self._version("3.8.0")
        self._version("3.9.0")
        self._pointer("3.9.0")
        rollback = self.root / ".offline-upgrade-rollback"
        rollback.mkdir()
        self._pointer("3.8.0", rollback / "previous-current.json")
        (rollback / "state.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "had_current": True,
                    "had_extension": False,
                    "previous_version": "3.8.0",
                    "new_version": "3.9.0",
                }
            ),
            encoding="utf-8",
        )

        with mock.patch.object(offline_upgrade, "_read_local_health", return_value={"status": "ok", "version": "3.9.0"}):
            result = recover_pending_upgrade(self.root, health_url="http://127.0.0.1:8765/health")

        self.assertEqual("healthy_upgrade_confirmed", result["status"])
        self.assertEqual("3.9.0", transaction_status(self.root)["current_version"])
        self.assertFalse(rollback.exists())

    def test_unhealthy_pending_version_only_rolls_back_after_explicit_failed_attempt(self) -> None:
        self._version("3.8.0")
        self._version("3.9.0")
        self._pointer("3.9.0")
        rollback = self.root / ".offline-upgrade-rollback"
        rollback.mkdir()
        self._pointer("3.8.0", rollback / "previous-current.json")
        (rollback / "state.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "had_current": True,
                    "had_extension": False,
                    "previous_version": "3.8.0",
                    "new_version": "3.9.0",
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.object(offline_upgrade, "_read_local_health", return_value=None):
            waiting = recover_pending_upgrade(self.root, health_url="http://127.0.0.1:8765/health")
            self.assertEqual("pending_health_unconfirmed", waiting["status"])
            self.assertEqual("3.9.0", transaction_status(self.root)["current_version"])
            rolled_back = recover_pending_upgrade(
                self.root,
                health_url="http://127.0.0.1:8765/health",
                rollback_if_unhealthy=True,
            )

        self.assertEqual("unhealthy_upgrade_rolled_back", rolled_back["status"])
        self.assertEqual("3.8.0", transaction_status(self.root)["current_version"])
        self.assertFalse(rollback.exists())

    def test_power_loss_after_pointer_switch_repairs_missing_target_only_with_exact_health(self) -> None:
        self._version("3.8.0")
        self._version("3.9.0")
        self._pointer("3.9.0")
        rollback = self.root / ".offline-upgrade-rollback"
        rollback.mkdir()
        self._pointer("3.8.0", rollback / "previous-current.json")
        (rollback / "state.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "had_current": True,
                    "had_extension": False,
                    "previous_version": "3.8.0",
                    "new_version": None,
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.object(offline_upgrade, "_read_local_health", return_value={"status": "ok", "version": "3.9.0"}):
            result = recover_pending_upgrade(self.root, health_url="http://127.0.0.1:8765/health")

        self.assertEqual("healthy_upgrade_confirmed", result["status"])
        self.assertEqual("3.9.0", transaction_status(self.root)["current_version"])
        self.assertFalse(rollback.exists())


if __name__ == "__main__":
    unittest.main()
