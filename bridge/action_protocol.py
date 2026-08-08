"""Safe, deterministic action drafts for future Qianchuan execution.

This module deliberately does not execute browser operations.  It defines the
contract that proposal, policy, confirmation, execution and verification
layers will share when write support is introduced.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

try:
    from .promotion_mode import build_promotion_context
except ImportError:
    from promotion_mode import build_promotion_context

ACTION_SCHEMA_VERSION = 1
ACTION_DRAFT_TTL_SECONDS = 10 * 60
EXECUTABLE_OPERATIONS = {"adjust_budget", "restore_budget", "pause_plan", "adjust_bid", "set_schedule"}
ACTION_STATES = {
    "draft",
    "confirmed",
    "executing",
    "succeeded",
    "verified",
    "failed",
    "cancelled",
    "expired",
    "rolled_back",
}
ACTION_TRANSITIONS = {
    "draft": {"confirmed", "cancelled", "expired"},
    "confirmed": {"executing", "cancelled", "expired"},
    "executing": {"succeeded", "failed"},
    "succeeded": {"verified", "failed", "rolled_back"},
    "verified": {"rolled_back"},
    "failed": {"cancelled"},
    "cancelled": set(),
    "expired": set(),
    "rolled_back": set(),
}


def _canonical_value(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {str(key): _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    return value


def _identity_payload(action: dict[str, Any]) -> dict[str, Any]:
    target = action.get("target_ref") or {}
    change = action.get("change") or {}
    evidence = action.get("evidence_ref") or {}
    return {
        "schema_version": action.get("schema_version"),
        "account_key": target.get("account_key"),
        "target_kind": target.get("kind"),
        "target_id": target.get("id"),
        "target_name": target.get("name"),
        "operation_type": action.get("operation_type"),
        "field": change.get("field"),
        "current_value": change.get("current_value"),
        "target_value": change.get("target_value"),
        "source": evidence.get("source"),
        "page_type": evidence.get("page_type"),
        "captured_at_ms": evidence.get("captured_at_ms"),
        "quality_score": evidence.get("quality_score"),
        "confidence": evidence.get("confidence"),
        "rollback_of_action_id": evidence.get("rollback_of_action_id"),
        "policy": action.get("policy"),
        "blocked_reasons": action.get("blocked_reasons"),
        "can_confirm": action.get("can_confirm"),
        "expires_at_ms": action.get("expires_at_ms"),
        "copy_text": action.get("copy_text"),
        "promotion_context": action.get("promotion_context"),
    }


def action_integrity_hash(action: dict[str, Any]) -> str:
    raw = json.dumps(_canonical_value(_identity_payload(action)), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _block(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def build_action_draft(
    *,
    operation_type: str,
    operation_label: str,
    target_kind: str,
    target_id: str | None,
    target_name: str,
    account_key: str | None,
    account_label: str | None,
    field: str | None,
    current_value: Any,
    target_value: Any,
    source: str,
    page_type: str,
    captured_at_ms: int | None,
    quality_score: int,
    confidence: str,
    evidence: dict[str, Any] | None = None,
    copy_text: str = "",
    promotion_context: dict[str, Any] | str | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Create a policy-checked action draft.

    ``can_confirm`` only means the user may confirm the proposal for the local
    audit trail.  ``execution_enabled`` remains false until a separate,
    verified executor is implemented.
    """

    now_ms = int(now_ms or time.time() * 1000)
    captured_at_ms = int(captured_at_ms or 0)
    account_key = str(account_key or "").strip().lower()
    target_id = str(target_id or "").strip()
    target_name = str(target_name or "").strip()[:120]
    operation_type = str(operation_type or "").strip()
    blocked: list[dict[str, str]] = []

    if operation_type not in EXECUTABLE_OPERATIONS:
        blocked.append(_block("NON_EXECUTABLE_ACTION", "该建议属于运营任务，当前不是可执行的千川资金动作。"))
    if not account_key:
        blocked.append(_block("ACCOUNT_NOT_LOCKED", "未锁定千川账号，不能生成可确认的投放动作。"))
    if not target_id:
        blocked.append(_block("TARGET_ID_MISSING", "缺少计划唯一 ID，仅凭计划名称不能安全执行。"))
    if not target_name:
        blocked.append(_block("TARGET_NAME_MISSING", "缺少计划名称，无法向投手展示明确目标。"))
    if captured_at_ms <= 0:
        blocked.append(_block("CAPTURE_TIME_MISSING", "缺少数据采集时间，请重新同步千川计划。"))
    elif now_ms - captured_at_ms > ACTION_DRAFT_TTL_SECONDS * 1000:
        blocked.append(_block("DATA_STALE", "计划数据已超过 10 分钟，请重新同步后再确认。"))
    if int(quality_score or 0) < 70:
        blocked.append(_block("DATA_QUALITY_LOW", "页面采集质量不足 70 分，暂不允许确认资金动作。"))
    if confidence != "high":
        blocked.append(_block("CONFIDENCE_NOT_HIGH", "当前判断置信度不足，需补齐消耗、ROI 和成交数据。"))

    change_percent: float | None = None
    if operation_type in {"adjust_budget", "adjust_bid", "restore_budget"}:
        if not isinstance(current_value, (int, float)) or float(current_value) <= 0:
            blocked.append(_block("CURRENT_VALUE_MISSING", "缺少可回读的当前数值，禁止生成调价动作。"))
        if not isinstance(target_value, (int, float)) or float(target_value) <= 0:
            blocked.append(_block("TARGET_VALUE_INVALID", "目标数值无效，禁止生成调价动作。"))
        if isinstance(current_value, (int, float)) and float(current_value) > 0 and isinstance(target_value, (int, float)):
            change_percent = round((float(target_value) - float(current_value)) / float(current_value) * 100, 2)
            if operation_type == "restore_budget":
                rollback_id = str((evidence or {}).get("rollback_of_action_id") or "")
                if not rollback_id:
                    blocked.append(_block("ROLLBACK_SOURCE_MISSING", "恢复预算必须绑定已验收的原执行记录。"))
                if change_percent <= 0 or change_percent > 50:
                    blocked.append(_block("ROLLBACK_RANGE_INVALID", "恢复预算只能回到原值，且单次恢复幅度不能超过 50%。"))
            elif change_percent > 15:
                blocked.append(_block("INCREASE_LIMIT_EXCEEDED", "单次增加幅度不能超过 15%。"))
            if operation_type != "restore_budget" and change_percent < -30:
                blocked.append(_block("DECREASE_LIMIT_EXCEEDED", "单次降低幅度不能超过 30%。"))
    elif operation_type == "pause_plan":
        if str(current_value or "") not in {"投放中", "启用", "生效中", "运行中"}:
            blocked.append(_block("CURRENT_STATUS_UNVERIFIED", "未确认计划当前处于投放状态，禁止生成暂停动作。"))
        if str(target_value or "") != "暂停":
            blocked.append(_block("TARGET_STATUS_INVALID", "暂停计划的目标状态必须明确为“暂停”。"))

    risk_level = "high" if operation_type in {"pause_plan", "restore_budget"} or (change_percent or 0) > 0 else "medium"
    action: dict[str, Any] = {
        "schema_version": ACTION_SCHEMA_VERSION,
        "state": "draft",
        "mode": "proposal_only",
        "operation_type": operation_type,
        "operation_label": operation_label,
        "target_ref": {
            "kind": str(target_kind or "qianchuan_plan"),
            "id": target_id,
            "name": target_name,
            "account_key": account_key,
            "account_label": str(account_label or "")[:80],
        },
        "change": {
            "field": field,
            "current_value": current_value,
            "target_value": target_value,
            "change_percent": change_percent,
        },
        "evidence_ref": {
            "source": source,
            "page_type": page_type,
            "captured_at_ms": captured_at_ms,
            "quality_score": int(quality_score or 0),
            "confidence": confidence,
            **(evidence or {}),
        },
        "policy": {
            "risk_level": risk_level,
            "requires_user_confirmation": True,
            "requires_preflight_reread": True,
            "requires_postflight_verification": True,
            "rollback_snapshot_required": True,
            "execution_enabled": False,
            "max_increase_percent": 15,
            "max_decrease_percent": 30,
        },
        "blocked_reasons": blocked,
        "can_confirm": not blocked,
        "can_execute": False,
        "created_at_ms": now_ms,
        "expires_at_ms": captured_at_ms + ACTION_DRAFT_TTL_SECONDS * 1000 if captured_at_ms else now_ms,
        "copy_text": str(copy_text or "")[:500],
        "promotion_context": build_promotion_context(promotion_context),
        # Backward-compatible fields for the existing side-panel renderer.
        "target": target_name,
        "field": field,
        "current_value": current_value,
        "target_value": target_value,
    }
    integrity_hash = action_integrity_hash(action)
    action["integrity_hash"] = integrity_hash
    action["action_id"] = integrity_hash[:24]
    action["idempotency_key"] = f"dian-action-{integrity_hash[:32]}"
    return action


