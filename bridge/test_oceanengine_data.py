import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError, URLError

import oceanengine_data
from oceanengine_data import (
    OceanEngineAPIError,
    OceanEngineDataClient,
    _atomic_json,
    _safe_plan_name,
    load_sync_status,
)


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
        self.omit_ratio_inputs = False
        self.include_standard_report = False
        self.standard_plans_by_goal = {}
        self.returned_standard_rows = []
        self.learning_status_by_id = {}
        self.learning_fail_calls = set()
        self.learning_requests = []
        self.low_efficiency_ids = set()
        self.fail_low_efficiency = False
        self.low_efficiency_requests = []
        self.uni_promotion_requests = []
        self.uni_report_requests = []
        self.reject_report_only_uni_list_fields = False

    def _get(self, token, endpoint, params):
        self.assert_token_not_public = token == "internal-token"
        self.called_endpoints.append(endpoint)
        if endpoint.endswith("shop/advertiser/list/"):
            return {"list": ["adv-1"] if params["shop_id"] == "shop-1" else []}
        if endpoint.endswith("ad/learning_status/get/"):
            self.learning_requests.append(dict(params))
            if len(self.learning_requests) in self.learning_fail_calls:
                raise OceanEngineAPIError(endpoint, "learning_unavailable", "学习期接口暂不可用")
            return {
                "list": [
                    {"ad_id": ad_id, "status": self.learning_status_by_id[str(ad_id)]}
                    for ad_id in params.get("ad_ids", [])
                    if str(ad_id) in self.learning_status_by_id
                ]
            }
        if endpoint.endswith("qianchuan/lq_ad/get/"):
            self.low_efficiency_requests.append(dict(params))
            if self.fail_low_efficiency:
                raise OceanEngineAPIError(endpoint, "lq_unavailable", "低效计划接口暂不可用")
            return {"ad_ids": [int(value) for value in sorted(self.low_efficiency_ids, key=int)]}
        if endpoint.endswith("qianchuan/ad/get/"):
            goal = str((params.get("filtering") or {}).get("marketing_goal") or "")
            source = self.standard_plans_by_goal.get(goal, [])
            page_size = int(params.get("page_size") or 100)
            page = int(params.get("page") or 1)
            start = (page - 1) * page_size
            values = [dict(item) for item in source[start:start + page_size]]
            self.returned_standard_rows.extend(values)
            total_page = (len(source) + page_size - 1) // page_size
            return {"list": values, "page_info": {"total_page": total_page}}
        if endpoint.endswith("uni_promotion/list/"):
            self.uni_promotion_requests.append(dict(params))
            if self.reject_report_only_uni_list_fields and {
                "show_cnt",
                "click_cnt",
            }.intersection(params.get("fields") or ()):
                raise OceanEngineAPIError(
                    endpoint,
                    40000,
                    "fields value is not one of the allowed values",
                )
            stats = {
                "stat_cost": "20",
                "total_pay_order_count_for_roi2": "2",
                "total_pay_order_gmv_for_roi2": "50",
                "show_cnt": "200",
                "click_cnt": "20",
                # Deliberately wrong derived values: sync must ignore them.
                "total_prepay_and_pay_order_roi2": 999,
                "ctr": 999,
            }
            if self.omit_ratio_inputs:
                stats.pop("total_pay_order_gmv_for_roi2")
                stats.pop("show_cnt")
            return {
                "ad_list": [
                    {
                        "ad_info": {
                            "ad_id": f"ad-{params['marketing_goal'].lower()}",
                            "name": "全域计划",
                            "status": "DELIVERY_OK",
                            "marketing_goal": params["marketing_goal"],
                            "budget": 100,
                        },
                        "stats_info": stats,
                    }
                ],
                "page_info": {"total_page": 1},
            }
        if endpoint.endswith("report/uni_promotion/get/"):
            self.uni_report_requests.append(dict(params))
            return {
                "stat_cost": 20,
                "total_pay_order_count_for_roi2": 2,
                "total_pay_order_gmv_for_roi2": 50,
                "show_cnt": 200,
                "click_cnt": 20,
                "total_prepay_and_pay_order_roi2": 999,
                "ctr": 999,
            }
        if endpoint.endswith("report/advertiser/get/") and self.include_standard_report:
            return {
                "list": [{
                    "stat_cost": 20,
                    "pay_order_count": 2,
                    "pay_order_amount": 50,
                    "show_cnt": 200,
                    "click_cnt": 20,
                }],
                "page_info": {"total_page": 1},
            }
        if endpoint.endswith("uni_promotion/ad/control_task/list/"):
            return {
                "list": [{
                    "task_id": f"task-{params['marketing_goal'].lower()}-{params['scene'].lower()}",
                    "scene": params["scene"],
                    "status": "PROCESSING",
                    "budget": "80",
                }]
            }
        if endpoint.endswith("video/get/"):
            return {
                "list": [{"filename": "素材.mp4", "duration": 12}],
                "page_info": {"total_page": 1},
            }
        return {"list": [], "page_info": {"total_page": 0}}


