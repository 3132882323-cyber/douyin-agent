"""店策 Agent 本地 companion service。

接收 Chrome 扩展提交的脱敏页面快照，按平台和页面类型原子保存，
并提供健康状态、数据目录和确定性经营诊断。仅监听 127.0.0.1。
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

from action_protocol import assess_automation_readiness, build_action_draft, transition_action, validate_action_draft
from oceanengine_data import OceanEngineDataClient, load_sync_status
from oceanengine_oauth import OceanEngineOAuth

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(message)s")
logger = logging.getLogger("dian-agent-http")

import state
BASE_DIR = state.BASE_DIR
DATA_DIR = state.DATA_DIR
PORT = state.PORT
MAX_BODY_BYTES = state.MAX_BODY_BYTES
ALLOWED_SOURCES = state.ALLOWED_SOURCES
SAFE_KEY = re.compile(r"^[a-z0-9_-]{1,48}$")
STALE_SECONDS = state.STALE_SECONDS


def set_data_dir(path: Path) -> Path:
    """Keep facade DATA_DIR and shared state.DATA_DIR in sync for tests."""
    global DATA_DIR
    DATA_DIR = state.set_data_dir(path)
    return DATA_DIR
REPORT_TEMPLATE_KEYS = {"default", "brief", "handover", "custom"}
DEFAULT_CUSTOM_REPORT_TEMPLATE = """# 店策 Agent 经营日志 - {{date}}

## 今日结论
{{headline}}
{{summary}}

## 今日重点
{{top_tasks}}

## 千川计划
{{plans}}

## 库存风险
{{inventory}}

## 数据状态
{{scan_status}}
"""
DEFAULT_AGENT_SETTINGS = {
    "roi_target": 1.5,
    "min_spend_for_action": 100.0,
    "low_inventory_threshold": 10,
    "critical_inventory_threshold": 3,
    "inventory_days_warning": 3.0,
    "daily_report_enabled": True,
    "daily_report_time": "09:00",
    "report_retention_days": 30,
    "report_template": "default",
    "custom_report_template": DEFAULT_CUSTOM_REPORT_TEMPLATE,
    "history_retention_days": 30,
    "qianchuan_account_key": "",
    "max_daily_execution_count": 3,
    "max_daily_budget_reduction": 300.0,
    "execution_cooldown_minutes": 30,
    "execution_mode": "observe",
}

_state_lock = state._state_lock
_analysis_cache = state._analysis_cache
_CACHE_TTL_SECONDS = state._CACHE_TTL_SECONDS


def _cached(key: str, builder):
    """Return cached result if fresh, else rebuild and cache."""
    now = time.time()
    entry = _analysis_cache.get(key)
    if entry and (now - entry[0]) < _CACHE_TTL_SECONDS:
        return entry[1]
    result = builder()
    _analysis_cache[key] = (now, result)
    return result


def _invalidate_cache() -> None:
    """Clear all cached analysis results (call after data changes)."""
    _analysis_cache.clear()


def _bridge_token_path() -> Path:
    return DATA_DIR / "bridge_token.txt"


def ensure_bridge_token() -> str:
    """Load or create the local companion write-token."""
    path = _bridge_token_path()
    try:
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
            if re.fullmatch(r"[A-Za-z0-9_-]{24,128}", token):
                return token
    except OSError:
        logger.exception("读取 bridge_token 失败: %s", path)
    token = secrets.token_urlsafe(32)
    try:
        path.write_text(token, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except OSError:
        logger.exception("写入 bridge_token 失败: %s", path)
        raise
    logger.info("已生成本地 bridge_token（仅本机扩展写入接口使用）")
    return token


def load_bridge_token() -> str:
    return ensure_bridge_token()


def _schema_version_check() -> list[str]:
    """Check if any on-disk snapshots have outdated schema versions."""
    warnings: list[str] = []
    current_version = 2
    for source in ALLOWED_SOURCES:
        source_dir = DATA_DIR / source
        if not source_dir.exists():
            continue
        for path in source_dir.glob("*.json"):
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    inner = data.get("data", {})
                    sv = int(inner.get("schema_version", 1) if isinstance(inner, dict) else 1)
                    if sv < current_version:
                        warnings.append(f"{source}/{path.stem} (schema v{sv})")
            except (OSError, json.JSONDecodeError, ValueError):
                continue
    return warnings


def _disk_usage_check() -> dict[str, Any]:
    """Check free disk space on the data directory's filesystem."""
    import shutil
    try:
        usage = shutil.disk_usage(str(DATA_DIR))
        free_mb = round(usage.free / (1024 * 1024), 1)
        total_mb = round(usage.total / (1024 * 1024), 1)
        return {
            "free_mb": free_mb,
            "total_mb": total_mb,
            "used_percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0,
            "warning": free_mb < 100,  # warn if less than 100MB free
        }
    except OSError:
        return {"free_mb": -1, "total_mb": 0, "used_percent": 0, "warning": False, "error": "无法读取磁盘信息"}


from storage import (
    _account_catalog_path,
    _account_snapshot_path,
    _atomic_json_write,
    _canonical_qianchuan_account_key,
    _history_dir,
    _is_valid_qianchuan_account_label,
    _normalized_account_label,
    _now_label,
    _remember_qianchuan_account,
    _safe_page_type,
    _save_history_point,
    _snapshot_path,
    build_store_catalog,
    list_qianchuan_accounts,
    list_snapshots,
    load_data,
    load_history,
    save_data,
)

from insights import (
    _action_params_for_plan,
    _age_label,
    _clean_entity_name,
    _entity_identifier,
    _evidence_value,
    _extract_labeled_number,
    _metric_matches,
    _parse_number,
    _pick,
    _plan_workbench_fields,
    _safe_snapshot_metrics,
    _table_records,
    _timestamp_seconds,
    build_action_center,
    build_automation_readiness,
    build_insights,
    build_inventory_alerts,
    build_live_analysis,
    build_operation_context,
    build_ops_manager,
    build_plan_recommendations,
    build_qianchuan_creative_analysis,
    build_scan_receipt,
    build_shelf_analysis,
    build_stop_loss_queue,
    build_trends,
)



def _settings_path() -> Path:
    return DATA_DIR / "settings.json"


def load_agent_settings() -> dict[str, Any]:
    settings = dict(DEFAULT_AGENT_SETTINGS)
    path = _settings_path()
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as file:
                saved = json.load(file)
            if isinstance(saved, dict):
                settings.update(saved)
        except (OSError, json.JSONDecodeError):
            logger.exception("读取 Agent 设置失败: %s", path)
    return settings


def save_agent_settings(values: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ValueError("settings must be an object")
    current = load_agent_settings()
    allowed = set(DEFAULT_AGENT_SETTINGS)
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"unknown settings: {', '.join(sorted(unknown))}")

    next_settings = {**current, **values}
    next_settings["roi_target"] = min(20.0, max(0.1, float(next_settings["roi_target"])))
    next_settings["min_spend_for_action"] = min(1_000_000.0, max(0.0, float(next_settings["min_spend_for_action"])))
    next_settings["low_inventory_threshold"] = min(1_000_000, max(0, int(next_settings["low_inventory_threshold"])))
    next_settings["critical_inventory_threshold"] = min(
        next_settings["low_inventory_threshold"],
        max(0, int(next_settings["critical_inventory_threshold"])),
    )
    next_settings["inventory_days_warning"] = min(365.0, max(0.1, float(next_settings["inventory_days_warning"])))
    next_settings["daily_report_enabled"] = bool(next_settings["daily_report_enabled"])
    report_time = str(next_settings["daily_report_time"])
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", report_time):
        raise ValueError("daily_report_time must be HH:MM")
    next_settings["daily_report_time"] = report_time
    next_settings["report_retention_days"] = min(365, max(1, int(next_settings["report_retention_days"])))
    report_template = str(next_settings.get("report_template") or "default")
    if report_template not in REPORT_TEMPLATE_KEYS:
        raise ValueError("report_template must be default, brief, handover or custom")
    next_settings["report_template"] = report_template
    custom_template = str(next_settings.get("custom_report_template") or "").strip()
    if len(custom_template) > 12_000:
        raise ValueError("custom_report_template is too long")
    next_settings["custom_report_template"] = custom_template or DEFAULT_CUSTOM_REPORT_TEMPLATE
    next_settings["history_retention_days"] = min(365, max(1, int(next_settings["history_retention_days"])))
    next_settings["max_daily_execution_count"] = min(50, max(1, int(next_settings["max_daily_execution_count"])))
    next_settings["max_daily_budget_reduction"] = min(
        1_000_000.0,
        max(1.0, float(next_settings["max_daily_budget_reduction"])),
    )
    next_settings["execution_cooldown_minutes"] = min(1440, max(0, int(next_settings["execution_cooldown_minutes"])))
    execution_mode = str(next_settings.get("execution_mode") or "observe")
    if execution_mode not in {"observe", "shadow", "supervised"}:
        raise ValueError("execution_mode must be observe, shadow or supervised")
    next_settings["execution_mode"] = execution_mode
    account_key = str(next_settings.get("qianchuan_account_key") or "").lower()
    if account_key and not SAFE_KEY.fullmatch(account_key):
        raise ValueError("invalid qianchuan_account_key")
    next_settings["qianchuan_account_key"] = account_key
    _atomic_json_write(_settings_path(), next_settings)
    return next_settings


def _integrations_path() -> Path:
    return DATA_DIR / "integrations.json"


def _validate_webhook(platform: str, value: str) -> str:
    webhook = str(value or "").strip()
    if not webhook:
        return ""
    parsed = urlparse(webhook)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Webhook 必须是平台提供的 HTTPS 地址。")
    if platform == "feishu":
        valid = parsed.hostname == "open.feishu.cn" and parsed.path.startswith("/open-apis/bot/v2/hook/")
    elif platform == "dingtalk":
        valid = parsed.hostname == "oapi.dingtalk.com" and parsed.path == "/robot/send" and bool(parse_qs(parsed.query).get("access_token"))
    else:
        raise ValueError("不支持的通知平台。")
    if not valid:
        raise ValueError(f"{platform} Webhook 地址格式不正确。")
    return webhook


def _load_integration_secrets() -> dict[str, Any]:
    values = {"feishu_webhook": "", "dingtalk_webhook": "", "auto_send_reports": False}
    path = _integrations_path()
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                values.update({key: saved.get(key, values[key]) for key in values})
        except (OSError, json.JSONDecodeError):
            logger.exception("读取通知连接设置失败")
    return values


def get_integration_settings() -> dict[str, Any]:
    values = _load_integration_secrets()
    return {
        "feishu": {"configured": bool(values["feishu_webhook"]), "label": "已连接" if values["feishu_webhook"] else "未连接"},
        "dingtalk": {"configured": bool(values["dingtalk_webhook"]), "label": "已连接" if values["dingtalk_webhook"] else "未连接"},
        "auto_send_reports": bool(values["auto_send_reports"]),
        "secrets_exposed": False,
    }


def save_integration_settings(values: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ValueError("integration settings must be an object")
    allowed = {"feishu_webhook", "dingtalk_webhook", "auto_send_reports"}
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"unknown integration settings: {', '.join(sorted(unknown))}")
    current = _load_integration_secrets()
    for platform in ("feishu", "dingtalk"):
        key = f"{platform}_webhook"
        if key in values:
            current[key] = _validate_webhook(platform, str(values.get(key) or ""))
    if "auto_send_reports" in values:
        current["auto_send_reports"] = bool(values["auto_send_reports"])
    _atomic_json_write(_integrations_path(), current)
    return get_integration_settings()


def _post_json(url: str, payload: dict[str, Any], timeout: float = 8.0) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "DianAgent/2"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL is allowlisted by _validate_webhook
        raw = response.read(64 * 1024).decode("utf-8", errors="replace")
    return json.loads(raw) if raw else {}


