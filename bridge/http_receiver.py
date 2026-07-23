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
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(message)s")
logger = logging.getLogger("dian-agent-http")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DIAN_AGENT_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

PORT = int(os.environ.get("BRIDGE_PORT", "8765"))
MAX_BODY_BYTES = int(os.environ.get("BRIDGE_MAX_BODY", str(2 * 1024 * 1024)))
ALLOWED_SOURCES = {"doudian", "qianchuan"}
SAFE_KEY = re.compile(r"^[a-z0-9_-]{1,48}$")
STALE_SECONDS = 10 * 60
DEFAULT_AGENT_SETTINGS = {
    "roi_target": 1.5,
    "min_spend_for_action": 100.0,
    "low_inventory_threshold": 10,
    "critical_inventory_threshold": 3,
    "inventory_days_warning": 3.0,
    "daily_report_enabled": True,
    "daily_report_time": "09:00",
    "report_retention_days": 30,
    "history_retention_days": 30,
    "qianchuan_account_key": "",
}


def _now_label() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _safe_page_type(value: Any) -> str:
    page_type = str(value or "unknown").lower()
    return page_type if SAFE_KEY.fullmatch(page_type) else "unknown"


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _snapshot_path(source: str, page_type: str) -> Path:
    return DATA_DIR / source / f"{page_type}.json"


def _account_snapshot_path(account_key: str, page_type: str) -> Path:
    return DATA_DIR / "qianchuan_accounts" / account_key / f"{page_type}.json"


def _account_catalog_path() -> Path:
    return DATA_DIR / "qianchuan_accounts.json"


def list_qianchuan_accounts() -> list[dict[str, Any]]:
    path = _account_catalog_path()
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        accounts = value.get("accounts", []) if isinstance(value, dict) else []
        return accounts if isinstance(accounts, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _remember_qianchuan_account(account: dict[str, Any]) -> None:
    key = str(account.get("key") or "").lower()
    if not SAFE_KEY.fullmatch(key):
        return
    label = str(account.get("label") or "千川账号").strip()[:80]
    accounts = {str(item.get("key")): item for item in list_qianchuan_accounts() if isinstance(item, dict)}
    accounts[key] = {"key": key, "label": label, "last_seen": _now_label()}
    _atomic_json_write(_account_catalog_path(), {"accounts": sorted(accounts.values(), key=lambda item: item.get("last_seen", ""), reverse=True)})


def save_data(source: str, data: dict[str, Any]) -> dict[str, Any]:
    if source not in ALLOWED_SOURCES:
        raise ValueError(f"unknown source: {source}")
    if not isinstance(data, dict):
        raise ValueError("data must be an object")

    page_type = _safe_page_type(data.get("page_type"))
    captured_at_ms = int(data.get("captured_at") or data.get("timestamp") or int(time.time() * 1000))
    normalized = {
        **data,
        "schema_version": int(data.get("schema_version") or 1),
        "source": source,
        "page_type": page_type,
        "captured_at": captured_at_ms,
    }
    payload = {
        "source": source,
        "page_type": page_type,
        "data": normalized,
        "timestamp": time.time(),
        "saved_at": _now_label(),
    }
    _atomic_json_write(_snapshot_path(source, page_type), payload)
    if source == "qianchuan" and isinstance(normalized.get("account"), dict):
        account = normalized["account"]
        account_key = str(account.get("key") or "").lower()
        if SAFE_KEY.fullmatch(account_key):
            _atomic_json_write(_account_snapshot_path(account_key, page_type), payload)
            _remember_qianchuan_account(account)
    # Backward-compatible latest snapshot for existing MCP clients.
    _atomic_json_write(DATA_DIR / f"{source}.json", payload)
    _save_history_point(payload)
    logger.info("已保存 %s/%s 快照（质量 %s）", source, page_type, normalized.get("quality", {}).get("score", "-"))
    return payload


def load_data(source: str, page_type: str | None = None, account_key: str | None = None) -> dict[str, Any] | None:
    if source not in ALLOWED_SOURCES:
        return None
    selected_account = account_key
    if source == "qianchuan" and selected_account is None:
        selected_account = str(load_agent_settings().get("qianchuan_account_key") or "")
    if source == "qianchuan" and selected_account:
        safe_account = str(selected_account).lower()
        if not SAFE_KEY.fullmatch(safe_account):
            return None
        if page_type:
            path = _account_snapshot_path(safe_account, _safe_page_type(page_type))
        else:
            account_dir = DATA_DIR / "qianchuan_accounts" / safe_account
            candidates = sorted(account_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True) if account_dir.exists() else []
            path = candidates[0] if candidates else account_dir / "missing.json"
    else:
        path = _snapshot_path(source, _safe_page_type(page_type)) if page_type else DATA_DIR / f"{source}.json"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        logger.exception("读取快照失败: %s", path)
        return None


def list_snapshots() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for source in sorted(ALLOWED_SOURCES):
        source_dir = DATA_DIR / source
        if not source_dir.exists():
            continue
        for path in sorted(source_dir.glob("*.json")):
            snapshot = load_data(source, path.stem, account_key="")
            if not snapshot:
                continue
            data = snapshot.get("data", {})
            quality = data.get("quality", {}) if isinstance(data, dict) else {}
            age = max(0, int(time.time() - float(snapshot.get("timestamp", 0))))
            items.append(
                {
                    "source": source,
                    "page_type": snapshot.get("page_type", path.stem),
                    "saved_at": snapshot.get("saved_at"),
                    "age_seconds": age,
                    "fresh": age < STALE_SECONDS,
                    "title": data.get("title", "") if isinstance(data, dict) else "",
                    "url": data.get("url", "") if isinstance(data, dict) else "",
                    "quality_score": int(quality.get("score", 0) or 0),
                    "metric_count": int(quality.get("metric_count", 0) or 0),
                    "row_count": int(quality.get("row_count", 0) or 0),
                    "warnings": quality.get("warnings", []),
                }
            )
    return sorted(items, key=lambda item: item.get("age_seconds", 10**9))


def _parse_number(value: Any) -> float | None:
    text = str(value or "").replace(",", "").replace("¥", "").replace("￥", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group(0))
    if "万" in text:
        number *= 10_000
    elif "亿" in text:
        number *= 100_000_000
    return number


def _history_dir(source: str, page_type: str) -> Path:
    return DATA_DIR / "history" / source / page_type


def _save_history_point(snapshot: dict[str, Any]) -> None:
    data = snapshot.get("data", {})
    source = str(snapshot.get("source") or "unknown")
    page_type = str(snapshot.get("page_type") or "unknown")
    captured_at = int(data.get("captured_at") or time.time() * 1000)
    point = {
        "source": source,
        "page_type": page_type,
        "captured_at": captured_at,
        "saved_at": snapshot.get("saved_at"),
        "metrics": data.get("metrics", {}),
        "safe_metrics": data.get("safe_metrics", {}),
        "quality": data.get("quality", {}),
        "account_key": str((data.get("account") or {}).get("key") or "") if isinstance(data.get("account"), dict) else "",
    }
    directory = _history_dir(source, page_type)
    _atomic_json_write(directory / f"{captured_at}.json", point)
    retention_days = int(load_agent_settings().get("history_retention_days", 30))
    cutoff_ms = int((time.time() - retention_days * 86400) * 1000)
    paths = sorted(directory.glob("*.json"), key=lambda path: path.name, reverse=True)
    for path in paths[500:]:
        path.unlink(missing_ok=True)
    for path in paths[:500]:
        try:
            if int(path.stem) < cutoff_ms:
                path.unlink(missing_ok=True)
        except ValueError:
            continue


def load_history(source: str | None = None, page_type: str | None = None, days: int = 7) -> list[dict[str, Any]]:
    days = min(90, max(1, int(days)))
    cutoff_ms = int((time.time() - days * 86400) * 1000)
    root = DATA_DIR / "history"
    if not root.exists():
        return []
    patterns = [root / source / page_type] if source and page_type else [root / source] if source else [root]
    points: list[dict[str, Any]] = []
    selected_account = str(load_agent_settings().get("qianchuan_account_key") or "") if source == "qianchuan" else ""
    for base in patterns:
        if not base.exists():
            continue
        for path in base.rglob("*.json"):
            try:
                if int(path.stem) < cutoff_ms:
                    continue
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict) and (not selected_account or value.get("account_key") == selected_account):
                    points.append(value)
            except (ValueError, OSError, json.JSONDecodeError):
                continue
    return sorted(points, key=lambda item: int(item.get("captured_at", 0)))


