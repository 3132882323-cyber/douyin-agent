from __future__ import annotations

import json
import tempfile
import time
import unittest
from unittest.mock import patch
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
        with patch.object(
            LocalStore,
            "snapshot_lifecycle_preview",
            side_effect=AssertionError("system status must not run semantic history scan"),
        ):
            status = http_receiver.build_system_status()

        self.assertTrue(status["ready"])
        self.assertTrue(status["product_operational"])
        self.assertFalse(status["public_distribution_ready"])
        self.assertFalse(status["ai_required"])
        self.assertEqual(status["mode"], "local_first")
        self.assertEqual(status["required_extension_version"], status["agent_version"])
        self.assertEqual(
            http_receiver.COMMERCIAL_RUNTIME_LOADED,
            status["edition"]["commercial_runtime_loaded"],
        )
        if http_receiver.COMMERCIAL_RUNTIME_LOADED:
            self.assertIn(
                status["edition"]["build_flavor"],
                {"developer_checkout", "private_commercial"},
            )
            self.assertFalse(status["edition"]["redistributable"])
        else:
            self.assertEqual("public_community", status["edition"]["build_flavor"])
            self.assertFalse(status["edition"]["commercial_modules_included"])
            self.assertTrue(status["edition"]["redistributable"])
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
        self.assertIn("platform_code_signature", status["release_readiness"]["blockers"])
        self.assertIn("browser_store_publication", status["release_readiness"]["blockers"])
        self.assertIn("extension", status["distribution"])
        self.assertEqual(status["runtime"]["state"], "unknown")
        self.assertEqual(status["execution"]["mode"], "observe")
        self.assertFalse(status["execution"]["enabled"])
        self.assertEqual(status["storage"]["snapshot_count"], 0)
        self.assertEqual("/storage/lifecycle", status["storage"]["lifecycle"]["endpoint"])
        self.assertEqual("preview_only", status["storage"]["lifecycle"]["mode"])
        self.assertFalse(status["storage"]["lifecycle"]["automatic_cleanup_enabled"])
        self.assertEqual(status["storage"]["schema_warnings"], [])
        self.assertIn("disk", status["storage"])
        self.assertEqual(
            set(status["storage"]["sources"]),
            set(http_receiver.ALLOWED_SOURCES),
        )

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

    def test_managed_runtime_start_records_launchd_health(self) -> None:
        with patch.dict(
            "os.environ",
            {"DIAN_AGENT_AUTOSTART_SOURCE": "macos_launchagent"},
            clear=False,
        ):
            http_receiver._record_managed_runtime_start()

        status = http_receiver.build_agent_runtime_status()
        self.assertEqual("healthy", status["state"])
        self.assertTrue(status["autostart_enabled"])
        self.assertTrue(status["keepalive_enabled"])
        self.assertEqual("macos_launchagent", status["source"])

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

    def test_industry_pack_status_and_rules_follow_current_store_binding(self) -> None:
        http_receiver.save_agent_settings({"store_key": "store_a"})
        center = http_receiver._update_center()
        center.bind_industry_pack("store_a", "industry.apparel")

        status = http_receiver._knowledge_status()
        effective = center.load_effective_pack(store_key="store_a")
        rule_ids = {rule["rule_id"] for rule in effective["rules"]}

        self.assertEqual("industry.apparel", status["pack_id"])
        self.assertEqual("apparel", status["industry"])
        self.assertEqual("服饰鞋包", status["industry_label"])
        self.assertGreaterEqual(status["installed_count"], 3)
        self.assertEqual(2, len(status["layers"]))
        self.assertIn("system.data_stale", rule_ids)
        self.assertIn("apparel.inventory.size_break_risk", rule_ids)

    def test_verified_knowledge_pack_contributes_to_dashboard_diagnostics(self) -> None:
        store_key = "store-knowledge1"
        account_key = "acct-knowledge1"
        http_receiver._remember_store_identity({
            "key": store_key,
            "confidence": "high",
            "identity_source": "test",
        })
        http_receiver._remember_qianchuan_account({
            "key": account_key,
            "store_key": store_key,
            "confidence": "high",
            "identity_source": "test",
        })
        http_receiver.link_store_account(store_key, account_key)
        http_receiver.save_agent_settings({
            "store_key": store_key,
            "qianchuan_account_key": account_key,
        })
        http_receiver.save_data(
            "qianchuan",
            {
                "schema_version": 2,
                "page_type": "report",
                "captured_at": int(time.time() * 1000),
                "store": {"key": store_key, "confidence": "high"},
                "account": {
                    "key": account_key,
                    "store_key": store_key,
                    "confidence": "high",
                },
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

    def test_stale_roi_and_inventory_metrics_never_trigger_business_rules(self) -> None:
        stale_captured_at = int((time.time() - 2 * 60 * 60) * 1000)
        http_receiver.save_data(
            "qianchuan",
            {
                "schema_version": 2,
                "page_type": "report",
                "captured_at": stale_captured_at,
                "quality": {"score": 90},
                "metrics": {"ROI": "0.80", "spend": "500"},
            },
        )
        http_receiver.save_data(
            "doudian",
            {
                "schema_version": 2,
                "page_type": "inventory",
                "captured_at": stale_captured_at,
                "quality": {"score": 90},
                "metrics": {"可售库存": "1"},
            },
        )
        http_receiver.save_data(
            "doudian",
            {
                "schema_version": 2,
                "page_type": "orders",
                "captured_at": int(time.time() * 1000),
                "quality": {"score": 90},
                "metrics": {"支付订单": "5"},
            },
        )

        alerts = http_receiver._evaluate_knowledge_rules(
            http_receiver.list_snapshots(),
            http_receiver.load_agent_settings(),
        )
        rule_ids = {item["evidence"]["rule_id"] for item in alerts}

        self.assertIn("system.data_stale", rule_ids)
        self.assertNotIn("qianchuan.roi_loss", rule_ids)
        self.assertNotIn("shop.inventory.low", rule_ids)

    def test_low_quality_metrics_never_trigger_knowledge_rules(self) -> None:
        http_receiver.save_data(
            "qianchuan",
            {
                "schema_version": 2,
                "page_type": "report",
                "captured_at": int(time.time() * 1000),
                "quality": {"score": 40},
                "metrics": {"ROI": "0.80", "spend": "500"},
            },
        )

        alerts = http_receiver._evaluate_knowledge_rules(
            http_receiver.list_snapshots(),
            http_receiver.load_agent_settings(),
        )
        rule_ids = {item["evidence"]["rule_id"] for item in alerts}

        self.assertNotIn("qianchuan.roi_loss", rule_ids)


if __name__ == "__main__":
    unittest.main()
