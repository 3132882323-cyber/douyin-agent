import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import http_receiver


class MultiStoreCatalogTests(unittest.TestCase):
    def test_official_and_browser_stores_remain_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            official_dir = data_dir / "qianchuan_accounts" / "acct_official"
            browser_dir = data_dir / "qianchuan_accounts" / "acct_browser"
            official_dir.mkdir(parents=True)
            browser_dir.mkdir(parents=True)
            (official_dir / "plans.json").write_text("{}", encoding="utf-8")
            (browser_dir / "overview.json").write_text("{}", encoding="utf-8")
            accounts = [
                {"key": "acct_official", "label": "千川账户 A", "store_key": "store_official"},
                {"key": "acct_browser", "label": "千川账户 B", "store_key": "store_browser"},
            ]
            stores = [
                {"key": "store_official", "label": "店铺 A", "account_keys": ["acct_official"]},
                {"key": "store_browser", "label": "店铺 B", "account_keys": ["acct_browser"]},
            ]
            sync = {
                "accounts": [
                    {
                        "account_key": "acct_official",
                        "account_name": "甲店",
                        "advertiser_count": 1,
                    }
                ]
            }
            with (
                patch.object(http_receiver, "DATA_DIR", data_dir),
                patch.object(http_receiver, "list_qianchuan_accounts", return_value=accounts),
                patch.object(http_receiver, "list_store_identities", return_value=stores),
                patch.object(http_receiver, "load_sync_status", return_value=sync),
                patch.object(
                    http_receiver,
                    "load_agent_settings",
                    return_value={"store_key": "store_official", "qianchuan_account_key": "acct_official"},
                ),
            ):
                result = http_receiver.build_store_catalog()

            self.assertEqual(result["mode"], "multi_store")
            self.assertEqual(result["data_isolation"], "per_store")
            self.assertEqual(result["store_count"], 2)
            by_key = {item["key"]: item for item in result["stores"]}
            self.assertEqual(by_key["store_official"]["state"], "ready")
            self.assertEqual(by_key["store_official"]["page_count"], 1)
            self.assertTrue(by_key["store_official"]["selected"])
            self.assertEqual(by_key["store_browser"]["state"], "browser_only")
            self.assertEqual(by_key["store_browser"]["channel"], "qianchuan_browser")

    def test_store_without_advertiser_is_not_reported_as_sync_failure(self):
        with (
            patch.object(http_receiver, "list_qianchuan_accounts", return_value=[
                {"key": "acct_empty", "label": "千川账户 EMPTY", "store_key": "store_empty"}
            ]),
            patch.object(http_receiver, "list_store_identities", return_value=[
                {"key": "store_empty", "label": "店铺 EMPTY", "account_keys": ["acct_empty"]}
            ]),
            patch.object(http_receiver, "load_sync_status", return_value={
                "accounts": [{"account_key": "acct_empty", "advertiser_count": 0}]
            }),
            patch.object(
                http_receiver,
                "load_agent_settings",
                return_value={"store_key": "store_empty", "qianchuan_account_key": "acct_empty"},
            ),
        ):
            result = http_receiver.build_store_catalog()
        self.assertEqual(result["stores"][0]["state"], "not_linked")
        self.assertEqual(result["stores"][0]["state_label"], "未关联广告账户")

    def test_operation_context_allows_review_for_fresh_official_store(self):
        now = int(time.time())
        result = http_receiver.build_operation_context(
            catalog={
                "selected_store_key": "acct_ready",
                "selected_account_key": "acct_ready_account",
                "stores": [{
                    "key": "acct_ready",
                    "label": "甲店",
                    "state": "ready",
                    "state_label": "官方 API 可用",
                    "channel": "official_api",
                    "advertiser_count": 1,
                    "page_count": 4,
                    "qianchuan_page_count": 4,
                    "account_keys": ["acct_ready_account"],
                    "updated_at": now,
                }],
            },
            receipt={
                "account_key": "",
                "finished_at": 0,
                "analysis_ready": False,
                "summary": {"coverage_rate": 0},
                "warnings": [],
            },
        )
        self.assertEqual(result["state"], "ready")
        self.assertTrue(result["execution_review_allowed"])
        self.assertEqual(result["source_label"], "千川官方 API")

    def test_operation_context_blocks_cross_store_receipt(self):
        result = http_receiver.build_operation_context(
            catalog={
                "selected_store_key": "acct_a",
                "stores": [{
                    "key": "acct_a",
                    "label": "甲店",
                    "state": "browser_only",
                    "state_label": "网页数据",
                    "channel": "browser",
                    "page_count": 1,
                    "updated_at": int(time.time()),
                }],
            },
            receipt={
                "account_key": "acct_b",
                "finished_at": int(time.time() * 1000),
                "analysis_ready": True,
                "summary": {"coverage_rate": 100},
                "warnings": [],
            },
        )
        self.assertEqual(result["state"], "blocked")
        self.assertFalse(result["analysis_allowed"])
        self.assertIn("最近巡检账号与当前店铺不一致", result["blockers"])

    def test_operation_context_requires_review_for_incomplete_browser_scan(self):
        result = http_receiver.build_operation_context(
            catalog={
                "selected_store_key": "acct_a",
                "stores": [{
                    "key": "acct_a",
                    "label": "甲店",
                    "state": "browser_only",
                    "state_label": "网页数据",
                    "channel": "browser",
                    "page_count": 2,
                    "updated_at": int(time.time()),
                }],
            },
            receipt={
                "account_key": "acct_a",
                "finished_at": int(time.time() * 1000),
                "analysis_ready": False,
                "summary": {"coverage_rate": 65},
                "warnings": ["2 个页面读取失败"],
            },
        )
        self.assertEqual(result["state"], "review")
        self.assertTrue(result["analysis_allowed"])
        self.assertFalse(result["execution_review_allowed"])


if __name__ == "__main__":
    unittest.main()
