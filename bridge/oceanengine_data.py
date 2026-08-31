"""巨量千川官方 API 只读取数。

按“已授权店铺 -> 关联广告账户 -> 计划/报表/素材”解析多账号关系。
本模块不包含任何创建、修改、启停或调价接口。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from oceanengine_oauth import HTTP_TIMEOUT_SECONDS, OceanEngineOAuth

API_BASE = "https://api.oceanengine.com/open_api/"
MAX_API_RESPONSE_BYTES = 4 * 1024 * 1024
SYNC_STATUS_FILE = "oceanengine_sync_status.json"
MARKETING_GOALS = ("LIVE_PROM_GOODS", "VIDEO_PROM_GOODS")
CONTROL_TASK_SCENES = ("SMART_BOOST", "MATERIAL_ADD_BUDGET")
OFFICIAL_METRIC_CONTRACT_VERSION = "qianchuan-official-exact-v1"


class _RejectOceanEngineRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        # Access-Token is a non-standard header and urllib may copy it when
        # following redirects. Official read APIs must never redirect it.
        try:
            if fp is not None:
                fp.close()
        finally:
            raise URLError("巨量引擎数据接口返回了重定向，已拒绝转发 Access Token。")


_OCEANENGINE_OPENER = build_opener(_RejectOceanEngineRedirects())
_SYNC_STATUS_WRITE_LOCK = threading.Lock()


def urlopen(request: Request, timeout: float):
    """Patchable official-API opener with redirect rejection."""

    return _OCEANENGINE_OPENER.open(request, timeout=timeout)


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"official API JSON number {value} is not finite")


def _parse_finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"official API JSON number {value} is not finite")
    return parsed
LEARNING_STATUS_CONTRACT = {
    "LEARNING": ("learning", "学习中"),
    "LEARNED": ("learned", "学习完成"),
    "LEARN_FAILED": ("failed", "学习失败"),
    "DEFAULT": ("none", "无学习期状态"),
}
REPORT_FIELDS = (
    "stat_cost",
    "show_cnt",
    "click_cnt",
    "ctr",
    "pay_order_count",
    "pay_order_amount",
    "prepay_and_pay_order_roi",
)
# The plan-list endpoint and the aggregate report endpoint expose different
# field contracts.  In particular, qianchuan/uni_promotion/list rejects
# show_cnt and click_cnt with code 40000.  Keep its field set to the metrics
# the platform explicitly reports as supported instead of sharing the broader
# report field tuple.
UNI_PROMOTION_LIST_FIELDS = (
    "stat_cost",
    "total_cost_per_pay_order_for_roi2",
    "total_pay_order_count_for_roi2",
    "total_pay_order_gmv_for_roi2",
    "total_prepay_and_pay_order_roi2",
)
UNI_PROMOTION_REPORT_FIELDS = (
    "stat_cost",
    "show_cnt",
    "click_cnt",
    "total_cost_per_pay_order_for_roi2",
    "total_pay_order_count_for_roi2",
    "total_pay_order_gmv_for_roi2",
    "total_prepay_and_pay_order_roi2",
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


def _number(value: Any) -> float | None:
    """Return one finite non-negative API metric without coercing text labels."""

    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        normalized = value.strip()
        if not re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", normalized):
            return None
        result = float(normalized)
    elif isinstance(value, (int, float)):
        result = float(value)
    else:
        return None
    if result < 0 or not math.isfinite(result):
        return None
    return result


def _nonnegative_integer(value: Any) -> int:
    """Parse one official integer without accepting booleans or truncating floats."""

    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise ValueError("not an integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        parsed = int(value.strip())
    else:
        raise ValueError("not an integer")
    if parsed < 0 or parsed > 1_000_000:
        raise ValueError("integer is outside the supported range")
    return parsed


def _first_number(source: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _number(source.get(key))
        if value is not None:
            return value
    return None


def _ratio_contract(
    numerator: float | None,
    denominator: float | None,
    *,
    numerator_field: str,
    denominator_field: str,
    scale: float = 1.0,
    unit: str = "ratio",
) -> dict[str, Any]:
    if numerator is None or denominator is None:
        reason = "missing_numerator" if numerator is None else "missing_denominator"
        return {
            "status": "unavailable",
            "value": None,
            "reason": reason,
            "numerator_field": numerator_field,
            "denominator_field": denominator_field,
            "unit": unit,
        }
    if denominator <= 0:
        return {
            "status": "unavailable",
            "value": None,
            "reason": "zero_denominator",
            "numerator_field": numerator_field,
            "numerator_value": numerator,
            "denominator_field": denominator_field,
            "denominator_value": denominator,
            "unit": unit,
        }
    return {
        "status": "available",
        "value": round(numerator / denominator * scale, 6),
        "reason": "recomputed_from_additive_metrics",
        "numerator_field": numerator_field,
        "numerator_value": numerator,
        "denominator_field": denominator_field,
        "denominator_value": denominator,
        "unit": unit,
    }


def _metric_contract(source: Any) -> dict[str, Any]:
    """Build derived metrics only from auditable additive numerators/denominators."""

    raw = source if isinstance(source, dict) else {}
    spend = _first_number(raw, "stat_cost")
    revenue = _first_number(raw, "total_pay_order_gmv_for_roi2", "pay_order_amount")
    impressions = _first_number(raw, "show_cnt")
    clicks = _first_number(raw, "click_cnt")
    orders = _first_number(raw, "total_pay_order_count_for_roi2", "pay_order_count")
    roi = _ratio_contract(
        revenue,
        spend,
        numerator_field="pay_order_amount",
        denominator_field="stat_cost",
    )
    ctr = _ratio_contract(
        clicks,
        impressions,
        numerator_field="click_cnt",
        denominator_field="show_cnt",
        scale=100.0,
        unit="percent",
    )
    return {
        "version": OFFICIAL_METRIC_CONTRACT_VERSION,
        "source": "official_api",
        "additive": {
            "stat_cost": spend,
            "pay_order_amount": revenue,
            "pay_order_count": orders,
            "show_cnt": impressions,
            "click_cnt": clicks,
        },
        "derived": {"roi": roi, "ctr": ctr},
    }


def _first_text(source: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(source.get(key) or "").strip()
        if value:
            return value[:128]
    return ""


def _safe_plan_name(value: Any, *, maximum: int = 120) -> str:
    """Return a bounded, display-only plan name from official API text.

    Plan names are untrusted operator input.  Keep them useful for identifying
    a production target, while removing controls, bidi overrides and markup
    delimiters before the value enters the trusted snapshot/UI path.
    """

    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    normalized = unicodedata.normalize("NFKC", str(value)[: maximum * 8])
    cleaned: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if category.startswith("C"):
            if character in "\t\r\n":
                cleaned.append(" ")
            continue
        if character in "<>":
            cleaned.append(" ")
            continue
        cleaned.append(character)
    return re.sub(r"\s+", " ", "".join(cleaned)).strip()[:maximum]


def _standard_ad_id(source: Any) -> str:
    """Return one classic Qianchuan uint64 plan id without guessing by name."""

    if not isinstance(source, dict):
        return ""
    value = _first_text(source, "ad_id", "id")
    return value if re.fullmatch(r"[1-9]\d*", value) else ""


def _canonical_error_code(value: Any, *, maximum: int = 120) -> str:
    """Normalize a platform/status error code without accepting loose scalars."""

    if isinstance(value, bool):
        return "invalid"
    if isinstance(value, (str, int)):
        normalized = str(value).strip()
        if normalized and len(normalized) <= maximum:
            return normalized
    return "invalid"


def _learning_status_contract(raw_status: Any, *, unavailable: bool = False) -> dict[str, str]:
    """Normalize the API value without copying the SDK's broken LEARNED enum."""

    if unavailable:
        return {
            "learning_status_raw": "",
            "learning_phase": "unavailable",
            "learning_status_label": "暂不可读",
        }
    raw = str(raw_status or "").strip().upper()[:64]
    if not raw:
        return {
            "learning_status_raw": "",
            "learning_phase": "unknown",
            "learning_status_label": "状态未知",
        }
    phase, label = LEARNING_STATUS_CONTRACT.get(raw, ("unknown", "未知状态"))
    return {
        "learning_status_raw": raw,
        "learning_phase": phase,
        "learning_status_label": label,
    }


