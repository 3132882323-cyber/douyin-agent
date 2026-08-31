"""Truthful, user-facing assessment of how far the local product can work today.

The assessment is deliberately derived from existing runtime evidence.  It
does not turn a proposal, local binding, or preflight into a successful write.
"""

from __future__ import annotations

from typing import Any


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return min(1_000_000, max(0, int(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _stage(
    stage_id: str,
    label: str,
    status: str,
    summary: str,
    evidence: list[str],
    target: str,
    action_label: str,
) -> dict[str, Any]:
    return {
        "id": stage_id,
        "label": label,
        "status": status,
        "status_label": {
            "ready": "可用",
            "attention": "需复核",
            "blocked": "待补齐",
            "inactive": "尚未发生",
        }.get(status, "待检查"),
        "summary": summary,
        "evidence": [str(item)[:180] for item in evidence if str(item).strip()][:5],
        "target": target,
        "action_label": action_label,
    }


def build_product_capability_diagnostic(
    *,
    onboarding: Any = None,
    operation_context: Any = None,
    plan_console: Any = None,
    automation_readiness: Any = None,
    preflight: Any = None,
    effectiveness: Any = None,
) -> dict[str, Any]:
    """Return one evidence-backed product journey without overstating execution."""

    onboarding_data = _mapping(onboarding)
    context = _mapping(operation_context)
    plans = _mapping(plan_console)
    automation = _mapping(automation_readiness)
    preflight_data = _mapping(preflight)
    effect = _mapping(effectiveness)

    store_confirmed = onboarding_data.get("store_confirmed") is True
    analysis_allowed = context.get("analysis_allowed") is True
    context_state = str(context.get("state") or "blocked")
    data_ready = bool(store_confirmed and analysis_allowed)
    # A review-state context is still sufficient for read-only diagnosis.  It
    # must not keep the journey stuck on data setup once analysis is allowed;
    # any review caveat remains visible in the evidence below.
    data_status = "ready" if data_ready else "blocked"
    data_stage = _stage(
        "data",
        "店铺与数据",
        data_status,
        "巡店诊断可以使用" if data_ready else "还不能可靠判断当前店铺",
        [
            "当前店铺已确认" if store_confirmed else "尚未确认当前店铺",
            f"经营上下文：{str(context.get('state_label') or context_state)}",
        ],
        "scan-card" if store_confirmed else "connection-guide",
        "开始巡店" if store_confirmed else "确认店铺",
    )

    plan_summary = _mapping(plans.get("summary"))
    plan_total = _count(plan_summary.get("total"))
    plan_rows = _items(plans.get("rows"))
    if not plan_total and plan_rows:
        plan_total = len(plan_rows)
    identity_ready = _count(plan_summary.get("binding_ready"))
    stale_plans = _count(plan_summary.get("stale"))
    plan_fresh = str(plans.get("freshness_status") or "missing") == "fresh" and stale_plans == 0
    if not data_ready:
        plan_status = "inactive"
    elif plan_total == 0:
        plan_status = "attention"
    elif not plan_fresh:
        plan_status = "blocked"
    else:
        plan_status = "ready"
    plan_stage = _stage(
        "plans",
        "计划可见",
        plan_status,
        (
            f"已读取 {plan_total} 个计划，但快照过期或缺少时效证据"
            if plan_total and not plan_fresh
            else f"已读取 {plan_total} 个计划"
            if plan_total
            else "尚未读取到千川计划"
        ),
        [
            f"计划总数：{plan_total}",
            f"过期计划：{stale_plans}" if stale_plans else "当前计划快照未标记过期",
            "当前仅做只读分析" if plans.get("platform_write_enabled") is not True else "平台写能力已连接",
        ],
        "promotion-plan-center",
        "查看计划" if plan_total else "同步千川计划",
    )

    identity_blockers = []
    for row in plan_rows:
        if not isinstance(row, dict):
            continue
        for blocker in _items(row.get("binding_blockers")):
            value = str(blocker).strip()
            if value and value not in identity_blockers:
                identity_blockers.append(value)
    if plan_status != "ready":
        identity_status = "inactive"
    elif identity_ready > 0:
        identity_status = "ready"
    else:
        identity_status = "blocked"
    identity_stage = _stage(
        "identity",
        "计划身份",
        identity_status,
        f"{identity_ready} 个计划具备本地绑定条件" if identity_ready else "计划身份还不能进入托管",
        [
            f"具备账户、计划 ID、模式和质量证据：{identity_ready} 个",
            *(f"待补齐：{item}" for item in identity_blockers[:3]),
            "本地绑定不等于平台授权，也不会自动修改千川",
        ],
        "promotion-plan-center",
        "补齐计划身份",
    )

    automation_summary = _mapping(automation.get("summary"))
    preflight_ready = _count(automation_summary.get("preflight_ready"))
    confirmable = _count(automation_summary.get("confirmable"))
    manual_only = _count(automation_summary.get("manual_only"))
    blocked_actions = _count(automation_summary.get("blocked"))
    action_blockers = []
    for item in _items(automation.get("items")):
        if not isinstance(item, dict):
            continue
        for blocker in _items(item.get("blocked_reasons")):
            value = str(blocker.get("message") if isinstance(blocker, dict) else blocker).strip()
            if value and value not in action_blockers:
                action_blockers.append(value)
    preflight_state = str(preflight_data.get("state") or "idle")
    preflight_session = _mapping(preflight_data.get("session"))
    selected_store = _mapping(context.get("selected_store"))
    current_store_key = str(
        onboarding_data.get("store_key") or selected_store.get("key") or ""
    ).strip().lower()
    current_account_keys = {
        str(row.get("account_key") or "").strip().lower()
        for row in plan_rows
        if isinstance(row, dict) and str(row.get("account_key") or "").strip()
    }
    preflight_store_key = str(preflight_session.get("store_key") or "").strip().lower()
    preflight_account_key = str(preflight_session.get("account_key") or "").strip().lower()
    preflight_scope_matches = bool(
        current_store_key
        and preflight_store_key == current_store_key
        and preflight_account_key
        and preflight_account_key in current_account_keys
    )
    active_preflight = bool(
        preflight_state in {"awaiting_reread", "ready_for_final_confirmation", "authorized"}
        and preflight_scope_matches
    )
    if identity_status != "ready":
        action_status = "inactive"
    elif preflight_ready > 0 or active_preflight:
        action_status = "ready"
    elif confirmable > 0:
        action_status = "attention"
    else:
        action_status = "blocked"
    action_stage = _stage(
        "action",
        "动作与授权",
        action_status,
        "已有动作可进入受监督执行前检查" if action_status == "ready" else "目前只能建议、预演或人工处理",
        [
            f"可进执行前检查：{preflight_ready} 项",
            f"待确认：{confirmable} 项；阻塞：{blocked_actions} 项；仅人工：{manual_only} 项",
            *(f"门禁：{item}" for item in action_blockers[:2]),
            "真实写入必须再次读取页面、人工授权并通过短时效门禁",
        ],
        "automation-section",
        "查看受控执行",
    )

    effect_summary = _mapping(effect.get("summary"))
    current_accounts = {
        str(row.get("account_key") or "")
        for row in plan_rows if isinstance(row, dict) and str(row.get("account_key") or "")
    }
    effect_items = [
        item for item in _items(effect.get("items"))
        if isinstance(item, dict) and (not current_accounts or str(item.get("account_key") or "") in current_accounts)
    ]
    if _items(effect.get("items")):
        effect_total = len(effect_items)
        evaluated_items = [item for item in effect_items if str(item.get("status") or "") in {"effective", "ineffective", "rollback_recommended"}]
        evaluated = len(evaluated_items)
        effective = sum(str(item.get("status") or "") == "effective" for item in evaluated_items)
    else:
        effect_total = _count(effect_summary.get("total"))
        evaluated = _count(effect_summary.get("evaluated"))
        effective = _count(effect_summary.get("effective"))
    if action_status != "ready":
        readback_status = "inactive"
    elif evaluated > 0:
        readback_status = "ready"
    else:
        readback_status = "attention"
    readback_stage = _stage(
        "readback",
        "结果回读",
        readback_status,
        f"已复核 {evaluated} 个动作结果" if evaluated else "还没有可验证的执行结果",
        [
            f"进入观察窗口：{effect_total} 项；完成评价：{evaluated} 项",
            f"当前有效：{effective} 项" if evaluated else "动作执行后必须等待观察窗口并重新读取数据",
        ],
        "promotion-operation-log",
        "查看操作与回读",
    )

    stages = [data_stage, plan_stage, identity_stage, action_stage, readback_stage]
    ready_count = sum(item["status"] == "ready" for item in stages)
    if data_status == "ready" and plan_status == "ready" and identity_status == "ready" and action_status == "ready" and readback_status == "ready":
        level, level_label = "closed_loop", "已经形成结果回读"
    elif action_status == "ready":
        level, level_label = "supervised_ready", "受监督执行准备就绪"
    elif identity_status == "ready":
        level, level_label = "local_management", "本地计划管理可用"
    elif plan_status == "ready":
        level, level_label = "plan_read_only", "投放计划只读可用"
    elif data_ready:
        level, level_label = "diagnosis_ready", "巡店与经营诊断可用"
    else:
        level, level_label = "setup_required", "先完成店铺与数据准备"

    next_stage = next((item for item in stages if item["status"] not in {"ready", "inactive"}), None)
    if next_stage is None:
        next_stage = next((item for item in stages if item["status"] == "inactive"), readback_stage)

    available_now = []
    if data_ready:
        available_now.append("巡店、经营诊断和今日任务")
    if plan_status == "ready":
        available_now.append("千川计划只读筛选与本地预演")
    if identity_status == "ready":
        available_now.append("具备身份的计划可建立本地托管绑定")
    if action_status == "ready":
        available_now.append("受监督单步执行前检查")
    if readback_status == "ready":
        available_now.append("执行效果回读与复盘")

    unavailable = []
    if identity_ready == 0:
        unavailable.append("自动托管尚无通过身份门禁的计划")
    if action_status != "ready":
        unavailable.append("真实投放调整尚未进入短时效人工授权")
    if evaluated == 0:
        unavailable.append("尚无已验证的投放动作结果")

    return {
        "schema_version": 1,
        "level": level,
        "level_label": level_label,
        "ready_count": ready_count,
        "total_stages": len(stages),
        "stages": stages,
        "next_action": {
            "stage_id": next_stage["id"],
            "label": next_stage["action_label"],
            "target": next_stage["target"],
            "reason": next_stage["summary"],
        },
        "truth": {
            "available_now": available_now,
            "not_available_yet": unavailable,
            "execution_enabled": bool(
                active_preflight
                and automation.get("execution_enabled") is True
                and preflight_data.get("execution_enabled") is True
            ),
        },
        "note": "只按当前本机证据判断可用程度；草稿、建议和预演不会被计为真实执行。",
    }


__all__ = ["build_product_capability_diagnostic"]