def build_trends(days: int = 7, source: str | None = None, page_type: str | None = None) -> dict[str, Any]:
    points = load_history(source, page_type, days)
    series: dict[str, list[dict[str, Any]]] = {}
    for point in points:
        metrics = point.get("safe_metrics") or {}
        for label, raw_value in metrics.items():
            value = _parse_number(raw_value)
            if value is None:
                continue
            key = f"{point.get('source')}/{point.get('page_type')}/{label}"
            series.setdefault(key, []).append({"captured_at": point.get("captured_at"), "value": value, "raw": raw_value})
    changes = []
    for key, values in series.items():
        if len(values) < 2:
            continue
        first, last = values[0]["value"], values[-1]["value"]
        delta = last - first
        delta_percent = delta / abs(first) * 100 if first else None
        changes.append({"key": key, "label": key.rsplit("/", 1)[-1], "first": first, "last": last, "delta": delta, "delta_percent": delta_percent, "points": values[-48:]})
    changes.sort(key=lambda item: abs(item["delta_percent"] if item["delta_percent"] is not None else item["delta"]), reverse=True)
    return {"generated_at": _now_label(), "days": days, "history_points": len(points), "series_count": len(series), "changes": changes[:30], "mode": "read_only"}


def _metric_matches(source: str, keywords: tuple[str, ...]) -> list[tuple[dict[str, Any], str, Any]]:
    matches: list[tuple[dict[str, Any], str, Any]] = []
    for item in list_snapshots():
        if item["source"] != source:
            continue
        snapshot = load_data(source, item["page_type"])
        metrics = (snapshot or {}).get("data", {}).get("metrics", {})
        if not isinstance(metrics, dict):
            continue
        for label, value in metrics.items():
            if any(keyword.lower() in str(label).lower() for keyword in keywords):
                matches.append((item, str(label), value))
    return matches


def _age_label(seconds: int) -> str:
    if seconds < 60:
        return "刚刚更新"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


def build_insights() -> dict[str, Any]:
    catalog = list_snapshots()
    coverage = [{**item, "age_label": _age_label(item["age_seconds"])} for item in catalog]
    alerts: list[dict[str, Any]] = []

    for item in catalog:
        if not item["fresh"]:
            alerts.append(
                {
                    "level": "warning",
                    "title": f"{item['source']}/{item['page_type']} 数据已过期",
                    "detail": f"最后更新于 {item['saved_at']}",
                    "action": "打开对应后台页面并点击“同步并诊断”。",
                    "evidence": item,
                }
            )
        if item["quality_score"] < 25:
            alerts.append(
                {
                    "level": "info",
                    "title": f"{item['page_type']} 页面字段不足",
                    "detail": "当前页面可能仍在加载，或页面结构已变化。",
                    "action": "刷新页面后重新同步；若仍失败，请更新页面适配器。",
                    "evidence": item,
                }
            )

    roi_metrics = _metric_matches("qianchuan", ("roi", "支付roi", "成交roi"))
    for item, label, value in roi_metrics[:3]:
        roi = _parse_number(value)
        if roi is not None and roi < 1:
            alerts.append(
                {
                    "level": "high",
                    "title": f"千川 {label} 低于 1",
                    "detail": f"当前页面显示 {label} = {value}。",
                    "action": "先核对统计周期和归因口径，再检查高消耗低成交计划；不要直接批量提价。",
                    "evidence": {"source": "qianchuan", "page_type": item["page_type"], "label": label, "value": value},
                }
            )

    refund_metrics = _metric_matches("doudian", ("退款率", "退货率"))
    for item, label, value in refund_metrics[:3]:
        rate = _parse_number(value)
        if rate is not None and rate > 20:
            alerts.append(
                {
                    "level": "warning",
                    "title": f"{label} 偏高",
                    "detail": f"当前页面显示 {value}。",
                    "action": "按商品和退款原因下钻，优先处理尺码、描述不符和质量类问题。",
                    "evidence": {"source": "doudian", "page_type": item["page_type"], "label": label, "value": value},
                }
            )

    inventory_metrics = _metric_matches("doudian", ("库存", "可售库存"))
    for item, label, value in inventory_metrics[:5]:
        inventory = _parse_number(value)
        if inventory is not None and 0 <= inventory <= 10:
            alerts.append(
                {
                    "level": "warning",
                    "title": "发现低库存指标",
                    "detail": f"{label} = {value}。",
                    "action": "核对在投商品库存，避免有消耗但无法持续成交。",
                    "evidence": {"source": "doudian", "page_type": item["page_type"], "label": label, "value": value},
                }
            )

    present = {(item["source"], item["page_type"]) for item in catalog}
    recommended_pages = [
        ("doudian", "overview", "打开抖店经营首页，补齐经营概览"),
        ("doudian", "orders", "打开订单管理，补齐订单履约数据"),
        ("doudian", "products", "打开商品管理，补齐商品与库存数据"),
        ("qianchuan", "campaigns", "打开千川推广管理，补齐计划数据"),
        ("qianchuan", "report", "打开千川数据报表，补齐消耗与 ROI"),
    ]
    missing = [message for source, page_type, message in recommended_pages if (source, page_type) not in present]
    if missing:
        alerts.append(
            {
                "level": "info",
                "title": f"还有 {len(missing)} 类核心页面未同步",
                "detail": "；".join(missing[:3]),
                "action": "依次打开所需页面，每个页面只需同步一次即可进入本地目录。",
            }
        )

    fresh_count = sum(1 for item in catalog if item["fresh"])
    if not catalog:
        headline = "尚未收到经营数据"
        summary = "请打开已登录的抖店或千川后台，然后点击扩展中的“立即同步”。"
    elif alerts and alerts[0].get("level") == "high":
        headline = "今天先处理高优先级投放异常"
        summary = f"已覆盖 {len(catalog)} 类页面，其中 {fresh_count} 类数据在 10 分钟内更新。建议先核对证据，再执行调整。"
    else:
        headline = "经营数据链路已建立"
        summary = f"已覆盖 {len(catalog)} 类页面，其中 {fresh_count} 类数据为最新。当前建议以补齐数据和人工核对为主。"

    return {
        "generated_at": _now_label(),
        "headline": headline,
        "summary": summary,
        "coverage": coverage,
        "alerts": alerts[:10],
        "safety": {
            "mode": "read_only",
            "privacy": "masked_by_default",
            "note": "诊断来自当前网页快照，不等同于官方 API；所有建议需结合后台口径核对。",
        },
    }


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
    next_settings["history_retention_days"] = min(365, max(1, int(next_settings["history_retention_days"])))
    account_key = str(next_settings.get("qianchuan_account_key") or "").lower()
    if account_key and not SAFE_KEY.fullmatch(account_key):
        raise ValueError("invalid qianchuan_account_key")
    next_settings["qianchuan_account_key"] = account_key
    _atomic_json_write(_settings_path(), next_settings)
    return next_settings


