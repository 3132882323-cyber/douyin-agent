"""Read-only Qianchuan promotion-mode contract and execution guard.

The platform contract for Chengfang is not assumed here.  A mode is accepted
only when the snapshot supplies explicit evidence; missing or conflicting
evidence remains ``unknown`` and therefore cannot enter legacy plan writes.
"""

from __future__ import annotations

from typing import Any

PROMOTION_MODES = frozenset({"standard", "full_domain", "chengfang", "unknown"})
LEGACY_SINGLE_PLAN_OPERATIONS = frozenset({"adjust_budget", "restore_budget", "pause_plan"})
METRIC_DEFINITIONS = frozenset({"pay_roi", "net_revenue_roi", "gross_profit_roi", "unknown"})


def normalize_promotion_mode(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "standard": "standard",
        "常规": "standard",
        "full_domain": "full_domain",
        "全域": "full_domain",
        "chengfang": "chengfang",
        "乘方": "chengfang",
    }
    return aliases.get(normalized, "unknown")


def build_promotion_context(value: Any = None) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    mode = normalize_promotion_mode(raw.get("promotion_mode") if raw else value)
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
    metric = raw.get("metric") if isinstance(raw.get("metric"), dict) else {}
    metric_definition = str(metric.get("definition") or "unknown").strip().lower()
    if metric_definition not in METRIC_DEFINITIONS:
        metric_definition = "unknown"
    costs = raw.get("cost_ledger") if isinstance(raw.get("cost_ledger"), dict) else {}
    normalized_costs = {
        key: costs.get(key)
        for key in ("ad_spend", "commission", "platform_fee", "discount", "refund", "subsidy")
        if isinstance(costs.get(key), (int, float))
    }
    missing = []
    if mode == "unknown":
        missing.append("promotion_mode")
    if not raw.get("strategy_id"):
        missing.append("strategy_id")
    if metric_definition == "unknown":
        missing.append("metric_definition")
    if not normalized_costs:
        missing.append("cost_ledger")
    return {
        "schema_version": 1,
        "promotion_mode": mode,
        "strategy_id": str(raw.get("strategy_id") or "")[:128],
        "metric": {
            "definition": metric_definition,
            "label": str(metric.get("label") or "")[:80],
            "value": metric.get("value") if isinstance(metric.get("value"), (int, float)) else None,
            "period": str(metric.get("period") or "")[:80],
        },
        "cost_ledger": normalized_costs,
        "platform_managed_fields": [str(item)[:80] for item in raw.get("platform_managed_fields", []) if isinstance(item, str)][:30],
        "evidence": {
            "source": str(evidence.get("source") or "unverified")[:80],
            "label": str(evidence.get("label") or "")[:120],
            "captured_at_ms": int(evidence.get("captured_at_ms") or 0),
        },
        "missing_fields": missing,
        "data_ready": not missing,
    }


def legacy_execution_guard(operation_type: Any, promotion_context: Any) -> dict[str, Any]:
    context = build_promotion_context(promotion_context)
    operation = str(operation_type or "").strip()
    mode = context["promotion_mode"]
    blocked = operation in LEGACY_SINGLE_PLAN_OPERATIONS and mode in {"chengfang", "unknown"}
    if mode == "chengfang":
        code = "UNSUPPORTED_FOR_CHENGFANG"
        reason = "乘方由平台协同管理，旧单计划预算、暂停和恢复执行器已停用。"
    elif mode == "unknown":
        code = "PROMOTION_MODE_UNVERIFIED"
        reason = "尚未确认当前投放模式，禁止使用旧单计划预算、暂停和恢复执行器。"
    else:
        code = "ALLOWED"
        reason = "当前投放模式允许进入既有受监督检查。"
    return {
        "allowed": not blocked,
        "code": code if blocked else "ALLOWED",
        "reason": reason,
        "promotion_mode": mode,
        "operation_type": operation,
    }


def build_chengfang_readiness(promotion_context: Any = None) -> dict[str, Any]:
    context = build_promotion_context(promotion_context)
    return {
        "promotion_context": context,
        "capabilities": {
            "read_only_mode_detection": True,
            "strategy_summary": bool(context["strategy_id"]),
            "metric_interpretation": context["metric"]["definition"] != "unknown",
            "cost_ledger": bool(context["cost_ledger"]),
            "legacy_single_plan_write": context["promotion_mode"] in {"standard", "full_domain"},
            "chengfang_write": False,
            "official_api_adapter": False,
        },
        "ready_for_chengfang_write": False,
        "status": "read_only" if context["promotion_mode"] == "chengfang" else "waiting_for_mode",
        "blockers": [
            "未取得或验证乘方官方写接口合同。",
            "未建立乘方策略级执行、回读和回滚协议。",
        ],
    }
