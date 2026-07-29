"""巨量千川官方 API 只读取数。

按“已授权店铺 -> 关联广告账户 -> 计划/报表/素材”解析多账号关系。
本模块不包含任何创建、修改、启停或调价接口。
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from oceanengine_oauth import HTTP_TIMEOUT_SECONDS, OceanEngineOAuth

API_BASE = "https://api.oceanengine.com/open_api/"
SYNC_STATUS_FILE = "oceanengine_sync_status.json"
MARKETING_GOALS = ("LIVE_PROM_GOODS", "VIDEO_PROM_GOODS")
REPORT_FIELDS = (
    "stat_cost",
    "show_cnt",
    "click_cnt",
    "ctr",
    "pay_order_count",
    "pay_order_amount",
    "prepay_and_pay_order_roi",
)
METRIC_LABELS = {
    "stat_cost": "广告消耗",
    "show_cnt": "展示次数",
    "click_cnt": "点击次数",
    "ctr": "点击率",
    "pay_order_count": "成交订单数",
    "pay_order_amount": "成交金额",
    "prepay_and_pay_order_roi": "支付ROI",
}


class OceanEngineAPIError(ValueError):
    def __init__(self, endpoint: str, code: Any, message: str):
        self.endpoint = endpoint
        self.code = code
        super().__init__(message or f"接口 {endpoint} 返回错误")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _safe_account_key(account_id: str) -> str:
    digest = hashlib.sha256(f"oceanengine:{account_id}".encode("utf-8")).hexdigest()
    return f"acct_api_{digest[:12]}"


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _table(rows: list[dict[str, Any]], columns: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    return {
        "headers": [label for _, label in columns],
        "rows": [
            [_stringify(row.get(key)) for key, _ in columns]
            for row in rows
        ],
    }


class OceanEngineDataClient:
    def __init__(self, oauth: OceanEngineOAuth):
        self.oauth = oauth

    def _get(self, token: str, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        query: dict[str, str] = {}
        for key, value in params.items():
            if value is None or value == "":
                continue
            query[key] = (
                json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                if isinstance(value, (dict, list, tuple))
                else str(value)
            )
        request = Request(
            f"{API_BASE}{endpoint}?{urlencode(query)}",
            headers={
                "Access-Token": token,
                "Accept": "application/json",
                "User-Agent": "Dian-Agent/2.27 read-only",
            },
        )
        try:
            with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise OceanEngineAPIError(endpoint, error.code, "官方接口暂时不可用。") from error
        except (URLError, OSError, json.JSONDecodeError) as error:
            raise OceanEngineAPIError(endpoint, "network", "连接巨量引擎失败，请稍后重试。") from error
        if not isinstance(payload, dict):
            raise OceanEngineAPIError(endpoint, "invalid", "官方接口返回格式异常。")
        code = payload.get("code", 0)
        if int(code or 0) != 0:
            raise OceanEngineAPIError(endpoint, code, str(payload.get("message") or "官方接口拒绝请求。"))
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def _paged(
        self,
        token: str,
        endpoint: str,
        params: dict[str, Any],
        *,
        page_size: int = 100,
        max_pages: int = 10,
        list_key: str = "list",
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            data = self._get(token, endpoint, {**params, "page": page, "page_size": page_size})
            values = data.get(list_key)
            if isinstance(values, list):
                records.extend(item for item in values if isinstance(item, dict))
            info = data.get("page_info") if isinstance(data.get("page_info"), dict) else {}
            total_page = int(info.get("total_page") or 0)
            if not values or (total_page and page >= total_page) or len(values) < page_size:
                break
        return records

    @staticmethod
    def _endpoint_result(
        name: str, fn: Callable[[], list[dict[str, Any]]]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        try:
            rows = fn()
            return rows, {"name": name, "ok": True, "count": len(rows), "message": "读取成功"}
        except OceanEngineAPIError as error:
            return [], {
                "name": name,
                "ok": False,
                "count": 0,
                "code": error.code,
                "message": str(error),
            }

    def sync(
        self,
        save_snapshot: Callable[[str, dict[str, Any]], dict[str, Any]],
        selected_account_ids: list[str] | None = None,
        days: int = 7,
    ) -> dict[str, Any]:
        token = self.oauth.get_valid_access_token()
        accounts = self.oauth.authorized_accounts_private()
        selected = {str(value) for value in (selected_account_ids or []) if str(value)}
        if selected:
            accounts = [item for item in accounts if str(item.get("account_id") or "") in selected]
        if not accounts:
            raise ValueError("没有可同步的授权账号，请先完成千川授权。")

        end_date = date.today() - timedelta(days=1)
        start_date = end_date - timedelta(days=max(1, min(30, int(days))) - 1)
        statuses: list[dict[str, Any]] = []
        resolved: dict[str, list[str]] = {}
        saved_pages = 0

        for account in accounts:
            shop_id = str(account.get("account_id") or "")
            shop_name = str(account.get("account_name") or "未命名千川账号")
            account_public = {
                "key": _safe_account_key(shop_id),
                "label": shop_name,
                "confidence": "high",
                "identity_source": "official_api",
            }
            endpoint_status: list[dict[str, Any]] = []
            try:
                advertiser_data = self._get(
                    token,
                    "v1.0/qianchuan/shop/advertiser/list/",
                    {"shop_id": shop_id, "page": 1, "page_size": 100},
                )
                advertiser_ids = [
                    str(value)
                    for value in (advertiser_data.get("list") or advertiser_data.get("adv_id_list") or [])
                    if str(value)
                ]
                endpoint_status.append(
                    {"name": "关联广告账户", "ok": True, "count": len(advertiser_ids), "message": "读取成功"}
                )
            except OceanEngineAPIError as error:
                advertiser_ids = []
                endpoint_status.append(
                    {"name": "关联广告账户", "ok": False, "count": 0, "code": error.code, "message": str(error)}
                )
            resolved[shop_id] = advertiser_ids

            plans: list[dict[str, Any]] = []
            reports: list[dict[str, Any]] = []
            materials: list[dict[str, Any]] = []
            videos: list[dict[str, Any]] = []
            for advertiser_id in advertiser_ids:
                for goal in MARKETING_GOALS:
                    goal_filter = {"marketing_goal": goal}
                    rows, status = self._endpoint_result(
                        f"{'直播' if goal.startswith('LIVE') else '短视频'}计划",
                        lambda aid=advertiser_id, f=goal_filter: self._paged(
                            token,
                            "v1.0/qianchuan/ad/get/",
                            {"advertiser_id": aid, "filtering": f},
                            page_size=100,
                        ),
                    )
                    for row in rows:
                        row["_marketing_goal"] = goal
                    plans.extend(rows)
                    endpoint_status.append(status)
                    rows, status = self._endpoint_result(
                        f"{'直播' if goal.startswith('LIVE') else '短视频'}经营报表",
                        lambda aid=advertiser_id, f=goal_filter: self._paged(
                            token,
                            "v1.0/qianchuan/report/advertiser/get/",
                            {
                                "advertiser_id": aid,
                                "start_date": start_date.isoformat(),
                                "end_date": end_date.isoformat(),
                                "fields": REPORT_FIELDS,
                                "filtering": f,
                            },
                        ),
                    )
                    for row in rows:
                        row["_marketing_goal"] = goal
                    reports.extend(rows)
                    endpoint_status.append(status)
                    uni_fields = (
                        "stat_cost",
                        "total_cost_per_pay_order_for_roi2",
                        "total_pay_order_count_for_roi2",
                        "total_pay_order_gmv_for_roi2",
                        "total_prepay_and_pay_order_roi2",
                    )
                    rows, status = self._endpoint_result(
                        f"{'直播' if goal.startswith('LIVE') else '商品'}全域推广",
                        lambda aid=advertiser_id, g=goal, fields=uni_fields: self._paged(
                            token,
                            "v1.0/qianchuan/uni_promotion/list/",
                            {
                                "advertiser_id": aid,
                                "start_time": f"{start_date.isoformat()} 00:00:00",
                                "end_time": f"{end_date.isoformat()} 23:59:59",
                                "marketing_goal": g,
                                "fields": fields,
                                "filtering": {},
                            },
                            page_size=100,
                            list_key="ad_list",
                        ),
                    )
                    for row in rows:
                        info = row.get("ad_info") if isinstance(row.get("ad_info"), dict) else {}
                        stats = row.get("stats_info") if isinstance(row.get("stats_info"), dict) else {}
                        plans.append(
                            {
                                "ad_name": info.get("name"),
                                "status": info.get("status"),
                                "_marketing_goal": info.get("marketing_goal") or goal,
                                "budget": info.get("budget"),
                                "bid": info.get("roi2_goal"),
                                "create_time": info.get("create_time"),
                                **stats,
                            }
                        )
                    endpoint_status.append(status)
                    try:
                        uni_report = self._get(
                            token,
                            "v1.0/qianchuan/report/uni_promotion/get/",
                            {
                                "advertiser_id": advertiser_id,
                                "start_date": start_date.isoformat(),
                                "end_date": end_date.isoformat(),
                                "marketing_goal": goal,
                                "fields": uni_fields,
                            },
                        )
                        uni_report["_marketing_goal"] = goal
                        reports.append(uni_report)
                        endpoint_status.append(
                            {
                                "name": f"{'直播' if goal.startswith('LIVE') else '商品'}全域报表",
                                "ok": True,
                                "count": 1,
                                "message": "读取成功",
                            }
                        )
                    except OceanEngineAPIError as error:
                        endpoint_status.append(
                            {
                                "name": f"{'直播' if goal.startswith('LIVE') else '商品'}全域报表",
                                "ok": False,
                                "count": 0,
                                "code": error.code,
                                "message": str(error),
                            }
                        )
                rows, status = self._endpoint_result(
                    "素材投放报表",
                    lambda aid=advertiser_id: self._paged(
                        token,
                        "v1.0/qianchuan/report/material/get/",
                        {
                            "advertiser_id": aid,
                            "start_date": start_date.isoformat(),
                            "end_date": end_date.isoformat(),
                            "fields": REPORT_FIELDS,
                            "filtering": {"material_type": "video"},
                        },
                    ),
                )
                materials.extend(rows)
                endpoint_status.append(status)
                rows, status = self._endpoint_result(
                    "视频素材库",
                    lambda aid=advertiser_id: self._paged(
                        token,
                        "v1.0/qianchuan/video/get/",
                        {"advertiser_id": aid},
                        page_size=100,
                    ),
                )
                videos.extend(rows)
                endpoint_status.append(status)

            captured_at = int(time.time() * 1000)
            privacy = {"masked": True, "raw_dom_sent": False, "page_text_included": False}
            common = {
                "schema_version": 2,
                "source": "qianchuan",
                "captured_at": captured_at,
                "reason": "official-api-manual-sync",
                "channel": "official_api",
                "privacy": privacy,
                "account": account_public,
                "date_range": f"{start_date.isoformat()} 至 {end_date.isoformat()}",
            }

            aggregate = {key: 0.0 for key in REPORT_FIELDS}
            for row in reports:
                metric_source = row.get("metrics") if isinstance(row.get("metrics"), dict) else row
                for key in REPORT_FIELDS:
                    value = metric_source.get(key)
                    if isinstance(value, (int, float)):
                        aggregate[key] += float(value)
                unified_aliases = {
                    "stat_cost": "stat_cost",
                    "total_pay_order_count_for_roi2": "pay_order_count",
                    "total_pay_order_gmv_for_roi2": "pay_order_amount",
                    "total_prepay_and_pay_order_roi2": "prepay_and_pay_order_roi",
                }
                for source_key, target_key in unified_aliases.items():
                    value = metric_source.get(source_key)
                    if source_key != target_key and isinstance(value, (int, float)):
                        aggregate[target_key] += float(value)
            safe_metrics = {
                METRIC_LABELS[key]: _stringify(value)
                for key, value in aggregate.items()
            }
            snapshots = [
                {
                    **common,
                    "page_type": "overview",
                    "title": "千川官方 API 经营概览",
                    "metrics": safe_metrics,
                    "safe_metrics": safe_metrics,
                    "signals": [],
                    "tables": [],
                },
                {
                    **common,
                    "page_type": "plans",
                    "title": "千川官方 API 投放计划",
                    "metrics": {"计划数": len(plans)},
                    "safe_metrics": {"计划数": len(plans)},
                    "signals": [],
                    "tables": [_table(plans, (
                        ("ad_name", "计划名称"), ("status", "状态"),
                        ("_marketing_goal", "推广类型"), ("budget", "预算"),
                        ("bid", "出价"), ("create_time", "创建时间"),
                    ))],
                },
                {
                    **common,
                    "page_type": "material_report",
                    "title": "千川官方 API 素材投放报表",
                    "metrics": {"素材数": len(materials)},
                    "safe_metrics": {"素材数": len(materials)},
                    "signals": [],
                    "tables": [_table(materials, (
                        ("material_id", "素材ID"), ("material_type", "素材类型"),
                        ("stat_cost", "消耗"), ("pay_order_amount", "成交金额"),
                        ("prepay_and_pay_order_roi", "支付ROI"), ("analysis_type", "素材建议"),
                    ))],
                },
                {
                    **common,
                    "page_type": "video_library",
                    "title": "千川官方 API 视频素材库",
                    "metrics": {"视频数": len(videos)},
                    "safe_metrics": {"视频数": len(videos)},
                    "signals": [],
                    "tables": [_table(videos, (
                        ("filename", "视频名称"), ("title", "抖音标题"),
                        ("source", "素材来源"), ("duration", "时长"),
                        ("create_time", "上传时间"), ("is_recommend", "平台推荐"),
                    ))],
                },
            ]
            canonical_account_key = account_public["key"]
            for snapshot in snapshots:
                row_count = sum(len(table.get("rows") or []) for table in snapshot["tables"])
                snapshot["quality"] = {
                    "score": 100 if advertiser_ids else 70,
                    "metric_count": len(snapshot["safe_metrics"]),
                    "table_count": len(snapshot["tables"]),
                    "row_count": row_count,
                    "warnings": [] if advertiser_ids else ["该店铺暂未关联千川广告账户"],
                    "pages_scanned": 1,
                    "virtual_scroll_passes": 0,
                    "pagination_truncated": False,
                }
                saved = save_snapshot("qianchuan", snapshot)
                saved_account = (
                    saved.get("data", {}).get("account")
                    if isinstance(saved.get("data"), dict)
                    else None
                )
                if isinstance(saved_account, dict) and saved_account.get("key"):
                    canonical_account_key = str(saved_account["key"])
                saved_pages += 1
            statuses.append(
                {
                    "account_key": canonical_account_key,
                    "account_name": shop_name,
                    "advertiser_count": len(advertiser_ids),
                    "pages_saved": 4,
                    "endpoints": endpoint_status,
                }
            )

        self.oauth.save_account_advertisers(resolved)
        failures = sum(
            1 for account in statuses for endpoint in account["endpoints"] if not endpoint["ok"]
        )
        result = {
            "ok": True,
            "mode": "read_only",
            "synced_at": int(time.time()),
            "date_range": f"{start_date.isoformat()} 至 {end_date.isoformat()}",
            "account_count": len(statuses),
            "saved_pages": saved_pages,
            "failure_count": failures,
            "accounts": statuses,
            "fallback": "browser_snapshot",
        }
        _atomic_json(self.oauth.data_dir / SYNC_STATUS_FILE, result)
        return result


def load_sync_status(data_dir: Path) -> dict[str, Any]:
    path = Path(data_dir) / SYNC_STATUS_FILE
    if not path.exists():
        return {
            "ok": True,
            "mode": "read_only",
            "synced_at": None,
            "account_count": 0,
            "saved_pages": 0,
            "failure_count": 0,
            "accounts": [],
            "fallback": "browser_snapshot",
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
