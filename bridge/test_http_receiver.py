from __future__ import annotations

import tempfile
import threading
import time
import unittest
import json
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys

BRIDGE_DIR = str(Path(__file__).resolve().parent)
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)
import http_receiver


class SnapshotStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_dir = http_receiver.DATA_DIR
        self._temp = tempfile.TemporaryDirectory()
        http_receiver.DATA_DIR = Path(self._temp.name)

    def tearDown(self) -> None:
        http_receiver.DATA_DIR = self._original_dir
        self._temp.cleanup()

    def test_snapshots_are_partitioned_by_source_and_page_type(self) -> None:
        http_receiver.save_data(
            "doudian",
            {
                "schema_version": 2,
                "page_type": "orders",
                "captured_at": int(time.time() * 1000),
                "quality": {"score": 80, "metric_count": 2, "row_count": 3},
                "metrics": {"待发货": "3"},
            },
        )
        http_receiver.save_data(
            "doudian",
            {
                "schema_version": 2,
                "page_type": "products",
                "captured_at": int(time.time() * 1000),
                "quality": {"score": 70, "metric_count": 1, "row_count": 8},
                "metrics": {"在售商品": "8"},
            },
        )

        self.assertEqual(http_receiver.load_data("doudian", "orders")["page_type"], "orders")
        self.assertEqual(http_receiver.load_data("doudian", "products")["page_type"], "products")
        self.assertEqual(len(http_receiver.list_snapshots()), 2)

    def test_unsafe_page_type_is_normalized(self) -> None:
        saved = http_receiver.save_data("qianchuan", {"page_type": "../../secret", "quality": {}})
        self.assertEqual(saved["page_type"], "unknown")
        self.assertTrue((http_receiver.DATA_DIR / "qianchuan" / "unknown.json").exists())

    def test_low_roi_creates_evidence_based_alert(self) -> None:
        http_receiver.save_data(
            "qianchuan",
            {
                "schema_version": 2,
                "page_type": "report",
                "captured_at": int(time.time() * 1000),
                "quality": {"score": 90, "metric_count": 2, "row_count": 5},
                "metrics": {"支付 ROI": "0.82", "消耗": "¥320"},
            },
        )
        insights = http_receiver.build_insights()
        alert = next(item for item in insights["alerts"] if "ROI" in item["title"])
        self.assertEqual(alert["level"], "high")
        self.assertEqual(alert["evidence"]["value"], "0.82")

    def test_plan_recommendation_uses_plan_level_evidence(self) -> None:
        http_receiver.save_data(
            "qianchuan",
            {
                "schema_version": 2,
                "page_type": "campaigns",
                "quality": {"score": 90, "metric_count": 0, "row_count": 2},
                "tables": [
                    {
                        "headers": ["计划名称", "消耗", "支付 ROI", "成交订单"],
                        "rows": [["共2条计划", "¥800", "1.20", "10"]],
                    },
                    {
                        "headers": [],
                        "rows": [["计划 A", "¥500", "0.60", "2"], ["计划 B", "¥300", "2.20", "8"]],
                    },
                ],
            },
        )
        recommendations = http_receiver.build_plan_recommendations()
        self.assertFalse(any(item["plan"].startswith("共") for item in recommendations))
        plan_a = next(item for item in recommendations if item["plan"] == "计划 A")
        plan_b = next(item for item in recommendations if item["plan"] == "计划 B")
        self.assertEqual(plan_a["action_type"], "reduce_budget")
        self.assertEqual(plan_a["level"], "high")
        self.assertEqual(plan_a["diagnosis"], "ROI 明显低于目标")
        self.assertIn("观察", plan_a["observation_window"])
        self.assertIn("ROI", plan_a["acceptance"])
        self.assertRegex(plan_a["task_id"], r"^[a-f0-9]{16}$")
        self.assertEqual(plan_b["action_type"], "scale_cautiously")
        self.assertIn("10%–15%", plan_b["adjustment_range"])

    def test_qianchuan_budget_draft_requires_identity_and_supports_local_audit(self) -> None:
        http_receiver.save_data(
            "qianchuan",
            {
                "schema_version": 2,
                "page_type": "campaigns",
                "captured_at": int(time.time() * 1000),
                "account": {"key": "acct_safe1234", "label": "主投放账号", "confidence": "high"},
                "quality": {"score": 90, "metric_count": 0, "row_count": 1},
                "tables": [
                    {
                        "headers": ["计划ID", "计划名称", "日预算", "消耗", "支付 ROI", "成交订单"],
                        "rows": [["plan_987654", "夏季直播计划", "500", "300", "0.60", "2"]],
                    }
                ],
            },
        )
        http_receiver.save_agent_settings({"qianchuan_account_key": "acct_safe1234"})
        item = http_receiver.build_plan_recommendations()[0]
        draft = item["action_params"]
        self.assertTrue(draft["can_confirm"])
        self.assertFalse(draft["can_execute"])
        self.assertEqual(draft["target_ref"]["id"], "plan_987654")
        self.assertEqual(draft["change"]["target_value"], 400)

        confirmed = http_receiver.confirm_action_draft(draft)
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertFalse(http_receiver.get_action_audit()["execution_enabled"])
        self.assertEqual(http_receiver.get_action_audit()["summary"]["executed"], 0)
        self.assertEqual(http_receiver.confirm_action_draft(draft)["action_id"], confirmed["action_id"])

        cancelled = http_receiver.cancel_confirmed_action(confirmed["action_id"])
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(http_receiver.get_action_audit()["summary"]["cancelled"], 1)

    def test_missing_budget_stays_blocked_and_never_falls_back_to_pause(self) -> None:
        http_receiver.save_data(
            "qianchuan",
            {
                "schema_version": 2,
                "page_type": "campaigns",
                "captured_at": int(time.time() * 1000),
                "account": {"key": "acct_safe1234", "label": "主投放账号"},
                "quality": {"score": 90, "row_count": 1},
                "tables": [
                    {
                        "headers": ["计划ID", "计划名称", "消耗", "支付 ROI", "成交订单"],
                        "rows": [["plan_987654", "无预算字段计划", "300", "0", "0"]],
                    }
                ],
            },
        )
        http_receiver.save_agent_settings({"qianchuan_account_key": "acct_safe1234"})
        draft = http_receiver.build_plan_recommendations()[0]["action_params"]
        self.assertEqual(draft["operation_type"], "adjust_budget")
        self.assertFalse(draft["can_confirm"])
        self.assertIn("CURRENT_VALUE_MISSING", {item["code"] for item in draft["blocked_reasons"]})

    def test_automation_readiness_builds_candidate_queue_without_execution(self) -> None:
        base = {
            "operation_type": "adjust_budget",
            "operation_label": "降低预算 20%",
            "target_kind": "qianchuan_plan",
            "target_id": "plan-ready-1",
            "target_name": "准备度测试计划",
            "account_key": "account-ready-1",
            "account_label": "准备度测试账号",
            "field": "预算",
            "current_value": 500.0,
            "target_value": 400.0,
            "source": "qianchuan",
            "page_type": "campaigns",
            "captured_at_ms": 1_000_000,
            "quality_score": 90,
            "confidence": "high",
            "now_ms": 1_001_000,
        }
        confirmable = http_receiver.build_action_draft(**base)
        preflight = http_receiver.transition_action(confirmable, "confirmed")
        blocked = http_receiver.build_action_draft(**{**base, "target_id": "", "account_key": ""})
        report = http_receiver.build_automation_readiness(
            [
                {"plan": "待授权计划", "level": "high", "action_params": confirmable},
                {"plan": "已授权计划", "level": "high", "action_params": preflight},
                {"plan": "缺少身份计划", "level": "warning", "action_params": blocked},
                {"plan": "人工建议", "level": "warning"},
            ]
        )
        self.assertFalse(report["execution_enabled"])
        self.assertEqual(4, report["summary"]["total"])
        self.assertEqual(1, report["summary"]["preflight_ready"])
        self.assertEqual(1, report["summary"]["confirmable"])
        self.assertEqual(1, report["summary"]["blocked"])
        self.assertEqual(1, report["summary"]["manual_only"])
        self.assertEqual("preflight_ready", report["items"][0]["status"])

    def test_supervised_preflight_requires_new_readback_and_can_be_stopped(self) -> None:
        captured_at = int(time.time() * 1000)
        snapshot = {
            "schema_version": 2,
            "page_type": "campaigns",
            "captured_at": captured_at,
            "account": {"key": "acct_preflight1", "label": "止损试运行账号"},
            "quality": {"score": 90, "row_count": 1},
            "tables": [
                {
                    "headers": ["计划ID", "计划名称", "日预算", "消耗", "支付 ROI", "成交订单"],
                    "rows": [["plan_preflight1", "止损检查计划", "500", "300", "0.60", "2"]],
                }
            ],
        }
        http_receiver.save_data("qianchuan", snapshot)
        draft = http_receiver.build_action_draft(
            operation_type="adjust_budget",
            operation_label="降低预算 20%",
            target_kind="qianchuan_plan",
            target_id="plan_preflight1",
            target_name="止损检查计划",
            account_key="acct_preflight1",
            account_label="止损试运行账号",
            field="预算",
            current_value=500.0,
            target_value=400.0,
            source="qianchuan",
            page_type="campaigns",
            captured_at_ms=captured_at,
            quality_score=90,
            confidence="high",
        )
        confirmed = http_receiver.confirm_action_draft(draft)
        awaiting = http_receiver.start_execution_preflight(confirmed["action_id"])
        self.assertEqual("awaiting_reread", awaiting["state"])
        self.assertFalse(awaiting["execution_enabled"])

        snapshot["captured_at"] = awaiting["session"]["started_at_ms"] + 1000
        http_receiver.save_data("qianchuan", snapshot)
        ready = http_receiver.build_execution_preflight_report()
        self.assertEqual("ready_for_final_confirmation", ready["state"])
        self.assertTrue(all(item["passed"] for item in ready["checks"]))
        self.assertFalse(ready["write_enabled"])

        stopped = http_receiver.stop_execution_preflight(ready["session"]["session_id"])
        self.assertEqual("stopped", stopped["state"])
        self.assertFalse(stopped["execution_enabled"])

    def test_supervised_preflight_rejects_budget_increase(self) -> None:
        now_ms = int(time.time() * 1000)
        draft = http_receiver.build_action_draft(
            operation_type="adjust_budget",
            operation_label="增加预算 10%",
            target_kind="qianchuan_plan",
            target_id="plan_scale001",
            target_name="放量计划",
            account_key="acct_scale001",
            account_label="放量账号",
            field="预算",
            current_value=500.0,
            target_value=550.0,
            source="qianchuan",
            page_type="campaigns",
            captured_at_ms=now_ms,
            quality_score=90,
            confidence="high",
        )
        confirmed = http_receiver.confirm_action_draft(draft)
        with self.assertRaisesRegex(ValueError, "不开放自动放量"):
            http_receiver.start_execution_preflight(confirmed["action_id"])

    def test_shadow_execution_requires_manual_claim_and_matches_later_readback(self) -> None:
        captured_at = int(time.time() * 1000)
        snapshot = {
            "schema_version": 2,
            "page_type": "campaigns",
            "captured_at": captured_at,
            "account": {"key": "acct_shadow123", "label": "影子测试账号"},
            "quality": {"score": 90, "row_count": 1},
            "tables": [
                {
                    "headers": ["计划ID", "计划名称", "日预算", "消耗", "支付 ROI", "成交订单"],
                    "rows": [["plan_shadow01", "影子计划", "500", "300", "0.60", "2"]],
                }
            ],
        }
        http_receiver.save_data("qianchuan", snapshot)
        http_receiver.save_agent_settings({"qianchuan_account_key": "acct_shadow123"})
        draft = http_receiver.build_plan_recommendations()[0]["action_params"]
        confirmed = http_receiver.confirm_action_draft(draft)

        before_claim = http_receiver.build_shadow_execution_report()
        self.assertEqual(before_claim["items"][0]["status"], "awaiting_manual_action")
        marker = http_receiver.mark_action_manually_applied(confirmed["action_id"])
        self.assertFalse(marker["execution_enabled"])
        awaiting = http_receiver.build_shadow_execution_report()
        self.assertEqual(awaiting["items"][0]["status"], "awaiting_readback")

        snapshot["captured_at"] = marker["reported_applied_at_ms"] + 1000
        snapshot["tables"][0]["rows"][0][2] = "400"
        http_receiver.save_data("qianchuan", snapshot)
        verified = http_receiver.build_shadow_execution_report()
        self.assertFalse(verified["execution_enabled"])
        self.assertEqual(verified["summary"]["matched"], 1)
        self.assertEqual(verified["items"][0]["status"], "matched")
        self.assertEqual(verified["items"][0]["readback"]["current_value"], 400)

    def test_inventory_alert_uses_days_of_cover(self) -> None:
        http_receiver.save_data(
            "doudian",
            {
                "schema_version": 2,
                "page_type": "inventory",
                "quality": {"score": 85, "metric_count": 0, "row_count": 2},
                "tables": [
                    {
                        "headers": ["商品名称", "可售库存", "近7日销量"],
                        "rows": [["商品 A", "14", "70"], ["商品 B", "0", "3"]],
                    }
                ],
            },
        )
        alerts = http_receiver.build_inventory_alerts()
        product_a = next(item for item in alerts if item["product"] == "商品 A")
        product_b = next(item for item in alerts if item["product"] == "商品 B")
        self.assertEqual(product_a["title"], "预计即将售罄")
        self.assertAlmostEqual(product_a["evidence"]["days_of_cover"], 1.4)
        self.assertEqual(product_b["title"], "已缺货")

    def test_qianchuan_video_library_builds_live_creative_actions(self) -> None:
        http_receiver.save_data(
            "qianchuan",
            {
                "schema_version": 2,
                "page_type": "video_library",
                "quality": {"score": 90, "metric_count": 0, "row_count": 3},
                "tables": [
                    {
                        "headers": ["视频", "素材评估", "消耗(元)", "整体支付ROI", "成交订单数", "标签", "时长"],
                        "rows": [
                            ["开场引流 A", "优质", "500", "2.20", "6", "直播引流", "00:18"],
                            ["低效素材 B", "", "300", "0", "0", "直播引流", "00:24"],
                            ["待测素材 C", "", "0", "-", "0", "商品卖点", "00:15"],
                        ],
                    }
                ],
            },
        )
        analysis = http_receiver.build_qianchuan_creative_analysis()
        self.assertEqual(analysis["summary"]["total_videos"], 3)
        self.assertEqual(analysis["summary"]["risky_videos"], 1)
        self.assertEqual(analysis["summary"]["untested_videos"], 1)
        self.assertEqual(analysis["summary"]["high_potential_videos"], 1)
        self.assertEqual(analysis["videos"][0]["name"], "低效素材 B")
        self.assertTrue(any("高消耗低转化" in item["title"] for item in analysis["recommendations"]))

    def test_qianchuan_accounts_are_partitioned_and_selectable(self) -> None:
        def snapshot(account_key: str, label: str, video: str) -> dict:
            return {
                "page_type": "video_library",
                "account": {"key": account_key, "label": label, "confidence": "high"},
                "quality": {"score": 90, "row_count": 1},
                "tables": [{"headers": ["视频", "素材评估", "消耗(元)"], "rows": [[video, "优质", "100"]]}],
            }

        http_receiver.save_data("qianchuan", snapshot("acct_aaaa1111", "千川账号 A", "素材 A"))
        http_receiver.save_data("qianchuan", snapshot("acct_bbbb2222", "千川账号 B", "素材 B"))
        self.assertEqual(len(http_receiver.list_qianchuan_accounts()), 2)

        http_receiver.save_agent_settings({"qianchuan_account_key": "acct_aaaa1111"})
        account_a = http_receiver.build_qianchuan_creative_analysis()
        self.assertEqual([item["name"] for item in account_a["videos"]], ["素材 A"])

        http_receiver.save_agent_settings({"qianchuan_account_key": "acct_bbbb2222"})
        account_b = http_receiver.build_qianchuan_creative_analysis()
        self.assertEqual([item["name"] for item in account_b["videos"]], ["素材 B"])

    def test_qianchuan_account_catalog_filters_false_accounts_and_deduplicates_labels(self) -> None:
        http_receiver._atomic_json_write(
            http_receiver._account_catalog_path(),
            {
                "accounts": [
                    {"key": "acct_real0001", "label": "真实旗舰店", "last_seen": "2026-07-27 10:00:00"},
                    {"key": "acct_duplicate", "label": " 真实旗舰店 ", "last_seen": "2026-07-27 09:00:00"},
                    {"key": "acct_store000", "label": "店铺", "last_seen": "2026-07-27 08:00:00"},
                    {"key": "acct_funds000", "label": "我的资金 账户明细 账户余额 0.00 元 立即充值", "last_seen": "2026-07-27 07:00:00"},
                    {"key": "acct_id000000", "label": "ID：", "last_seen": "2026-07-27 06:00:00"},
                ]
            },
        )
        accounts = http_receiver.list_qianchuan_accounts()
        self.assertEqual([(item["key"], item["label"]) for item in accounts], [("acct_real0001", "真实旗舰店")])

    def test_same_qianchuan_account_label_reuses_canonical_key_across_pages(self) -> None:
        first = http_receiver.save_data(
            "qianchuan",
            {
                "page_type": "overview",
                "account": {"key": "acct_route001", "label": "跨页面旗舰店", "confidence": "medium"},
                "quality": {"score": 80},
            },
        )
        second = http_receiver.save_data(
            "qianchuan",
            {
                "page_type": "campaigns",
                "account": {"key": "acct_route002", "label": "跨页面旗舰店", "confidence": "high"},
                "quality": {"score": 90},
            },
        )
        self.assertEqual(first["data"]["account"]["key"], "acct_route001")
        self.assertEqual(second["data"]["account"]["key"], "acct_route001")
        self.assertTrue((http_receiver.DATA_DIR / "qianchuan_accounts" / "acct_route001" / "campaigns.json").exists())
        self.assertEqual(len(http_receiver.list_qianchuan_accounts()), 1)

    def test_settings_and_daily_report_are_local_and_configurable(self) -> None:
        settings = http_receiver.save_agent_settings(
            {"roi_target": 2.0, "low_inventory_threshold": 20, "daily_report_time": "08:30"}
        )
        self.assertEqual(settings["roi_target"], 2.0)
        self.assertEqual(settings["daily_report_time"], "08:30")
        report = http_receiver.generate_daily_report("2026-07-22")
        report_path = Path(report["path"])
        self.assertTrue(report_path.exists())
        self.assertIn("千川计划调整建议", report_path.read_text(encoding="utf-8"))

    def test_report_templates_support_builtin_and_custom_layouts(self) -> None:
        http_receiver.save_agent_settings({"report_template": "brief"})
        brief = http_receiver.generate_daily_report("2026-07-22")
        self.assertEqual(brief["template"], "brief")
        self.assertIn("老板简报", brief["content"])

        http_receiver.save_agent_settings(
            {
                "report_template": "custom",
                "custom_report_template": "# 店策 Agent 自定义日志 {{date}}\n{{headline}}\n{{scan_status}}",
            }
        )
        custom = http_receiver.generate_daily_report("2026-07-23")
        self.assertEqual(custom["template"], "custom")
        self.assertIn("自定义日志 2026-07-23", custom["content"])
        self.assertNotIn("{{headline}}", custom["content"])

    def test_notification_webhooks_are_local_masked_and_platform_specific(self) -> None:
        public = http_receiver.save_integration_settings(
            {
                "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/test-hook-id",
                "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=test-token",
                "auto_send_reports": True,
            }
        )
        self.assertTrue(public["feishu"]["configured"])
        self.assertTrue(public["dingtalk"]["configured"])
        self.assertNotIn("hook", json.dumps(public))
        saved = json.loads((http_receiver.DATA_DIR / "integrations.json").read_text(encoding="utf-8"))
        self.assertIn("test-hook-id", saved["feishu_webhook"])

        sent: list[tuple[str, dict]] = []
        original_post = http_receiver._post_json
        try:
            def fake_post(url: str, payload: dict, timeout: float = 8.0) -> dict:
                sent.append((url, payload))
                return {"code": 0} if "feishu" in url else {"errcode": 0}

            http_receiver._post_json = fake_post
            self.assertTrue(http_receiver.test_integration("feishu")["ok"])
            self.assertTrue(http_receiver.test_integration("dingtalk")["ok"])
        finally:
            http_receiver._post_json = original_post
        self.assertEqual(sent[0][1]["msg_type"], "text")
        self.assertEqual(sent[1][1]["msgtype"], "text")
        self.assertIn("店策 Agent", sent[0][1]["content"]["text"])

        with self.assertRaises(ValueError):
            http_receiver.save_integration_settings({"feishu_webhook": "https://example.com/hook/token"})

    def test_shelf_live_and_ops_manager_priorities(self) -> None:
        http_receiver.save_data("doudian", {"page_type": "shelf", "quality": {"score": 70}, "safe_metrics": {"曝光人数": "28", "点击人数": "4", "成交人数": "0", "订单量": "0", "用户支付金额": "¥0.00"}, "signals": ["商品主图存在不良暗示，请优化", "猜你喜欢未入选 1"]})
        http_receiver.save_data("doudian", {"page_type": "live", "quality": {"score": 70}, "safe_metrics": {"直播场次": "0", "成交金额": "¥0.00"}, "signals": ["当前待直播计划 0"]})
        shelf = http_receiver.build_shelf_analysis()
        live = http_receiver.build_live_analysis()
        ops = http_receiver.build_ops_manager()
        self.assertAlmostEqual(shelf["funnel"]["click_rate"], 14.2857, places=3)
        self.assertEqual(shelf["recommendations"][0]["level"], "high")
        self.assertIn("基准直播", live["recommendations"][0]["title"])
        self.assertIn("主图合规", ops["today_top_actions"][0]["title"])
        self.assertEqual(len({item["id"] for item in ops["all_tasks"]}), len(ops["all_tasks"]))
        report = http_receiver.generate_daily_report("2026-07-22")
        content = Path(report["path"]).read_text(encoding="utf-8")
        self.assertIn("运营总管今日任务", content)
        self.assertIn("货架运营", content)
        self.assertIn("直播与内容运营", content)

    def test_operation_task_status_is_persisted(self) -> None:
        http_receiver.save_data("doudian", {"page_type": "shelf", "quality": {"score": 70}, "safe_metrics": {"曝光人数": "20", "点击人数": "2", "成交人数": "0"}, "signals": ["商品主图存在不良暗示，请优化"]})
        before = http_receiver.build_ops_manager()
        task = before["must_do"][0]
        updated = http_receiver.update_task_state(task["id"], "doing")
        after = http_receiver.build_ops_manager()
        self.assertEqual(updated["status"], "doing")
        self.assertEqual(next(item for item in after["all_tasks"] if item["id"] == task["id"])["status"], "doing")
        http_receiver.update_task_state(task["id"], "done")
        completed = http_receiver.build_ops_manager()
        self.assertEqual(completed["progress"]["done"], 1)
        self.assertFalse(any(item["id"] == task["id"] for item in completed["today_top_actions"]))

    def test_embedded_qianchuan_scan_drives_plan_recommendations(self) -> None:
        http_receiver.save_data("doudian", {"page_type": "qianchuan_live", "quality": {"score": 90, "row_count": 1}, "tables": [{"headers": ["抖音号", "投放状态", "投放设置", "整体消耗(元)", "整体支付ROI", "整体成交订单数"], "rows": [["直播大屏\n测试店铺\n设置直播规划", "投放中", "ROI目标\n3.00", "500", "3.50", "6"]]}]})
        recommendations = http_receiver.build_plan_recommendations()
        item = next(value for value in recommendations if value["plan"] == "直播大屏 · 测试店铺")
        self.assertEqual(item["action_type"], "scale_cautiously")
        self.assertEqual(item["evidence"]["roi_target"], 3.0)

    def test_auto_scan_status_is_saved_for_reports(self) -> None:
        saved = http_receiver.save_scan_status({"status": "partial", "index": 16, "total": 16, "success": 14, "failed": 2, "results": [{"id": "orders", "ok": True}]})
        self.assertEqual(saved["success"], 14)
        self.assertEqual(http_receiver.load_scan_status()["failed"], 2)
        report = http_receiver.generate_daily_report("2026-07-22")
        self.assertIn("自动巡检：partial；成功 14 页，失败 2 页", Path(report["path"]).read_text(encoding="utf-8"))

    def test_scan_receipt_explains_coverage_quality_and_single_page_retry_targets(self) -> None:
        http_receiver.save_scan_status(
            {
                "status": "partial",
                "account_key": "acct_safe1234",
                "started_at": 1_785_200_000_000,
                "finished_at": 1_785_200_060_000,
                "total": 3,
                "success": 2,
                "failed": 1,
                "results": [
                    {
                        "id": "orders",
                        "label": "订单管理",
                        "source": "doudian",
                        "ok": True,
                        "quality": {"score": 90, "metric_count": 4, "row_count": 12},
                    },
                    {
                        "id": "qianchuan_campaigns",
                        "label": "千川商品推广",
                        "source": "qianchuan",
                        "ok": True,
                        "account_label": "主投放账号",
                        "quality": {"score": 60, "metric_count": 2, "row_count": 1},
                    },
                    {
                        "id": "qianchuan_video_library",
                        "label": "千川视频库",
                        "source": "qianchuan",
                        "ok": False,
                        "error": "页面类型未识别",
                    },
                ],
            }
        )
        receipt = http_receiver.build_scan_receipt()
        self.assertEqual(receipt["readiness"], "attention")
        self.assertFalse(receipt["analysis_ready"])
        self.assertEqual(receipt["account_label"], "主投放账号")
        self.assertEqual(receipt["summary"]["coverage_rate"], 100)
        self.assertEqual(receipt["summary"]["needs_review"], 1)
        self.assertEqual(receipt["failed_page_ids"], ["qianchuan_video_library"])
        self.assertEqual(receipt["sources"]["qianchuan"]["failed"], 1)
        self.assertTrue(any("单独重试" in warning for warning in receipt["warnings"]))

    def test_history_snapshots_build_seven_day_trends(self) -> None:
        now_ms = int(time.time() * 1000)
        http_receiver.save_data("doudian", {"page_type": "shelf", "captured_at": now_ms - 60000, "quality": {"score": 70}, "safe_metrics": {"曝光人数": "20", "点击人数": "2"}})
        http_receiver.save_data("doudian", {"page_type": "shelf", "captured_at": now_ms, "quality": {"score": 70}, "safe_metrics": {"曝光人数": "30", "点击人数": "6"}})
        trends = http_receiver.build_trends(7, "doudian", "shelf")
        exposure = next(item for item in trends["changes"] if item["label"] == "曝光人数")
        self.assertEqual(exposure["first"], 20)
        self.assertEqual(exposure["last"], 30)
        self.assertEqual(exposure["delta_percent"], 50)

    def test_http_push_requires_bridge_header_and_updates_catalog(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        body = json.dumps(
            {
                "source": "doudian",
                "data": {
                    "schema_version": 2,
                    "page_type": "overview",
                    "quality": {"score": 60, "metric_count": 1, "row_count": 0},
                    "metrics": {"订单": "2"},
                },
            }
        ).encode("utf-8")
        try:
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(
                    urllib.request.Request(
                        f"{base_url}/push",
                        data=body,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                )
            self.assertEqual(context.exception.code, 403)

            response = urllib.request.urlopen(
                urllib.request.Request(
                    f"{base_url}/push",
                    data=body,
                    headers={"Content-Type": "application/json", "X-Dian-Agent": "2"},
                    method="POST",
                )
            )
            self.assertTrue(json.loads(response.read())["ok"])

            catalog = json.loads(urllib.request.urlopen(f"{base_url}/catalog").read())
            self.assertEqual(catalog["snapshots"][0]["page_type"], "overview")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_action_confirmation_records_but_never_executes(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        now_ms = int(time.time() * 1000)
        draft = http_receiver.build_action_draft(
            operation_type="adjust_budget",
            operation_label="降低预算 20%",
            target_kind="qianchuan_plan",
            target_id="plan_123456",
            target_name="接口测试计划",
            account_key="acct_safe1234",
            account_label="测试账号",
            field="预算",
            current_value=500,
            target_value=400,
            source="qianchuan",
            page_type="campaigns",
            captured_at_ms=now_ms,
            quality_score=90,
            confidence="high",
            now_ms=now_ms,
        )

        def post(path: str, payload: dict) -> dict:
            response = urllib.request.urlopen(
                urllib.request.Request(
                    f"{base_url}{path}",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json", "X-Dian-Agent": "2"},
                    method="POST",
                )
            )
            return json.loads(response.read())

        try:
            confirmed = post("/actions/confirm", {"action": draft})
            self.assertTrue(confirmed["ok"])
            self.assertFalse(confirmed["executed"])
            self.assertFalse(confirmed["execution_enabled"])
            audit = json.loads(urllib.request.urlopen(f"{base_url}/actions/audit").read())
            self.assertEqual(audit["summary"]["confirmed"], 1)
            self.assertEqual(audit["summary"]["executed"], 0)

            cancelled = post("/actions/cancel", {"action_id": draft["action_id"]})
            self.assertEqual(cancelled["action"]["state"], "cancelled")
            self.assertFalse(cancelled["executed"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
