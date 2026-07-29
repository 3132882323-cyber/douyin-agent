import json
import tempfile
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
                {"key": "acct_official", "label": "甲店"},
                {"key": "acct_browser", "label": "乙店"},
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
                patch.object(http_receiver, "load_sync_status", return_value=sync),
                patch.object(
                    http_receiver,
                    "load_agent_settings",
                    return_value={"qianchuan_account_key": "acct_official"},
                ),
            ):
                result = http_receiver.build_store_catalog()

            self.assertEqual(result["mode"], "multi_store")
            self.assertEqual(result["data_isolation"], "per_store")
            self.assertEqual(result["store_count"], 2)
            by_key = {item["key"]: item for item in result["stores"]}
            self.assertEqual(by_key["acct_official"]["state"], "ready")
            self.assertEqual(by_key["acct_official"]["page_count"], 1)
            self.assertTrue(by_key["acct_official"]["selected"])
            self.assertEqual(by_key["acct_browser"]["state"], "browser_only")
            self.assertEqual(by_key["acct_browser"]["channel"], "browser")

    def test_store_without_advertiser_is_not_reported_as_sync_failure(self):
        with (
            patch.object(http_receiver, "list_qianchuan_accounts", return_value=[
                {"key": "acct_empty", "label": "未投放店"}
            ]),
            patch.object(http_receiver, "load_sync_status", return_value={
                "accounts": [{"account_key": "acct_empty", "advertiser_count": 0}]
            }),
            patch.object(
                http_receiver,
                "load_agent_settings",
                return_value={"qianchuan_account_key": "acct_empty"},
            ),
        ):
            result = http_receiver.build_store_catalog()
        self.assertEqual(result["stores"][0]["state"], "not_linked")
        self.assertEqual(result["stores"][0]["state_label"], "未关联广告账户")


if __name__ == "__main__":
    unittest.main()