def send_notification(platform: str, message: str) -> dict[str, Any]:
    platform = str(platform or "").lower()
    values = _load_integration_secrets()
    key = f"{platform}_webhook"
    if key not in values:
        raise ValueError("不支持的通知平台。")
    webhook = _validate_webhook(platform, str(values.get(key) or ""))
    if not webhook:
        raise ValueError(f"{platform} 尚未连接。")
    text = str(message or "").strip()
    if not text:
        raise ValueError("通知内容不能为空。")
    if "店策 Agent" not in text:
        text = f"店策 Agent\n{text}"
    text = text[:12_000]
    payload = (
        {"msg_type": "text", "content": {"text": text}}
        if platform == "feishu"
        else {"msgtype": "text", "text": {"content": text}}
    )
    result = _post_json(webhook, payload)
    success = (
        int(result.get("code", result.get("StatusCode", 0)) or 0) == 0
        if platform == "feishu"
        else int(result.get("errcode", 0) or 0) == 0
    )
    if not success:
        raise ValueError(str(result.get("msg") or result.get("errmsg") or "平台拒绝了消息。"))
    return {"platform": platform, "ok": True, "message": "测试消息已发送。"}


def test_integration(platform: str) -> dict[str, Any]:
    return send_notification(platform, f"店策 Agent 连接测试\n时间：{_now_label()}\n连接成功，后续可发送经营日志。")


def send_report_notifications(report: dict[str, Any]) -> list[dict[str, Any]]:
    values = _load_integration_secrets()
    results: list[dict[str, Any]] = []
    for platform in ("feishu", "dingtalk"):
        if not values.get(f"{platform}_webhook"):
            continue
        try:
            results.append(send_notification(platform, str(report.get("content") or "")))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            results.append({"platform": platform, "ok": False, "message": str(error)})
    return results


def _action_audit_path() -> Path:
    return DATA_DIR / "action_audit.json"


def load_action_audit() -> dict[str, Any]:
    path = _action_audit_path()
    if not path.exists():
        return {"schema_version": 1, "actions": [], "execution_enabled": False}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        actions = value.get("actions", []) if isinstance(value, dict) else []
        if not isinstance(actions, list):
            actions = []
        return {
            "schema_version": 1,
            "updated_at": value.get("updated_at") if isinstance(value, dict) else None,
            "actions": [item for item in actions if isinstance(item, dict)],
            "execution_enabled": False,
        }
    except (OSError, json.JSONDecodeError):
        logger.exception("读取操作审计记录失败: %s", path)
        return {"schema_version": 1, "actions": [], "execution_enabled": False}


def get_action_audit(limit: int = 100) -> dict[str, Any]:
    audit = load_action_audit()
    actions = sorted(
        audit["actions"],
        key=lambda item: int(item.get("state_updated_at_ms") or item.get("created_at_ms") or 0),
        reverse=True,
    )[: min(500, max(1, int(limit)))]
    return {
        **audit,
        "actions": actions,
        "summary": {
            "total": len(audit["actions"]),
            "confirmed": sum(item.get("state") == "confirmed" for item in audit["actions"]),
            "cancelled": sum(item.get("state") == "cancelled" for item in audit["actions"]),
            "executed": sum(item.get("state") in {"succeeded", "verified"} for item in audit["actions"]),
        },
    }


def confirm_action_draft(action: dict[str, Any]) -> dict[str, Any]:
    errors = validate_action_draft(action)
    if errors:
        messages = "；".join(dict.fromkeys(str(item.get("message") or "动作校验失败") for item in errors))
        raise ValueError(messages)
    action_id = str(action.get("action_id") or "")
    if not re.fullmatch(r"[a-f0-9]{24}", action_id):
        raise ValueError("动作编号无效，请重新生成方案。")
    with _state_lock:
        audit = load_action_audit()
        existing = next((item for item in audit["actions"] if item.get("action_id") == action_id), None)
        if existing and existing.get("state") == "confirmed":
            return existing
        if existing and existing.get("state") == "cancelled":
            raise ValueError("该操作确认已撤销，请重新同步千川数据后生成新方案。")
        confirmed = transition_action(action, "confirmed")
        confirmed["confirmed_at_ms"] = int(time.time() * 1000)
        confirmed["confirmed_by"] = "local_user"
        confirmed["execution_note"] = "已确认方案，尚未执行任何千川页面操作。"
        actions = [item for item in audit["actions"] if item.get("action_id") != action_id]
        actions.append(confirmed)
        actions = sorted(
            actions,
            key=lambda item: int(item.get("state_updated_at_ms") or item.get("created_at_ms") or 0),
            reverse=True,
        )[:500]
        _atomic_json_write(
            _action_audit_path(),
            {"schema_version": 1, "updated_at": _now_label(), "execution_enabled": False, "actions": actions},
        )
    return confirmed


def cancel_confirmed_action(action_id: str) -> dict[str, Any]:
    action_id = str(action_id or "").lower()
    if not re.fullmatch(r"[a-f0-9]{24}", action_id):
        raise ValueError("动作编号无效。")
    with _state_lock:
        audit = load_action_audit()
        existing = next((item for item in audit["actions"] if item.get("action_id") == action_id), None)
        if not existing:
            raise ValueError("未找到对应的操作确认记录。")
        if existing.get("state") == "cancelled":
            return existing
        cancelled = transition_action(existing, "cancelled")
        cancelled["cancelled_at_ms"] = int(time.time() * 1000)
        actions = [cancelled if item.get("action_id") == action_id else item for item in audit["actions"]]
        _atomic_json_write(
            _action_audit_path(),
            {"schema_version": 1, "updated_at": _now_label(), "execution_enabled": False, "actions": actions},
        )
    return cancelled


def _shadow_audit_path() -> Path:
    return DATA_DIR / "shadow_execution.json"


def load_shadow_execution() -> dict[str, Any]:
    path = _shadow_audit_path()
    if not path.exists():
        return {"schema_version": 1, "records": [], "execution_enabled": False}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        records = value.get("records", []) if isinstance(value, dict) else []
        return {
            "schema_version": 1,
            "updated_at": value.get("updated_at") if isinstance(value, dict) else None,
            "records": [item for item in records if isinstance(item, dict)] if isinstance(records, list) else [],
            "execution_enabled": False,
        }
    except (OSError, json.JSONDecodeError):
        logger.exception("读取影子执行记录失败: %s", path)
        return {"schema_version": 1, "records": [], "execution_enabled": False}


def mark_action_manually_applied(action_id: str) -> dict[str, Any]:
    """Record an operator claim without claiming or triggering execution."""
    action_id = str(action_id or "").lower()
    if not re.fullmatch(r"[a-f0-9]{24}", action_id):
        raise ValueError("动作编号无效。")
    action = next(
        (item for item in load_action_audit().get("actions", []) if item.get("action_id") == action_id),
        None,
    )
    if not action or action.get("state") != "confirmed":
        raise ValueError("只有已确认且未撤销的方案可以进入影子核验。")
    now_ms = int(time.time() * 1000)
    with _state_lock:
        shadow = load_shadow_execution()
        existing = next((item for item in shadow["records"] if item.get("action_id") == action_id), None)
        if existing:
            return existing
        record = {
            "action_id": action_id,
            "reported_state": "manually_applied",
            "reported_applied_at_ms": now_ms,
            "reported_by": "local_user",
            "execution_source": "official_qianchuan_manual",
            "execution_enabled": False,
            "note": "仅记录用户声明；插件未点击、提交或修改千川。",
        }
        records = [item for item in shadow["records"] if item.get("action_id") != action_id]
        records.append(record)
        _atomic_json_write(
            _shadow_audit_path(),
            {"schema_version": 1, "updated_at": _now_label(), "execution_enabled": False, "records": records[-500:]},
        )
    return record


def _find_plan_readback(action: dict[str, Any]) -> dict[str, Any] | None:
    target = action.get("target_ref") if isinstance(action.get("target_ref"), dict) else {}
    account_key = str(target.get("account_key") or "")
    plan_id = str(target.get("id") or "")
    if not account_key or not plan_id:
        return None
    snapshot = load_data("qianchuan", "campaigns", account_key=account_key)
    data = (snapshot or {}).get("data", {})
    if not isinstance(data, dict):
        return None
    tables = data.get("tables") if isinstance(data.get("tables"), list) else []
    canonical_headers: list[str] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        headers = [str(value).strip() for value in table.get("headers", [])]
        rows = table.get("rows") if isinstance(table.get("rows"), list) else []
        if headers:
            canonical_headers = headers
        elif canonical_headers:
            headers = canonical_headers
        if not headers:
            continue
        for row in rows:
            if not isinstance(row, list):
                continue
            values = [str(value).strip() for value in row]
            record = {headers[index]: values[index] if index < len(values) else "" for index in range(len(headers))}
            if _entity_identifier(record, ("计划id", "项目id", "广告组id", "单元id")) != plan_id:
                continue
            budget = _evidence_value(record, ("日预算", "每日预算", "预算上限", "预算"))
            spend = _evidence_value(record, ("消耗", "总消耗", "广告消耗"))
            roi = _evidence_value(record, ("支付roi", "roi", "整体roi"))
            orders = _evidence_value(record, ("成交订单", "支付订单", "成交订单数", "订单数"))
            captured_at_ms = int(
                data.get("captured_at")
                or (float((snapshot or {}).get("timestamp", 0)) * 1000)
                or 0
            )
            return {
                "account_key": account_key,
                "account_label": str((data.get("account") or {}).get("label") or ""),
                "plan_id": plan_id,
                "plan_name": _clean_entity_name(next(iter(record.values()), ""), str(target.get("name") or plan_id)),
                "current_value": budget,
                "spend": spend,
                "roi": roi,
                "orders": orders,
                "captured_at_ms": captured_at_ms,
                "quality_score": int((data.get("quality") or {}).get("score", 0) or 0),
                "pagination_truncated": bool((data.get("quality") or {}).get("pagination_truncated")),
            }
    return None


def _execution_preflight_path() -> Path:
    return DATA_DIR / "execution_preflight.json"


def load_execution_preflight() -> dict[str, Any]:
    path = _execution_preflight_path()
    if not path.exists():
        return {"schema_version": 1, "session": None, "execution_enabled": False}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        session = value.get("session") if isinstance(value, dict) else None
        return {
            "schema_version": 1,
            "updated_at": value.get("updated_at") if isinstance(value, dict) else None,
            "session": session if isinstance(session, dict) else None,
            "execution_enabled": False,
        }
    except (OSError, json.JSONDecodeError):
        logger.exception("读取执行前检查会话失败: %s", path)
        return {"schema_version": 1, "session": None, "execution_enabled": False}


def _save_execution_preflight(session: dict[str, Any] | None) -> None:
    _atomic_json_write(
        _execution_preflight_path(),
        {"schema_version": 1, "updated_at": _now_label(), "session": session, "execution_enabled": False},
    )