def validate_action_draft(action: dict[str, Any], *, now_ms: int | None = None) -> list[dict[str, str]]:
    now_ms = int(now_ms or time.time() * 1000)
    errors: list[dict[str, str]] = []
    if not isinstance(action, dict):
        return [_block("INVALID_ACTION", "动作草稿格式无效。")]
    if action.get("schema_version") != ACTION_SCHEMA_VERSION:
        errors.append(_block("SCHEMA_VERSION_MISMATCH", "动作协议版本不匹配。"))
    if action.get("state") not in {"draft", "confirmed"}:
        errors.append(_block("INVALID_STATE", "动作当前状态不允许确认。"))
    expected_hash = action_integrity_hash(action)
    if action.get("integrity_hash") != expected_hash:
        errors.append(_block("INTEGRITY_CHECK_FAILED", "动作参数已变化，请重新生成方案。"))
    if action.get("action_id") != expected_hash[:24] or action.get("idempotency_key") != f"dian-action-{expected_hash[:32]}":
        errors.append(_block("ACTION_ID_MISMATCH", "动作编号与参数不一致，请重新生成方案。"))
    if int(action.get("expires_at_ms") or 0) <= now_ms:
        errors.append(_block("ACTION_EXPIRED", "动作草稿已过期，请重新同步并生成方案。"))
    if action.get("blocked_reasons"):
        errors.extend(action["blocked_reasons"])
    if not action.get("can_confirm"):
        errors.append(_block("CONFIRMATION_BLOCKED", "该动作不满足确认条件。"))
    return errors


