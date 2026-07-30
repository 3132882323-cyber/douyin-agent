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
from execution_modes import EXECUTION_MODES, execution_mode_label, normalize_execution_mode

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(message)s")
logger = logging.getLogger("dian-agent-http")

import state
from reports import DEFAULT_CUSTOM_REPORT_TEMPLATE, REPORT_TEMPLATE_KEYS
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


def _secret_file_hints() -> list[dict[str, str]]:
    """Surface local secret-file permission reminders without exposing values."""
    hints: list[dict[str, str]] = []
    candidates = [
        ("bridge_token", DATA_DIR / "bridge_token.txt", "本机写入令牌"),
        ("integrations", DATA_DIR / "integrations.json", "Webhook 连接配置"),
        ("oceanengine_oauth", DATA_DIR / "oceanengine_oauth.json", "千川 OAuth 凭证"),
    ]
    for key, path, label in candidates:
        if not path.exists():
            continue
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            continue
        if mode & 0o077:
            hints.append(
                {
                    "key": key,
                    "label": label,
                    "message": f"{label} 权限过宽（当前 {oct(mode)}），建议仅当前用户可读（如 600）。",
                }
            )
    return hints


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



from reports import (
    _atomic_text_write,
    _cleanup_old_reports,
    _daily_report_scheduler,
    _render_selected_report,
    _report_list,
    _reports_dir,
    generate_daily_report,
    load_latest_report,
)

from actions import (
    _action_audit_path,
    _execution_preflight_path,
    _execution_request_for_action,
    _find_plan_readback,
    _save_execution_preflight,
    _shadow_audit_path,
    assess_execution_quota,
    authorize_execution_preflight,
    build_execution_effectiveness_report,
    build_execution_preflight_report,
    build_shadow_execution_report,
    cancel_confirmed_action,
    confirm_action_draft,
    consume_execution_authorization,
    create_budget_rollback_draft,
    get_action_audit,
    load_action_audit,
    load_execution_preflight,
    load_shadow_execution,
    mark_action_manually_applied,
    preview_execution_authorization,
    record_execution_result,
    start_execution_preflight,
    stop_execution_preflight,
    verify_execution_result,
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
    raw_mode = str(next_settings.get("execution_mode") or "observe").strip().lower()
    if raw_mode not in EXECUTION_MODES:
        raise ValueError("execution_mode must be observe, shadow or supervised")
    next_settings["execution_mode"] = raw_mode
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
            execution_mode = normalize_execution_mode(agent_settings.get("execution_mode"))
            self._json(
                {
                    "status": "ok",
                    "version": "3.3.1",
                    "mode": execution_mode,
                    "execution_mode_label": execution_mode_label(execution_mode),
                    "execution_enabled": execution_mode == "supervised",
                    "auth_required": True,
                    "secret_files": _secret_file_hints(),
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
