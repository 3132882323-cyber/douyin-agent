from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

import http_receiver
from local_store import LocalStore
from rule_engine import RuleEngine


class SystemRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_data_dir = http_receiver.DATA_DIR
        self.temp = tempfile.TemporaryDirectory()
        http_receiver.DATA_DIR = Path(self.temp.name) / "data"
        http_receiver.DATA_DIR.mkdir(parents=True)

    def tearDown(self) -> None:
        http_receiver.DATA_DIR = self.original_data_dir
        self.temp.cleanup()

    def test_system_status_is_ready_without_ai(self) -> None:
        status = http_receiver.build_system_status()

        self.assertTrue(status["ready"])
        self.assertTrue(status["product_operational"])
        self.assertFalse(status["public_distribution_ready"])
        self.assertFalse(status["ai_required"])
        self.assertEqual(status["mode"], "local_first")
        self.assertEqual(status["required_extension_version"], status["agent_version"])
        self.assertEqual(status["program_update_mode"], "offline_bundle")
        self.assertFalse(status["online_program_updates_configured"])
        self.assertTrue(status["offline_upgrade_signature_ready"])
        self.assertFalse(status["offline_upgrade_production_trust_configured"])
        self.assertFalse(status["offline_upgrade_production_available"])
        self.assertEqual(status["database"]["status"], "ready")
        self.assertEqual(status["knowledge"]["status"], "ready")
        self.assertGreater(status["knowledge"]["rule_count"], 0)
        self.assertFalse(status["telemetry"]["enabled"])
        self.assertFalse(status["telemetry"]["raw_shop_data_uploaded"])
        self.assertEqual("local_queue_only", status["telemetry"]["local_queue"]["mode"])
        self.assertFalse(status["telemetry"]["local_queue"]["upload_configured"])
        self.assertIn("production_ed25519_trust", status["release_readiness"]["blockers"])
        self.assertIn("windows_authenticode", status["release_readiness"]["blockers"])
        self.assertIn("browser_store_publication", status["release_readiness"]["blockers"])
        self.assertIn("extension", status["distribution"])
        self.assertEqual(status["runtime"]["state"], "unknown")

    def test_runtime_status_exposes_autostart_recovery_without_local_paths(self) -> None:
        state_path = http_receiver.DATA_DIR / "runtime" / "startup-state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps({
            "state": "healthy",
            "state_label": "Agent 异常退出后已自动恢复",
            "autostart_enabled": True,
            "keepalive_enabled": True,
            "hidden_launcher": True,
            "last_recovery_at": "2026-08-04T02:00:00Z",
            "source": "release_watchdog",
            "private_path": "C:/should/not/leak",
        }), encoding="utf-8")

        status = http_receiver.build_agent_runtime_status()

        self.assertTrue(status["autostart_enabled"])
        self.assertTrue(status["hidden_launcher"])
        self.assertEqual(status["last_recovery_at"], "2026-08-04T02:00:00Z")
        self.assertNotIn("private_path", status)

    def test_telemetry_requires_explicit_boolean_opt_in(self) -> None:
        self.assertFalse(http_receiver._load_update_settings()["telemetry_enabled"])

        enabled = http_receiver._save_update_settings({"telemetry_enabled": True})
        self.assertTrue(enabled["telemetry_enabled"])
        self.assertTrue(http_receiver._load_update_settings()["telemetry_enabled"])

        disabled = http_receiver._save_update_settings({"telemetry_enabled": False})
        self.assertFalse(disabled["telemetry_enabled"])

    def test_legacy_json_and_sqlite_are_kept_in_sync(self) -> None:
        saved = http_receiver.save_data(
            "doudian",
            {
                "schema_version": 2,
                "page_type": "orders",
                "captured_at": int(time.time() * 1000),
                "quality": {"score": 90},
                "metrics": {"支付订单": 3},
            },
        )

        self.assertTrue((http_receiver.DATA_DIR / "doudian" / "orders.json").exists())
        rows = list(LocalStore(http_receiver.DATA_DIR.parent).iter_snapshots(snapshot_type="orders"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"], saved)

    def test_builtin_rule_engine_has_no_ai_dependency_and_keeps_guardrails(self) -> None:
        pack = http_receiver._update_center().load_effective_pack()
        result = RuleEngine(pack).evaluate(
            {"spend": 500, "roi": 0.8, "data_age_minutes": 5},
            {"roi_target": 1.5, "min_spend": 100},
        )

        self.assertFalse(result["ai_required"])
        self.assertEqual(result["mode"], "deterministic_local")
        self.assertGreater(result["matched_count"], 0)
        self.assertIn("requires_user_confirmation", result["safety_policy"])
        self.assertTrue(result["safety_policy"]["requires_user_confirmation"])

    def test_verified_knowledge_pack_contributes_to_dashboard_diagnostics(self) -> None:
        http_receiver.save_data(
            "qianchuan",
            {
                "schema_version": 2,
                "page_type": "report",
                "captured_at": int(time.time() * 1000),
                "quality": {"score": 90},
                "metrics": {"支付 ROI": "0.80", "消耗": "500"},
            },
        )

        insights = http_receiver.build_insights()
        knowledge_alert = next(
            item for item in insights["alerts"]
            if (item.get("evidence") or {}).get("source") == "knowledge_pack"
        )
        self.assertEqual(knowledge_alert["evidence"]["rule_id"], "qianchuan.roi_loss")
        self.assertFalse(knowledge_alert["execution_enabled"])


if __name__ == "__main__":
    unittest.main()
