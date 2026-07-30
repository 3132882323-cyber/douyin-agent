"""Browser vs official API snapshot reconciliation."""

from __future__ import annotations

import re
from typing import Any


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


def _normalize_plan_name(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    return text[:120]


def _pick(record: dict[str, Any], keywords: tuple[str, ...]) -> Any:
    for label, value in record.items():
        normalized = str(label).lower().replace(" ", "")
        if any(keyword.lower().replace(" ", "") in normalized for keyword in keywords):
            return value
    return None


def official_plan_index(plans_snapshot: dict[str, Any] | None) -> dict[str, dict[str, float | None]]:
    """Map normalized plan name -> budget/spend from an official_api plans snapshot."""
    if not isinstance(plans_snapshot, dict):
        return {}
    data = plans_snapshot.get("data") if isinstance(plans_snapshot.get("data"), dict) else plans_snapshot
    if not isinstance(data, dict):
        return {}
    channel = str(data.get("channel") or "")
    if channel and channel != "official_api":
        return {}
    index: dict[str, dict[str, float | None]] = {}
    tables = data.get("tables") if isinstance(data.get("tables"), list) else []
    for table in tables:
        if not isinstance(table, dict):
            continue
        headers = [str(value).strip() for value in table.get("headers", [])]
        rows = table.get("rows", [])
        if not isinstance(rows, list) or not headers:
            continue
        for row in rows:
            if not isinstance(row, list):
                continue
            record = {headers[i]: (row[i] if i < len(row) else "") for i in range(len(headers))}
            name = _normalize_plan_name(_pick(record, ("计划名称", "计划", "项目名称", "广告组")))
            if not name:
                continue
            index[name] = {
                "budget": _parse_number(_pick(record, ("日预算", "每日预算", "预算上限", "预算"))),
                "spend": _parse_number(_pick(record, ("消耗", "花费", "支出", "广告消耗"))),
            }
    return index


def reconcile_plan_against_official(
    *,
    plan_name: str,
    browser_budget: float | None,
    browser_spend: float | None,
    official_index: dict[str, dict[str, float | None]],
    relative_threshold: float = 0.2,
    absolute_budget_threshold: float = 50.0,
    absolute_spend_threshold: float = 50.0,
) -> dict[str, Any]:
    """Compare one browser plan row with official API values.

    Returns a reconcile report. ``confidence_cap`` is ``medium`` when a material
    mismatch is found; otherwise ``None`` (caller keeps existing confidence).
    """
    result: dict[str, Any] = {
        "available": bool(official_index),
        "matched": False,
        "confidence_cap": None,
        "reasons": [],
        "browser_budget": browser_budget,
        "browser_spend": browser_spend,
        "official_budget": None,
        "official_spend": None,
        "budget_delta_pct": None,
        "spend_delta_pct": None,
    }
    if not official_index:
        return result
    official = official_index.get(_normalize_plan_name(plan_name))
    if not official:
        result["reasons"].append("official_plan_not_found")
        return result
    result["matched"] = True
    result["official_budget"] = official.get("budget")
    result["official_spend"] = official.get("spend")

    def _mismatch(browser: float | None, official_value: float | None, absolute: float) -> tuple[bool, float | None]:
        if browser is None or official_value is None:
            return False, None
        delta = abs(browser - official_value)
        base = max(abs(official_value), abs(browser), 1.0)
        pct = delta / base
        return delta >= absolute or pct >= relative_threshold, round(pct * 100, 2)

    budget_mismatch, budget_pct = _mismatch(browser_budget, official.get("budget"), absolute_budget_threshold)
    spend_mismatch, spend_pct = _mismatch(browser_spend, official.get("spend"), absolute_spend_threshold)
    result["budget_delta_pct"] = budget_pct
    result["spend_delta_pct"] = spend_pct
    if budget_mismatch:
        result["reasons"].append("budget_mismatch")
    if spend_mismatch:
        result["reasons"].append("spend_mismatch")
    if budget_mismatch or spend_mismatch:
        result["confidence_cap"] = "medium"
    return result
