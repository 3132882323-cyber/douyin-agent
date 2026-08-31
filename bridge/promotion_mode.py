"""Read-only Qianchuan promotion-mode contract and execution guard.

The platform contract for Chengfang is not assumed here.  A mode is accepted
only when the snapshot supplies explicit evidence; missing or conflicting
evidence remains ``unknown`` and therefore cannot enter legacy plan writes.
"""

from __future__ import annotations

import math
from typing import Any

PROMOTION_MODES = frozenset({"standard", "full_domain", "chengfang", "suixintui", "unknown"})
LEGACY_SINGLE_PLAN_OPERATIONS = frozenset({"adjust_budget", "restore_budget", "pause_plan"})
METRIC_DEFINITIONS = frozenset({"pay_roi", "net_revenue_roi", "gross_profit_roi", "unknown"})
COST_FIELDS = ("ad_spend", "commission", "platform_fee", "discount", "refund", "subsidy", "product_cost", "fulfillment_cost")
RESULT_FIELDS = ("pay_amount", "net_revenue", "orders", "contribution_margin", "refund_amount", "inventory_change")
DETERMINISTIC_MAX_FRESHNESS_SECONDS = 30 * 60
DETERMINISTIC_MIN_COMPLETENESS = 0.80


def _finite_number(value: Any) -> int | float | None:
    """Return a real finite JSON number without treating booleans as numbers."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(float(value)) else None


def normalize_promotion_mode(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "standard": "standard",
        "常规": "standard",
        "标准推广": "standard",
        "full_domain": "full_domain",
        "全域": "full_domain",
        "全域推广": "full_domain",
        "chengfang": "chengfang",
        "乘方": "chengfang",
        "suixintui": "suixintui",
        "sui_xin_tui": "suixintui",
        "随心推": "suixintui",
        "随心推推广": "suixintui",
        "小店随心推": "suixintui",
        "千川随心推": "suixintui",
        "巨量千川随心推": "suixintui",
    }
    return aliases.get(normalized, "unknown")


def build_promotion_context(value: Any = None) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    mode = normalize_promotion_mode(raw.get("promotion_mode") if raw else value)
    account = raw.get("account_scope") if isinstance(raw.get("account_scope"), dict) else {}
    evidence = raw.get("promotion_mode_evidence") if isinstance(raw.get("promotion_mode_evidence"), dict) else raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
    quality = raw.get("data_quality") if isinstance(raw.get("data_quality"), dict) else {}
    # A conflict asserted by the collector is first-class evidence.  Do not
    # erase it merely because the normalized evidence object lacks a conflict
    # flag; write guards must fail closed on either representation.
    conflict = (
        evidence.get("conflict") is True
        or str(evidence.get("source") or "") == "conflicting_visible_labels"
        or quality.get("mode_conflict") is True
    )
    if conflict:
        mode = "unknown"
    metric = raw.get("metric_contract") if isinstance(raw.get("metric_contract"), dict) else raw.get("metric") if isinstance(raw.get("metric"), dict) else {}
    metric_definition = str(metric.get("definition") or metric.get("name") or "unknown").strip().lower()
    if metric_definition not in METRIC_DEFINITIONS:
        metric_definition = "unknown"
    costs = raw.get("cost_ledger") if isinstance(raw.get("cost_ledger"), dict) else {}
    results = raw.get("result_ledger") if isinstance(raw.get("result_ledger"), dict) else {}
    normalized_costs = {
        key: finite
        for key in COST_FIELDS
        if (finite := _finite_number(costs.get(key))) is not None
    }
    normalized_results = {
        key: finite
        for key in RESULT_FIELDS
        if (finite := _finite_number(results.get(key))) is not None
    }
    strategy = raw.get("strategy") if isinstance(raw.get("strategy"), dict) else {}
    strategy_id = str(strategy.get("strategy_id") or raw.get("strategy_id") or "")[:128]
    freshness_value = _finite_number(quality.get("freshness_seconds"))
    freshness_provided = freshness_value is not None and (
        quality.get("freshness_provided") is True
        if "freshness_provided" in quality
        else "freshness_seconds" in quality and quality.get("freshness_seconds") is not None
    )
    completeness_value = _finite_number(quality.get("completeness"))
    if completeness_value is None or not 0 <= float(completeness_value) <= 1:
        completeness_value = 0.0
    evidence_captured_at = _finite_number(evidence.get("captured_at_ms"))
    if evidence_captured_at is None or evidence_captured_at < 0:
        evidence_captured_at = 0
    store_id = str(account.get("store_id") or "")[:128]
    account_id = str(account.get("account_id") or "")[:128]
    binding_conflict = account.get("conflict") is True or str(account.get("binding_status") or "") == "conflict"
    metric_version = str(metric.get("version") or "")[:80]
    missing = []
    if mode == "unknown":
        missing.append("promotion_mode")
    if not strategy_id:
        missing.append("strategy_id")
    if metric_definition == "unknown":
        missing.append("metric_definition")
    if not normalized_costs:
        missing.append("cost_ledger")
    if not normalized_results:
        missing.append("result_ledger")
    if not store_id:
        missing.append("store_id")
    if not account_id:
        missing.append("account_id")
    if not metric_version:
        missing.append("metric_contract_version")
    confidence = str(quality.get("confidence") or evidence.get("confidence") or ("conflict" if conflict else "unknown")).lower()
    if confidence not in {"high", "medium", "low", "unknown", "conflict"}:
        confidence = "unknown"
    write_identity_complete = bool(store_id and account_id and strategy_id and metric_version and mode != "unknown" and not binding_conflict)
    metric_contract = {
        "definition": metric_definition,
        "name": str(metric.get("name") or metric_definition)[:80],
        "version": metric_version,
        "numerator": str(metric.get("numerator") or "")[:120],
        "denominator": str(metric.get("denominator") or "")[:120],
        "attribution_window": str(metric.get("attribution_window") or metric.get("period") or "")[:80],
        "refund_policy": str(metric.get("refund_policy") or "")[:120],
        "value": _finite_number(metric.get("value")),
    }
    return {
        "schema_version": 2,
        "promotion_mode": mode,
        "account_scope": {
            "store_id": store_id,
            "account_id": account_id,
            "subject_id": str(account.get("subject_id") or "")[:128],
            "binding_status": str(account.get("binding_status") or "unverified")[:40],
            "conflict": binding_conflict,
        },
        "promotion_mode_evidence": {
            "source": str(evidence.get("source") or "unverified")[:80],
            "label": str(evidence.get("label") or "")[:120],
            "captured_at_ms": int(evidence_captured_at),
            "conflict": conflict,
            "confidence": confidence,
        },
        "strategy_id": strategy_id,
        "strategy": {
            "strategy_id": strategy_id,
            "goal": str(strategy.get("goal") or "")[:120],
            "status": str(strategy.get("status") or "unknown")[:80],
            "total_budget": _finite_number(strategy.get("total_budget")),
            "platform_managed_fields": [str(item)[:80] for item in strategy.get("platform_managed_fields", raw.get("platform_managed_fields", [])) if isinstance(item, str)][:30],
        },
        "metric_contract": metric_contract,
        # Compatibility alias for v1 consumers.
        "metric": {"definition": metric_definition, "label": str(metric.get("label") or metric.get("name") or "")[:80], "value": metric_contract["value"], "period": metric_contract["attribution_window"]},
        "cost_ledger": normalized_costs,
        "result_ledger": normalized_results,
        "platform_managed_fields": [str(item)[:80] for item in strategy.get("platform_managed_fields", raw.get("platform_managed_fields", [])) if isinstance(item, str)][:30],
        "evidence": {
            "source": str(evidence.get("source") or "unverified")[:80],
            "label": str(evidence.get("label") or "")[:120],
            "captured_at_ms": int(evidence_captured_at),
        },
        "data_quality": {
            "confidence": confidence,
            "freshness_seconds": int(freshness_value or 0),
            "freshness_provided": freshness_provided,
            "completeness": round(float(completeness_value), 4),
            "metric_conflict": quality.get("metric_conflict") is True,
            "mode_conflict": conflict,
            "identity_conflict": binding_conflict,
        },
        "missing_fields": missing,
        "data_ready": not missing,
        "write_identity_complete": write_identity_complete,
    }


def assess_deterministic_data_gate(promotion_context: Any = None) -> dict[str, Any]:
    context = build_promotion_context(promotion_context)
    quality = context["data_quality"]
    blockers = []
    freshness = int(quality.get("freshness_seconds") or 0)
    completeness = float(quality.get("completeness") or 0)
    if not context["account_scope"]["store_id"] or not context["account_scope"]["account_id"]:
        blockers.append("ACCOUNT_SCOPE_INCOMPLETE")
    if quality.get("identity_conflict"):
        blockers.append("ACCOUNT_SCOPE_CONFLICT")
    if quality.get("mode_conflict") or quality.get("metric_conflict"):
        blockers.append("DATA_CONTRACT_CONFLICT")
    if not quality.get("freshness_provided") or freshness < 0 or freshness > DETERMINISTIC_MAX_FRESHNESS_SECONDS:
        blockers.append("DATA_STALE_OR_UNTIMED")
    if completeness < DETERMINISTIC_MIN_COMPLETENESS:
        blockers.append("DATA_COMPLETENESS_LOW")
    if context["promotion_mode"] == "unknown":
        blockers.append("PROMOTION_MODE_UNVERIFIED")
    if context["metric_contract"]["definition"] == "unknown" or not context["metric_contract"]["version"]:
        blockers.append("METRIC_CONTRACT_UNVERIFIED")
    return {
        "deterministic_advice_allowed": not blockers,
        "read_only": True,
        "thresholds": {"max_freshness_seconds": DETERMINISTIC_MAX_FRESHNESS_SECONDS, "min_completeness": DETERMINISTIC_MIN_COMPLETENESS},
        "observed": {"freshness_seconds": freshness or None, "completeness": completeness, "confidence": quality.get("confidence")},
        "blocked_reasons": blockers,
    }


def build_chengfang_dashboard_summary(promotion_context: Any = None) -> dict[str, Any]:
    context = build_promotion_context(promotion_context)
    gate = assess_deterministic_data_gate(context)
    evidence = context["promotion_mode_evidence"]
    metric = context["metric_contract"]
    required_profit_costs = {"ad_spend", "commission", "platform_fee", "discount", "refund", "product_cost", "fulfillment_cost"}
    required_profit_results = {"net_revenue"}
    missing_costs = sorted(required_profit_costs - set(context["cost_ledger"]))
    missing_results = sorted(required_profit_results - set(context["result_ledger"]))
    profit_calculable = not missing_costs and not missing_results and metric["definition"] != "unknown"

    def field(value: Any, source: str) -> dict[str, Any]:
        return {"status": "present" if value is not None else "missing", "value": value, "source": source if value is not None else None}

    return {
        "schema_version": 1,
        "mode": {"value": context["promotion_mode"], "confidence": evidence["confidence"], "conflict": evidence["conflict"], "source": evidence["source"], "captured_at_ms": evidence["captured_at_ms"] or None},
        "scope": {**context["account_scope"], "complete": bool(context["account_scope"]["store_id"] and context["account_scope"]["account_id"] and not context["account_scope"]["conflict"])},
        "strategy": {"strategy_id": field(context["strategy_id"] or None, "snapshot"), "total_budget": field(context["strategy"]["total_budget"], "snapshot")},
        "metric_contract": metric,
        "metrics": {
            "ad_spend": field(context["cost_ledger"].get("ad_spend"), "cost_ledger"),
            "net_revenue": field(context["result_ledger"].get("net_revenue"), "result_ledger"),
            "contribution_margin": field(context["result_ledger"].get("contribution_margin"), "result_ledger"),
        },
        "profit_safety": {"calculable": profit_calculable, "missing_cost_fields": missing_costs, "missing_result_fields": missing_results, "note": "字段完整且指标口径确认后才能计算；真实 0 保留为 present。"},
        "data_quality_gate": gate,
        "read_only": True,
        "next_step": "补齐页面合同、身份作用域、指标口径及利润账本。" if not gate["deterministic_advice_allowed"] else "数据质量达到确定性只读建议门槛，写操作仍保持关闭。",
    }


def legacy_execution_guard(
    operation_type: Any,
    promotion_context: Any,
    *,
    expected_account_key: Any = None,
) -> dict[str, Any]:
    context = build_promotion_context(promotion_context)
    data_gate = assess_deterministic_data_gate(context)
    operation = str(operation_type or "").strip()
    mode = context["promotion_mode"]
    expected_account = str(expected_account_key or "").strip().lower()
    context_account = str(context.get("account_scope", {}).get("account_id") or "").strip().lower()
    account_mismatch = bool(expected_account and context_account and expected_account != context_account)
    identity_blocked = not context["write_identity_complete"]
    data_blocked = data_gate["deterministic_advice_allowed"] is not True
    blocked = account_mismatch or (
        operation in LEGACY_SINGLE_PLAN_OPERATIONS
        and (mode in {"chengfang", "suixintui", "unknown"} or identity_blocked or data_blocked)
    )
    if account_mismatch:
        code = "ACTION_CONTEXT_ACCOUNT_MISMATCH"
        reason = "Action target account does not match the promotion context account."
    elif mode == "chengfang":
        code = "UNSUPPORTED_FOR_CHENGFANG"
        reason = "乘方由平台协同管理，旧单计划预算、暂停和恢复执行器已停用。"
    elif mode == "suixintui":
        code = "UNSUPPORTED_FOR_SUIXINTUI"
        reason = "随心推尚未建立独立的执行、回读和回滚合同，禁止复用标准、全域或乘方执行器。"
    elif mode == "unknown":
        code = "PROMOTION_MODE_UNVERIFIED"
        reason = "尚未确认当前投放模式，禁止使用旧单计划预算、暂停和恢复执行器。"
    elif identity_blocked:
        code = "PROMOTION_SCOPE_UNVERIFIED"
        reason = "店铺、千川账户、策略或指标合同绑定不完整或存在冲突，禁止执行投放写操作。"
    elif data_blocked:
        code = str((data_gate.get("blocked_reasons") or ["DATA_QUALITY_UNVERIFIED"])[0])
        reason = {
            "ACCOUNT_SCOPE_CONFLICT": "店铺或千川账户作用域存在冲突，禁止执行投放写操作。",
            "DATA_CONTRACT_CONFLICT": "投放模式或指标口径存在冲突，禁止执行投放写操作。",
            "DATA_STALE_OR_UNTIMED": "计划证据已过期或缺少可信时间，请重新同步后再执行。",
            "DATA_COMPLETENESS_LOW": "计划证据完整度不足，禁止执行投放写操作。",
            "METRIC_CONTRACT_UNVERIFIED": "指标口径或版本尚未验真，禁止执行投放写操作。",
        }.get(code, "当前数据尚未通过确定性证据门禁，禁止执行投放写操作。")
    else:
        code = "ALLOWED"
        reason = "当前投放模式允许进入既有受监督检查。"
    return {
        "allowed": not blocked,
        "code": code if blocked else "ALLOWED",
        "reason": reason,
        "promotion_mode": mode,
        "operation_type": operation,
        "data_gate": data_gate,
    }


def build_chengfang_readiness(promotion_context: Any = None) -> dict[str, Any]:
    context = build_promotion_context(promotion_context)
    metric_ready = context["metric_contract"]["definition"] != "unknown" and bool(context["metric_contract"]["version"])
    profit_fields = {"ad_spend", "commission", "platform_fee", "discount", "refund", "product_cost", "fulfillment_cost"}
    missing_profit_fields = sorted(profit_fields - set(context["cost_ledger"]))
    blockers = [
        "官方写能力已发现，但当前应用权限、账户白名单与字段合同尚未受控验收。",
        "未建立乘方策略级执行、回读和回滚协议。",
    ]
    if context["promotion_mode"] == "unknown":
        blockers.insert(0, "当前投放模式缺少可信证据或存在冲突。")
    if not metric_ready:
        blockers.append("综合 ROI 指标口径或版本未确认。")
    if missing_profit_fields:
        blockers.append("利润账本字段不完整，不能计算可信贡献毛利。")
    return {
        "promotion_context": context,
        "summary": {
            "promotion_mode": context["promotion_mode"],
            "mode_confidence": context["data_quality"]["confidence"],
            "mode_conflict": context["data_quality"]["mode_conflict"],
            "metric_status": "confirmed" if metric_ready else "unverified",
            "profit_status": "complete" if not missing_profit_fields else "incomplete",
            "missing_profit_fields": missing_profit_fields,
            "data_quality": context["data_quality"],
        },
        "capabilities": {
            "read_only_mode_detection": True,
            "strategy_summary": bool(context["strategy_id"]),
            "metric_interpretation": metric_ready,
            "cost_ledger": bool(context["cost_ledger"]),
            "legacy_single_plan_write": context["promotion_mode"] in {"standard", "full_domain"},
            "chengfang_write": False,
            "official_api_adapter": False,
        },
        "ready_for_chengfang_write": False,
        "status": "read_only" if context["promotion_mode"] == "chengfang" else "waiting_for_mode",
        "blockers": blockers,
        "next_step": "同步真实乘方页面并确认店铺、账户、策略和指标口径。" if context["promotion_mode"] == "unknown" else "补齐指标口径、成本与结果账本，继续只读经营诊断。",
    }
