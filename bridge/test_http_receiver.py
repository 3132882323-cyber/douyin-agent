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
        self.assertEqual(plan_b["action_type"], "scale_cautiously")

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


if __name__ == "__main__":
    unittest.main()
