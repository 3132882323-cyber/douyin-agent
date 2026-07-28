import tempfile
import unittest
from pathlib import Path

from oceanengine_data import OceanEngineDataClient, load_sync_status


class FakeOAuth:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.saved_advertisers = None

    def get_valid_access_token(self):
        return "internal-token"

    def authorized_accounts_private(self):
        return [
            {"account_id": "shop-1", "account_name": "甲店"},
            {"account_id": "shop-2", "account_name": "乙店"},
        ]

    def save_account_advertisers(self, value):
        self.saved_advertisers = value


class FakeDataClient(OceanEngineDataClient):
    def __init__(self, oauth):
        super().__init__(oauth)
        self.called_endpoints = []

    def _get(self, token, endpoint, params):
        self.assert_token_not_public = token == "internal-token"
        self.called_endpoints.append(endpoint)
        if endpoint.endswith("shop/advertiser/list/"):
            return {"list": ["adv-1"] if params["shop_id"] == "shop-1" else []}
        if endpoint.endswith("uni_promotion/list/"):
            return {
                "ad_list": [
                    {
                        "ad_info": {
                            "name": "全域计划",
                            "status": "DELIVERY_OK",
                            "marketing_goal": params["marketing_goal"],
                            "budget": 100,
                        },
                        "stats_info": {"stat_cost": 20},
                    }
                ],
                "page_info": {"total_page": 1},
            }
        if endpoint.endswith("report/uni_promotion/get/"):
            return {
                "stat_cost": 20,
                "total_pay_order_count_for_roi2": 2,
                "total_pay_order_gmv_for_roi2": 50,
                "total_prepay_and_pay_order_roi2": 2.5,
            }
        if endpoint.endswith("video/get/"):
            return {
                "list": [{"filename": "素材.mp4", "duration": 12}],
                "page_info": {"total_page": 1},
            }
        return {"list": [], "page_info": {"total_page": 0}}


class OceanEngineDataTests(unittest.TestCase):
    def test_multi_shop_read_only_sync_and_safe_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            oauth = FakeOAuth(Path(directory))
            client = FakeDataClient(oauth)
            snapshots = []

            def save(source, data):
                self.assertEqual(source, "qianchuan")
                self.assertNotIn("access_token", str(data))
                self.assertEqual(data["channel"], "official_api")
                snapshots.append(data)
                return {"data": data}

            result = client.sync(save, days=7)

            self.assertTrue(result["ok"])
            self.assertEqual(result["mode"], "read_only")
            self.assertEqual(result["account_count"], 2)
            self.assertEqual(result["saved_pages"], 8)
            self.assertEqual(len(snapshots), 8)
            self.assertEqual(
                {item["page_type"] for item in snapshots},
                {"overview", "plans", "material_report", "video_library"},
            )
            self.assertEqual(oauth.saved_advertisers["shop-1"], ["adv-1"])
            self.assertEqual(oauth.saved_advertisers["shop-2"], [])
            self.assertTrue(all("/update/" not in path and "/create/" not in path for path in client.called_endpoints))
            self.assertTrue(client.assert_token_not_public)
            self.assertEqual(load_sync_status(Path(directory))["account_count"], 2)


if __name__ == "__main__":
    unittest.main()