class OceanEngineAPIError(ValueError):
    def __init__(self, endpoint: str, code: Any, message: str):
        self.endpoint = endpoint
        # Ocean Engine commonly returns numeric platform error codes. Persist a
        # single bounded string representation so a status written by this
        # client is always readable by the browser-safe status validator.
        self.code = _canonical_error_code(code)
        super().__init__(message or f"接口 {endpoint} 返回错误")


def _official_advertiser_ids(data: dict[str, Any], endpoint: str) -> list[str]:
    value: Any = None
    for field_name in ("list", "adv_id_list"):
        if field_name in data:
            value = data[field_name]
            break
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 100:
        raise OceanEngineAPIError(
            endpoint,
            "invalid_response",
            "Official advertiser list is not a bounded JSON array",
        )
    result: list[str] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            raise OceanEngineAPIError(
                endpoint,
                "invalid_response",
                "Official advertiser list contains an invalid account ID",
            )
        advertiser_id = str(item).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", advertiser_id):
            raise OceanEngineAPIError(
                endpoint,
                "invalid_response",
                "Official advertiser list contains an invalid account ID",
            )
        if advertiser_id not in result:
            result.append(advertiser_id)
    return result


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _SYNC_STATUS_WRITE_LOCK:
        handle, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


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
                raw_payload = response.read(MAX_API_RESPONSE_BYTES + 1)
                if len(raw_payload) > MAX_API_RESPONSE_BYTES:
                    raise OceanEngineAPIError(
                        endpoint,
                        "response_too_large",
                        "官方接口响应过大，已停止读取。",
                    )
                try:
                    payload = json.loads(
                        raw_payload.decode("utf-8"),
                        parse_float=_parse_finite_json_float,
                        parse_constant=_reject_nonfinite_json_constant,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                    raise OceanEngineAPIError(
                        endpoint,
                        "invalid",
                        "官方接口返回格式异常。",
                    ) from error
        except HTTPError as error:
            status_code = error.code
            error.close()
            raise OceanEngineAPIError(endpoint, status_code, "官方接口暂时不可用。") from error
        except (URLError, OSError) as error:
            raise OceanEngineAPIError(endpoint, "network", "连接巨量引擎失败，请稍后重试。") from error
        if not isinstance(payload, dict):
            raise OceanEngineAPIError(endpoint, "invalid", "官方接口返回格式异常。")
        code = payload.get("code", 0)
        if isinstance(code, bool):
            raise OceanEngineAPIError(endpoint, "invalid", "官方接口返回格式异常。")
        if isinstance(code, int):
            code_value = code
        elif isinstance(code, str) and re.fullmatch(r"[+-]?\d+", code.strip()):
            code_value = int(code.strip())
        else:
            raise OceanEngineAPIError(endpoint, "invalid", "官方接口返回格式异常。")
        if code_value != 0:
            raise OceanEngineAPIError(endpoint, code, str(payload.get("message") or "官方接口拒绝请求。"))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise OceanEngineAPIError(endpoint, "invalid", "官方接口返回格式异常。")
        return data

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
            if not isinstance(values, list):
                raise OceanEngineAPIError(endpoint, "invalid", "官方接口列表格式异常。")
            if len(values) > page_size or any(not isinstance(item, dict) for item in values):
                raise OceanEngineAPIError(endpoint, "invalid", "官方接口列表格式异常。")
            records.extend(values)
            info = data.get("page_info") if isinstance(data.get("page_info"), dict) else {}
            try:
                total_page = _nonnegative_integer(info.get("total_page"))
            except ValueError as error:
                raise OceanEngineAPIError(
                    endpoint,
                    "invalid",
                    "官方接口分页信息格式异常。",
                ) from error
            if total_page > max_pages:
                raise OceanEngineAPIError(
                    endpoint,
                    "pagination_truncated",
                    f"官方接口数据超过安全分页上限（{max_pages} 页），本次结果未用于经营判断。",
                )
            if not values or (total_page and page >= total_page) or len(values) < page_size:
                return records
        raise OceanEngineAPIError(
            endpoint,
            "pagination_truncated",
            f"官方接口在安全分页上限（{max_pages} 页）后仍有数据，本次结果未用于经营判断。",
        )

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

    def _learning_statuses(
        self,
        token: str,
        advertiser_id: str,
        ad_ids: list[str],
    ) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
        """Read classic-plan learning states in API-sized batches.

        A failed batch remains unavailable. A successful response that omits
        one requested id remains unknown. Neither state is coerced to a
        healthy/default value.
        """

        requested = list(dict.fromkeys(ad_id for ad_id in ad_ids if re.fullmatch(r"[1-9]\d*", ad_id)))
        results: dict[str, dict[str, str]] = {}
        failed_batches = 0
        first_error: OceanEngineAPIError | None = None
        for offset in range(0, len(requested), 50):
            batch = requested[offset:offset + 50]
            try:
                data = self._get(
                    token,
                    "v1.0/qianchuan/ad/learning_status/get/",
                    {"advertiser_id": advertiser_id, "ad_ids": [int(value) for value in batch]},
                )
            except OceanEngineAPIError as error:
                failed_batches += 1
                first_error = first_error or error
                for ad_id in batch:
                    results[ad_id] = _learning_status_contract(None, unavailable=True)
                continue

            # A successful batch can still omit individual ids. Preserve that
            # distinction as unknown instead of inventing DEFAULT/LEARNED.
            for ad_id in batch:
                results[ad_id] = _learning_status_contract(None)
            values: list[Any] = []
            for key in ("list", "learning_status_list", "ad_list"):
                if isinstance(data.get(key), list):
                    values = data[key]
                    break
            for item in values:
                if not isinstance(item, dict):
                    continue
                ad_id = _standard_ad_id(item)
                if ad_id not in results or ad_id not in batch:
                    continue
                raw_status = _first_text(item, "status", "learning_status")
                results[ad_id] = _learning_status_contract(raw_status)

        available_count = sum(
            item.get("learning_phase") not in {"unknown", "unavailable"}
            for item in results.values()
        )
        status: dict[str, Any] = {
            "name": "经典计划学习期状态",
            "ok": failed_batches == 0,
            "count": available_count,
            "requested_count": len(requested),
            "failed_batch_count": failed_batches,
            "message": "部分批次暂不可读" if failed_batches else "读取成功",
        }
        if first_error is not None:
            status["code"] = first_error.code
            status["message"] = str(first_error)
        return results, status

    def _low_efficiency_ids(
        self,
        token: str,
        advertiser_id: str,
    ) -> tuple[set[str] | None, dict[str, Any]]:
        """Read the platform's current low-efficiency classic-plan set once."""

        try:
            data = self._get(
                token,
                "v1.0/qianchuan/lq_ad/get/",
                {"advertiser_id": advertiser_id, "filtering": {"marketing_scene": "ALL"}},
            )
        except OceanEngineAPIError as error:
            return None, {
                "name": "经典计划平台低效标记",
                "ok": False,
                "count": 0,
                "code": error.code,
                "message": str(error),
            }

        values: list[Any] = []
        list_contract_found = False
        for key in ("ad_ids", "ids", "list"):
            if isinstance(data.get(key), list):
                values = data[key]
                list_contract_found = True
                break
        if not list_contract_found:
            return None, {
                "name": "经典计划平台低效标记",
                "ok": False,
                "count": 0,
                "code": "invalid",
                "message": "官方接口低效计划列表格式异常。",
            }
        ad_ids: set[str] = set()
        invalid_item = False
        for item in values:
            if isinstance(item, dict):
                ad_id = _standard_ad_id(item)
            else:
                value = str(item or "").strip()
                ad_id = value if re.fullmatch(r"[1-9]\d*", value) else ""
            if ad_id:
                ad_ids.add(ad_id)
            else:
                invalid_item = True
        if invalid_item:
            return None, {
                "name": "经典计划平台低效标记",
                "ok": False,
                "count": 0,
                "code": "invalid",
                "message": "官方接口低效计划标识格式异常。",
            }
        return ad_ids, {
            "name": "经典计划平台低效标记",
            "ok": True,
            "count": len(ad_ids),
            "message": "读取成功",
        }

    def _enrich_standard_plan_health(
        self,
        token: str,
        advertiser_id: str,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Attach diagnostic-only health fields to classic plan rows."""

        plan_ids = list(dict.fromkeys(filter(None, (_standard_ad_id(row) for row in rows))))
        if not plan_ids:
            return []
        learning, learning_status = self._learning_statuses(token, advertiser_id, plan_ids)
        low_efficiency, low_efficiency_status = self._low_efficiency_ids(token, advertiser_id)
        for row in rows:
            ad_id = _standard_ad_id(row)
            if not ad_id:
                continue
            row.update(learning.get(ad_id, _learning_status_contract(None, unavailable=True)))
            if low_efficiency is None:
                row["platform_low_efficiency"] = "unknown"
                row["platform_low_efficiency_label"] = "暂不可读"
            elif ad_id in low_efficiency:
                row["platform_low_efficiency"] = "flagged"
                row["platform_low_efficiency_label"] = "平台标记低效"
            else:
                row["platform_low_efficiency"] = "not_flagged_currently"
                row["platform_low_efficiency_label"] = "本次未命中"
            row["diagnostic_source"] = "official_api"
            if row.get("learning_phase") == "unavailable" or row["platform_low_efficiency"] == "unknown":
                row["diagnostic_source_label"] = "官方 API（部分不可用）"
            elif row.get("learning_phase") == "unknown":
                row["diagnostic_source_label"] = "官方 API（状态未知）"
            else:
                row["diagnostic_source_label"] = "官方 API"
        return [learning_status, low_efficiency_status]

    def _control_tasks(
        self,
        token: str,
        advertiser_id: str,
        ad_id: str,
        marketing_goal: str,
        start_time: str,
        end_time: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Read control tasks for one exact parent plan without any write fallback."""

        tasks: list[dict[str, Any]] = []
        statuses: list[dict[str, Any]] = []
        for scene in CONTROL_TASK_SCENES:
            try:
                normalized: list[dict[str, Any]] = []
                for page in range(1, 11):
                    data = self._get(
                        token,
                        "v1.0/qianchuan/uni_promotion/ad/control_task/list/",
                        {
                            "advertiser_id": advertiser_id,
                            "marketing_goal": marketing_goal,
                            "ad_id": ad_id,
                            "start_time": start_time,
                            "end_time": end_time,
                            "scene": scene,
                            "page": page,
                            "page_size": 100,
                        },
                    )
                    values: list[Any] = []
                    list_contract_found = False
                    for key in ("list", "task_list", "control_task_list", "tasks"):
                        if isinstance(data.get(key), list):
                            values = data[key]
                            list_contract_found = True
                            break
                    if (
                        not list_contract_found
                        or len(values) > 100
                        or any(not isinstance(item, dict) for item in values)
                    ):
                        raise OceanEngineAPIError(
                            "v1.0/qianchuan/uni_promotion/ad/control_task/list/",
                            "invalid",
                            "官方调控任务列表格式异常。",
                        )
                    for item in values:
                        task_id = _first_text(item, "task_id", "id", "control_task_id")
                        normalized.append({**item, "task_id": task_id, "scene": _first_text(item, "scene") or scene})
                    page_info = data.get("page_info") if isinstance(data.get("page_info"), dict) else {}
                    try:
                        total_page = _nonnegative_integer(page_info.get("total_page"))
                    except ValueError as error:
                        raise OceanEngineAPIError(
                            "v1.0/qianchuan/uni_promotion/ad/control_task/list/",
                            "invalid",
                            "官方调控任务分页信息格式异常。",
                        ) from error
                    if total_page > 10:
                        raise OceanEngineAPIError(
                            "v1.0/qianchuan/uni_promotion/ad/control_task/list/",
                            "pagination_truncated",
                            "官方调控任务超过安全分页上限，本次任务结果未用于经营判断。",
                        )
                    if not values or len(values) < 100 or (total_page and page >= total_page):
                        break
                    if page == 10:
                        raise OceanEngineAPIError(
                            "v1.0/qianchuan/uni_promotion/ad/control_task/list/",
                            "pagination_truncated",
                            "官方调控任务在安全分页上限后仍有数据，本次任务结果未用于经营判断。",
                        )
                tasks.extend(normalized)
                statuses.append({
                    "name": f"乘方调控任务/{scene}",
                    "ok": True,
                    "count": len(normalized),
                    "truncated": False,
                    "message": "读取成功",
                })
            except OceanEngineAPIError as error:
                statuses.append({
                    "name": f"乘方调控任务/{scene}",
                    "ok": False,
                    "count": 0,
                    "code": error.code,
                    "message": str(error),
                })
        deduplicated: dict[str, dict[str, Any]] = {}
        anonymous: list[dict[str, Any]] = []
        for item in tasks:
            task_id = str(item.get("task_id") or "")
            if task_id:
                deduplicated[task_id] = item
            else:
                anonymous.append(item)
        return [*deduplicated.values(), *anonymous], statuses

    def sync(
        self,
        save_snapshot: Callable[..., dict[str, Any]],
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
        decision_date = date.today()
        decision_start_time = f"{decision_date.isoformat()} 00:00:00"
        decision_end_time = f"{decision_date.isoformat()} 23:59:59"
        statuses: list[dict[str, Any]] = []
        resolved: dict[str, list[str]] = {}
        saved_pages = 0

        for account in accounts:
            shop_id = str(account.get("account_id") or "")
            account_public = {
                "key": _safe_account_key(shop_id),
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
                advertiser_ids = _official_advertiser_ids(
                    advertiser_data,
                    "v1.0/qianchuan/shop/advertiser/list/",
                )
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
            decision_records: list[dict[str, Any]] = []
            materials: list[dict[str, Any]] = []
            videos: list[dict[str, Any]] = []
            for advertiser_id in advertiser_ids:
                standard_plan_rows: list[dict[str, Any]] = []
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
                        row.setdefault("advertiser_id", advertiser_id)
                        row["_plan_source_family"] = "standard_ad"
                    standard_plan_rows.extend(rows)
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
                    has_standard_report = bool(rows)
                    endpoint_status.append(status)
                    rows, status = self._endpoint_result(
                        f"{'直播' if goal.startswith('LIVE') else '商品'}全域推广",
                        lambda aid=advertiser_id, g=goal: self._paged(
                            token,
                            "v1.0/qianchuan/uni_promotion/list/",
                            {
                                "advertiser_id": aid,
                                "start_time": decision_start_time,
                                "end_time": decision_end_time,
                                "marketing_goal": g,
                                "fields": UNI_PROMOTION_LIST_FIELDS,
                                "filtering": {},
                            },
                            page_size=100,
                            list_key="ad_list",
                        ),
                    )
                    for row in rows:
                        info = row.get("ad_info") if isinstance(row.get("ad_info"), dict) else {}
                        stats = row.get("stats_info") if isinstance(row.get("stats_info"), dict) else {}
                        ad_id = _first_text(info, "ad_id", "id") or _first_text(row, "ad_id", "id")
                        marketing_goal = _first_text(info, "marketing_goal") or goal
                        plan_budget = _first_number(info, "budget", "total_budget")
                        plan_name = _safe_plan_name(info.get("name"))
                        plans.append(
                            {
                                "shop_id": shop_id,
                                "advertiser_id": advertiser_id,
                                "ad_id": ad_id,
                                "ad_name": plan_name,
                                "status": info.get("status"),
                                "_marketing_goal": marketing_goal,
                                "budget": plan_budget,
                                "bid": info.get("roi2_goal"),
                                "create_time": info.get("create_time"),
                                **stats,
                            }
                        )
                        tasks: list[dict[str, Any]] = []
                        if ad_id:
                            tasks, task_statuses = self._control_tasks(
                                token,
                                advertiser_id,
                                ad_id,
                                marketing_goal,
                                f"{start_date.isoformat()} 00:00:00",
                                decision_end_time,
                            )
                            endpoint_status.extend(task_statuses)
                        task_rows = tasks or [{}]
                        metric_contract = _metric_contract(stats)
                        for task in task_rows:
                            task_id = _first_text(task, "task_id", "id", "control_task_id")
                            task_budget = _first_number(task, "budget", "total_budget")
                            additive = metric_contract["additive"]
                            derived = metric_contract["derived"]
                            decision_records.append({
                                "schema_version": 1,
                                "shop_id": shop_id,
                                "advertiser_id": advertiser_id,
                                "ad_id": ad_id,
                                "task_id": task_id,
                                "marketing_goal": marketing_goal,
                                "scene": _first_text(task, "scene"),
                                "task_status": _first_text(task, "status", "task_status"),
                                "plan_name": plan_name,
                                "captured_at_ms": int(time.time() * 1000),
                                "metric_window_start": decision_start_time,
                                "metric_window_end": decision_end_time,
                                "metric_scope": "parent_ad",
                                "metric_contract_version": OFFICIAL_METRIC_CONTRACT_VERSION,
                                "metric_contract": metric_contract,
                                # Keep the historical decision field for the A1
                                # evidence model, but never let a control-task
                                # budget masquerade as the parent plan value at
                                # the production write boundary.
                                "current_total_budget": task_budget if task_budget is not None else plan_budget,
                                "parent_plan_budget": plan_budget,
                                "control_task_budget": task_budget,
                                "today_spend": additive["stat_cost"],
                                "attributed_orders": additive["pay_order_count"],
                                "pay_order_amount": additive["pay_order_amount"],
                                "actual_roi": derived["roi"]["value"],
                                "actual_roi_status": derived["roi"]["status"],
                                "actual_roi_unavailable_reason": derived["roi"].get("reason") if derived["roi"]["status"] != "available" else "",
                                "show_cnt": additive["show_cnt"],
                                "click_cnt": additive["click_cnt"],
                                "ctr": derived["ctr"]["value"],
                                "ctr_status": derived["ctr"]["status"],
                                "ctr_unavailable_reason": derived["ctr"].get("reason") if derived["ctr"]["status"] != "available" else "",
                                "production_identifier_complete": bool(shop_id and advertiser_id and ad_id and task_id and marketing_goal),
                                "platform_write_enabled": False,
                            })
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
                                "fields": UNI_PROMOTION_REPORT_FIELDS,
                            },
                        )
                        uni_report["_marketing_goal"] = goal
                        # Both report families may cover the same traffic. Use
                        # the uni-promotion response only as a fallback so the
                        # overview never double-counts spend, orders or GMV.
                        if not has_standard_report:
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
                endpoint_status.extend(
                    self._enrich_standard_plan_health(
                        token,
                        advertiser_id,
                        standard_plan_rows,
                    )
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
                "identity_claims": [
                    {"kind": "douyin_shop_id", "raw_id": shop_id, "evidence_source": "official_api", "confidence": "high"},
                    {"kind": "qianchuan_account_id", "raw_id": shop_id, "evidence_source": "official_api", "confidence": "high"},
                ],
                "identity_status": "resolved_by_bridge",
                "date_range": f"{start_date.isoformat()} 至 {end_date.isoformat()}",
            }

            # Only additive metrics may be summed. CTR and ROI are ratios and
            # are always recomputed from their auditable numerator/denominator.
            aggregate: dict[str, float | None] = {
                "stat_cost": None,
                "show_cnt": None,
                "click_cnt": None,
                "pay_order_count": None,
                "pay_order_amount": None,
            }
            for row in reports:
                metric_source = row.get("metrics") if isinstance(row.get("metrics"), dict) else row
                aliases = {
                    "stat_cost": ("stat_cost",),
                    "show_cnt": ("show_cnt",),
                    "click_cnt": ("click_cnt",),
                    "pay_order_count": ("total_pay_order_count_for_roi2", "pay_order_count"),
                    "pay_order_amount": ("total_pay_order_gmv_for_roi2", "pay_order_amount"),
                }
                for target_key, source_keys in aliases.items():
                    value = _first_number(metric_source, *source_keys)
                    if value is not None:
                        aggregate[target_key] = float(aggregate[target_key] or 0) + value
            aggregate_metric_contract = _metric_contract(aggregate)
            roi_contract = aggregate_metric_contract["derived"]["roi"]
            ctr_contract = aggregate_metric_contract["derived"]["ctr"]
            safe_metrics = {
                "广告消耗": _stringify(aggregate["stat_cost"]) if aggregate["stat_cost"] is not None else "unavailable",
                "展示次数": _stringify(aggregate["show_cnt"]) if aggregate["show_cnt"] is not None else "unavailable",
                "点击次数": _stringify(aggregate["click_cnt"]) if aggregate["click_cnt"] is not None else "unavailable",
                "成交订单数": _stringify(aggregate["pay_order_count"]) if aggregate["pay_order_count"] is not None else "unavailable",
                "成交金额": _stringify(aggregate["pay_order_amount"]) if aggregate["pay_order_amount"] is not None else "unavailable",
                "支付ROI": _stringify(roi_contract["value"]) if roi_contract["status"] == "available" else "unavailable",
                "点击率": _stringify(ctr_contract["value"]) if ctr_contract["status"] == "available" else "unavailable",
            }
            snapshots = [
                {
                    **common,
                    "page_type": "overview",
                    "title": "千川官方 API 经营概览",
                    "metrics": safe_metrics,
                    "safe_metrics": safe_metrics,
                    "metric_contract": aggregate_metric_contract,
                    "metric_contract_version": OFFICIAL_METRIC_CONTRACT_VERSION,
                    "signals": [],
                    "tables": [],
                },
                {
                    **common,
                    "page_type": "plans",
                    "title": "千川官方 API 投放计划",
                    "metrics": {"计划数": len(plans)},
                    "safe_metrics": {"计划数": len(plans)},
                    "decision_records": decision_records,
                    "metric_contract_version": OFFICIAL_METRIC_CONTRACT_VERSION,
                    "platform_write_enabled": False,
                    "signals": [],
                    "tables": [_table(plans, (
                         ("advertiser_id", "广告主ID"), ("ad_id", "计划ID"),
                         ("ad_name", "计划名称"), ("status", "状态"),
                         ("learning_status_label", "学习期状态"),
                         ("platform_low_efficiency_label", "平台低效"),
                         ("diagnostic_source_label", "诊断来源"),
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
            pagination_truncated = any(
                str(item.get("code") or "") == "pagination_truncated"
                or item.get("truncated") is True
                for item in endpoint_status
                if isinstance(item, dict)
            )
            failed_endpoint_count = sum(
                item.get("ok") is not True
                for item in endpoint_status
                if isinstance(item, dict)
            )
            for snapshot in snapshots:
                row_count = sum(len(table.get("rows") or []) for table in snapshot["tables"])
                exact_records = snapshot.get("decision_records") if isinstance(snapshot.get("decision_records"), list) else []
                incomplete_identifiers = sum(
                    item.get("production_identifier_complete") is not True
                    for item in exact_records if isinstance(item, dict)
                )
                unavailable_roi = sum(
                    item.get("actual_roi_status") != "available"
                    for item in exact_records if isinstance(item, dict)
                )
                warnings = [] if advertiser_ids else ["该店铺暂未关联千川广告账户"]
                if incomplete_identifiers:
                    warnings.append(f"{incomplete_identifiers} 条乘方决策记录缺少官方任务标识，仅可用于只读诊断")
                if unavailable_roi:
                    warnings.append(f"{unavailable_roi} 条乘方决策记录缺少 ROI 分子或分母，ROI 标记为 unavailable")
                if pagination_truncated:
                    warnings.append("官方接口数据超过安全分页上限，本轮快照不参与自动经营判断")
                if failed_endpoint_count:
                    warnings.append(
                        f"{failed_endpoint_count} 个官方接口读取失败，本轮快照不完整"
                    )
                quality_score = 100 if advertiser_ids else 70
                if exact_records and incomplete_identifiers:
                    quality_score = min(quality_score, 80)
                if exact_records and unavailable_roi:
                    quality_score = min(quality_score, 70)
                if pagination_truncated:
                    quality_score = min(quality_score, 40)
                if failed_endpoint_count:
                    quality_score = min(quality_score, 40)
                snapshot["quality"] = {
                    "score": quality_score,
                    "metric_count": len(snapshot["safe_metrics"]),
                    "table_count": len(snapshot["tables"]),
                    "row_count": row_count,
                    "decision_record_count": len(exact_records),
                    "incomplete_identifier_count": incomplete_identifiers,
                    "unavailable_roi_count": unavailable_roi,
                    "warnings": warnings,
                    "pages_scanned": 1,
                    "virtual_scroll_passes": 0,
                    "pagination_truncated": pagination_truncated,
                    "collection_complete": not pagination_truncated
                    and failed_endpoint_count == 0,
                }
                saved = save_snapshot("qianchuan", snapshot, trusted_origin="official_api_oauth_client")
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
                    "account_name": f"匿名店铺 {canonical_account_key[-6:].upper()}",
                    "advertiser_count": len(advertiser_ids),
                    "pages_saved": 4,
                    "pagination_truncated": pagination_truncated,
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
            "pagination_truncated": any(
                account.get("pagination_truncated") is True for account in statuses
            ),
            "data_complete": failures == 0 and not any(
                account.get("pagination_truncated") is True for account in statuses
            ),
            "accounts": statuses,
            "fallback": "browser_snapshot",
        }
        _atomic_json(self.oauth.data_dir / SYNC_STATUS_FILE, result)
        return result


def _empty_sync_status() -> dict[str, Any]:
    return {
        "ok": True,
        "mode": "read_only",
        "synced_at": None,
        "account_count": 0,
        "saved_pages": 0,
        "failure_count": 0,
        "pagination_truncated": False,
        "data_complete": False,
        "accounts": [],
        "fallback": "browser_snapshot",
    }


def _corrupt_sync_status() -> dict[str, Any]:
    return {
        **_empty_sync_status(),
        "ok": False,
        "status": "corrupt",
        "corruption_detected": True,
        "evidence_preserved": True,
        "recovery_required": True,
        "error": {
            "code": "SYNC_STATUS_CORRUPT",
            "message": "The saved synchronization status is invalid. Run a new read-only sync to recover.",
        },
    }


def _sync_status_integer(value: Any, field_name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise ValueError(f"invalid {field_name}")
    return value


def _sync_status_text(value: Any, field_name: str, maximum: int, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError(f"invalid {field_name}")
    return value


def _validated_sync_status(value: Any) -> dict[str, Any]:
    """Return a bounded browser-safe status, dropping all unknown fields."""

    if not isinstance(value, dict):
        raise ValueError("invalid sync status")
    if value.get("ok", True) is not True:
        raise ValueError("invalid sync status.ok")
    if value.get("mode", "read_only") != "read_only":
        raise ValueError("invalid sync status.mode")
    if value.get("fallback", "browser_snapshot") != "browser_snapshot":
        raise ValueError("invalid sync status.fallback")

    synced_at_raw = value.get("synced_at")
    synced_at = None
    if synced_at_raw is not None:
        synced_at = _sync_status_integer(synced_at_raw, "sync status.synced_at", 10_000_000_000)

    raw_accounts = value.get("accounts", [])
    if not isinstance(raw_accounts, list) or len(raw_accounts) > 1000:
        raise ValueError("invalid sync status.accounts")
    accounts: list[dict[str, Any]] = []
    account_key_pattern = re.compile(r"^(?:acct_api_[a-f0-9]{12}|adacct_v1_[a-f0-9]{26})$")
    endpoint_integer_fields = ("count", "requested_count", "failed_batch_count")
    for raw_account in raw_accounts:
        if not isinstance(raw_account, dict):
            raise ValueError("invalid sync status account")
        account_key = _sync_status_text(
            raw_account.get("account_key"), "sync status account.account_key", 64, required=True
        )
        if not account_key_pattern.fullmatch(account_key):
            raise ValueError("invalid sync status account.account_key")
        account_name = _sync_status_text(
            raw_account.get("account_name", ""), "sync status account.account_name", 128
        )
        advertiser_count = _sync_status_integer(
            raw_account.get("advertiser_count", 0), "sync status account.advertiser_count", 1_000_000
        )
        pages_saved = _sync_status_integer(
            raw_account.get("pages_saved", 0), "sync status account.pages_saved", 1_000_000
        )
        pagination_truncated = raw_account.get("pagination_truncated", False)
        if not isinstance(pagination_truncated, bool):
            raise ValueError("invalid sync status account.pagination_truncated")
        raw_endpoints = raw_account.get("endpoints", [])
        if not isinstance(raw_endpoints, list) or len(raw_endpoints) > 10_000:
            raise ValueError("invalid sync status account.endpoints")
        endpoints: list[dict[str, Any]] = []
        for raw_endpoint in raw_endpoints:
            if not isinstance(raw_endpoint, dict):
                raise ValueError("invalid sync status endpoint")
            endpoint = {
                "name": _sync_status_text(
                    raw_endpoint.get("name"), "sync status endpoint.name", 160, required=True
                ),
                "ok": raw_endpoint.get("ok"),
            }
            if not isinstance(endpoint["ok"], bool):
                raise ValueError("invalid sync status endpoint.ok")
            for field_name in endpoint_integer_fields:
                if field_name in raw_endpoint:
                    endpoint[field_name] = _sync_status_integer(
                        raw_endpoint[field_name], f"sync status endpoint.{field_name}", 10_000_000
                    )
            if "code" in raw_endpoint:
                raw_code = raw_endpoint["code"]
                # v4.14.3 could persist the platform's integer code directly.
                # Accept only the exact legacy scalar shape, migrate it in
                # memory to a bounded string, and keep rejecting bool/float or
                # arbitrary objects so corrupt evidence still fails closed.
                if isinstance(raw_code, bool) or not isinstance(raw_code, (str, int)):
                    raise ValueError("invalid sync status endpoint.code")
                endpoint["code"] = _sync_status_text(
                    str(raw_code), "sync status endpoint.code", 120, required=True
                )
            if "message" in raw_endpoint:
                endpoint["message"] = _sync_status_text(
                    raw_endpoint["message"], "sync status endpoint.message", 1000
                )
            if "truncated" in raw_endpoint:
                if not isinstance(raw_endpoint["truncated"], bool):
                    raise ValueError("invalid sync status endpoint.truncated")
                endpoint["truncated"] = raw_endpoint["truncated"]
            endpoints.append(endpoint)
        accounts.append({
            "account_key": account_key,
            "account_name": account_name,
            "advertiser_count": advertiser_count,
            "pages_saved": pages_saved,
            "pagination_truncated": pagination_truncated,
            "endpoints": endpoints,
        })

    account_count = _sync_status_integer(
        value.get("account_count", len(accounts)), "sync status.account_count", 1000
    )
    if account_count != len(accounts):
        raise ValueError("invalid sync status.account_count")
    saved_pages = _sync_status_integer(
        value.get("saved_pages", 0), "sync status.saved_pages", 1_000_000
    )
    failure_count = _sync_status_integer(
        value.get("failure_count", 0), "sync status.failure_count", 10_000_000
    )
    pagination_truncated = value.get(
        "pagination_truncated",
        any(account["pagination_truncated"] for account in accounts),
    )
    data_complete = value.get(
        "data_complete",
        failure_count == 0 and not pagination_truncated and synced_at is not None,
    )
    if not isinstance(pagination_truncated, bool) or not isinstance(data_complete, bool):
        raise ValueError("invalid sync status completeness")

    normalized = {
        "ok": True,
        "mode": "read_only",
        "synced_at": synced_at,
        "account_count": account_count,
        "saved_pages": saved_pages,
        "failure_count": failure_count,
        "pagination_truncated": pagination_truncated,
        "data_complete": data_complete,
        "accounts": accounts,
        "fallback": "browser_snapshot",
    }
    if "date_range" in value:
        normalized["date_range"] = _sync_status_text(
            value["date_range"], "sync status.date_range", 96
        )
    return normalized


def load_sync_status(data_dir: Path) -> dict[str, Any]:
    path = Path(data_dir) / SYNC_STATUS_FILE
    if not path.exists():
        return _empty_sync_status()
    if path.is_symlink() or not path.is_file():
        return _corrupt_sync_status()
    try:
        return _validated_sync_status(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError):
        # A read endpoint must remain available, but the evidence file is not
        # renamed, rewritten or deleted. A later explicit sync may replace it.
        return _corrupt_sync_status()
