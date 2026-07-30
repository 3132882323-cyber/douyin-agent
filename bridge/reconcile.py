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


def _plan_identifier(record: dict[str, Any]) -> str:
    for label, value in record.items():
        normalized = str(label).lower().replace(" ", "")
        if not any(key in normalized for key in ("计划id", "项目id", "广告组id", "单元id", "planid", "adid")):
            continue
        text = str(value or "").strip()
        match = re.search(r"(?:id\s*[:：]\s*)?([a-z0-9][a-z0-9_-]{3,63})", text, re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def _pick_plan_name(record: dict[str, Any]) -> Any:
    for want in ("计划名称", "项目名称", "广告组名称", "单元名称"):
        for label, value in record.items():
            if str(label).replace(" ", "") == want:
                return value
    # Avoid bare「计划」which also matches「计划ID」.
    return _pick(record, ("计划名称", "项目名称", "广告组名称", "单元名称"))


def official_plan_index(plans_snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Index official plans by plan_id first, then by normalized name.

    Duplicate names are marked ``ambiguous`` and must not silently overwrite.
    """
    if not isinstance(plans_snapshot, dict):
        return {}
    data = plans_snapshot.get("data") if isinstance(plans_snapshot.get("data"), dict) else plans_snapshot
    if not isinstance(data, dict):
        return {}
    channel = str(data.get("channel") or "")
    if channel and channel != "official_api":
        return {}
    by_id: dict[str, dict[str, Any]] = {}
    name_groups: dict[str, list[dict[str, Any]]] = {}
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
            name = _normalize_plan_name(_pick_plan_name(record))
            plan_id = _plan_identifier(record)
            entry = {
                "budget": _parse_number(_pick(record, ("日预算", "每日预算", "预算上限", "预算"))),
                "spend": _parse_number(_pick(record, ("消耗", "花费", "支出", "广告消耗"))),
                "plan_id": plan_id,
                "name": name,
                "ambiguous": False,
            }
            if plan_id:
                by_id[plan_id] = entry
            if name:
                name_groups.setdefault(name, []).append(entry)
    by_name: dict[str, dict[str, Any]] = {}
    for name, entries in name_groups.items():
        if len(entries) == 1:
            by_name[name] = {**entries[0], "ambiguous": False}
        else:
            by_name[name] = {
                **entries[-1],
                "ambiguous": True,
                "duplicate_count": len(entries),
            }
    if not by_id and not by_name:
        return {}
    return {"by_id": by_id, "by_name": by_name}


def _as_index(official_index: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(official_index, dict) or not official_index:
        return {"by_id": {}, "by_name": {}}
    if "by_id" in official_index or "by_name" in official_index:
        return {
            "by_id": official_index.get("by_id") if isinstance(official_index.get("by_id"), dict) else {},
            "by_name": official_index.get("by_name") if isinstance(official_index.get("by_name"), dict) else {},
        }
    # Legacy flat name -> metrics map.
    return {"by_id": {}, "by_name": official_index}


def reconcile_plan_against_official(
    *,
    plan_name: str,
    browser_budget: float | None,
    browser_spend: float | None,
    official_index: dict[str, Any],
    plan_id: str = "",
    relative_threshold: float = 0.2,
    absolute_budget_threshold: float = 50.0,
    absolute_spend_threshold: float = 50.0,
) -> dict[str, Any]:
    """Compare one browser plan row with official API values.

    Returns a reconcile report. ``confidence_cap`` is ``medium`` when a material
    mismatch is found; otherwise ``None`` (caller keeps existing confidence).
    """
    index = _as_index(official_index)
    result: dict[str, Any] = {
        "available": bool(index["by_id"] or index["by_name"]),
        "matched": False,
        "confidence_cap": None,
        "reasons": [],
        "browser_budget": browser_budget,
        "browser_spend": browser_spend,
        "official_budget": None,
        "official_spend": None,
        "budget_delta_pct": None,
        "spend_delta_pct": None,
        "match_key": None,
    }
    if not result["available"]:
        return result

    official: dict[str, Any] | None = None
    plan_id = str(plan_id or "").strip()
    if plan_id and plan_id in index["by_id"]:
        official = index["by_id"][plan_id]
        result["match_key"] = "plan_id"
    else:
        candidate = index["by_name"].get(_normalize_plan_name(plan_name))
        if candidate and candidate.get("ambiguous"):
            result["reasons"].append("ambiguous_plan_name")
            result["confidence_cap"] = "medium"
            return result
        if not candidate:
            result["reasons"].append("official_plan_not_found")
            return result
        official = candidate
        result["match_key"] = "plan_name"

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