def _table_records(source: str, page_types: set[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in list_snapshots():
        if item["source"] != source or item["page_type"] not in page_types:
            continue
        snapshot = load_data(source, item["page_type"])
        tables = (snapshot or {}).get("data", {}).get("tables", [])
        if not isinstance(tables, list):
            continue
        canonical_headers: list[str] = []
        for table_index, table in enumerate(tables):
            if not isinstance(table, dict):
                continue
            headers = [str(value).strip() for value in table.get("headers", [])]
            rows = table.get("rows", [])
            if not isinstance(rows, list):
                continue
            header_like = bool(headers) and sum("\n" not in header and len(header) <= 40 for header in headers) >= max(2, len(headers) // 2)
            if header_like:
                canonical_headers = headers
            elif canonical_headers and headers and len(headers) == len(canonical_headers):
                # Legacy snapshots treated the first data row as headers when a
                # virtualized body table was separate from its header table.
                rows = [headers, *rows]
                headers = canonical_headers
            elif canonical_headers and not headers:
                headers = canonical_headers
            if not headers:
                continue
            for row_index, row in enumerate(rows):
                if not isinstance(row, list):
                    continue
                values = [str(value).strip() for value in row]
                record = {headers[index]: values[index] if index < len(values) else "" for index in range(len(headers))}
                records.append(
                    {
                        "source": source,
                        "page_type": item["page_type"],
                        "quality_score": item["quality_score"],
                        "table_index": table_index,
                        "row_index": row_index,
                        "record": record,
                    }
                )
    return records


def _pick(record: dict[str, Any], keywords: tuple[str, ...]) -> tuple[str, Any] | tuple[None, None]:
    for label, value in record.items():
        normalized = str(label).lower().replace(" ", "")
        if any(keyword.lower().replace(" ", "") in normalized for keyword in keywords):
            return str(label), value
    return None, None


def _evidence_value(record: dict[str, Any], keywords: tuple[str, ...]) -> float | None:
    _, value = _pick(record, keywords)
    return _parse_number(value)


def _extract_labeled_number(record: dict[str, Any], label: str) -> float | None:
    pattern = re.compile(rf"{re.escape(label)}\s*[:：]?\s*\n?\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
    for value in record.values():
        match = pattern.search(str(value))
        if match:
            return float(match.group(1))
    return None


def _clean_entity_name(value: Any, fallback: str) -> str:
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    ignored = {"扶持中", "投放中", "商品", "素材", "保", "审核建议"}
    candidates = [
        line for line in lines
        if line not in ignored and not line.startswith("ID：") and not line.startswith("ID:") and not line.isdigit()
    ]
    return (max(candidates, key=len) if candidates else fallback)[:100]


def _plan_workbench_fields(item: dict[str, Any], task_states: dict[str, Any]) -> dict[str, Any]:
    action_type = str(item.get("action_type") or "")
    evidence = item.get("evidence") or {}
    roi = evidence.get("roi")
    roi_target = evidence.get("roi_target")
    ctr = evidence.get("ctr")
    orders = evidence.get("orders")
    definitions = {
        "stop_loss": {
            "diagnosis": "有点击无成交" if ctr and not orders else "高消耗未转化",
            "judgment": "继续消耗的边际风险已高于继续观察的价值，应先止损再排查素材、人群和商品承接。",
            "adjustment_range": "预算下调 30%，或暂停新增消耗；任何资金动作均由投手人工确认。",
            "observation_window": "调整后观察 2 小时或 1 个完整转化窗口。",
            "acceptance": f"出现有效成交，且 ROI 恢复到 {float(roi_target or 0) * 0.8:g} 以上；否则继续止损。",
        },
        "reduce_budget": {
            "diagnosis": "ROI 明显低于目标",
            "judgment": "当前消耗已达到判断门槛，低效计划继续原预算运行会放大亏损。",
            "adjustment_range": "单次预算建议下调 20%，不要同时修改出价、素材和人群。",
            "observation_window": "调整后观察 2 小时或 1 个完整转化窗口。",
            "acceptance": f"ROI 至少恢复到 {float(roi_target or 0) * 0.8:g}，且成交成本不继续上升。",
        },
        "optimize": {
            "diagnosis": "素材点击不足" if ctr is not None and ctr < 1 else "ROI 待改善",
            "judgment": "数据尚未达到强制止损条件，但当前效率不足以支持放量，应先修复转化瓶颈。",
            "adjustment_range": "预算保持不变；一次只替换 1 组素材或优化 1 个承接环节。",
            "observation_window": "新素材累计 100 次点击或运行 2 小时后复盘。",
            "acceptance": f"点击率改善且 ROI 达到目标 {float(roi_target or 0):g}；未改善则进入止损评估。",
        },
        "scale_cautiously": {
            "diagnosis": "表现稳定，可谨慎放量",
            "judgment": "当前 ROI 和成交样本达到放量条件，但仍需控制单次调整幅度，避免打乱模型。",
            "adjustment_range": "单次预算增加 10%–15%，一个观察窗口内只调整一次。",
            "observation_window": "放量后观察 2–4 小时或 1 个完整转化窗口。",
            "acceptance": f"ROI 保持在目标 {float(roi_target or 0):g} 以上，成交量增长且成本未明显上升。",
        },
        "inspect_plans": {
            "diagnosis": "账户汇总异常，待定位计划",
            "judgment": "只有账户汇总数据，无法安全定位到具体计划，不应直接批量调整。",
            "adjustment_range": "暂不调整预算；先同步计划列表并锁定异常计划。",
            "observation_window": "计划明细同步完成后立即重新诊断。",
            "acceptance": "定位到具体计划，并补齐消耗、ROI、成交和素材证据。",
        },
        "hold_and_observe": {
            "diagnosis": "账户表现稳定，继续观察",
            "judgment": "汇总表现达到目标，但计划级证据不足，暂不执行批量放量。",
            "adjustment_range": "预算保持不变，补齐计划明细后再判断。",
            "observation_window": "下一个完整转化窗口。",
            "acceptance": "计划级 ROI、成交和消耗数据完整，并确认无异常计划。",
        },
    }
    fields = definitions.get(action_type, {
        "diagnosis": "计划需要人工复核",
        "judgment": "当前证据不足以自动形成明确调整结论。",
        "adjustment_range": "暂不修改预算或出价。",
        "observation_window": "补齐数据后重新诊断。",
        "acceptance": "消耗、ROI、成交与素材证据完整。",
    })
    title = f"{item.get('plan') or '千川计划'} · {fields['diagnosis']}"
    task_id = hashlib.sha256(f"投放运营|{title}".encode("utf-8")).hexdigest()[:16]
    task_state = task_states.get(task_id, {})
    return {
        **fields,
        "found": str(item.get("reason") or "当前计划数据异常"),
        "action": str(item.get("suggestion") or "请回到千川后台核对。"),
        "owner": "投放运营",
        "workbench_title": title,
        "task_id": task_id,
        "task_status": task_state.get("status", "todo"),
        "task_updated_at": task_state.get("updated_at"),
        "current_roi": roi,
    }


def build_plan_recommendations(settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    settings = settings or load_agent_settings()
    roi_target = float(settings["roi_target"])
    min_spend = float(settings["min_spend_for_action"])
    results: list[dict[str, Any]] = []
    records = _table_records("doudian", {"qianchuan_campaigns", "qianchuan_live", "qianchuan_report"})
    records.extend(_table_records("qianchuan", {"campaigns", "qianchuan_live", "report"}))

    for entry in records:
        record = entry["record"]
        _, plan_value = _pick(record, ("计划名称", "计划", "项目名称", "广告组", "单元名称", "抖音号"))
        plan_lines = [line.strip() for line in str(plan_value or "").splitlines() if line.strip() and line.strip() not in {"设置直播规划", "素材"}]
        if plan_lines and plan_lines[0] == "直播大屏" and len(plan_lines) > 1:
            plan = f"直播大屏 · {plan_lines[1]}"[:100]
        else:
            plan = (plan_lines[0] if plan_lines else f"第 {entry['row_index'] + 1} 行计划")[:100]
        if re.match(r"^共\s*\d+\s*(?:条计划|个抖音号)", plan):
            continue
        spend = _evidence_value(record, ("消耗", "花费", "支出"))
        roi = _evidence_value(record, ("支付roi", "成交roi", "roi"))
        orders = _evidence_value(record, ("成交订单", "支付订单", "成交数", "转化数"))
        ctr = _evidence_value(record, ("点击率", "ctr"))
        if spend is None and roi is None:
            continue
        _, status_value = _pick(record, ("投放状态", "计划状态", "状态"))
        if spend == 0 and "暂停" in str(status_value or ""):
            continue

        plan_roi_target = _extract_labeled_number(record, "ROI目标")
        effective_roi_target = plan_roi_target or roi_target
        evidence = {
            "spend": spend,
            "roi": roi,
            "roi_target": effective_roi_target,
            "orders": orders,
            "ctr": ctr,
            "page_type": entry["page_type"],
        }
        confidence = "high" if entry["quality_score"] >= 70 and spend is not None and roi is not None else "medium"
        base = {
            "id": f"{entry['page_type']}-{entry['table_index']}-{entry['row_index']}",
            "plan": plan,
            "evidence": evidence,
            "confidence": confidence,
            "guardrail": "仅生成建议；执行前请核对统计周期、归因口径和当日预算。",
        }

        if spend is not None and spend >= min_spend and (orders == 0 or orders is None and roi == 0):
            results.append(
                {
                    **base,
                    "level": "high",
                    "action_type": "stop_loss",
                    "suggestion": "先降预算 30% 或暂停新增消耗，检查素材、人群和商品承接后再恢复。",
                    "reason": f"消耗已达到 {spend:g}，但当前未观察到成交。",
                }
            )
        elif roi is not None and spend is not None and spend >= min_spend and roi < effective_roi_target * 0.8:
            results.append(
                {
                    **base,
                    "level": "high",
                    "action_type": "reduce_budget",
                    "suggestion": "建议先降预算 20%，保留观察窗口；优先替换低点击素材并核对商品转化。",
                    "reason": f"ROI {roi:g} 明显低于目标 {effective_roi_target:g}，且消耗已达到判断门槛。",
                }
            )
        elif roi is not None and roi < effective_roi_target:
            reason = f"ROI {roi:g} 低于目标 {effective_roi_target:g}，暂不适合放量。"
            suggestion = "预算保持不变，先优化素材点击率与商品承接；达到目标后再逐级放量。"
            if ctr is not None and ctr < 1:
                suggestion = "预算保持不变，优先更换前 3 秒表达、封面和卖点；不要先提高出价。"
                reason += f" 当前点击率为 {ctr:g}。"
            results.append({**base, "level": "warning", "action_type": "optimize", "suggestion": suggestion, "reason": reason})
        elif roi is not None and roi >= effective_roi_target and (orders or 0) >= 3:
            results.append(
                {
                    **base,
                    "level": "opportunity",
                    "action_type": "scale_cautiously",
                    "suggestion": "可尝试增加预算 10%–15%，每次只调一次，并观察一个完整转化窗口。",
                    "reason": f"ROI {roi:g} 达到目标 {effective_roi_target:g}，且已有 {orders:g} 个成交。",
                }
            )

    if not results:
        roi_metrics = _metric_matches("qianchuan", ("roi", "支付roi", "成交roi"))
        spend_metrics = _metric_matches("qianchuan", ("消耗", "花费"))
        if roi_metrics:
            item, label, value = roi_metrics[0]
            roi = _parse_number(value)
            spend = _parse_number(spend_metrics[0][2]) if spend_metrics else None
            if roi is not None:
                results.append(
                    {
                        "id": "account-summary",
                        "plan": "账户汇总",
                        "level": "warning" if roi < roi_target else "opportunity",
                        "action_type": "inspect_plans" if roi < roi_target else "hold_and_observe",
                        "suggestion": "打开千川计划列表同步明细，定位具体计划后再调整预算。",
                        "reason": f"当前汇总 {label} 为 {value}，计划级证据尚不完整。",
                        "evidence": {"roi": roi, "spend": spend, "page_type": item["page_type"]},
                        "confidence": "low",
                        "guardrail": "没有计划明细时不建议执行批量调价。",
                    }
                )

    priority = {"high": 0, "warning": 1, "opportunity": 2, "info": 3}
    ordered = sorted(results, key=lambda item: (priority.get(item["level"], 9), -(item["evidence"].get("spend") or 0)))
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in ordered:
        unique.setdefault((item["plan"], item["action_type"]), item)
    task_states = load_task_states()
    return [{**item, **_plan_workbench_fields(item, task_states)} for item in list(unique.values())[:20]]


def build_qianchuan_creative_analysis(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Analyze Qianchuan video-library rows for live-stream acquisition work."""
    settings = settings or load_agent_settings()
    roi_target = float(settings["roi_target"])
    min_spend = float(settings["min_spend_for_action"])
    videos: list[dict[str, Any]] = []
    # Include legacy `campaigns` because v2.5.x misclassified the real video
    # library route as a campaign-management page.
    records = _table_records("qianchuan", {"video_library", "materials", "campaigns"})
    for entry in records:
        record = entry["record"]
        _, raw_name = _pick(record, ("视频", "素材名称", "创意名称", "视频名称"))
        _, assessment = _pick(record, ("素材评估", "素材状态", "评估"))
        if raw_name is None or not any(keyword in "|".join(record) for keyword in ("视频", "素材评估", "时长", "创作者声明")):
            continue
        name = _clean_entity_name(raw_name, f"第 {entry['row_index'] + 1} 条视频")
        spend = _evidence_value(record, ("消耗", "花费"))
        roi = _evidence_value(record, ("支付roi", "成交roi", "roi"))
        orders = _evidence_value(record, ("成交订单", "支付订单", "转化数"))
        impressions = _evidence_value(record, ("展示", "曝光"))
        clicks = _evidence_value(record, ("点击数", "点击量"))
        ctr = _evidence_value(record, ("点击率", "ctr"))
        _, tags = _pick(record, ("标签",))
        _, source = _pick(record, ("来源",))
        _, duration = _pick(record, ("时长",))
        assessment_text = str(assessment or "")
        evidence = {
            "spend": spend,
            "roi": roi,
            "orders": orders,
            "impressions": impressions,
            "clicks": clicks,
            "ctr": ctr,
            "assessment": assessment_text[:80],
            "tags": str(tags or "")[:80],
            "source": str(source or "")[:80],
            "duration": str(duration or "")[:40],
        }
        if spend is not None and spend >= min_spend and (orders == 0 or roi == 0):
            level, status = "high", "高消耗低转化"
            suggestion = "暂停继续复制该视频，先复盘前 3 秒、直播利益点和进房后承接；修改后用小预算重新测试。"
        elif roi is not None and roi >= roi_target and (orders or 0) >= 3:
            level, status = "opportunity", "可复制放量"
            suggestion = "保留原素材继续投放，并拆出同钩子、不同卖点或不同主播口播的变体，小步扩量验证。"
        elif any(keyword in assessment_text for keyword in ("优质", "高潜", "跑量")):
            level, status = "opportunity", "高潜素材"
            suggestion = "优先进入下一轮直播引流测试，补齐消耗、进房和成交数据后再决定放量。"
        elif spend == 0:
            level, status = "warning", "尚未测试"
            suggestion = "放入小预算素材测试组，统一人群、出价和时段后比较点击、进房与成交。"
        else:
            level, status = "info", "观察中"
            suggestion = "继续观察消耗、点击、进房和成交；数据不足时不要仅凭播放量判断素材。"
        videos.append(
            {
                "id": f"creative-{entry['table_index']}-{entry['row_index']}",
                "name": name,
                "level": level,
                "status": status,
                "suggestion": suggestion,
                "evidence": evidence,
                "confidence": "high" if entry["quality_score"] >= 70 and spend is not None else "medium",
                "guardrail": "只生成素材建议，不上传、删除或修改千川视频。",
            }
        )

    risky = [item for item in videos if item["level"] == "high"]
    opportunities = [item for item in videos if item["level"] == "opportunity"]
    untested = [item for item in videos if item["status"] == "尚未测试"]
    spending = [item for item in videos if (item["evidence"].get("spend") or 0) > 0]
    measured = [item for item in videos if item["evidence"].get("roi") is not None or item["evidence"].get("ctr") is not None]
    recommendations: list[dict[str, Any]] = []
    if not videos:
        recommendations.append({"level": "info", "owner": "投放运营", "title": "同步千川视频库", "action": "登录巨量千川，打开素材工具中的视频库后点击同步或重新巡查。", "acceptance": "视频库出现素材数量、消耗和素材评估。", "evidence": "当前没有可识别的视频库表格。"})
    else:
        if risky:
            recommendations.append({"level": "high", "owner": "投放运营", "title": f"先处理 {len(risky)} 条高消耗低转化视频", "action": "停止继续复制低效素材，逐条复盘前 3 秒钩子、核心卖点、直播利益点和进房承接。", "acceptance": "低效素材不再新增无效消耗，改版素材完成小预算复测。", "evidence": f"视频库识别到 {len(risky)} 条达到消耗门槛但无成交或 ROI 为 0 的素材。"})
        if len(videos) < 3:
            recommendations.append({"level": "warning", "owner": "直播运营", "title": "直播引流素材储备不足", "action": "至少补齐开场钩子、商品卖点、直播利益点三类视频，再用相同投放条件横向测试。", "acceptance": "三类素材均有可比较的点击、进房和成交数据。", "evidence": f"当前视频库仅识别到 {len(videos)} 条素材。"})
        if untested and len(untested) >= max(2, len(videos) // 2):
            recommendations.append({"level": "warning", "owner": "投放运营", "title": "建立素材小预算测试矩阵", "action": "把未测试素材按钩子、卖点和场景分组，统一人群、出价、时段与预算，避免不同变量混测。", "acceptance": "每条候选素材都取得首轮消耗、点击和进房数据。", "evidence": f"{len(untested)}/{len(videos)} 条素材尚未获得消耗。"})
        if opportunities:
            recommendations.append({"level": "opportunity", "owner": "直播运营", "title": f"复用 {len(opportunities)} 条高潜素材结构", "action": "保留有效钩子，分别替换卖点、主播口播或直播利益点，形成可持续素材变体。", "acceptance": "变体素材达到原素材点击或进房效率，并至少有一条形成成交。", "evidence": f"视频库识别到 {len(opportunities)} 条高潜或达到 ROI 目标的素材。"})
        if len(measured) < len(videos):
            recommendations.append({"level": "info", "owner": "投放运营", "title": "补齐视频到直播成交链路", "action": "在千川报表中补充展示、点击、进房、商品点击、成交和 ROI，避免只按消耗或素材评估做判断。", "acceptance": "主要在投视频都能关联到点击、进房和成交指标。", "evidence": f"仅 {len(measured)}/{len(videos)} 条视频包含 ROI 或点击率字段。"})

    priority = {"high": 0, "warning": 1, "opportunity": 2, "info": 3}
    videos.sort(key=lambda item: (priority.get(item["level"], 9), -(item["evidence"].get("spend") or 0)))
    return {
        "generated_at": _now_label(),
        "data_status": "ready" if videos else "missing",
        "summary": {
            "total_videos": len(videos),
            "spending_videos": len(spending),
            "untested_videos": len(untested),
            "risky_videos": len(risky),
            "high_potential_videos": len(opportunities),
            "total_spend": round(sum(item["evidence"].get("spend") or 0 for item in videos), 2),
        },
        "videos": videos[:30],
        "recommendations": recommendations,
        "mode": "read_only",
    }


def build_inventory_alerts(settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    settings = settings or load_agent_settings()
    low = int(settings["low_inventory_threshold"])
    critical = int(settings["critical_inventory_threshold"])
    days_warning = float(settings["inventory_days_warning"])
    results: list[dict[str, Any]] = []

    for entry in _table_records("doudian", {"inventory", "products"}):
        record = entry["record"]
        _, product_value = _pick(record, ("商品名称", "商品", "sku名称", "规格名称"))
        _, sku_value = _pick(record, ("sku编码", "商家编码", "规格编码", "sku"))
        stock = _evidence_value(record, ("可售库存", "现货库存", "库存数量", "库存"))
        daily_sales = _evidence_value(record, ("日均销量", "近1日销量", "昨日销量"))
        seven_day_sales = _evidence_value(record, ("近7日销量", "7日销量"))
        if daily_sales is None and seven_day_sales is not None:
            daily_sales = seven_day_sales / 7
        if stock is None:
            continue
        days_of_cover = stock / daily_sales if daily_sales and daily_sales > 0 else None
        product = str(product_value or f"第 {entry['row_index'] + 1} 行商品")[:100]
        evidence = {"stock": stock, "daily_sales": daily_sales, "days_of_cover": days_of_cover, "page_type": entry["page_type"]}
        base = {
            "id": f"{entry['page_type']}-{entry['table_index']}-{entry['row_index']}",
            "product": product,
            "sku": str(sku_value or "")[:80],
            "evidence": evidence,
        }
        if stock <= 0:
            results.append({**base, "level": "high", "title": "已缺货", "suggestion": "立即暂停该商品继续放量，并核对补货时间。"})
        elif stock <= critical:
            results.append({**base, "level": "high", "title": "库存极低", "suggestion": f"库存仅 {stock:g}，优先补货；补货确认前不要扩大千川消耗。"})
        elif days_of_cover is not None and days_of_cover <= days_warning:
            results.append({**base, "level": "warning", "title": "预计即将售罄", "suggestion": f"按当前销量约可售 {days_of_cover:.1f} 天，建议补货或降低投放强度。"})
        elif stock <= low:
            results.append({**base, "level": "warning", "title": "低库存", "suggestion": f"库存 {stock:g}，请核对在投计划和补货周期。"})

    priority = {"high": 0, "warning": 1, "info": 2}
    return sorted(results, key=lambda item: (priority.get(item["level"], 9), item["evidence"]["stock"]))[:30]


def _safe_snapshot_metrics(source: str, page_types: set[str]) -> tuple[dict[str, Any], list[str], dict[str, Any] | None]:
    metrics: dict[str, Any] = {}
    signals: list[str] = []
    newest: dict[str, Any] | None = None
    for item in list_snapshots():
        if item["source"] != source or item["page_type"] not in page_types:
            continue
        data = (load_data(source, item["page_type"]) or {}).get("data", {})
        for key, value in (data.get("safe_metrics") or {}).items():
            metrics[str(key)] = value
        for signal in data.get("signals") or []:
            if signal not in signals:
                signals.append(str(signal))
        if newest is None or item["age_seconds"] < newest["age_seconds"]:
            newest = item
    return metrics, signals, newest


def build_shelf_analysis() -> dict[str, Any]:
    metrics, signals, snapshot = _safe_snapshot_metrics("doudian", {"shelf"})
    exposure = _parse_number(metrics.get("曝光人数"))
    clicks = _parse_number(metrics.get("点击人数"))
    buyers = _parse_number(metrics.get("成交人数"))
    orders = _parse_number(metrics.get("订单量"))
    payment = _parse_number(metrics.get("用户支付金额"))
    click_rate = clicks / exposure * 100 if exposure and clicks is not None else None
    actions: list[dict[str, Any]] = []
    if any("不良暗示" in signal for signal in signals):
        actions.append({"level": "high", "owner": "货架运营", "title": "先修复商品主图合规", "action": "替换存在不良暗示的主图并重新检查审核状态。", "acceptance": "违规提示消失，商品恢复正常分发资格。", "evidence": "页面明确提示商品主图存在不良暗示。"})
    if exposure and clicks and not buyers:
        actions.append({"level": "warning", "owner": "货架运营", "title": "点击后没有成交，先修承接", "action": "检查详情页首屏、价格权益、评价信任和规格选择；修复前不优先加流量。", "acceptance": "成交人数大于 0，点击成交率连续两个观察周期改善。", "evidence": f"曝光 {exposure:g}、点击 {clicks:g}、成交人数 {buyers or 0:g}，推算点击率 {click_rate:.1f}%。"})
    if any("猜你喜欢未入选" in signal for signal in signals):
        actions.append({"level": "warning", "owner": "货架运营", "title": "恢复猜你喜欢入选资格", "action": "按后台诊断逐项修复商品信息、主图和基础销量门槛。", "acceptance": "未入选商品数降为 0。", "evidence": next(signal for signal in signals if "猜你喜欢未入选" in signal)})
    if not snapshot:
        actions.append({"level": "info", "owner": "货架运营", "title": "缺少货架数据", "action": "打开商城运营概览并同步。", "acceptance": "出现曝光、点击、成交漏斗。", "evidence": "尚无货架页面快照。"})
    return {"generated_at": _now_label(), "data_status": "ready" if snapshot else "missing", "snapshot": snapshot, "metrics": metrics, "funnel": {"exposure": exposure, "clicks": clicks, "buyers": buyers, "orders": orders, "payment": payment, "click_rate": click_rate}, "signals": signals, "recommendations": actions, "mode": "read_only"}


def build_live_analysis() -> dict[str, Any]:
    metrics, signals, snapshot = _safe_snapshot_metrics("doudian", {"live", "qianchuan_live"})
    q_metrics, q_signals, q_snapshot = _safe_snapshot_metrics("qianchuan", {"qianchuan_live", "live_dashboard"})
    metrics.update({key: value for key, value in q_metrics.items() if key not in metrics})
    signals.extend(signal for signal in q_signals if signal not in signals)
    live_records = _table_records("qianchuan", {"qianchuan_live", "live_dashboard"})
    record = live_records[0]["record"] if live_records else {}
    sessions = _parse_number(metrics.get("直播场次"))
    impressions = _parse_number(metrics.get("展示次数")) or _evidence_value(record, ("展示", "曝光"))
    views = _parse_number(metrics.get("进入直播间人数") or metrics.get("直播间观看人数") or metrics.get("观看次数")) or _evidence_value(record, ("进入直播间", "观看人数"))
    product_clicks = _parse_number(metrics.get("直播间商品点击人数") or metrics.get("商品点击人数")) or _evidence_value(record, ("商品点击人数", "商品点击"))
    orders = _parse_number(metrics.get("整体成交订单数") or metrics.get("直播间成交订单数") or metrics.get("成交订单数")) or _evidence_value(record, ("整体成交订单", "净成交订单", "成交订单"))
    gmv = _parse_number(metrics.get("整体成交金额(元)") or metrics.get("直播间成交金额") or metrics.get("成交金额") or metrics.get("用户支付金额")) or _evidence_value(record, ("整体成交金额", "净成交金额", "成交金额"))
    spend = _parse_number(metrics.get("整体消耗(元)") or metrics.get("视频消耗") or metrics.get("投放消耗（店铺被投）")) or _evidence_value(record, ("整体消耗", "消耗", "花费"))
    roi = _parse_number(metrics.get("整体支付ROI") or metrics.get("净成交ROI")) or _evidence_value(record, ("整体支付roi", "净成交roi", "roi"))
    refund_rate = _parse_number(metrics.get("1小时内退款率")) or _evidence_value(record, ("退款率",))
    enter_rate = views / impressions * 100 if impressions and views is not None else None
    product_click_rate = product_clicks / views * 100 if views and product_clicks is not None else None
    conversion_rate = orders / product_clicks * 100 if product_clicks and orders is not None else None
    actions: list[dict[str, Any]] = []
    if sessions == 0 or any("当前待直播计划 0" in signal for signal in signals):
        actions.append({"level": "warning", "owner": "直播运营", "title": "先排一场基准直播", "action": "建立开播计划，确定主播、货盘、脚本和至少一个主推品；先跑出完整漏斗再谈 ROI 优化。", "acceptance": "直播场次大于 0，并取得观看、商品点击和成交三段数据。", "evidence": f"直播场次 {sessions or 0:g}，当前未形成可分析的直播样本。"})
    elif spend and impressions and not views:
        actions.append({"level": "high", "owner": "投放运营", "title": "视频有曝光但没有进房", "action": "优先更换前 3 秒钩子、封面文案和直播利益点，不要先提高出价。", "acceptance": "进房人数大于 0，进房率连续两个测试周期改善。", "evidence": f"展示 {impressions:g}，进房 {views or 0:g}，已消耗 {spend:g}。"})
    elif views and not product_clicks:
        actions.append({"level": "warning", "owner": "直播运营", "title": "有人看但不点商品", "action": "优化开场钩子、商品讲解顺序和购物车引导。", "acceptance": "商品点击率连续两个场次提升。", "evidence": f"观看 {views:g}，商品点击 {product_clicks or 0:g}。"})
    elif product_clicks and not orders:
        actions.append({"level": "warning", "owner": "直播运营", "title": "商品有点击但未成交", "action": "检查价格机制、库存规格、信任证明和逼单节奏。", "acceptance": "成交订单数大于 0。", "evidence": f"商品点击 {product_clicks:g}，成交订单 {orders or 0:g}。"})
    if spend and not orders:
        actions.insert(0, {"level": "high", "owner": "投放运营", "title": "直播投放先止损", "action": "降低或暂停新增消耗，核查直播间承接后再恢复。", "acceptance": "恢复投放前取得自然流量成交或明确修复项。", "evidence": f"直播投放消耗 {spend:g}，成交订单 {orders or 0:g}。"})
    if not snapshot and not q_snapshot:
        actions.append({"level": "info", "owner": "直播运营", "title": "缺少直播大屏数据", "action": "打开店铺直播或千川直播大屏并同步。", "acceptance": "出现直播场次与观看转化指标。", "evidence": "尚无直播快照。"})
    if refund_rate is not None and refund_rate >= 20:
        actions.append({"level": "warning", "owner": "直播运营", "title": "成交后退款偏高", "action": "核对主播承诺、商品预期、尺码说明和售后原因，避免素材与直播间过度承诺。", "acceptance": "退款率回落并且净成交 ROI 改善。", "evidence": f"当前退款率 {refund_rate:g}%。"})
    return {"generated_at": _now_label(), "data_status": "ready" if snapshot or q_snapshot else "missing", "snapshot": snapshot or q_snapshot, "metrics": metrics, "funnel": {"sessions": sessions, "impressions": impressions, "views": views, "enter_rate": enter_rate, "product_clicks": product_clicks, "product_click_rate": product_click_rate, "orders": orders, "conversion_rate": conversion_rate, "gmv": gmv, "spend": spend, "roi": roi, "refund_rate": refund_rate}, "signals": signals, "recommendations": actions, "mode": "read_only"}


def build_ops_manager() -> dict[str, Any]:
    shelf, live = build_shelf_analysis(), build_live_analysis()
    plans, inventory = build_plan_recommendations(), build_inventory_alerts()
    creative = build_qianchuan_creative_analysis()
    tasks = [*shelf["recommendations"], *live["recommendations"], *creative["recommendations"]]
    for item in plans[:5]:
        tasks.append({
            "level": item["level"],
            "owner": "投放运营",
            "title": item["workbench_title"],
            "action": item["action"],
            "acceptance": item["acceptance"],
            "evidence": item["found"],
            "impact": item["adjustment_range"],
            "observation_window": item["observation_window"],
        })
    for item in inventory[:3]:
        tasks.append({"level": item["level"], "owner": "商品运营", "title": f"{item['product']} · {item['title']}", "action": item["suggestion"], "acceptance": "补货或投放限制已人工确认。", "evidence": f"当前库存 {item['evidence']['stock']:g}。"})
    priority = {"high": 0, "warning": 1, "opportunity": 2, "info": 3}
    tasks.sort(key=lambda item: (priority.get(item["level"], 9), 0 if "合规" in item["title"] or "主图" in item["title"] else 1))
    states = load_task_states()
    for item in tasks:
        raw_key = f"{item['owner']}|{item['title']}"
        item["id"] = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
        item["status"] = states.get(item["id"], {}).get("status", "todo")
        item["updated_at"] = states.get(item["id"], {}).get("updated_at")
        item["confidence"] = "high" if item["level"] in {"high", "opportunity"} else "medium"
        item["impact"] = "风险优先" if item["level"] == "high" else "增长机会" if item["level"] == "opportunity" else "影响转化"
    unique_tasks: dict[str, dict[str, Any]] = {}
    for item in tasks:
        unique_tasks.setdefault(item["id"], item)
    tasks = list(unique_tasks.values())
    active = [item for item in tasks if item["status"] != "done"]
    must_do = [item for item in active if item["level"] != "opportunity"][:3]
    opportunities = [item for item in active if item["level"] == "opportunity"][:3]
    progress = {status: sum(1 for item in tasks if item["status"] == status) for status in ("todo", "doing", "observing", "done")}
    return {
        "generated_at": _now_label(),
        "headline": "先处理风险与转化瓶颈，再安排放量",
        "must_do": must_do,
        "growth_opportunities": opportunities,
        "today_top_actions": active[:10],
        "all_tasks": tasks,
        "progress": {**progress, "total": len(tasks), "completed_rate": round(progress["done"] / len(tasks) * 100) if tasks else 0},
        "roles": ["运营总管", "货架运营", "直播运营", "投放运营", "商品运营"],
        "modules": {"shelf": {"status": shelf["data_status"], "action_count": len(shelf["recommendations"])}, "live": {"status": live["data_status"], "action_count": len(live["recommendations"])}, "qianchuan": {"action_count": len(plans)}, "creative": {"status": creative["data_status"], "action_count": len(creative["recommendations"])}, "inventory": {"alert_count": len(inventory)}},
        "mode": "read_only",
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
    states = load_task_states()
    states[task_id] = {"status": status, "updated_at": _now_label()}
    _atomic_json_write(_task_states_path(), states)
    return {"task_id": task_id, **states[task_id]}


def _scan_status_path() -> Path:
    return DATA_DIR / "scan_status.json"


def save_scan_status(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("scan status must be an object")
    allowed = {"status", "reason", "account_key", "started_at", "finished_at", "current", "index", "total", "success", "failed", "low_quality", "results", "error"}
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


def build_action_center() -> dict[str, Any]:
    settings = load_agent_settings()
    plans = build_plan_recommendations(settings)
    inventory = build_inventory_alerts(settings)
    creative = build_qianchuan_creative_analysis(settings)
    return {
        "generated_at": _now_label(),
        "settings": settings,
        "plan_recommendations": plans,
        "inventory_alerts": inventory,
        "shelf_analysis": build_shelf_analysis(),
        "live_analysis": build_live_analysis(),
        "creative_analysis": creative,
        "summary": {
            "plan_actions": len(plans),
            "high_risk_plans": sum(1 for item in plans if item["level"] == "high"),
            "inventory_alerts": len(inventory),
            "critical_inventory": sum(1 for item in inventory if item["level"] == "high"),
            "creative_actions": len(creative["recommendations"]),
        },
        "mode": "read_only",
    }


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


def generate_daily_report(report_date: str | None = None) -> dict[str, Any]:
    report_date = report_date or time.strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date):
        raise ValueError("report_date must be YYYY-MM-DD")
    insights = build_insights()
    action_center = build_action_center()
    ops = build_ops_manager()
    scan = load_scan_status()
    report_path = _reports_dir() / f"{report_date}.md"
    lines = [
        f"# 店策 Agent 每日经营报告 - {report_date}",
        "",
        f"> 生成时间：{_now_label()}｜模式：只读建议",
        "",
        "## 今日结论",
        "",
        f"- {insights['headline']}",
        f"- {insights['summary']}",
        f"- 已同步页面：{len(insights['coverage'])}；千川调整项：{len(action_center['plan_recommendations'])}；库存预警：{len(action_center['inventory_alerts'])}",
        f"- 自动巡检：{scan.get('status', 'idle')}；成功 {scan.get('success', 0)} 页，失败 {scan.get('failed', 0)} 页，低质量 {scan.get('low_quality', 0)} 页。",
        "",
        "## 运营总管今日任务",
        "",
    ]
    for index, item in enumerate(ops["today_top_actions"][:8], 1):
        lines.extend([f"{index}. **[{item['owner']}] {item['title']}**：{item['action']}", f"   - 依据：{item['evidence']}｜验收：{item['acceptance']}"])
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
        for index, item in enumerate(plans[:10], 1):
            lines.extend([f"{index}. **{item['plan']}**：{item['suggestion']}", f"   - 依据：{item['reason']}"])
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
        for index, item in enumerate(inventory[:15], 1):
            lines.append(f"{index}. **{item['product']}**：{item['title']}；{item['suggestion']}")
    else:
        lines.append("- 暂无库存预警，或尚未同步商品/库存页面。")
    lines.extend(["", "## 其他优先事项", ""])
    for index, item in enumerate(insights["alerts"][:8], 1):
        lines.append(f"{index}. **{item['title']}**：{item.get('action') or item.get('detail') or ''}")
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
    _atomic_text_write(report_path, "\n".join(lines))
    _cleanup_old_reports(int(action_center["settings"]["report_retention_days"]))
    return {
        "date": report_date,
        "generated_at": _now_label(),
        "path": str(report_path),
        "headline": insights["headline"],
        "summary": action_center["summary"],
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
                generate_daily_report(now.strftime("%Y-%m-%d"))
                logger.info("已生成每日经营报告: %s", target)
        except Exception:
            logger.exception("生成定时日报失败")


class Handler(BaseHTTPRequestHandler):
    server_version = "DianAgent/2.7.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug(fmt, *args)

    def _cors(self) -> None:
        origin = self.headers.get("Origin", "")
        if origin.startswith("chrome-extension://"):
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

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Dian-Agent")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed_url = urlparse(self.path)
        path = unquote(parsed_url.path).rstrip("/") or "/"
        query = parse_qs(parsed_url.query)
        if path == "/health":
            catalog = list_snapshots()
            self._json(
                {
                    "status": "ok",
                    "version": "2.7.0",
                    "mode": "read_only",
                    "snapshot_count": len(catalog),
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
        if path == "/catalog":
            self._json({"snapshots": list_snapshots()})
            return
        if path in {"/insights", "/brief"}:
            self._json(build_insights())
            return
        if path in {"/action-center", "/recommendations"}:
            self._json(build_action_center())
            return
        if path == "/shelf-analysis":
            self._json(build_shelf_analysis())
            return
        if path == "/live-analysis":
            self._json(build_live_analysis())
            return
        if path == "/qianchuan-creative-analysis":
            self._json(build_qianchuan_creative_analysis())
            return
        if path == "/qianchuan-accounts":
            self._json({"accounts": list_qianchuan_accounts(), "selected_account_key": load_agent_settings().get("qianchuan_account_key", "")})
            return
        if path == "/ops-manager":
            self._json(build_ops_manager())
            return
        if path == "/tasks":
            self._json({"states": load_task_states(), "tasks": build_ops_manager()["all_tasks"]})
            return
        if path == "/scan-status":
            self._json(load_scan_status())
            return
        if path == "/trends":
            self._json(build_trends(int(query.get("days", ["7"])[0]), query.get("source", [None])[0], query.get("page_type", [None])[0]))
            return
        if path == "/settings":
            self._json(load_agent_settings())
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
                self._json({"ok": True, "source": source, "page_type": saved["page_type"]})
                return
            if path == "/settings":
                self._json({"ok": True, "settings": save_agent_settings(payload)})
                return
            if path == "/reports/generate":
                self._json({"ok": True, "report": generate_daily_report(payload.get("date"))})
                return
            if path == "/tasks/update":
                self._json({"ok": True, "task": update_task_state(str(payload.get("task_id") or ""), str(payload.get("status") or ""))})
                return
            if path == "/scan-status":
                self._json({"ok": True, "scan": save_scan_status(payload)})
                return
            self._json({"error": "not_found"}, 404)
        except (json.JSONDecodeError, ValueError, TypeError) as error:
            self._json({"error": str(error)}, 400)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    stop_event = threading.Event()
    scheduler = threading.Thread(target=_daily_report_scheduler, args=(stop_event,), daemon=True)
    scheduler.start()
    logger.info("店策 Agent 本地服务已启动: http://127.0.0.1:%d", PORT)
    logger.info("只读模式；数据目录: %s", DATA_DIR)
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
