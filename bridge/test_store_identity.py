from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import http_receiver


def claim(kind: str, raw_id: str, source: str = "url_parameter") -> dict[str, str]:
    return {"kind": kind, "raw_id": raw_id, "evidence_source": source, "confidence": "high"}


class StoreIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_dir = http_receiver.DATA_DIR
        self.temp = tempfile.TemporaryDirectory()
        http_receiver.DATA_DIR = Path(self.temp.name)

    def tearDown(self) -> None:
        http_receiver.DATA_DIR = self.original_dir
        self.temp.cleanup()

    def save_doudian(self, shop_id: str, page_type: str, value: str = "1", quality_score: int = 80) -> dict:
        return http_receiver.save_data("doudian", {
            "page_type": page_type,
            "identity_claims": [claim("douyin_shop_id", shop_id)],
            "identity_status": "resolved_by_bridge",
            "quality": {"score": quality_score, "row_count": 1},
            "safe_metrics": {"曝光人数": value},
            "signals": ["商品主图存在不良暗示，请优化"] if page_type == "shelf" else [],
        })

    def test_pure_doudian_identity_and_first_value_do_not_require_qianchuan(self) -> None:
        overview = self.save_doudian("shop-1001", "overview", "10")
        shelf = self.save_doudian("shop-1001", "shelf", "20")
        store_key = overview["data"]["store"]["key"]
        self.assertEqual(store_key, shelf["data"]["store"]["key"])
        self.assertTrue(store_key.startswith("store_v1_"))
        self.assertNotIn("shop-1001", json.dumps(overview, ensure_ascii=False))
        self.assertFalse(any("shop-1001" in path.read_text(encoding="utf-8") for path in http_receiver.DATA_DIR.rglob("*.json")))
        database = http_receiver._local_store().paths.database
        self.assertNotIn(b"shop-1001", database.read_bytes())

        catalog = http_receiver.select_store_context(store_key)
        self.assertEqual(catalog["selected_store_key"], store_key)
        self.assertEqual(catalog["selected_account_key"], "")
        self.assertEqual(catalog["stores"][0]["state"], "doudian_ready")

        first_task = {"id": "f" * 16, "status": "todo", "title": "优化商品主图", "action": "替换主图", "evidence": "货架页存在风险提示", "acceptance": "风险提示消失"}
        with patch.object(http_receiver, "build_ops_manager", return_value={"all_tasks": [first_task]}), patch.object(
            http_receiver, "build_scan_receipt", return_value={"summary": {"success": 2}, "first_value_ready": True}
        ):
            discovered = http_receiver.build_onboarding_status()
            self.assertFalse(next(item for item in discovered["steps"] if item["id"] == "sync")["complete"])
            self.assertTrue(discovered["discovered"]["out_of_order"])

            for page_type in ("overview", "orders", "products", "shelf"):
                self.save_doudian("shop-1001", page_type, "30")
            state = http_receiver._load_onboarding_state()
            state["scopes"][store_key]["first_task_viewed_at"] = "2026-08-04 10:00:00"
            http_receiver._atomic_json_write(http_receiver._onboarding_state_path(), state)
            status = http_receiver.build_onboarding_status(state=state)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["missing_data"], [])
        self.assertEqual(status["optional_enhancements"][0]["id"], "qianchuan_overview")
        with patch.object(http_receiver, "build_ops_manager", return_value={"all_tasks": [first_task]}), patch.object(
            http_receiver, "build_automation_readiness", return_value={"items": [], "summary": {}}
        ):
            guide = http_receiver.build_connection_guide()
        self.assertEqual(guide["level"], "L2")
        self.assertEqual(guide["automation"]["mode"], "off")
        self.assertTrue(guide["next_upgrade"]["optional"])

        context = http_receiver.build_operation_context(
            catalog=http_receiver.build_store_catalog(),
            receipt={"store_key": store_key, "finished_at": 0, "analysis_ready": False, "summary": {"coverage_rate": 40}, "warnings": []},
        )
        self.assertTrue(context["analysis_allowed"])
        self.assertFalse(context["execution_review_allowed"])

    def test_unlinked_qianchuan_account_requires_explicit_manual_link(self) -> None:
        store_key = self.save_doudian("shop-2001", "overview")["data"]["store"]["key"]
        account_snapshot = http_receiver.save_data("qianchuan", {
            "page_type": "overview",
            "identity_claims": [claim("qianchuan_advertiser_id", "adv-9001")],
            "identity_status": "resolved_by_bridge",
            "quality": {"score": 90},
        })
        account_key = account_snapshot["data"]["account"]["key"]
        http_receiver.select_store_context(store_key)
        before = http_receiver.build_store_catalog()
        self.assertTrue(before["link_required"])
        self.assertEqual(before["stores"][0]["account_keys"], [])
        self.assertEqual(before["unlinked_accounts"][0]["key"], account_key)

        linked = http_receiver.link_store_account(store_key, account_key)
        self.assertEqual(linked["selected_account_key"], account_key)
        self.assertEqual(linked["stores"][0]["account_keys"], [account_key])
        reread = http_receiver.save_data("qianchuan", {
            "page_type": "campaigns",
            "identity_claims": [claim("qianchuan_advertiser_id", "adv-9001")],
            "identity_status": "resolved_by_bridge",
            "quality": {"score": 90},
        })
        self.assertEqual(reread["data"]["store"]["key"], store_key)

    def test_low_quality_page_is_discovered_but_does_not_unlock_l2(self) -> None:
        store_key = self.save_doudian("shop-quality", "overview")["data"]["store"]["key"]
        http_receiver.select_store_context(store_key)
        for page_type in ("overview", "orders", "products"):
            self.save_doudian("shop-quality", page_type)
        self.save_doudian("shop-quality", "shelf", quality_score=40)
        with patch.object(http_receiver, "build_ops_manager", return_value={"all_tasks": []}):
            status = http_receiver.build_onboarding_status()
        self.assertFalse(next(item for item in status["steps"] if item["id"] == "sync")["complete"])
        self.assertEqual(status["discovered"]["formal_snapshot_count"], 4)
        self.assertEqual(status["discovered"]["usable_snapshot_count"], 3)
        self.assertIn("doudian_shelf", [item["id"] for item in status["missing_data"]])

    def test_cross_store_snapshots_tasks_and_reports_are_isolated(self) -> None:
        store_a = self.save_doudian("shop-A001", "shelf", "10")["data"]["store"]["key"]
        store_b = self.save_doudian("shop-B002", "shelf", "99")["data"]["store"]["key"]
        http_receiver.select_store_context(store_a)
        self.assertEqual(http_receiver.load_data("doudian", "shelf")["data"]["safe_metrics"]["曝光人数"], "10")
        http_receiver.update_task_state("a" * 16, "doing", store_key=store_a)
        report_a = http_receiver.generate_daily_report("2026-08-04")["path"]

        http_receiver.select_store_context(store_b)
        self.assertEqual(http_receiver.load_data("doudian", "shelf")["data"]["safe_metrics"]["曝光人数"], "99")
        self.assertEqual(http_receiver.load_task_states(), {})
        http_receiver.update_task_state("b" * 16, "doing", store_key=store_b)
        report_b = http_receiver.generate_daily_report("2026-08-04")["path"]
        self.assertNotEqual(report_a, report_b)
        self.assertIn(store_a, report_a)
        self.assertIn(store_b, report_b)

    def test_no_identity_data_remains_unscoped_and_does_not_accumulate_value(self) -> None:
        saved = http_receiver.save_data("doudian", {"page_type": "overview", "quality": {"score": 80}, "safe_metrics": {"订单量": "3"}})
        self.assertEqual(saved["data"]["identity_resolution"], "unresolved")
        self.assertEqual(http_receiver.build_store_catalog()["store_count"], 0)
        onboarding = http_receiver.build_onboarding_status()
        self.assertEqual(onboarding["current_step"]["id"], "store")
        ledger = http_receiver.build_value_ledger()
        self.assertFalse(ledger["trusted_scope"])
        with self.assertRaisesRegex(ValueError, "尚未识别当前店铺"):
            http_receiver.update_task_state("c" * 16, "doing")

    def test_hmac_identity_is_stable_and_namespaces_do_not_collide(self) -> None:
        first = http_receiver._local_identity_key("douyin_shop_id", "same-1001")
        second = http_receiver._local_identity_key("qianchuan_shop_id", "same-1001")
        account = http_receiver._local_identity_key("qianchuan_account_id", "same-1001")
        self.assertEqual(first, second)
        self.assertNotEqual(first, account)


if __name__ == "__main__":
    unittest.main()