def assess_automation_readiness(action: dict[str, Any]) -> dict[str, Any]:
    """Classify a proposal for the future supervised executor.

    This is intentionally a readiness assessment, not an execution decision.
    It gives the product and UI a stable way to explain what can move into
    preflight, what still needs user confirmation and what must remain manual.
    """

    if not isinstance(action, dict):
        return {
            "status": "blocked",
            "status_label": "暂时阻止",
            "stage": "qualification",
            "next_step": "重新生成投放建议。",
            "can_enter_preflight": False,
            "execution_enabled": False,
            "blocked_reasons": [_block("INVALID_ACTION", "动作草稿格式无效。")],
        }

    blocked = [item for item in action.get("blocked_reasons", []) if isinstance(item, dict)]
    codes = {str(item.get("code") or "") for item in blocked}
    state = str(action.get("state") or "draft")

    if "NON_EXECUTABLE_ACTION" in codes:
        status = "manual_only"
        label = "仅人工处理"
        stage = "proposal"
        next_step = "该建议不涉及受支持的千川资金动作，保留人工处理。"
    elif blocked:
        status = "blocked"
        label = "暂时阻止"
        stage = "qualification"
        if codes & {"DATA_STALE", "CAPTURE_TIME_MISSING", "DATA_QUALITY_LOW", "CONFIDENCE_NOT_HIGH"}:
            next_step = "重新读取当前千川页面，并补齐高质量消耗、成交和 ROI 数据。"
        elif codes & {"ACCOUNT_NOT_LOCKED", "TARGET_ID_MISSING", "TARGET_NAME_MISSING"}:
            next_step = "锁定正确千川账号，并补齐计划唯一 ID。"
        else:
            next_step = "按阻止原因补齐条件后重新生成方案。"
    elif state == "confirmed":
        status = "preflight_ready"
        label = "可进入执行前检查"
        stage = "preflight"
        next_step = "执行前重新读取页面，核对账号、计划、当前值和授权额度。"
    else:
        status = "confirmable"
        label = "等待人工授权"
        stage = "authorization"
        next_step = "投手确认动作范围后，进入执行前重新读取。"

    return {
        "status": status,
        "status_label": label,
        "stage": stage,
        "next_step": next_step,
        "can_enter_preflight": status == "preflight_ready",
        "execution_enabled": False,
        "blocked_reasons": blocked,
    }


def transition_action(action: dict[str, Any], next_state: str, *, allow_execution: bool = False) -> dict[str, Any]:
    current_state = str(action.get("state") or "")
    if current_state not in ACTION_STATES or next_state not in ACTION_STATES:
        raise ValueError("invalid action state")
    if next_state not in ACTION_TRANSITIONS[current_state]:
        raise ValueError(f"invalid action transition: {current_state} -> {next_state}")
    if next_state == "executing" and not allow_execution:
        raise ValueError("execution is disabled")
    return {**action, "state": next_state, "state_updated_at_ms": int(time.time() * 1000)}