def assess_execution_quota(action: dict[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    """Apply per-account daily limits and a cooldown before any page write."""

    now_ms = int(now_ms or time.time() * 1000)
    settings = load_agent_settings()
    target = action.get("target_ref") if isinstance(action.get("target_ref"), dict) else {}
    change = action.get("change") if isinstance(action.get("change"), dict) else {}
    account_key = str(target.get("account_key") or "")
    today = datetime.fromtimestamp(now_ms / 1000).date()
    completed: list[dict[str, Any]] = []
    for item in load_action_audit().get("actions", []):
        item_target = item.get("target_ref") if isinstance(item.get("target_ref"), dict) else {}
        executed_at = int(item.get("execution_reported_at_ms") or 0)
        if item.get("state") not in {"succeeded", "verified"} or item_target.get("account_key") != account_key or executed_at <= 0:
            continue
        if datetime.fromtimestamp(executed_at / 1000).date() == today:
            completed.append(item)
    reductions = []
    for item in completed:
        item_change = item.get("change") if isinstance(item.get("change"), dict) else {}
        current = item_change.get("current_value")
        target_value = item_change.get("target_value")
        if isinstance(current, (int, float)) and isinstance(target_value, (int, float)):
            reductions.append(max(0.0, float(current) - float(target_value)))
    proposed_reduction = (
        max(0.0, float(change["current_value"]) - float(change["target_value"]))
        if isinstance(change.get("current_value"), (int, float)) and isinstance(change.get("target_value"), (int, float))
        else 0.0
    )
    last_execution_ms = max((int(item.get("execution_reported_at_ms") or 0) for item in completed), default=0)
    cooldown_ms = int(settings["execution_cooldown_minutes"]) * 60 * 1000
    blockers: list[dict[str, str]] = []
    recovery_exemption = action.get("operation_type") == "restore_budget"
    if not recovery_exemption and len(completed) >= int(settings["max_daily_execution_count"]):
        blockers.append({"code": "DAILY_EXECUTION_COUNT_LIMIT", "message": "该账户今日执行次数已达到上限。"})
    if not recovery_exemption and sum(reductions) + proposed_reduction > float(settings["max_daily_budget_reduction"]):
        blockers.append({"code": "DAILY_BUDGET_REDUCTION_LIMIT", "message": "该账户今日累计预算影响金额将超过上限。"})
    if not recovery_exemption and last_execution_ms and now_ms - last_execution_ms < cooldown_ms:
        remaining = max(1, int((cooldown_ms - (now_ms - last_execution_ms) + 59_999) // 60_000))
        blockers.append({"code": "EXECUTION_COOLDOWN", "message": f"距离该账户上次执行不足冷却时间，请等待约 {remaining} 分钟。"})
    return {
        "allowed": not blockers,
        "account_key": account_key,
        "today_execution_count": len(completed),
        "max_daily_execution_count": int(settings["max_daily_execution_count"]),
        "today_budget_reduction": round(sum(reductions), 2),
        "proposed_budget_reduction": round(proposed_reduction, 2),
        "max_daily_budget_reduction": float(settings["max_daily_budget_reduction"]),
        "cooldown_minutes": int(settings["execution_cooldown_minutes"]),
        "blocked_reasons": blockers,
        "recovery_exemption": recovery_exemption,
    }


def create_budget_rollback_draft(action_id: str) -> dict[str, Any]:
    """Build a fresh restore-to-original-budget action from a verified write."""

    action_id = str(action_id or "").lower()
    original = next(
        (item for item in load_action_audit().get("actions", []) if item.get("action_id") == action_id),
        None,
    )
    if not original or original.get("state") != "verified" or original.get("operation_type") != "adjust_budget":
        raise ValueError("只有已完成页面验收的降低预算动作可以生成回滚。")
    readback = _find_plan_readback(original)
    target = original.get("target_ref") if isinstance(original.get("target_ref"), dict) else {}
    change = original.get("change") if isinstance(original.get("change"), dict) else {}
    current_value = (readback or {}).get("current_value")
    reduced_value = change.get("target_value")
    original_value = change.get("current_value")
    if not readback or int(readback.get("captured_at_ms") or 0) <= int(original.get("execution_reported_at_ms") or 0):
        raise ValueError("缺少执行后的新页面数据，请先重新读取当前千川计划页。")
    if not all(isinstance(value, (int, float)) for value in (current_value, reduced_value, original_value)):
        raise ValueError("预算回读数据不完整，不能生成回滚。")
    if abs(float(current_value) - float(reduced_value)) > 0.01:
        raise ValueError("当前预算已被再次修改，不能按旧记录回滚。")
    return build_action_draft(
        operation_type="restore_budget",
        operation_label=f"恢复原预算至 {float(original_value):g}",
        target_kind=str(target.get("kind") or "qianchuan_plan"),
        target_id=str(target.get("id") or ""),
        target_name=str(target.get("name") or ""),
        account_key=str(target.get("account_key") or ""),
        account_label=str(target.get("account_label") or ""),
        field=str(change.get("field") or "预算"),
        current_value=float(current_value),
        target_value=float(original_value),
        source="qianchuan",
        page_type="campaigns",
        captured_at_ms=int(readback.get("captured_at_ms") or 0),
        quality_score=int(readback.get("quality_score") or 0),
        confidence="high",
        evidence={
            "rollback_of_action_id": action_id,
            "spend": readback.get("spend"),
            "roi": readback.get("roi"),
            "orders": readback.get("orders"),
        },
        copy_text=f"{target.get('name') or '千川计划'} | 预算 {float(current_value):g} → {float(original_value):g}（恢复原值）",
    )


def start_execution_preflight(action_id: str) -> dict[str, Any]:
    """Start a short-lived, read-only supervised-execution preflight."""

    execution_mode = load_agent_settings().get("execution_mode", "observe")
    if execution_mode == "observe":
        raise ValueError("当前账户处于观察模式，只生成诊断和建议，不能启动执行。")
    if execution_mode == "shadow":
        raise ValueError("当前账户处于影子模式，请在千川人工操作后回到插件核验结果。")
    if execution_mode != "supervised":
        raise ValueError("账户运行模式无效，不能启动执行。")
    action_id = str(action_id or "").lower()
    if not re.fullmatch(r"[a-f0-9]{24}", action_id):
        raise ValueError("动作编号无效。")
    action = next(
        (item for item in load_action_audit().get("actions", []) if item.get("action_id") == action_id),
        None,
    )
    if not action or action.get("state") != "confirmed":
        raise ValueError("只有已确认且未撤销的方案可以启动执行前检查。")
    errors = validate_action_draft(action)
    if errors:
        messages = "；".join(dict.fromkeys(str(item.get("message") or "动作校验失败") for item in errors))
        raise ValueError(messages)
    change = action.get("change") if isinstance(action.get("change"), dict) else {}
    current_value = change.get("current_value")
    target_value = change.get("target_value")
    operation_type = str(action.get("operation_type") or "")
    if operation_type not in {"adjust_budget", "restore_budget"}:
        raise ValueError("首批受监督执行只开放降低预算和恢复原预算。")
    if not isinstance(current_value, (int, float)) or not isinstance(target_value, (int, float)):
        raise ValueError("预算数据不完整，不能执行。")
    if operation_type == "adjust_budget" and float(target_value) >= float(current_value):
        raise ValueError("降低预算动作不能增加预算。")
    if operation_type == "restore_budget" and float(target_value) <= float(current_value):
        raise ValueError("恢复预算动作必须回到更高的原预算。")
    quota = assess_execution_quota(action)
    if not quota["allowed"]:
        raise ValueError("；".join(item["message"] for item in quota["blocked_reasons"]))

    now_ms = int(time.time() * 1000)
    session_seed = f"{action_id}:{now_ms}".encode("utf-8")
    session = {
        "session_id": hashlib.sha256(session_seed).hexdigest()[:24],
        "action_id": action_id,
        "state": "awaiting_reread",
        "started_at_ms": now_ms,
        "expires_at_ms": now_ms + 3 * 60 * 1000,
        "pilot_scope": "reduce_or_restore_budget",
        "operation_type": operation_type,
        "current_value": current_value,
        "target_value": target_value,
        "quota": quota,
        "write_enabled": False,
        "execution_enabled": False,
    }
    with _state_lock:
        _save_execution_preflight(session)
    return build_execution_preflight_report()


def stop_execution_preflight(session_id: str) -> dict[str, Any]:
    session_id = str(session_id or "").lower()
    stored = load_execution_preflight().get("session")
    if not stored or stored.get("session_id") != session_id:
        raise ValueError("未找到对应的执行前检查会话。")
    stopped = {
        **stored,
        "state": "stopped",
        "stopped_at_ms": int(time.time() * 1000),
        "write_enabled": False,
        "execution_enabled": False,
    }
    with _state_lock:
        _save_execution_preflight(stopped)
    return build_execution_preflight_report()


def authorize_execution_preflight(session_id: str, confirmation_text: str) -> dict[str, Any]:
    """Issue a short-lived, single-use grant after an exact final confirmation.

    The grant is deliberately not an execution command.  A future browser
    executor must consume it atomically and perform its own final page checks.
    """

    session_id = str(session_id or "").lower()
    if not re.fullmatch(r"[a-f0-9]{24}", session_id):
        raise ValueError("执行前检查会话编号无效。")
    report = build_execution_preflight_report()
    session = report.get("session") if isinstance(report.get("session"), dict) else {}
    if session.get("session_id") != session_id:
        raise ValueError("未找到对应的执行前检查会话。")
    if report.get("state") != "ready_for_final_confirmation":
        raise ValueError("执行前检查尚未全部通过，不能生成最终授权。")
    action = report.get("action") if isinstance(report.get("action"), dict) else {}
    verb = "恢复预算" if session.get("operation_type") == "restore_budget" else "降低预算"
    expected_text = f"确认{verb}至{action.get('target_value')}"
    if str(confirmation_text or "").strip() != expected_text:
        raise ValueError(f"确认口令不一致，请完整输入：{expected_text}")

    now_ms = int(time.time() * 1000)
    grant_seed = f"{session_id}:{session.get('action_id')}:{now_ms}:{os.urandom(16).hex()}".encode("utf-8")
    authorized = {
        **session,
        "state": "authorized",
        "authorized_at_ms": now_ms,
        "authorization_expires_at_ms": now_ms + 60 * 1000,
        "authorization_id": hashlib.sha256(grant_seed).hexdigest()[:32],
        "authorization_consumed": False,
        "confirmation_text_hash": hashlib.sha256(expected_text.encode("utf-8")).hexdigest(),
        "write_enabled": False,
        "execution_enabled": False,
    }
    with _state_lock:
        _save_execution_preflight(authorized)
    return build_execution_preflight_report()


def consume_execution_authorization(authorization_id: str) -> dict[str, Any]:
    """Atomically consume a valid grant for a future in-process executor."""

    authorization_id = str(authorization_id or "").lower()
    if not re.fullmatch(r"[a-f0-9]{32}", authorization_id):
        raise ValueError("执行授权编号无效。")
    session_before = load_execution_preflight().get("session")
    action_before = next(
        (item for item in load_action_audit().get("actions", []) if item.get("action_id") == (session_before or {}).get("action_id")),
        None,
    )
    if not action_before:
        raise ValueError("授权对应的动作不存在。")
    quota = assess_execution_quota(action_before)
    if not quota["allowed"]:
        raise ValueError("；".join(item["message"] for item in quota["blocked_reasons"]))
    with _state_lock:
        session = load_execution_preflight().get("session")
        if not session or session.get("authorization_id") != authorization_id:
            raise ValueError("未找到对应的执行授权。")
        if session.get("state") != "authorized" or session.get("authorization_consumed"):
            raise ValueError("执行授权已使用或已失效。")
        now_ms = int(time.time() * 1000)
        if int(session.get("authorization_expires_at_ms") or 0) <= now_ms:
            _save_execution_preflight({**session, "state": "expired", "execution_enabled": False, "write_enabled": False})
            raise ValueError("执行授权已过期。")
        consumed = {
            **session,
            "state": "authorization_consumed",
            "authorization_consumed": True,
            "authorization_consumed_at_ms": now_ms,
            "execution_enabled": False,
            "write_enabled": False,
        }
        _save_execution_preflight(consumed)
    action = next(
        (item for item in load_action_audit().get("actions", []) if item.get("action_id") == consumed.get("action_id")),
        None,
    )
    if not action:
        raise ValueError("授权对应的动作不存在。")
    return {
        **consumed,
        "execution_request": _execution_request_for_action(action),
    }


def _execution_request_for_action(action: dict[str, Any]) -> dict[str, Any]:
    target = action.get("target_ref") if isinstance(action.get("target_ref"), dict) else {}
    change = action.get("change") if isinstance(action.get("change"), dict) else {}
    return {
        "operation_type": action.get("operation_type"),
        "account_key": target.get("account_key"),
        "plan_id": target.get("id"),
        "plan_name": target.get("name"),
        "field": change.get("field"),
        "expected_current_value": change.get("current_value"),
        "target_value": change.get("target_value"),
        "mode": "supervised_submit",
    }


def preview_execution_authorization(authorization_id: str) -> dict[str, Any]:
    """Return probe parameters without consuming or extending the grant."""

    authorization_id = str(authorization_id or "").lower()
    if not re.fullmatch(r"[a-f0-9]{32}", authorization_id):
        raise ValueError("执行授权编号无效。")
    session = load_execution_preflight().get("session")
    if not session or session.get("authorization_id") != authorization_id:
        raise ValueError("未找到对应的执行授权。")
    if session.get("state") != "authorized" or session.get("authorization_consumed"):
        raise ValueError("执行授权已使用或已失效。")
    if int(session.get("authorization_expires_at_ms") or 0) <= int(time.time() * 1000):
        raise ValueError("执行授权已过期。")
    action = next(
        (item for item in load_action_audit().get("actions", []) if item.get("action_id") == session.get("action_id")),
        None,
    )
    if not action or action.get("state") != "confirmed":
        raise ValueError("授权对应的动作不可执行。")
    quota = assess_execution_quota(action)
    if not quota["allowed"]:
        raise ValueError("；".join(item["message"] for item in quota["blocked_reasons"]))
    return {
        "action_id": session.get("action_id"),
        "authorization_expires_at_ms": session.get("authorization_expires_at_ms"),
        "execution_request": _execution_request_for_action(action),
        "quota": quota,
    }


def record_execution_result(action_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Persist the browser executor receipt without trusting it as verification."""

    action_id = str(action_id or "").lower()
    if not re.fullmatch(r"[a-f0-9]{24}", action_id) or not isinstance(result, dict):
        raise ValueError("执行回执无效。")
    submitted = result.get("submitted") is True
    now_ms = int(time.time() * 1000)
    with _state_lock:
        audit = load_action_audit()
        action = next((item for item in audit["actions"] if item.get("action_id") == action_id), None)
        if not action or action.get("state") != "confirmed":
            raise ValueError("执行动作不存在、已撤销或已记录。")
        executing = transition_action(action, "executing", allow_execution=True)
        updated = transition_action(executing, "succeeded" if submitted else "failed", allow_execution=True)
        updated.update({
            "execution_source": "qianchuan_browser_supervised",
            "execution_reported_at_ms": now_ms,
            "execution_receipt": {
                "submitted": submitted,
                "platform_success_observed": result.get("platform_success_observed") is True,
                "plan_id": str(result.get("plan_id") or ""),
                "target_value": result.get("target_value"),
                "error": str(result.get("error") or "")[:300],
            },
            "execution_note": "浏览器已提交，等待页面重新读取验收。" if submitted else "浏览器执行未完成，未记录为成功。",
        })
        actions = [updated if item.get("action_id") == action_id else item for item in audit["actions"]]
        _atomic_json_write(
            _action_audit_path(),
            {"schema_version": 1, "updated_at": _now_label(), "execution_enabled": True, "actions": actions},
        )
    return updated


def verify_execution_result(action_id: str) -> dict[str, Any]:
    action_id = str(action_id or "").lower()
    with _state_lock:
        audit = load_action_audit()
        action = next((item for item in audit["actions"] if item.get("action_id") == action_id), None)
        if not action or action.get("state") not in {"succeeded", "verified"}:
            raise ValueError("只有已提交动作可以进行页面回读验收。")
        readback = _find_plan_readback(action)
        target_value = (action.get("change") or {}).get("target_value")
        submitted_at = int(action.get("execution_reported_at_ms") or 0)
        matched = bool(
            readback
            and int(readback.get("captured_at_ms") or 0) > submitted_at
            and isinstance(readback.get("current_value"), (int, float))
            and isinstance(target_value, (int, float))
            and abs(float(readback["current_value"]) - float(target_value)) <= 0.01
        )
        if matched and action.get("state") != "verified":
            action = transition_action(action, "verified")
            action["verified_at_ms"] = int(time.time() * 1000)
            action["verification_readback"] = readback
            actions = [action if item.get("action_id") == action_id else item for item in audit["actions"]]
            rollback_of = str((action.get("evidence_ref") or {}).get("rollback_of_action_id") or "")
            if rollback_of:
                next_actions = []
                for item in actions:
                    if item.get("action_id") == rollback_of and item.get("state") == "verified":
                        rolled_back = transition_action(item, "rolled_back")
                        rolled_back["rolled_back_at_ms"] = action["verified_at_ms"]
                        rolled_back["rollback_action_id"] = action_id
                        next_actions.append(rolled_back)
                    else:
                        next_actions.append(item)
                actions = next_actions
            _atomic_json_write(
                _action_audit_path(),
                {"schema_version": 1, "updated_at": _now_label(), "execution_enabled": True, "actions": actions},
            )
    return {"action_id": action_id, "verified": matched, "state": action.get("state"), "readback": readback}


def build_execution_effectiveness_report(*, now_ms: int | None = None) -> dict[str, Any]:
    """Evaluate verified budget actions after a spend-sensitive observation window."""

    now_ms = int(now_ms or time.time() * 1000)
    items: list[dict[str, Any]] = []
    for action in load_action_audit().get("actions", []):
        if action.get("state") not in {"succeeded", "verified"}:
            continue
        executed_at_ms = int(action.get("execution_reported_at_ms") or 0)
        if executed_at_ms <= 0:
            continue
        target = action.get("target_ref") if isinstance(action.get("target_ref"), dict) else {}
        change = action.get("change") if isinstance(action.get("change"), dict) else {}
        evidence = action.get("evidence_ref") if isinstance(action.get("evidence_ref"), dict) else {}
        before = {
            "spend": evidence.get("spend"),
            "roi": evidence.get("roi"),
            "orders": evidence.get("orders"),
            "budget": change.get("current_value"),
        }
        after = _find_plan_readback(action)
        spend_value = before.get("spend")
        observation_minutes = 30 if isinstance(spend_value, (int, float)) and spend_value >= 1000 else 120 if isinstance(spend_value, (int, float)) and spend_value >= 300 else 24 * 60
        due_at_ms = executed_at_ms + observation_minutes * 60 * 1000
        fresh_after = bool(after and int(after.get("captured_at_ms") or 0) > executed_at_ms)
        if now_ms < due_at_ms:
            status = "waiting"
            label = f"等待 {observation_minutes // 60} 小时复查" if observation_minutes >= 60 else f"等待 {observation_minutes} 分钟复查"
            verdict = "观察窗口尚未结束，不追加动作。"
        elif not fresh_after:
            status = "needs_reread"
            label = "等待重新读取"
            verdict = "请打开对应千川计划页重新读取后再判断效果。"
        else:
            before_roi = before.get("roi")
            after_roi = after.get("roi")
            before_orders = before.get("orders")
            after_orders = after.get("orders")
            improved = bool(
                isinstance(after_roi, (int, float))
                and isinstance(before_roi, (int, float))
                and float(after_roi) >= float(before_roi) * 1.2
            ) or bool(
                isinstance(after_orders, (int, float))
                and isinstance(before_orders, (int, float))
                and float(after_orders) > float(before_orders)
            )
            status = "effective" if improved else "review"
            label = "止损有效" if improved else "需要人工复核"
            verdict = (
                "ROI 或成交已经改善，保持当前预算并继续观察。"
                if improved
                else "暂未观察到明确改善；建议检查素材、人群和转化承接，必要时生成原预算回滚方案。"
            )
        items.append({
            "action_id": action.get("action_id"),
            "account_label": target.get("account_label"),
            "plan_name": target.get("name"),
            "executed_at_ms": executed_at_ms,
            "due_at_ms": due_at_ms,
            "observation_window_minutes": observation_minutes,
            "status": status,
            "status_label": label,
            "verdict": verdict,
            "before": before,
            "after": after,
            "change": {
                "from": change.get("current_value"),
                "to": change.get("target_value"),
            },
            "rollback_available": status == "review" and action.get("state") == "verified",
        })
    items.sort(key=lambda item: (0 if item["status"] in {"review", "needs_reread"} else 1, -item["executed_at_ms"]))
    return {
        "observation_window_policy": "高消耗30分钟、普通消耗2小时、低消耗24小时",
        "items": items[:100],
        "summary": {
            "total": len(items),
            "waiting": sum(item["status"] == "waiting" for item in items),
            "needs_reread": sum(item["status"] == "needs_reread" for item in items),
            "effective": sum(item["status"] == "effective" for item in items),
            "review": sum(item["status"] == "review" for item in items),
        },
    }


def build_execution_preflight_report() -> dict[str, Any]:
    """Recheck a short-lived session against the latest Qianchuan page."""

    stored = load_execution_preflight().get("session")
    if not stored:
        return {
            "mode": "supervised_preflight",
            "state": "idle",
            "state_label": "尚未启动",
            "execution_enabled": False,
            "write_enabled": False,
            "session": None,
            "checks": [],
        }

    now_ms = int(time.time() * 1000)
    state = str(stored.get("state") or "awaiting_reread")
    authorization_expires_at_ms = int(stored.get("authorization_expires_at_ms") or 0)
    if state == "authorized" and authorization_expires_at_ms <= now_ms:
        state = "expired"
        stored = {**stored, "state": state, "write_enabled": False, "execution_enabled": False}
        with _state_lock:
            _save_execution_preflight(stored)
    elif state not in {"stopped", "expired", "authorized"} and int(stored.get("expires_at_ms") or 0) <= now_ms:
        state = "expired"
        stored = {**stored, "state": state, "write_enabled": False, "execution_enabled": False}
        with _state_lock:
            _save_execution_preflight(stored)

    action = next(
        (item for item in load_action_audit().get("actions", []) if item.get("action_id") == stored.get("action_id")),
        None,
    )
    readback = _find_plan_readback(action) if isinstance(action, dict) else None
    target = action.get("target_ref") if isinstance(action, dict) and isinstance(action.get("target_ref"), dict) else {}
    change = action.get("change") if isinstance(action, dict) and isinstance(action.get("change"), dict) else {}
    started_at_ms = int(stored.get("started_at_ms") or 0)
    current_value = change.get("current_value")
    target_value = change.get("target_value")
    observed = (readback or {}).get("current_value")
    checks = [
        {
            "id": "fresh_reread",
            "label": "确认后重新读取页面",
            "passed": bool(readback and int(readback.get("captured_at_ms") or 0) > started_at_ms),
            "detail": "必须使用本次检查启动后的新页面数据。",
        },
        {
            "id": "account_match",
            "label": "千川账号一致",
            "passed": bool(readback and readback.get("account_key") == target.get("account_key")),
            "detail": str((readback or {}).get("account_label") or target.get("account_label") or "账号未识别"),
        },
        {
            "id": "plan_match",
            "label": "计划唯一 ID 一致",
            "passed": bool(readback and readback.get("plan_id") == target.get("id")),
            "detail": str(target.get("id") or "缺少计划 ID"),
        },
        {
            "id": "quality",
            "label": "页面质量分不低于 70",
            "passed": bool(readback and int(readback.get("quality_score") or 0) >= 70),
            "detail": f"当前质量分 {int((readback or {}).get('quality_score') or 0)}",
        },
        {
            "id": "not_truncated",
            "label": "计划列表未截断",
            "passed": bool(readback and not readback.get("pagination_truncated")),
            "detail": "列表超过采集页数上限，请补采后再执行。" if (readback or {}).get("pagination_truncated") else "列表完整或无需分页。",
        },
        {
            "id": "current_value_match",
            "label": "当前预算未被其他人修改",
            "passed": bool(
                isinstance(observed, (int, float))
                and isinstance(current_value, (int, float))
                and abs(float(observed) - float(current_value)) <= 0.01
            ),
            "detail": f"方案值 {current_value if current_value is not None else '--'}，页面值 {observed if observed is not None else '--'}",
        },
        {
            "id": "pilot_scope",
            "label": "符合首批止损或回滚范围",
            "passed": bool(
                isinstance(current_value, (int, float))
                and isinstance(target_value, (int, float))
                and float(current_value) > 0
                and (
                    0 < (float(current_value) - float(target_value)) / float(current_value) <= 0.30
                    if action and action.get("operation_type") == "adjust_budget"
                    else 0 < (float(target_value) - float(current_value)) / float(current_value) <= 0.50
                )
            ),
            "detail": "降低预算不超过 30%；恢复预算必须绑定原执行记录且增幅不超过 50%。",
        },
    ]

    if state == "authorization_consumed":
        label = "授权凭证已使用"
    elif state == "authorized":
        label = "最终授权已生成"
    elif state == "stopped":
        label = "已紧急停止"
    elif state == "expired":
        label = "检查会话已过期"
    elif not checks[0]["passed"]:
        state = "awaiting_reread"
        label = "等待重新读取当前千川页"
    elif all(item["passed"] for item in checks):
        state = "ready_for_final_confirmation"
        label = "执行前检查已通过"
    else:
        state = "blocked"
        label = "执行前检查未通过"

    report_session = {
        **stored,
        "state": state,
        "write_enabled": False,
        "execution_enabled": False,
    }
    if state != stored.get("state") and state not in {"awaiting_reread", "blocked"}:
        with _state_lock:
            _save_execution_preflight(report_session)
    return {
        "mode": "supervised_preflight",
        "state": state,
        "state_label": label,
        "execution_enabled": False,
        "write_enabled": False,
        "session": report_session,
        "action": {
            "plan_name": str(target.get("name") or ""),
            "account_label": str(target.get("account_label") or ""),
            "plan_id": str(target.get("id") or ""),
            "field": change.get("field"),
            "current_value": current_value,
            "target_value": target_value,
            "operation_type": action.get("operation_type") if isinstance(action, dict) else "",
            "impact_preview": {
                "budget_change": round(float(target_value) - float(current_value), 2) if isinstance(current_value, (int, float)) and isinstance(target_value, (int, float)) else None,
                "change_percent": round((float(target_value) - float(current_value)) / float(current_value) * 100, 1) if isinstance(current_value, (int, float)) and isinstance(target_value, (int, float)) and float(current_value) else None,
                "today_spend": ((action or {}).get("evidence_ref") or {}).get("spend") if isinstance((action or {}).get("evidence_ref"), dict) else None,
                "daily_budget_impact_limit": load_agent_settings().get("max_daily_budget_reduction"),
                "rollback_condition": "执行后页面验收成功，且当前预算仍等于本次目标值。",
            },
        },
        "readback": readback,
        "checks": checks,
        "next_step": (
            "授权凭证已被原子消费，不能再次使用；请等待平台提交回执和执行后页面验收。"
            if state == "authorization_consumed"
            else
            "已生成 60 秒单次授权凭证；受监督执行器将只提交本次降低预算动作。"
            if state == "authorized"
            else
            "全部闸门已通过；输入与目标预算绑定的最终确认口令后，受监督执行器将提交本次降低预算动作。"
            if state == "ready_for_final_confirmation"
            else "重新读取当前千川页面，系统会自动复核账号、计划、预算和质量。"
            if state == "awaiting_reread"
            else "停止当前会话后重新生成方案。"
            if state in {"blocked", "expired"}
            else "会话已停止，未执行任何千川操作。"
        ),
    }


def build_shadow_execution_report() -> dict[str, Any]:
    """Compare user-reported manual actions with a later Qianchuan readback."""
    actions = [
        item for item in load_action_audit().get("actions", [])
        if isinstance(item, dict) and item.get("state") == "confirmed"
    ]
    markers = {
        str(item.get("action_id") or ""): item
        for item in load_shadow_execution().get("records", [])
        if isinstance(item, dict)
    }
    items: list[dict[str, Any]] = []
    for action in actions:
        action_id = str(action.get("action_id") or "")
        target = action.get("target_ref") if isinstance(action.get("target_ref"), dict) else {}
        change = action.get("change") if isinstance(action.get("change"), dict) else {}
        marker = markers.get(action_id)
        readback = _find_plan_readback(action)
        status = "awaiting_manual_action"
        status_label = "等待人工执行"
        detail = "方案已确认；请回到巨量千川人工执行，插件不会自动点击。"
        if marker:
            reported_at = int(marker.get("reported_applied_at_ms") or 0)
            if not readback or int(readback.get("captured_at_ms") or 0) <= reported_at:
                status = "awaiting_readback"
                status_label = "等待重新读取"
                detail = "已记录人工执行声明；请打开对应千川计划页面并重新读取。"
            else:
                observed = readback.get("current_value")
                current = change.get("current_value")
                target_value = change.get("target_value")
                if isinstance(observed, (int, float)) and isinstance(target_value, (int, float)) and abs(float(observed) - float(target_value)) <= 0.01:
                    status = "matched"
                    status_label = "回读已匹配"
                    detail = f"最新页面预算为 {observed:g}，与确认目标一致。"
                elif isinstance(observed, (int, float)) and isinstance(current, (int, float)) and abs(float(observed) - float(current)) <= 0.01:
                    status = "not_changed"
                    status_label = "页面尚未变化"
                    detail = f"最新页面预算仍为 {observed:g}，尚未观察到确认方案生效。"
                elif isinstance(observed, (int, float)):
                    status = "changed_differently"
                    status_label = "检测到其他修改"
                    detail = f"最新页面预算为 {observed:g}，与确认目标 {target_value} 不一致，请人工核对。"
                else:
                    status = "unverifiable"
                    status_label = "无法核验"
                    detail = "已读取计划，但没有获得可比较的当前预算。"
        items.append(
            {
                "action_id": action_id,
                "operation_type": action.get("operation_type"),
                "operation_label": action.get("operation_label"),
                "account_key": str(target.get("account_key") or ""),
                "account_label": str(target.get("account_label") or ""),
                "plan_id": str(target.get("id") or ""),
                "plan_name": str(target.get("name") or ""),
                "field": change.get("field"),
                "before_value": change.get("current_value"),
                "target_value": change.get("target_value"),
                "confirmed_at_ms": int(action.get("confirmed_at_ms") or 0),
                "reported_applied_at_ms": int((marker or {}).get("reported_applied_at_ms") or 0),
                "status": status,
                "status_label": status_label,
                "detail": detail,
                "readback": readback,
                "execution_enabled": False,
            }
        )
    order = {"changed_differently": 0, "not_changed": 1, "unverifiable": 2, "awaiting_readback": 3, "awaiting_manual_action": 4, "matched": 5}
    items.sort(key=lambda item: (order.get(item["status"], 9), -item["confirmed_at_ms"]))
    return {
        "generated_at": _now_label(),
        "mode": "shadow_only",
        "execution_enabled": False,
        "items": items,
        "summary": {
            "total": len(items),
            "awaiting_manual_action": sum(item["status"] == "awaiting_manual_action" for item in items),
            "awaiting_readback": sum(item["status"] == "awaiting_readback" for item in items),
            "matched": sum(item["status"] == "matched" for item in items),
            "needs_attention": sum(item["status"] in {"not_changed", "changed_differently", "unverifiable"} for item in items),
        },
    }


def _task_states_path() -> Path:
    return DATA_DIR / "task_states.json"


def load_task_states() -> dict[str, Any]:
    path = _task_states_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def update_task_state(task_id: str, status: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{16}", str(task_id or "")):
        raise ValueError("invalid task_id")
    if status not in {"todo", "doing", "observing", "done"}:
        raise ValueError("invalid task status")
    with _state_lock:
        states = load_task_states()
        previous_status = states.get(task_id, {}).get("status")
        states[task_id] = {"status": status, "updated_at": _now_label()}
        _atomic_json_write(_task_states_path(), states)
    # When task transitions to done, evaluate suggestion effectiveness
    if status == "done" and previous_status != "done":
        _evaluate_on_completion(task_id)
    return {"task_id": task_id, **states[task_id]}


# ---------------------------------------------------------------------------
# User feedback on suggestions (thumbs up / down)
# ---------------------------------------------------------------------------

def _feedback_path() -> Path:
    return DATA_DIR / "feedback.json"


def load_feedback() -> list[dict[str, Any]]:
    path = _feedback_path()
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_feedback(task_id: str, rating: str, comment: str = "", context: str = "") -> dict[str, Any]:
    if rating not in {"up", "down"}:
        raise ValueError("rating must be 'up' or 'down'")
    with _state_lock:
        feedback = load_feedback()
        entry = {
            "task_id": str(task_id or "")[:64],
            "rating": rating,
            "comment": str(comment or "")[:500],
            "context": str(context or "")[:200],
            "created_at": _now_label(),
        }
        feedback.append(entry)
        _atomic_json_write(_feedback_path(), feedback[-2000:])
    return entry


def get_feedback_stats() -> dict[str, Any]:
    feedback = load_feedback()
    up = sum(1 for item in feedback if item.get("rating") == "up")
    down = sum(1 for item in feedback if item.get("rating") == "down")
    total = up + down
    return {
        "total": total,
        "helpful": up,
        "not_helpful": down,
        "helpful_rate": round(up / total * 100, 1) if total else 0,
        "recent": feedback[-10:],
    }


# ---------------------------------------------------------------------------
# Selector / collection health monitoring
# ---------------------------------------------------------------------------

def _health_baselines_path() -> Path:
    return DATA_DIR / "health_baselines.json"


def load_health_baselines() -> dict[str, Any]:
    path = _health_baselines_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def update_health_baseline(source: str, page_type: str, quality: dict[str, Any]) -> None:
    key = f"{source}/{page_type}"
    with _state_lock:
        baselines = load_health_baselines()
        current = baselines.get(key, {"samples": []})
        samples = current.get("samples", [])
        samples.append({
            "quality_score": int(quality.get("score", 0) or 0),
            "row_count": int(quality.get("row_count", 0) or 0),
            "metric_count": int(quality.get("metric_count", 0) or 0),
            "captured_at": int(time.time() * 1000),
        })
        # Keep last 30 samples per page
        current["samples"] = samples[-30:]
        if len(samples) >= 5:
            avg_score = sum(s["quality_score"] for s in samples[-10:]) / min(len(samples), 10)
            avg_rows = sum(s["row_count"] for s in samples[-10:]) / min(len(samples), 10)
            avg_metrics = sum(s["metric_count"] for s in samples[-10:]) / min(len(samples), 10)
            current["baseline"] = {
                "avg_quality_score": round(avg_score, 1),
                "avg_row_count": round(avg_rows, 1),
                "avg_metric_count": round(avg_metrics, 1),
                "sample_count": len(samples),
            }
        baselines[key] = current
        _atomic_json_write(_health_baselines_path(), baselines)


def _consecutive_quality_drops(samples: list[dict[str, Any]], *, min_drops: int = 2, drop_points: int = 15) -> dict[str, Any] | None:
    """Detect consecutive quality declines on the same page."""
    recent = [item for item in samples if isinstance(item, dict)][-(min_drops + 1) :]
    if len(recent) < min_drops + 1:
        return None
    for index in range(1, len(recent)):
        previous = int(recent[index - 1].get("quality_score", 0) or 0)
        current = int(recent[index].get("quality_score", 0) or 0)
        if previous - current < drop_points:
            return None
    return {
        "from_score": int(recent[0].get("quality_score", 0) or 0),
        "to_score": int(recent[-1].get("quality_score", 0) or 0),
        "drop_count": min_drops,
    }


def check_selector_health() -> dict[str, Any]:
    baselines = load_health_baselines()
    alerts: list[dict[str, Any]] = []
    snapshots = list_snapshots()
    snapshot_map = {f"{item['source']}/{item['page_type']}": item for item in snapshots}
    for key, value in baselines.items():
        if not isinstance(value, dict):
            continue
        samples = [item for item in value.get("samples", []) if isinstance(item, dict)]
        baseline = value.get("baseline") if isinstance(value.get("baseline"), dict) else {}
        consecutive = _consecutive_quality_drops(samples)
        if consecutive:
            avg_score = float(baseline.get("avg_quality_score") or 0)
            latest = int(consecutive["to_score"])
            if avg_score <= 0 or latest < avg_score * 0.7 or consecutive["drop_count"] >= 2:
                alerts.append({
                    "level": "high",
                    "page": key,
                    "title": "疑似平台改版",
                    "detail": (
                        f"{key} 质量分连续 {consecutive['drop_count']} 次下降"
                        f"（{consecutive['from_score']} → {consecutive['to_score']}）"
                        + (f"，低于历史均值 {avg_score:.0f}。" if avg_score else "。")
                    ),
                    "action": "请打开该页面补采核对字段；如结构已变，优先更新选择器并补充 fixture 契约测试。",
                })

    for item in snapshots:
        key = f"{item['source']}/{item['page_type']}"
        baseline = baselines.get(key, {}).get("baseline") if isinstance(baselines.get(key), dict) else None
        if not baseline or baseline.get("sample_count", 0) < 3:
            continue
        if any(alert.get("page") == key and alert.get("title") == "疑似平台改版" for alert in alerts):
            continue
        current_score = item.get("quality_score", 0)
        avg_score = baseline.get("avg_quality_score", 0)
        current_rows = item.get("row_count", 0)
        avg_rows = baseline.get("avg_row_count", 0)
        # Alert if quality dropped significantly vs historical mean
        if avg_score > 30 and current_score < avg_score * 0.5:
            alerts.append({
                "level": "high",
                "page": key,
                "title": f"{item['page_type']} 采集质量异常下降",
                "detail": f"当前质量分 {current_score}，历史均值 {avg_score:.0f}。页面结构可能已改版。",
                "action": "请打开该页面检查字段是否正确识别，如需更新选择器请提交 Issue。",
            })
        elif avg_rows > 5 and current_rows < avg_rows * 0.3:
            alerts.append({
                "level": "warning",
                "page": key,
                "title": f"{item['page_type']} 采集行数偏低",
                "detail": f"当前 {current_rows} 行，历史均值 {avg_rows:.0f} 行。可能是虚拟滚动未触发或列表缩短。",
                "action": "刷新页面后重试；如仍偏低，平台可能调整了分页或列表结构。",
            })
    # Overall health score
    total_pages = len(baselines)
    healthy = sum(1 for _key, value in baselines.items() if isinstance(value, dict) and value.get("baseline", {}).get("sample_count", 0) >= 3)
    return {
        "generated_at": _now_label(),
        "total_tracked_pages": total_pages,
        "pages_with_baseline": healthy,
        "alerts": alerts,
        "baselines": {key: value.get("baseline", {}) for key, value in baselines.items() if isinstance(value, dict)},
        "mode": "read_only",
        "snapshot_pages": len(snapshot_map),
    }


# ---------------------------------------------------------------------------
# Task export to clipboard-friendly formats
# ---------------------------------------------------------------------------

def export_tasks(fmt: str = "clipboard") -> dict[str, Any]:
    ops = build_ops_manager()
    tasks = ops.get("all_tasks", [])
    today_label = time.strftime("%Y-%m-%d")
    todo = [item for item in tasks if item.get("status") == "todo"]
    doing = [item for item in tasks if item.get("status") == "doing"]
    observing = [item for item in tasks if item.get("status") == "observing"]
    done = [item for item in tasks if item.get("status") == "done"]
    total = len(tasks)
    completed = len(done)

    if fmt == "markdown":
        lines = [f"# 店策 Agent 任务清单 - {today_label}", "", f"共 {total} 项，已完成 {completed} 项", ""]
        if todo:
            lines.append("## 待处理")
            lines.extend(f"- [{item['owner']}] {item['title']}：{item['action']}" for item in todo)
        if doing:
            lines.append("\n## 进行中")
            lines.extend(f"- [{item['owner']}] {item['title']}：{item['action']}" for item in doing)
        if observing:
            lines.append("\n## 待观察")
            lines.extend(f"- [{item['owner']}] {item['title']}" for item in observing)
        if done:
            lines.append("\n## 已完成")
            lines.extend(f"- ~~[{item['owner']}] {item['title']}~~" for item in done)
        return {"format": "markdown", "content": "\n".join(lines), "task_count": total}

    # Default: plain text for clipboard (works in Feishu, WeChat Work, DingTalk)
    lines = [f"店策 Agent 任务清单 {today_label}", f"共 {total} 项 | 已完成 {completed} 项", ""]
    lines.append("【待处理】")
    for item in todo:
        lines.append(f"  [{item['owner']}] {item['title']}")
        lines.append(f"    → {item['action']}")
    lines.append("")
    lines.append("【进行中】")
    for item in doing:
        lines.append(f"  [{item['owner']}] {item['title']}")
    lines.append("")
    lines.append("【待观察】")
    for item in observing:
        lines.append(f"  [{item['owner']}] {item['title']}")
    lines.append("")
    lines.append("【已完成】")
    for item in done:
        lines.append(f"  ✓ [{item['owner']}] {item['title']}")
    return {"format": "clipboard", "content": "\n".join(lines), "task_count": total}


# ---------------------------------------------------------------------------
# Suggestion effectiveness tracking (close the loop)
# ---------------------------------------------------------------------------

def _suggestion_snapshots_path() -> Path:
    return DATA_DIR / "suggestion_snapshots.json"


def load_suggestion_snapshots() -> dict[str, Any]:
    path = _suggestion_snapshots_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_suggestion_snapshot(task_id: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    with _state_lock:
        snapshots = load_suggestion_snapshots()
        # Capture current metrics as baseline
        metrics: dict[str, Any] = {}
        for item in list_snapshots():
            snapshot = load_data(item["source"], item["page_type"])
            data = (snapshot or {}).get("data", {})
            for key, value in (data.get("safe_metrics") or {}).items():
                metrics[f"{item['source']}/{item['page_type']}/{key}"] = value
        entry = {
            "task_id": str(task_id or "")[:64],
            "created_at": _now_label(),
            "context": context or {},
            "metrics_snapshot": metrics,
            "evaluated": False,
            "evaluation": None,
        }
        snapshots[str(task_id)] = entry
        _atomic_json_write(_suggestion_snapshots_path(), snapshots)
    return entry


def _evaluate_on_completion(task_id: str) -> dict[str, Any] | None:
    snapshots = load_suggestion_snapshots()
    entry = snapshots.get(str(task_id))
    if not entry or entry.get("evaluated"):
        return None
    old_metrics = entry.get("metrics_snapshot", {})
    # Get current metrics
    current_metrics: dict[str, Any] = {}
    for item in list_snapshots():
        snapshot = load_data(item["source"], item["page_type"])
        data = (snapshot or {}).get("data", {})
        for key, value in (data.get("safe_metrics") or {}).items():
            current_metrics[f"{item['source']}/{item['page_type']}/{key}"] = value
    # Compare key metrics
    changes: list[dict[str, Any]] = []
    for key, old_value in old_metrics.items():
        new_value = current_metrics.get(key)
        if new_value is None:
            continue
        old_num = _parse_number(old_value)
        new_num = _parse_number(new_value)
        if old_num is not None and new_num is not None and old_num != 0:
            delta = new_num - old_num
            delta_pct = delta / abs(old_num) * 100
            changes.append({"metric": key.rsplit("/", 1)[-1], "old": old_num, "new": new_num, "delta": delta, "delta_percent": round(delta_pct, 1)})
    # Judge each metric in its correct direction. Growth metrics improve when
    # they rise; risk metrics such as refunds improve when they fall.
    positive_metrics = ("成交", "roi", "ROI", "转化", "订单", "点击率", "gmv", "GMV")
    negative_metrics = ("退款", "退货", "取消率", "投诉")
    improvements = 0
    degradations = 0
    for change in changes:
        metric = change["metric"]
        delta = change["delta"]
        if any(keyword in metric for keyword in positive_metrics):
            improvements += int(delta > 0)
            degradations += int(delta < 0)
        elif any(keyword in metric for keyword in negative_metrics):
            improvements += int(delta < 0)
            degradations += int(delta > 0)
    effective = improvements > degradations
    entry["evaluated"] = True
    entry["evaluation"] = {
        "evaluated_at": _now_label(),
        "effective": effective,
        "improvements": improvements,
        "degradations": degradations,
        "changes": changes[:10],
    }
    snapshots[str(task_id)] = entry
    _atomic_json_write(_suggestion_snapshots_path(), snapshots)
    return entry


def get_effectiveness_report() -> dict[str, Any]:
    snapshots = load_suggestion_snapshots()
    evaluated = [item for item in snapshots.values() if item.get("evaluated")]
    effective = sum(1 for item in evaluated if item.get("evaluation", {}).get("effective"))
    total = len(evaluated)
    return {
        "generated_at": _now_label(),
        "total_tracked": len(snapshots),
        "total_evaluated": total,
        "effective_count": effective,
        "effective_rate": round(effective / total * 100, 1) if total else 0,
        "recent_evaluations": [
            {
                "task_id": item["task_id"],
                "effective": item["evaluation"]["effective"],
                "changes": item["evaluation"]["changes"][:5],
                "evaluated_at": item["evaluation"]["evaluated_at"],
            }
            for item in evaluated[-10:]
        ],
        "mode": "read_only",
    }


def _scan_status_path() -> Path:
    return DATA_DIR / "scan_status.json"


def save_scan_status(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("scan status must be an object")
    allowed = {"status", "reason", "account_mode", "account_key", "account_label", "started_at", "finished_at", "current", "index", "total", "success", "failed", "low_quality", "results", "error"}
    status = {key: value[key] for key in allowed if key in value}
    if status.get("status") not in {"idle", "running", "completed", "partial", "cancelled", "error"}:
        raise ValueError("invalid scan status")
    results = status.get("results", [])
    if not isinstance(results, list) or len(results) > 100:
        raise ValueError("invalid scan results")
    _atomic_json_write(_scan_status_path(), status)
    return status


def load_scan_status() -> dict[str, Any]:
    path = _scan_status_path()
    if not path.exists():
        return {"status": "idle", "index": 0, "total": 18, "success": 0, "failed": 0, "results": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"status": "idle"}
    except (OSError, json.JSONDecodeError):
        return {"status": "error", "error": "巡检状态文件无法读取"}


def _atomic_text_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _reports_dir() -> Path:
    return DATA_DIR / "reports"


def _report_list(items: list[str], empty: str) -> str:
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, 1)) if items else f"- {empty}"


def _render_selected_report(
    template_key: str,
    custom_template: str,
    report_date: str,
    insights: dict[str, Any],
    action_center: dict[str, Any],
    ops: dict[str, Any],
    scan: dict[str, Any],
    scan_receipt: dict[str, Any],
) -> list[str] | None:
    if template_key == "default":
        return None
    top_tasks = [
        f"[{item['owner']}] {item['title']}：{item['action']}（验收：{item['acceptance']}）"
        for item in ops.get("today_top_actions", [])[:8]
    ]
    plans = [
        f"{item['plan']}：{item['suggestion']}（{item['reason']}）"
        for item in action_center.get("plan_recommendations", [])[:8]
    ]
    inventory = [
        f"{item['product']}：{item['title']}；{item['suggestion']}"
        for item in action_center.get("inventory_alerts", [])[:8]
    ]
    alerts = [
        f"{item['title']}：{item.get('action') or item.get('detail') or ''}"
        for item in insights.get("alerts", [])[:6]
    ]
    scan_status = (
        f"巡检 {scan.get('status', 'idle')}，成功 {scan.get('success', 0)} 页，"
        f"失败 {scan.get('failed', 0)} 页；体检覆盖率 {scan_receipt['summary']['coverage_rate']}%，"
        f"需复核 {scan_receipt['summary']['needs_review']} 页。"
    )
    context = {
        "date": report_date,
        "generated_at": _now_label(),
        "headline": str(insights.get("headline") or "暂无结论"),
        "summary": str(insights.get("summary") or "暂无摘要"),
        "top_tasks": _report_list(top_tasks, "暂无待办任务。"),
        "plans": _report_list(plans, "暂无千川调整建议。"),
        "inventory": _report_list(inventory, "暂无库存预警。"),
        "alerts": _report_list(alerts, "暂无其他异常。"),
        "scan_status": scan_status,
    }
    if template_key == "brief":
        return [
            f"# 店策 Agent 老板简报 - {report_date}",
            "",
            f"> 生成时间：{context['generated_at']}｜模式：只读建议",
            "",
            "## 一句话结论",
            "",
            f"- {context['headline']}",
            f"- {context['summary']}",
            "",
            "## 今天先做",
            "",
            context["top_tasks"],
            "",
            "## 需要关注",
            "",
            context["alerts"],
            "",
            "## 数据状态",
            "",
            f"- {scan_status}",
            "",
        ]
    if template_key == "handover":
        return [
            f"# 店策 Agent 运营交接日志 - {report_date}",
            "",
            f"> 交接生成时间：{context['generated_at']}｜所有执行动作需人工确认",
            "",
            "## 本班结论",
            "",
            f"- {context['headline']}",
            f"- {context['summary']}",
            "",
            "## 下一班优先事项",
            "",
            context["top_tasks"],
            "",
            "## 千川待处理",
            "",
            context["plans"],
            "",
            "## 库存待处理",
            "",
            context["inventory"],
            "",
            "## 数据交接",
            "",
            f"- {scan_status}",
            "",
        ]
    template = custom_template or DEFAULT_CUSTOM_REPORT_TEMPLATE
    for key, value in context.items():
        template = template.replace(f"{{{{{key}}}}}", str(value))
    return template.splitlines()


def generate_daily_report(report_date: str | None = None) -> dict[str, Any]:
    report_date = report_date or time.strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date):
        raise ValueError("report_date must be YYYY-MM-DD")
    insights = build_insights()
    action_center = build_action_center()
    ops = build_ops_manager()
    scan = load_scan_status()
    scan_receipt = build_scan_receipt()
    catalog = list_snapshots()
    report_path = _reports_dir() / f"{report_date}.md"

    # Build staleness map: source/page_type -> saved_at and age
    freshness_map: dict[str, dict[str, Any]] = {}
    for item in catalog:
        freshness_map[f"{item['source']}/{item['page_type']}"] = {
            "saved_at": item.get("saved_at", ""),
            "age_seconds": item.get("age_seconds", 0),
            "fresh": item.get("fresh", False),
        }

    # Identify stale data sources (>1 hour)
    stale_sources = [
        f"{key} (更新于 {info['saved_at']})"
        for key, info in freshness_map.items()
        if info["age_seconds"] > 3600
    ]

    lines = [
        f"# 店策 Agent 每日经营报告 - {report_date}",
        "",
        f"> 生成时间：{_now_label()}｜模式：只读建议",
        "",
    ]

    # Staleness warning section
    if stale_sources:
        lines.extend([
            "⚠️ **数据时效提醒**：以下数据源超过 1 小时未更新，建议重新同步后再做决策：",
            "",
        ])
        for src in stale_sources[:5]:
            lines.append(f"- {src}")
        lines.append("")

    lines.extend([
        "## 今日结论",
        "",
        f"- {insights['headline']}",
        f"- {insights['summary']}",
        f"- 已同步页面：{len(insights['coverage'])}；千川调整项：{len(action_center['plan_recommendations'])}；库存预警：{len(action_center['inventory_alerts'])}",
        f"- 自动巡检：{scan.get('status', 'idle')}；成功 {scan.get('success', 0)} 页，失败 {scan.get('failed', 0)} 页，低质量 {scan.get('low_quality', 0)} 页。",
        f"- 数据体检：{scan_receipt['readiness_label']}；覆盖率 {scan_receipt['summary']['coverage_rate']}%；需复核 {scan_receipt['summary']['needs_review']} 页。",
        "",
        "## 运营总管今日任务",
        "",
    ])
    for index, item in enumerate(ops["today_top_actions"][:8], 1):
        source_key = item.get("source", "")
        data_time = freshness_map.get(source_key, {}).get("saved_at", "未知")
        lines.extend([f"{index}. **[{item['owner']}] {item['title']}**：{item['action']}", f"   - 依据：{item['evidence']}｜验收：{item['acceptance']}｜数据时间：{data_time}"])
    lines.extend(["", "## 货架运营", ""])
    for item in action_center["shelf_analysis"]["recommendations"]:
        lines.append(f"- **{item['title']}**：{item['action']}（{item['evidence']}）")
    if not action_center["shelf_analysis"]["recommendations"]:
        lines.append("- 暂无货架专项建议。")
    lines.extend(["", "## 直播与内容运营", ""])
    for item in action_center["live_analysis"]["recommendations"]:
        lines.append(f"- **{item['title']}**：{item['action']}（{item['evidence']}）")
    if not action_center["live_analysis"]["recommendations"]:
        lines.append("- 暂无直播专项建议。")
    lines.extend([
        "",
        "## 千川计划调整建议",
        "",
    ])
    plans = action_center["plan_recommendations"]
    if plans:
        qianchuan_time = freshness_map.get("qianchuan/report", freshness_map.get("qianchuan/campaigns", {})).get("saved_at", "未知")
        for index, item in enumerate(plans[:10], 1):
            lines.extend([f"{index}. **{item['plan']}**：{item['suggestion']}", f"   - 依据：{item['reason']}｜数据时间：{qianchuan_time}"])
    else:
        lines.append("- 暂无可执行建议；请同步千川计划列表和报表页面。")
    lines.extend(["", "## 千川视频库与直播引流素材", ""])
    creative = action_center["creative_analysis"]
    summary = creative["summary"]
    lines.append(f"- 视频 {summary['total_videos']} 条；在投/有消耗 {summary['spending_videos']} 条；未测试 {summary['untested_videos']} 条；高风险 {summary['risky_videos']} 条；高潜 {summary['high_potential_videos']} 条。")
    for item in creative["recommendations"][:8]:
        lines.append(f"- **{item['title']}**：{item['action']}（{item['evidence']}）")
    lines.extend(["", "## 库存预警", ""])
    inventory = action_center["inventory_alerts"]
    if inventory:
        doudian_time = freshness_map.get("doudian/products", {}).get("saved_at", "未知")
        for index, item in enumerate(inventory[:15], 1):
            lines.append(f"{index}. **{item['product']}**：{item['title']}；{item['suggestion']}（数据时间：{doudian_time}）")
    else:
        lines.append("- 暂无库存预警，或尚未同步商品/库存页面。")
    lines.extend(["", "## 其他优先事项", ""])
    for index, item in enumerate(insights["alerts"][:8], 1):
        confidence_tag = f"[{item['confidence']}]" if item.get("confidence") else ""
        lines.append(f"{index}. **{item['title']}** {confidence_tag}：{item.get('action') or item.get('detail') or ''}")
    lines.extend(
        [
            "",
            "## 安全边界",
            "",
            "- 本报告来自已登录网页的本地脱敏快照，不等同于官方 API 数据。",
            "- 所有预算、启停和店铺变更建议必须在后台核对统计周期与归因口径后人工确认。",
            "",
        ]
    )
    template_key = str(action_center["settings"].get("report_template") or "default")
    selected_lines = _render_selected_report(
        template_key,
        str(action_center["settings"].get("custom_report_template") or ""),
        report_date,
        insights,
        action_center,
        ops,
        scan,
        scan_receipt,
    )
    if selected_lines is not None:
        lines = selected_lines
    _atomic_text_write(report_path, "\n".join(lines))
    _cleanup_old_reports(int(action_center["settings"]["report_retention_days"]))
    return {
        "date": report_date,
        "generated_at": _now_label(),
        "path": str(report_path),
        "headline": insights["headline"],
        "summary": action_center["summary"],
        "template": template_key,
        "stale_sources": stale_sources[:5],
        "content": "\n".join(lines),
    }


def _cleanup_old_reports(retention_days: int) -> None:
    cutoff = time.time() - retention_days * 86400
    reports_dir = _reports_dir()
    if not reports_dir.exists():
        return
    for path in reports_dir.glob("*.md"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            logger.exception("清理旧日报失败: %s", path)


def load_latest_report() -> dict[str, Any] | None:
    reports_dir = _reports_dir()
    if not reports_dir.exists():
        return None
    paths = sorted(reports_dir.glob("*.md"), reverse=True)
    if not paths:
        return None
    path = paths[0]
    try:
        return {"date": path.stem, "path": str(path), "content": path.read_text(encoding="utf-8")}
    except OSError:
        logger.exception("读取日报失败: %s", path)
        return None


def _daily_report_scheduler(stop_event: threading.Event) -> None:
    while not stop_event.wait(30):
        try:
            settings = load_agent_settings()
            if not settings["daily_report_enabled"]:
                continue
            now = datetime.now()
            if now.strftime("%H:%M") < settings["daily_report_time"]:
                continue
            target = _reports_dir() / f"{now:%Y-%m-%d}.md"
            if not target.exists():
                report = generate_daily_report(now.strftime("%Y-%m-%d"))
                if _load_integration_secrets().get("auto_send_reports"):
                    send_report_notifications(report)
                logger.info("已生成每日经营报告: %s", target)
        except Exception:
            logger.exception("生成定时日报失败")


class Handler(BaseHTTPRequestHandler):
    server_version = "DianAgent/3.0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug(fmt, *args)

    def _cors(self) -> None:
        origin = self.headers.get("Origin", "")
        if origin.startswith("chrome-extension://") or origin.startswith("moz-extension://") or origin.startswith("safari-web-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Dian-Agent, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def _client_is_loopback(self) -> bool:
        host = str(self.client_address[0] if self.client_address else "")
        return host in {"127.0.0.1", "::1", "localhost"}

    def _authorized_write(self) -> bool:
        if self.headers.get("X-Dian-Agent") not in {"1", "2"}:
            return False
        expected = load_bridge_token()
        auth = str(self.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
        else:
            provided = str(self.headers.get("X-Dian-Agent-Token") or "").strip()
        return bool(provided) and secrets.compare_digest(provided, expected)

    def do_GET(self) -> None:  # noqa: N802
        parsed_url = urlparse(self.path)
        path = unquote(parsed_url.path).rstrip("/") or "/"
        query = parse_qs(parsed_url.query)
        if path == "/health":
            catalog = list_snapshots()
            schema_warnings = _schema_version_check()
            disk_info = _disk_usage_check()
            agent_settings = load_agent_settings()
            execution_mode = str(agent_settings.get("execution_mode") or "observe")
            self._json(
                {
                    "status": "ok",
                    "version": "3.3.1",
                    "mode": execution_mode,
                    "execution_mode_label": {"observe": "观察模式", "shadow": "影子模式", "supervised": "受控执行"}.get(execution_mode, "未知模式"),
                    "execution_enabled": execution_mode == "supervised",
                    "auth_required": True,
                    "snapshot_count": len(catalog),
                    "schema_warnings": schema_warnings,
                    "disk": disk_info,
                    "sources": {
                        source: {
                            "has_data": any(item["source"] == source for item in catalog),
                            "pages": sum(1 for item in catalog if item["source"] == source),
                        }
                        for source in sorted(ALLOWED_SOURCES)
                    },
                }
            )
            return
        if path == "/auth/bootstrap":
            if not self._client_is_loopback():
                self._json({"error": "bootstrap_forbidden"}, 403)
                return
            if self.headers.get("X-Dian-Agent") not in {"1", "2"}:
                self._json({"error": "missing_bridge_header"}, 403)
                return
            self._json({"ok": True, "token": load_bridge_token(), "auth_required": True})
            return
        if path == "/oauth/oceanengine/status":
            self._json(OceanEngineOAuth(DATA_DIR).status())
            return
        if path == "/oauth/oceanengine/sync-status":
            self._json(load_sync_status(DATA_DIR))
            return
        if path == "/oauth/oceanengine/callback":
            oauth = OceanEngineOAuth(DATA_DIR)
            platform_error = str(
                query.get("error", query.get("error_code", [""]))[0] or ""
            )
            if platform_error:
                self._html(
                    oauth.result_page(
                        success=False,
                        title="千川授权未完成",
                        message="平台返回了取消或失败结果，请回到店策重新点击授权。",
                    ),
                    400,
                )
                return
            try:
                result = oauth.complete_authorization(
                    str(query.get("auth_code", query.get("code", [""]))[0] or ""),
                    str(query.get("state", [""])[0] or ""),
                )
                warning = str(result.get("warning") or "")
                message = "官方 API 已连接，店策可以读取本次授权的千川账号。"
                if warning:
                    message = f"{message} {warning}"
                self._html(
                    oauth.result_page(
                        success=True,
                        title="千川账号授权成功",
                        message=message,
                        account_count=int(result.get("account_count") or 0),
                    )
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                logger.warning("千川 OAuth 回调处理失败: %s", error)
                self._html(
                    oauth.result_page(
                        success=False,
                        title="千川授权未完成",
                        message=str(error),
                    ),
                    400,
                )
            return
        if path == "/catalog":
            self._json({"snapshots": list_snapshots()})
            return
        if path in {"/insights", "/brief"}:
            self._json(_cached("insights", build_insights))
            return
        if path in {"/action-center", "/recommendations"}:
            self._json(_cached("action_center", build_action_center))
            return
        if path == "/shelf-analysis":
            self._json(_cached("shelf", build_shelf_analysis))
            return
        if path == "/live-analysis":
            self._json(_cached("live", build_live_analysis))
            return
        if path == "/qianchuan-creative-analysis":
            self._json(_cached("creative", build_qianchuan_creative_analysis))
            return
        if path == "/qianchuan-accounts":
            self._json(build_store_catalog())
            return
        if path == "/stores":
            self._json(build_store_catalog())
            return
        if path == "/ops-manager":
            self._json(_cached("ops_manager", build_ops_manager))
            return
        if path == "/tasks":
            ops = _cached("ops_manager", build_ops_manager)
            self._json({"states": load_task_states(), "tasks": ops["all_tasks"]})
            return
        if path == "/scan-status":
            self._json(load_scan_status())
            return
        if path == "/scan-receipt":
            self._json(build_scan_receipt())
            return
        if path == "/operation-context":
            self._json(build_operation_context())
            return
        if path == "/health-monitor":
            self._json(check_selector_health())
            return
        if path == "/feedback":
            self._json(get_feedback_stats())
            return
        if path.startswith("/tasks/export"):
            fmt = query.get("format", ["clipboard"])[0]
            self._json(export_tasks(fmt))
            return
        if path == "/effectiveness":
            self._json(get_effectiveness_report())
            return
        if path == "/actions/audit":
            self._json(get_action_audit(int(query.get("limit", ["100"])[0])))
            return
        if path == "/actions/readiness":
            self._json(build_automation_readiness())
            return
        if path == "/actions/stop-loss-queue":
            self._json(build_stop_loss_queue())
            return
        if path == "/actions/preflight":
            self._json(build_execution_preflight_report())
            return
        if path == "/actions/shadow":
            self._json(build_shadow_execution_report())
            return
        if path == "/actions/effectiveness":
            self._json(build_execution_effectiveness_report())
            return
        if path == "/trends":
            self._json(build_trends(int(query.get("days", ["7"])[0]), query.get("source", [None])[0], query.get("page_type", [None])[0]))
            return
        if path == "/settings":
            self._json(load_agent_settings())
            return
        if path == "/integrations":
            self._json(get_integration_settings())
            return
        if path == "/reports/latest":
            report = load_latest_report()
            self._json(report or {"error": "report_not_found"}, 200 if report else 404)
            return
        if path.startswith("/data/"):
            parts = [part for part in path.split("/") if part]
            source = parts[1] if len(parts) > 1 else ""
            page_type = parts[2] if len(parts) > 2 else None
            snapshot = load_data(source, page_type)
            if snapshot:
                self._json(snapshot)
            else:
                self._json({"error": "snapshot_not_found", "source": source, "page_type": page_type}, 404)
            return
        self._json({"error": "not_found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path).rstrip("/") or "/"
        if self.headers.get("X-Dian-Agent") not in {"1", "2"}:
            self._json({"error": "missing_bridge_header"}, 403)
            return
        if not self._authorized_write():
            self._json({"error": "missing_or_invalid_bridge_token", "auth_required": True}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json({"error": "invalid_content_length"}, 400)
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json({"error": "body_too_large", "max_bytes": MAX_BODY_BYTES}, 413)
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if path == "/push":
                source = payload.get("source")
                data = payload.get("data")
                saved = save_data(source, data)
                _invalidate_cache()
                account = saved.get("data", {}).get("account") if isinstance(saved.get("data"), dict) else None
                self._json({"ok": True, "source": source, "page_type": saved["page_type"], "account": account})
                return
            if path == "/settings":
                settings = save_agent_settings(payload)
                _invalidate_cache()
                self._json({"ok": True, "settings": settings})
                return
            if path == "/oauth/oceanengine/start":
                result = OceanEngineOAuth(DATA_DIR).start_authorization(
                    str(payload.get("app_id") or ""),
                    str(payload.get("app_secret") or ""),
                )
                self._json({"ok": True, **result})
                return
            if path == "/oauth/oceanengine/sync":
                account_ids = payload.get("account_ids")
                if account_ids is not None and not isinstance(account_ids, list):
                    raise ValueError("账号选择格式不正确。")
                result = OceanEngineDataClient(
                    OceanEngineOAuth(DATA_DIR)
                ).sync(
                    save_data,
                    [str(value) for value in account_ids] if account_ids else None,
                    int(payload.get("days") or 7),
                )
                selected_key = str(
                    load_agent_settings().get("qianchuan_account_key") or ""
                )
                if not selected_key:
                    active_account = next(
                        (
                            account
                            for account in result.get("accounts", [])
                            if int(account.get("advertiser_count") or 0) > 0
                        ),
                        None,
                    )
                    if active_account:
                        selected_key = str(active_account.get("account_key") or "")
                        save_agent_settings(
                            {"qianchuan_account_key": selected_key}
                        )
                result["selected_account_key"] = selected_key
                _invalidate_cache()
                self._json(result)
                return
            if path == "/integrations/settings":
                self._json({"ok": True, "integrations": save_integration_settings(payload)})
                return
            if path == "/integrations/test":
                self._json({"ok": True, "result": test_integration(str(payload.get("platform") or ""))})
                return
            if path == "/reports/generate":
                report = generate_daily_report(payload.get("date"))
                deliveries = send_report_notifications(report) if payload.get("notify") else []
                self._json({"ok": True, "report": report, "deliveries": deliveries})
                return
            if path == "/tasks/update":
                task = update_task_state(str(payload.get("task_id") or ""), str(payload.get("status") or ""))
                _invalidate_cache()
                self._json({"ok": True, "task": task})
                return
            if path == "/tasks/track":
                self._json({"ok": True, "snapshot": save_suggestion_snapshot(str(payload.get("task_id") or ""), payload.get("context"))})
                return
            if path == "/feedback":
                self._json({"ok": True, "feedback": save_feedback(
                    str(payload.get("task_id") or ""),
                    str(payload.get("rating") or ""),
                    str(payload.get("comment") or ""),
                    str(payload.get("context") or ""),
                )})
                return
            if path == "/actions/confirm":
                action = payload.get("action")
                if not isinstance(action, dict):
                    raise ValueError("缺少有效的操作草稿。")
                confirmed = confirm_action_draft(action)
                _invalidate_cache()
                self._json({"ok": True, "action": confirmed, "executed": False, "execution_enabled": False})
                return
            if path == "/actions/cancel":
                cancelled = cancel_confirmed_action(str(payload.get("action_id") or ""))
                _invalidate_cache()
                self._json({"ok": True, "action": cancelled, "executed": False, "execution_enabled": False})
                return
            if path == "/actions/manual-applied":
                marker = mark_action_manually_applied(str(payload.get("action_id") or ""))
                _invalidate_cache()
                self._json({"ok": True, "marker": marker, "executed_by_plugin": False, "execution_enabled": False})
                return
            if path == "/actions/preflight/start":
                report = start_execution_preflight(str(payload.get("action_id") or ""))
                self._json({"ok": True, "preflight": report, "executed": False, "execution_enabled": False})
                return
            if path == "/actions/preflight/stop":
                report = stop_execution_preflight(str(payload.get("session_id") or ""))
                self._json({"ok": True, "preflight": report, "executed": False, "execution_enabled": False})
                return
            if path == "/actions/preflight/authorize":
                report = authorize_execution_preflight(
                    str(payload.get("session_id") or ""),
                    str(payload.get("confirmation_text") or ""),
                )
                self._json({"ok": True, "preflight": report, "executed": False, "execution_enabled": False})
                return
            if path == "/actions/preflight/consume":
                consumed = consume_execution_authorization(str(payload.get("authorization_id") or ""))
                self._json({"ok": True, "grant": consumed, "executed": False, "mode": "supervised_submit"})
                return
            if path == "/actions/preflight/preview":
                preview = preview_execution_authorization(str(payload.get("authorization_id") or ""))
                self._json({"ok": True, "preview": preview})
                return
            if path == "/actions/rollback/create":
                draft = create_budget_rollback_draft(str(payload.get("action_id") or ""))
                self._json({"ok": True, "action": draft})
                return
            if path == "/actions/execution/result":
                action = record_execution_result(str(payload.get("action_id") or ""), payload.get("result") or {})
                self._json({"ok": True, "action": action})
                return
            if path == "/actions/execution/verify":
                verification = verify_execution_result(str(payload.get("action_id") or ""))
                self._json({"ok": True, "verification": verification})
                return
            if path == "/scan-status":
                self._json({"ok": True, "scan": save_scan_status(payload)})
                return
            self._json({"error": "not_found"}, 404)
        except (json.JSONDecodeError, ValueError, TypeError) as error:
            self._json({"error": str(error)}, 400)


def main() -> None:
    # allow_reuse_address must be set before __init__ calls server_bind()
    ThreadingHTTPServer.allow_reuse_address = True
    ensure_bridge_token()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as error:
        logger.error(
            "端口 %d 无法使用，扩展只能连接此端口。请关闭占用该端口的程序后重试：%s",
            PORT,
            error,
        )
        sys.exit(1)
    stop_event = threading.Event()
    scheduler = threading.Thread(target=_daily_report_scheduler, args=(stop_event,), daemon=True)
    scheduler.start()
    logger.info("店策 Agent 本地服务已启动: http://127.0.0.1:%d", PORT)
    logger.info("写入接口已启用 bridge_token；方案确认模式；数据目录: %s", DATA_DIR)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("服务已停止")
    finally:
        stop_event.set()
        scheduler.join(timeout=2)
        server.server_close()


if __name__ == "__main__":
    main()
