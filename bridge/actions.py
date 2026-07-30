"""Action audit, shadow verification, and supervised execution preflight."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from action_protocol import build_action_draft, transition_action, validate_action_draft
from insights import _clean_entity_name, _entity_identifier, _evidence_value
from storage import _atomic_json_write, _now_label, load_data
import state

logger = logging.getLogger("dian-agent-http")
_state_lock = state._state_lock


def _data_dir() -> Path:
    return state.DATA_DIR


def _facade():
    import http_receiver as facade
    return facade


def load_agent_settings(*args, **kwargs):
    return _facade().load_agent_settings(*args, **kwargs)


def _action_audit_path() -> Path:
    return _data_dir() / "action_audit.json"


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
    return _data_dir() / "shadow_execution.json"


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
    return _data_dir() / "execution_preflight.json"


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