def _plan_table_records(snapshot):
    table = snapshot["tables"][0]
    return [dict(zip(table["headers"], row)) for row in table["rows"]]


def _stable_decision_records(snapshot):
    records = []
    for item in snapshot["decision_records"]:
        record = dict(item)
        record.pop("captured_at_ms", None)
        records.append(record)
    return records


class OceanEngineDataTests(unittest.TestCase):
    def test_plan_name_is_normalized_for_trusted_snapshot_display(self):
        value = _safe_plan_name(" <b>夏日\u202e\n  直播</b> ")
        self.assertEqual("b 夏日 直播 /b", value)
        self.assertNotIn("\u202e", value)
        self.assertNotIn("<", value)
        self.assertNotIn(">", value)

    def test_official_api_redirect_is_rejected_before_access_token_forwarding(self):
        handler = oceanengine_data._RejectOceanEngineRedirects()
        response = Mock()
        with self.assertRaisesRegex(URLError, "Access Token"):
            handler.redirect_request(
                object(), response, 307, "Temporary Redirect", {}, "https://attacker.example/data"
            )
        response.close.assert_called_once_with()

    def test_official_api_http_error_closes_response(self):
        response = Mock()
        error = HTTPError("https://api.oceanengine.com/test", 503, "unavailable", {}, response)
        client = OceanEngineDataClient(FakeOAuth(Path(".")))
        with patch.object(oceanengine_data, "urlopen", side_effect=error):
            with self.assertRaises(OceanEngineAPIError):
                client._get("internal-token", "test", {})
        response.close.assert_called_once_with()

    def test_official_api_response_read_is_bounded(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"x" * (oceanengine_data.MAX_API_RESPONSE_BYTES + 1)
        client = OceanEngineDataClient(FakeOAuth(Path(".")))

        with patch.object(oceanengine_data, "urlopen", return_value=response):
            with self.assertRaisesRegex(OceanEngineAPIError, "官方接口响应过大") as raised:
                client._get("internal-token", "test", {})

        self.assertEqual("response_too_large", raised.exception.code)
        response.read.assert_called_once_with(oceanengine_data.MAX_API_RESPONSE_BYTES + 1)
        response.__exit__.assert_called_once()

    def test_official_api_invalid_utf8_is_normalized(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"\xff"
        client = OceanEngineDataClient(FakeOAuth(Path(".")))

        with patch.object(oceanengine_data, "urlopen", return_value=response):
            with self.assertRaises(OceanEngineAPIError) as raised:
                client._get("internal-token", "test", {})

        self.assertEqual("invalid", raised.exception.code)

    def test_official_api_nonfinite_json_is_rejected(self):
        client = OceanEngineDataClient(FakeOAuth(Path(".")))
        for raw in (
            b'{"code":0,"data":{"metric":NaN}}',
            b'{"code":0,"data":{"metric":Infinity}}',
            b'{"code":0,"data":{"metric":1e9999}}',
        ):
            response = MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = raw
            with self.subTest(raw=raw), patch.object(
                oceanengine_data, "urlopen", return_value=response
            ), self.assertRaises(OceanEngineAPIError) as raised:
                client._get("internal-token", "test", {})
            self.assertEqual("invalid", raised.exception.code)

    def test_official_api_malformed_code_is_normalized(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({"code": {"unexpected": True}, "data": {}}).encode("utf-8")
        client = OceanEngineDataClient(FakeOAuth(Path(".")))

        with patch.object(oceanengine_data, "urlopen", return_value=response):
            with self.assertRaises(OceanEngineAPIError) as raised:
                client._get("internal-token", "test", {})

        self.assertEqual("invalid", raised.exception.code)

    def test_official_api_requires_object_data_contract(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({"code": 0, "data": None}).encode("utf-8")
        client = OceanEngineDataClient(FakeOAuth(Path(".")))

        with patch.object(oceanengine_data, "urlopen", return_value=response):
            with self.assertRaises(OceanEngineAPIError) as raised:
                client._get("internal-token", "test", {})

        self.assertEqual("invalid", raised.exception.code)

    def test_official_api_fractional_success_code_is_rejected(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({"code": 0.5, "data": {}}).encode("utf-8")
        client = OceanEngineDataClient(FakeOAuth(Path(".")))

        with patch.object(oceanengine_data, "urlopen", return_value=response):
            with self.assertRaises(OceanEngineAPIError) as raised:
                client._get("internal-token", "test", {})

        self.assertEqual("invalid", raised.exception.code)

    def test_official_api_malformed_pagination_is_normalized(self):
        client = OceanEngineDataClient(FakeOAuth(Path(".")))
        with patch.object(
            client,
            "_get",
            return_value={"list": [{"id": "one"}], "page_info": {"total_page": {"bad": True}}},
        ):
            with self.assertRaises(OceanEngineAPIError) as raised:
                client._paged("internal-token", "test", {}, page_size=1)

        self.assertEqual("invalid", raised.exception.code)

    def test_official_api_pagination_never_silently_truncates(self):
        client = OceanEngineDataClient(FakeOAuth(Path(".")))
        with patch.object(
            client,
            "_get",
            return_value={"list": [{"id": "one"}], "page_info": {"total_page": 11}},
        ) as getter:
            with self.assertRaises(OceanEngineAPIError) as raised:
                client._paged("internal-token", "test", {}, page_size=1, max_pages=10)
        self.assertEqual("pagination_truncated", raised.exception.code)
        getter.assert_called_once()

        with patch.object(
            client,
            "_get",
            return_value={"list": [{"id": "one"}], "page_info": {}},
        ) as getter:
            with self.assertRaises(OceanEngineAPIError) as raised:
                client._paged("internal-token", "test", {}, page_size=1, max_pages=10)
        self.assertEqual("pagination_truncated", raised.exception.code)
        self.assertEqual(10, getter.call_count)

    def test_official_api_pagination_rejects_fractional_boolean_and_scalar_rows(self):
        client = OceanEngineDataClient(FakeOAuth(Path(".")))
        for payload in (
            {"list": [{"id": "one"}], "page_info": {"total_page": 1.5}},
            {"list": [{"id": "one"}], "page_info": {"total_page": True}},
            {"list": ["not-an-object"], "page_info": {"total_page": 1}},
        ):
            with self.subTest(payload=payload), patch.object(client, "_get", return_value=payload):
                with self.assertRaises(OceanEngineAPIError) as raised:
                    client._paged("internal-token", "test", {}, page_size=1)
                self.assertEqual("invalid", raised.exception.code)

    def test_official_api_missing_list_contract_is_rejected(self):
        client = OceanEngineDataClient(FakeOAuth(Path(".")))
        with patch.object(client, "_get", return_value={"page_info": {"total_page": 0}}):
            with self.assertRaises(OceanEngineAPIError) as raised:
                client._paged("internal-token", "test", {})

        self.assertEqual("invalid", raised.exception.code)

    def test_missing_low_efficiency_list_is_unknown_not_healthy_empty(self):
        client = OceanEngineDataClient(FakeOAuth(Path(".")))
        with patch.object(client, "_get", return_value={}):
            identifiers, status = client._low_efficiency_ids("internal-token", "advertiser-1")

        self.assertIsNone(identifiers)
        self.assertFalse(status["ok"])
        self.assertEqual("invalid", status["code"])

    def test_concurrent_sync_status_writes_remain_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oceanengine_sync_status.json"
            errors = []

            def writer(index):
                try:
                    for revision in range(20):
                        _atomic_json(path, {"writer": index, "revision": revision})
                except Exception as error:  # pragma: no cover - diagnostic capture
                    errors.append(error)

            workers = [threading.Thread(target=writer, args=(index,)) for index in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

            self.assertEqual([], errors)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn(saved["writer"], range(8))
            self.assertIn(saved["revision"], range(20))
            self.assertEqual([], list(path.parent.glob(f".{path.name}.*.tmp")))

    def test_corrupt_sync_status_fails_safe_without_overwriting_evidence(self):
        corrupt_values = (
            b'{"unfinished":',
            b'["not-an-object"]',
            b'{"ok":true,"mode":"read_only","accounts":"not-a-list"}',
            b'{"ok":true,"mode":"read_only","account_count":1,"accounts":['
            b'{"account_key":"acct_api_0123456789ab","advertiser_count":false,"endpoints":[]}]}',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oceanengine_sync_status.json"
            for raw in corrupt_values:
                with self.subTest(raw=raw):
                    path.write_bytes(raw)
                    modified_at = path.stat().st_mtime_ns
                    status = load_sync_status(Path(directory))
                    self.assertFalse(status["ok"])
                    self.assertEqual("corrupt", status["status"])
                    self.assertEqual("SYNC_STATUS_CORRUPT", status["error"]["code"])
                    self.assertEqual([], status["accounts"])
                    self.assertTrue(status["evidence_preserved"])
                    self.assertEqual(raw, path.read_bytes())
                    self.assertEqual(modified_at, path.stat().st_mtime_ns)

    def test_numeric_platform_error_code_round_trips_as_bounded_text(self):
        rows, endpoint = OceanEngineDataClient._endpoint_result(
            "计划列表",
            lambda: (_ for _ in ()).throw(
                OceanEngineAPIError("v1.0/qianchuan/ad/get/", 40000, "权限不足")
            ),
        )
        self.assertEqual([], rows)
        self.assertEqual("40000", endpoint["code"])

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oceanengine_sync_status.json"
            path.write_text(json.dumps({
                "ok": True,
                "mode": "read_only",
                "synced_at": 1_800_000_000,
                "account_count": 1,
                "saved_pages": 0,
                "failure_count": 1,
                "pagination_truncated": False,
                "data_complete": False,
                "accounts": [{
                    "account_key": "acct_api_0123456789ab",
                    "account_name": "测试店铺",
                    "advertiser_count": 1,
                    "pages_saved": 0,
                    "pagination_truncated": False,
                    "endpoints": [{
                        "name": "计划列表",
                        "ok": False,
                        "count": 0,
                        # Legacy v4.14.3 status files stored this as an int.
                        "code": 50000,
                        "message": "平台暂时不可用",
                    }],
                }],
                "fallback": "browser_snapshot",
            }, ensure_ascii=False), encoding="utf-8")

            status = load_sync_status(Path(directory))
            self.assertTrue(status["ok"])
            self.assertEqual("50000", status["accounts"][0]["endpoints"][0]["code"])
            self.assertEqual(50000, json.loads(path.read_text(encoding="utf-8"))["accounts"][0]["endpoints"][0]["code"])

    def test_sync_status_rejects_non_integral_legacy_error_code(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oceanengine_sync_status.json"
            path.write_text(json.dumps({
                "ok": True,
                "mode": "read_only",
                "account_count": 1,
                "accounts": [{
                    "account_key": "acct_api_0123456789ab",
                    "advertiser_count": 0,
                    "pages_saved": 0,
                    "endpoints": [{"name": "计划列表", "ok": False, "code": 1.5}],
                }],
            }), encoding="utf-8")

            status = load_sync_status(Path(directory))
            self.assertFalse(status["ok"])
            self.assertEqual("SYNC_STATUS_CORRUPT", status["error"]["code"])

    def test_multi_shop_read_only_sync_and_safe_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            oauth = FakeOAuth(Path(directory))
            client = FakeDataClient(oauth)
            snapshots = []

            def save(source, data, *, trusted_origin=None):
                self.assertEqual(source, "qianchuan")
                self.assertEqual("official_api_oauth_client", trusted_origin)
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
            overview = next(item for item in snapshots if item["page_type"] == "overview" and item.get("quality", {}).get("score") == 100)
            self.assertEqual("2.5", overview["safe_metrics"]["支付ROI"])
            self.assertEqual("10", overview["safe_metrics"]["点击率"])
            self.assertEqual("recomputed_from_additive_metrics", overview["metric_contract"]["derived"]["roi"]["reason"])
            plans = next(item for item in snapshots if item["page_type"] == "plans" and item["decision_records"])
            exact = plans["decision_records"][0]
            self.assertEqual("shop-1", exact["shop_id"])
            self.assertEqual("adv-1", exact["advertiser_id"])
            self.assertTrue(exact["ad_id"].startswith("ad-"))
            self.assertTrue(exact["task_id"].startswith("task-"))
            self.assertEqual("全域计划", exact["plan_name"])
            self.assertIn(exact["marketing_goal"], {"LIVE_PROM_GOODS", "VIDEO_PROM_GOODS"})
            self.assertGreater(exact["captured_at_ms"], 0)
            self.assertEqual("qianchuan-official-exact-v1", exact["metric_contract_version"])
            self.assertEqual(2.5, exact["actual_roi"])
            self.assertEqual(10, exact["ctr"])
            self.assertEqual(80, exact["current_total_budget"])
            self.assertEqual(100, exact["parent_plan_budget"])
            self.assertEqual(80, exact["control_task_budget"])
            self.assertTrue(exact["production_identifier_complete"])
            self.assertFalse(exact["platform_write_enabled"])
            self.assertEqual(load_sync_status(Path(directory))["account_count"], 2)

    def test_sync_rejects_scalar_advertiser_list_and_marks_snapshots_incomplete(self):
        class MalformedAdvertiserClient(FakeDataClient):
            def _get(self, token, endpoint, params):
                if endpoint.endswith("shop/advertiser/list/"):
                    return {"list": "12"}
                return super()._get(token, endpoint, params)

        with tempfile.TemporaryDirectory() as directory:
            oauth = FakeOAuth(Path(directory))
            client = MalformedAdvertiserClient(oauth)
            snapshots = []
            result = client.sync(
                lambda source, data, *, trusted_origin=None: (
                    snapshots.append(data) or {"data": data}
                ),
                selected_account_ids=["shop-1"],
                days=1,
            )

            self.assertFalse(result["data_complete"])
            self.assertEqual(1, result["failure_count"])
            self.assertEqual([], oauth.saved_advertisers["shop-1"])
            self.assertEqual(4, len(snapshots))
            self.assertTrue(
                all(item["quality"]["collection_complete"] is False for item in snapshots)
            )
            self.assertTrue(all(item["quality"]["score"] <= 40 for item in snapshots))

    def test_uni_plan_list_does_not_request_report_only_impression_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeDataClient(FakeOAuth(Path(directory)))
            client.reject_report_only_uni_list_fields = True
            result = client.sync(
                lambda source, data, *, trusted_origin=None: {"data": data},
                selected_account_ids=["shop-1"],
                days=1,
            )

            self.assertEqual(0, result["failure_count"])
            self.assertTrue(client.uni_promotion_requests)
            for request in client.uni_promotion_requests:
                fields = tuple(request["fields"])
                self.assertNotIn("show_cnt", fields)
                self.assertNotIn("click_cnt", fields)
                self.assertEqual(
                    (
                        "stat_cost",
                        "total_cost_per_pay_order_for_roi2",
                        "total_pay_order_count_for_roi2",
                        "total_pay_order_gmv_for_roi2",
                        "total_prepay_and_pay_order_roi2",
                    ),
                    fields,
                )

            # The aggregate report has an independent contract and still
            # requests click/impression inputs used to recompute CTR.
            self.assertTrue(client.uni_report_requests)
            for request in client.uni_report_requests:
                self.assertIn("show_cnt", request["fields"])
                self.assertIn("click_cnt", request["fields"])

    def test_sync_marks_all_snapshots_incomplete_after_pagination_truncation(self):
        class TruncatedPlanClient(FakeDataClient):
            def _paged(self, token, endpoint, params, **kwargs):
                if endpoint.endswith("qianchuan/ad/get/"):
                    raise OceanEngineAPIError(
                        endpoint,
                        "pagination_truncated",
                        "官方接口数据超过安全分页上限",
                    )
                return super()._paged(token, endpoint, params, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            snapshots = []
            client = TruncatedPlanClient(FakeOAuth(Path(directory)))
            result = client.sync(
                lambda source, data, *, trusted_origin=None: snapshots.append(data) or {"data": data},
                selected_account_ids=["shop-1"],
                days=1,
            )

        self.assertTrue(result["pagination_truncated"])
        self.assertFalse(result["data_complete"])
        self.assertTrue(snapshots)
        self.assertTrue(all(item["quality"]["pagination_truncated"] for item in snapshots))
        self.assertTrue(all(item["quality"]["collection_complete"] is False for item in snapshots))
        self.assertTrue(all(item["quality"]["score"] <= 40 for item in snapshots))

    def test_exact_snapshot_marks_derived_metrics_unavailable_without_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            oauth = FakeOAuth(Path(directory))
            client = FakeDataClient(oauth)
            client.omit_ratio_inputs = True
            snapshots = []

            def save(source, data, *, trusted_origin=None):
                snapshots.append(data)
                return {"data": data}

            client.sync(save, selected_account_ids=["shop-1"], days=1)
            plans = next(item for item in snapshots if item["page_type"] == "plans")
            exact = plans["decision_records"][0]
            self.assertIsNone(exact["actual_roi"])
            self.assertEqual("unavailable", exact["actual_roi_status"])
            self.assertEqual("missing_numerator", exact["actual_roi_unavailable_reason"])
            self.assertIsNone(exact["ctr"])
            self.assertEqual("unavailable", exact["ctr_status"])
            self.assertEqual("missing_denominator", exact["ctr_unavailable_reason"])
            self.assertIn("unavailable", " ".join(plans["quality"]["warnings"]))

    def test_overview_does_not_sum_overlapping_standard_and_uni_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            oauth = FakeOAuth(Path(directory))
            client = FakeDataClient(oauth)
            client.include_standard_report = True
            snapshots = []

            def save(source, data, *, trusted_origin=None):
                snapshots.append(data)
                return {"data": data}

            client.sync(save, selected_account_ids=["shop-1"], days=1)
            overview = next(item for item in snapshots if item["page_type"] == "overview")
            self.assertEqual("40", overview["safe_metrics"]["广告消耗"])
            self.assertEqual("100", overview["safe_metrics"]["成交金额"])
            self.assertEqual("2.5", overview["safe_metrics"]["支付ROI"])

    def test_standard_plans_receive_read_only_health_without_touching_uni_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline_snapshots = []
            baseline = FakeDataClient(FakeOAuth(Path(directory) / "baseline"))
            baseline.sync(
                lambda source, data, *, trusted_origin=None: baseline_snapshots.append(data) or {"data": data},
                selected_account_ids=["shop-1"],
                days=1,
            )
            self.assertEqual([], baseline.learning_requests)
            self.assertEqual([], baseline.low_efficiency_requests)

            snapshots = []
            client = FakeDataClient(FakeOAuth(Path(directory) / "enriched"))
            client.standard_plans_by_goal = {
                "LIVE_PROM_GOODS": [{"ad_id": 101, "name": "直播标准计划", "status": "AD_STATUS_DELIVERY_OK"}],
                "VIDEO_PROM_GOODS": [{"ad_id": 102, "name": "商品标准计划", "status": "AD_STATUS_DELIVERY_OK"}],
            }
            client.learning_status_by_id = {"101": "LEARNING", "102": "LEARNED"}
            client.low_efficiency_ids = {"102"}
            client.sync(
                lambda source, data, *, trusted_origin=None: snapshots.append(data) or {"data": data},
                selected_account_ids=["shop-1"],
                days=1,
            )

            plans = next(item for item in snapshots if item["page_type"] == "plans")
            baseline_plans = next(item for item in baseline_snapshots if item["page_type"] == "plans")
            records = {item["计划ID"]: item for item in _plan_table_records(plans)}
            self.assertEqual("学习中", records["101"]["学习期状态"])
            self.assertEqual("学习完成", records["102"]["学习期状态"])
            self.assertEqual("本次未命中", records["101"]["平台低效"])
            self.assertEqual("平台标记低效", records["102"]["平台低效"])
            self.assertEqual("官方 API", records["101"]["诊断来源"])
            uni_records = [item for key, item in records.items() if key.startswith("ad-")]
            self.assertTrue(uni_records)
            self.assertTrue(all(item["学习期状态"] == "" and item["平台低效"] == "" for item in uni_records))
            self.assertEqual(1, len(client.learning_requests))
            self.assertEqual([101, 102], client.learning_requests[0]["ad_ids"])
            self.assertEqual(1, len(client.low_efficiency_requests))
            self.assertEqual({"marketing_scene": "ALL"}, client.low_efficiency_requests[0]["filtering"])
            self.assertEqual(_stable_decision_records(baseline_plans), _stable_decision_records(plans))
            self.assertTrue(all("learning_phase" not in item for item in plans["decision_records"]))
            self.assertTrue(all("platform_low_efficiency" not in item for item in plans["decision_records"]))

    def test_learning_status_requests_are_chunked_to_fifty_unique_standard_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            oauth = FakeOAuth(Path(directory))
            client = FakeDataClient(oauth)
            ids = list(range(1, 102))
            client.standard_plans_by_goal = {
                "LIVE_PROM_GOODS": [
                    {"ad_id": ad_id, "name": f"标准计划 {ad_id}", "status": "AD_STATUS_DELIVERY_OK"}
                    for ad_id in ids
                ]
            }
            client.learning_status_by_id = {str(ad_id): "LEARNING" for ad_id in ids}
            snapshots = []

            client.sync(
                lambda source, data, *, trusted_origin=None: snapshots.append(data) or {"data": data},
                selected_account_ids=["shop-1"],
                days=1,
            )

            batches = [request["ad_ids"] for request in client.learning_requests]
            self.assertEqual([50, 50, 1], [len(batch) for batch in batches])
            self.assertEqual(ids, [value for batch in batches for value in batch])
            self.assertTrue(all(len(batch) <= 50 for batch in batches))
            self.assertEqual(1, len(client.low_efficiency_requests))
            plans = next(item for item in snapshots if item["page_type"] == "plans")
            records = {item["计划ID"]: item for item in _plan_table_records(plans)}
            self.assertEqual("学习中", records["101"]["学习期状态"])

    def test_health_endpoint_failures_degrade_to_unavailable_and_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            oauth = FakeOAuth(Path(directory))
            client = FakeDataClient(oauth)
            ids = list(range(1, 52))
            client.standard_plans_by_goal = {
                "LIVE_PROM_GOODS": [
                    {"ad_id": ad_id, "name": f"标准计划 {ad_id}", "status": "AD_STATUS_DELIVERY_OK"}
                    for ad_id in ids
                ]
            }
            client.learning_status_by_id = {str(ad_id): "LEARNING" for ad_id in ids}
            client.learning_fail_calls = {2}
            client.fail_low_efficiency = True
            snapshots = []

            result = client.sync(
                lambda source, data, *, trusted_origin=None: snapshots.append(data) or {"data": data},
                selected_account_ids=["shop-1"],
                days=1,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(2, result["failure_count"])
            plans = next(item for item in snapshots if item["page_type"] == "plans")
            records = {item["计划ID"]: item for item in _plan_table_records(plans)}
            self.assertEqual("学习中", records["1"]["学习期状态"])
            self.assertEqual("暂不可读", records["51"]["学习期状态"])
            self.assertEqual("暂不可读", records["1"]["平台低效"])
            self.assertEqual("暂不可读", records["51"]["平台低效"])
            self.assertTrue(all("false" not in item["平台低效"].lower() for item in records.values()))
            self.assertTrue(all("部分不可用" in records[str(ad_id)]["诊断来源"] for ad_id in ids))
            internal = {str(item["ad_id"]): item for item in client.returned_standard_rows}
            self.assertEqual("learning", internal["1"]["learning_phase"])
            self.assertEqual("unavailable", internal["51"]["learning_phase"])
            self.assertEqual("unknown", internal["1"]["platform_low_efficiency"])
            self.assertIsNot(False, internal["1"]["platform_low_efficiency"])
            self.assertTrue(all("learning_phase" not in item for item in plans["decision_records"]))
            self.assertTrue(all("platform_low_efficiency" not in item for item in plans["decision_records"]))


if __name__ == "__main__":
    unittest.main()
