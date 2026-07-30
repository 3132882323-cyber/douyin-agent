"""Deterministic business insights and recommendation builders."""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any

from action_protocol import assess_automation_readiness, build_action_draft
from reconcile import official_plan_index, reconcile_plan_against_official


def _facade():
    import http_receiver as facade
    return facade


def _now_label() -> str:
    return _facade()._now_label()


def load_history(*args, **kwargs):
    return _facade().load_history(*args, **kwargs)


def list_snapshots(*args, **kwargs):
    return _facade().list_snapshots(*args, **kwargs)


def load_data(*args, **kwargs):
    return _facade().load_data(*args, **kwargs)


def load_agent_settings(*args, **kwargs):
    return _facade().load_agent_settings(*args, **kwargs)


def load_task_states(*args, **kwargs):
    return _facade().load_task_states(*args, **kwargs)


def load_scan_status(*args, **kwargs):
    return _facade().load_scan_status(*args, **kwargs)


def load_action_audit(*args, **kwargs):
    return _facade().load_action_audit(*args, **kwargs)


def build_store_catalog(*args, **kwargs):
    return _facade().build_store_catalog(*args, **kwargs)


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
    settings = load_agent_settings()

    for item in catalog:
        if not item["fresh"]:
            alerts.append(
                {
                    "level": "warning",
                    "confidence": "high",
                    "title": f"{item['source']}/{item['page_type']} 数据已过期",
                    "detail": f"最后更新于 {item['saved_at']}",
                    "action": "打开对应后台页面并点击“同步并诊断”。",
                    "evidence": item,
                }
            )
        # Quality < 25: skip if snapshot is very recent (< 2 min, page may still be loading)
        if item["quality_score"] < 25:
            if item["age_seconds"] < 120:
                continue  # likely still loading, suppress false positive
            alerts.append(
                {
                    "level": "info",
                    "confidence": "medium" if item["age_seconds"] < 300 else "high",
                    "title": f"{item['page_type']} 页面字段不足",
                    "detail": f"数据采于 {_age_label(item['age_seconds'])}，质量分 {item['quality_score']}。",
                    "action": "刷新页面后重新同步；若仍失败，请更新页面适配器。",
                    "evidence": item,
                }
            )

    roi_metrics = _metric_matches("qianchuan", ("roi", "支付roi", "成交roi"))
    min_spend = float(settings.get("min_spend_for_action", 100.0))
    for item, label, value in roi_metrics[:3]:
        roi = _parse_number(value)
        if roi is not None and roi < 1:
            # Check if spend is above minimum threshold before alerting
            snapshot = load_data("qianchuan", item["page_type"])
            metrics = (snapshot or {}).get("data", {}).get("metrics", {})
            best_spend = None
            spend_label = ""
            for spend_label, spend_val in (metrics or {}).items():
                if any(keyword in str(spend_label) for keyword in ("消耗", "花费", "spend", "广告消耗")):
                    best_spend = _parse_number(spend_val)
                    if best_spend is not None:
                        break
            if best_spend is not None and best_spend < min_spend:
                continue  # spend too low for ROI to be actionable
            alerts.append(
                {
                    "level": "high",
                    "confidence": "high" if (best_spend is not None and best_spend >= min_spend * 2) else "medium",
                    "title": f"千川 {label} 低于 1",
                    "detail": f"当前 {label} = {value}" + (f"，消耗 {spend_label}" if best_spend is not None else "") + "。",
                    "action": "先核对统计周期和归因口径，再检查高消耗低成交计划；不要直接批量提价。",
                    "evidence": {"source": "qianchuan", "page_type": item["page_type"], "label": label, "value": value, "spend": best_spend},
                }
            )

    refund_metrics = _metric_matches("doudian", ("退款率", "退货率"))
    for item, label, value in refund_metrics[:3]:
        rate = _parse_number(value)
        if rate is not None and rate > 20:
            # Include period context if available in snapshot
            snapshot = load_data("doudian", item["page_type"])
            period_info = ""
            data = (snapshot or {}).get("data", {})
            for key in ("date_range", "period", "stat_date", "time_range"):
                period_val = data.get(key) or (data.get("safe_metrics") or {}).get(key)
                if period_val:
                    period_info = f"（统计周期: {period_val}）"
                    break
            alerts.append(
                {
                    "level": "warning",
                    "confidence": "medium" if item["age_seconds"] > 3600 else "high",
                    "title": f"{label} 偏高",
                    "detail": f"当前 {value}{period_info}。",
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
                    "confidence": "high",
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
                "confidence": "high",
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

def _table_records(source: str, page_types: set[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in list_snapshots():
        if item["source"] != source or item["page_type"] not in page_types:
            continue
        snapshot = load_data(source, item["page_type"])
        snapshot_data = (snapshot or {}).get("data", {})
        if not isinstance(snapshot_data, dict):
            continue
        tables = snapshot_data.get("tables", [])
        if not isinstance(tables, list):
            continue
        account = snapshot_data.get("account") if isinstance(snapshot_data.get("account"), dict) else {}
        quality = snapshot_data.get("quality") if isinstance(snapshot_data.get("quality"), dict) else {}
        captured_at_ms = int(
            snapshot_data.get("captured_at")
            or (float((snapshot or {}).get("timestamp", 0)) * 1000)
            or 0
        )
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
                        "quality_score": int(quality.get("score", item["quality_score"]) or 0),
                        "pagination_truncated": bool(quality.get("pagination_truncated") or item.get("pagination_truncated")),
                        "captured_at_ms": captured_at_ms,
                        "account_key": str(account.get("key") or "").lower(),
                        "account_label": str(account.get("label") or ""),
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


def _entity_identifier(record: dict[str, Any], keywords: tuple[str, ...]) -> str:
    """Read a platform identifier without ever substituting a row index or name."""
    for label, value in record.items():
        normalized_label = str(label).lower().replace(" ", "")
        if not any(keyword.lower().replace(" ", "") in normalized_label for keyword in keywords):
            continue
        text = str(value or "").strip()
        match = re.search(r"(?:id\s*[:：]\s*)?([a-z0-9][a-z0-9_-]{3,63})", text, re.IGNORECASE)
        if match:
            return match.group(1)
    for value in record.values():
        text = str(value or "")
        match = re.search(r"(?:计划|项目|广告组|单元)\s*ID\s*[:：]\s*([a-z0-9][a-z0-9_-]{3,63})", text, re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def _action_params_for_plan(
    plan: str,
    action_type: str,
    evidence: dict[str, Any],
    entry: dict[str, Any],
    confidence: str,
) -> dict[str, Any]:
    """Generate a policy-checked operation draft tied to a fresh account snapshot."""
    record = evidence.get("_record") if isinstance(evidence.get("_record"), dict) else {}
    budget = _evidence_value(record, ("日预算", "每日预算", "预算上限", "预算"))
    plan_id = _entity_identifier(record, ("计划id", "项目id", "广告组id", "单元id"))
    operation_type = "replace_creative"
    operation_label = "优化素材"
    field = "素材"
    target_value: Any = None

    if action_type in {"stop_loss", "reduce_budget", "scale_cautiously"}:
        operation_type = "adjust_budget"
        field = "预算"
        percent = -30 if action_type == "stop_loss" else -20 if action_type == "reduce_budget" else 10
        operation_label = f"{'降低' if percent < 0 else '增加'}预算 {abs(percent)}%"
        target_value = round(budget * (1 + percent / 100), 2) if budget and budget > 0 else None

    current_label = f"{budget:g}" if budget and budget > 0 else "待重新读取"
    target_label = f"{target_value:g}" if isinstance(target_value, (int, float)) else "待重新计算"
    copy_text = (
        f"{plan} | 预算 {current_label} → {target_label}"
        if operation_type == "adjust_budget"
        else f"{plan} | 优化前 3 秒表达与卖点"
    )
    compact_evidence = {
        key: evidence.get(key)
        for key in ("spend", "roi", "roi_target", "orders", "ctr")
        if evidence.get(key) is not None
    }
    compact_evidence["pagination_truncated"] = bool(entry.get("pagination_truncated"))
    return build_action_draft(
        operation_type=operation_type,
        operation_label=operation_label,
        target_kind="qianchuan_plan",
        target_id=plan_id,
        target_name=plan,
        account_key=str(entry.get("account_key") or ""),
        account_label=str(entry.get("account_label") or ""),
        field=field,
        current_value=budget,
        target_value=target_value,
        source=str(entry.get("source") or ""),
        page_type=str(entry.get("page_type") or ""),
        captured_at_ms=int(entry.get("captured_at_ms") or 0),
        quality_score=int(entry.get("quality_score") or 0),
        confidence=confidence,
        evidence=compact_evidence,
        copy_text=copy_text,
        pagination_truncated=bool(entry.get("pagination_truncated")),
    )

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
    selected_account = str(settings.get("qianchuan_account_key") or "")
    official_index = official_plan_index(
        load_data("qianchuan", "plans", account_key=selected_account or None)
        if selected_account
        else load_data("qianchuan", "plans", account_key="")
    )
    if not official_index and selected_account:
        # Fall back to latest account-scoped plans if explicit load missed channel tag.
        official_index = official_plan_index(load_data("qianchuan", "plans", account_key=selected_account))

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
            "_record": record,
        }
        confidence = (
            "high"
            if entry["quality_score"] >= 70
            and not entry.get("pagination_truncated")
            and spend is not None
            and roi is not None
            else "medium"
        )
        browser_budget = _evidence_value(record, ("日预算", "每日预算", "预算上限", "预算"))
        _, named_plan = _pick(record, ("计划名称", "项目名称", "广告组名称", "单元名称"))
        reconcile_name = str(named_plan or plan).strip() or plan
        reconcile = reconcile_plan_against_official(
            plan_name=reconcile_name,
            browser_budget=browser_budget,
            browser_spend=spend,
            official_index=official_index,
        )
        if reconcile.get("available"):
            evidence["api_reconcile"] = {
                key: reconcile.get(key)
                for key in (
                    "matched",
                    "reasons",
                    "browser_budget",
                    "official_budget",
                    "budget_delta_pct",
                    "browser_spend",
                    "official_spend",
                    "spend_delta_pct",
                )
            }
            if reconcile.get("confidence_cap") == "medium":
                confidence = "medium"
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
                    "action_params": _action_params_for_plan(plan, "stop_loss", evidence, entry, confidence),
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
                    "action_params": _action_params_for_plan(plan, "reduce_budget", evidence, entry, confidence),
                }
            )
        elif roi is not None and roi < effective_roi_target:
            reason = f"ROI {roi:g} 低于目标 {effective_roi_target:g}，暂不适合放量。"
            suggestion = "预算保持不变，先优化素材点击率与商品承接；达到目标后再逐级放量。"
            if ctr is not None and ctr < 1:
                suggestion = "预算保持不变，优先更换前 3 秒表达、封面和卖点；不要先提高出价。"
                reason += f" 当前点击率为 {ctr:g}。"
            results.append({**base, "level": "warning", "action_type": "optimize", "suggestion": suggestion, "reason": reason, "action_params": _action_params_for_plan(plan, "optimize", evidence, entry, confidence)})
        elif roi is not None and roi >= effective_roi_target and (orders or 0) >= 3:
            results.append(
                {
                    **base,
                    "level": "opportunity",
                    "action_type": "scale_cautiously",
                    "suggestion": "可尝试增加预算 10%–15%，每次只调一次，并观察一个完整转化窗口。",
                    "reason": f"ROI {roi:g} 达到目标 {effective_roi_target:g}，且已有 {orders:g} 个成交。",
                    "action_params": _action_params_for_plan(plan, "scale_cautiously", evidence, entry, confidence),
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
    audit_states = {
        str(action.get("action_id") or ""): action
        for action in load_action_audit().get("actions", [])
        if isinstance(action, dict)
    }
    cleaned: list[dict[str, Any]] = []
    for item in list(unique.values())[:20]:
        ev = item.get("evidence")
        if isinstance(ev, dict):
            ev.pop("_record", None)
        action_params = item.get("action_params")
        if isinstance(action_params, dict):
            saved_action = audit_states.get(str(action_params.get("action_id") or ""))
            if saved_action:
                action_params = {
                    **action_params,
                    "state": saved_action.get("state", action_params.get("state")),
                    "confirmed_at_ms": saved_action.get("confirmed_at_ms"),
                    "cancelled_at_ms": saved_action.get("cancelled_at_ms"),
                }
                item["action_params"] = action_params
        cleaned.append({**item, **_plan_workbench_fields(item, task_states)})
    return cleaned


def build_automation_readiness(recommendations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build the future executor candidate queue without enabling execution."""

    recommendations = recommendations if recommendations is not None else build_plan_recommendations()
    items: list[dict[str, Any]] = []
    for recommendation in recommendations:
        action = recommendation.get("action_params")
        if not isinstance(action, dict):
            items.append(
                {
                    "plan": str(recommendation.get("plan") or "千川计划"),
                    "level": str(recommendation.get("level") or "info"),
                    "operation_label": str(recommendation.get("suggestion") or "人工复核"),
                    "status": "manual_only",
                    "status_label": "仅人工处理",
                    "stage": "proposal",
                    "next_step": "缺少结构化动作参数，保留为人工运营建议。",
                    "can_enter_preflight": False,
                    "execution_enabled": False,
                    "blocked_reasons": [],
                }
            )
            continue

        readiness = assess_automation_readiness(action)
        change = action.get("change") if isinstance(action.get("change"), dict) else {}
        target = action.get("target_ref") if isinstance(action.get("target_ref"), dict) else {}
        current_value = change.get("current_value")
        target_value = change.get("target_value")
        pilot_eligible = (
            action.get("operation_type") == "adjust_budget"
            and isinstance(current_value, (int, float))
            and isinstance(target_value, (int, float))
            and float(target_value) < float(current_value)
        )
        if readiness["status"] in {"confirmable", "preflight_ready"} and not pilot_eligible:
            readiness = {
                **readiness,
                "status": "blocked",
                "status_label": "试运行暂不开放",
                "stage": "qualification",
                "next_step": "首批受监督执行只开放降低预算止损；放量和其他动作继续人工处理。",
                "can_enter_preflight": False,
                "blocked_reasons": [
                    *readiness.get("blocked_reasons", []),
                    {"code": "PILOT_SCOPE_RESTRICTED", "message": "首批只允许降低预算，不开放自动放量。"},
                ],
            }
        items.append(
            {
                "action_id": str(action.get("action_id") or ""),
                "plan": str(recommendation.get("plan") or target.get("name") or "千川计划"),
                "level": str(recommendation.get("level") or "info"),
                "operation_type": str(action.get("operation_type") or ""),
                "operation_label": str(action.get("operation_label") or recommendation.get("suggestion") or "人工复核"),
                "account_label": str(target.get("account_label") or target.get("account_key") or "账号未锁定"),
                "plan_id": str(target.get("id") or ""),
                "field": change.get("field"),
                "current_value": current_value,
                "target_value": target_value,
                **readiness,
            }
        )

    order = {"preflight_ready": 0, "confirmable": 1, "blocked": 2, "manual_only": 3}
    items.sort(key=lambda item: (order.get(str(item.get("status")), 9), 0 if item.get("level") == "high" else 1))
    summary = {
        "total": len(items),
        "preflight_ready": sum(item["status"] == "preflight_ready" for item in items),
        "confirmable": sum(item["status"] == "confirmable" for item in items),
        "blocked": sum(item["status"] == "blocked" for item in items),
        "manual_only": sum(item["status"] == "manual_only" for item in items),
    }
    return {
        "generated_at": _now_label(),
        "mode": "readiness_only",
        "current_stage": "supervised_preflight",
        "next_stage": "supervised_execution",
        "execution_enabled": False,
        "criteria": [
            "锁定千川账号与计划唯一 ID",
            "页面数据不超过 10 分钟且质量分不低于 70",
            "消耗、成交和 ROI 支持高置信度判断",
            "单次预算增加不超过 15%，降低不超过 30%",
            "执行前重新读取，执行后再次回读验收",
        ],
        "summary": summary,
        "items": items,
    }


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
        # Build structured action_params for this video
        if level == "high":
            _ap = {"operation_type": "pause_creative", "operation_label": "暂停复制该素材", "target": name, "field": "素材状态", "current_value": status, "target_value": "暂停复制", "copy_text": f"{name} | 暂停复制 | {status}"}
        elif status == "可复制放量":
            _ap = {"operation_type": "duplicate_creative", "operation_label": "创建素材变体", "target": name, "field": "素材", "current_value": status, "target_value": "变体扩量", "copy_text": f"{name} | 创建变体 | 同钩子换卖点"}
        elif status == "高潜素材":
            _ap = {"operation_type": "test_creative", "operation_label": "进入下轮测试", "target": name, "field": "素材", "current_value": status, "target_value": "直播引流测试", "copy_text": f"{name} | 进入直播引流测试"}
        elif status == "尚未测试":
            _ap = {"operation_type": "test_creative", "operation_label": "小预算测试", "target": name, "field": "预算", "current_value": "0", "target_value": "测试预算", "copy_text": f"{name} | 加入小预算测试组"}
        else:
            _ap = {"operation_type": "observe", "operation_label": "继续观察", "target": name, "field": None, "current_value": status, "target_value": None, "copy_text": f"{name} | 继续观察"}
        videos.append(
            {
                "id": f"creative-{entry['table_index']}-{entry['row_index']}",
                "name": name,
                "level": level,
                "status": status,
                "suggestion": suggestion,
                "action_params": _ap,
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
            results.append({**base, "level": "high", "title": "已缺货", "suggestion": "立即暂停该商品继续放量，并核对补货时间。",
                            "action_params": {"operation_type": "pause_ad", "operation_label": "暂停该商品投放", "target": product, "field": "投放状态", "current_value": "投放中", "target_value": "暂停", "copy_text": f"{product} | 暂停千川投放 | 当前库存 0"}})
        elif stock <= critical:
            results.append({**base, "level": "high", "title": "库存极低", "suggestion": f"库存仅 {stock:g}，优先补货；补货确认前不要扩大千川消耗。",
                            "action_params": {"operation_type": "restock", "operation_label": "紧急补货", "target": product, "field": "库存", "current_value": stock, "target_value": None, "copy_text": f"{product} | 紧急补货 | 当前库存 {stock:g}"}})
        elif days_of_cover is not None and days_of_cover <= days_warning:
            results.append({**base, "level": "warning", "title": "预计即将售罄", "suggestion": f"按当前销量约可售 {days_of_cover:.1f} 天，建议补货或降低投放强度。",
                            "action_params": {"operation_type": "review_inventory", "operation_label": f"可售仅 {days_of_cover:.1f} 天", "target": product, "field": "库存", "current_value": stock, "target_value": None, "copy_text": f"{product} | 可售 {days_of_cover:.1f} 天 | 库存 {stock:g}"}})
        elif stock <= low:
            results.append({**base, "level": "warning", "title": "低库存", "suggestion": f"库存 {stock:g}，请核对在投计划和补货周期。",
                            "action_params": {"operation_type": "review_inventory", "operation_label": "核对库存", "target": product, "field": "库存", "current_value": stock, "target_value": None, "copy_text": f"{product} | 库存 {stock:g}"}})

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
        task_entry = {
            "level": item["level"],
            "owner": "投放运营",
            "title": item["workbench_title"],
            "action": item["action"],
            "acceptance": item["acceptance"],
            "evidence": item["found"],
            "impact": item["adjustment_range"],
            "observation_window": item["observation_window"],
        }
        if "action_params" in item:
            task_entry["action_params"] = item["action_params"]
        tasks.append(task_entry)
    for item in inventory[:3]:
        inv_entry = {"level": item["level"], "owner": "商品运营", "title": f"{item['product']} · {item['title']}", "action": item["suggestion"], "acceptance": "补货或投放限制已人工确认。", "evidence": f"当前库存 {item['evidence']['stock']:g}。"}
        if "action_params" in item:
            inv_entry["action_params"] = item["action_params"]
        tasks.append(inv_entry)
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

def build_scan_receipt() -> dict[str, Any]:
    """Turn the last browser scan into an operator-readable data receipt."""
    scan = load_scan_status()
    raw_results = scan.get("results") if isinstance(scan.get("results"), list) else []
    results: list[dict[str, Any]] = []
    source_totals = {
        "doudian": {"label": "抖店", "total": 0, "success": 0, "failed": 0, "needs_review": 0},
        "qianchuan": {"label": "千川", "total": 0, "success": 0, "failed": 0, "needs_review": 0},
    }
    account_label = str(scan.get("account_label") or "")[:80]
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue
        page_id = str(raw.get("id") or "unknown")[:64]
        source = str(raw.get("source") or ("qianchuan" if page_id.startswith("qianchuan") else "doudian"))
        if source not in source_totals:
            source = "doudian"
        quality = raw.get("quality") if isinstance(raw.get("quality"), dict) else {}
        quality_score = max(0, min(100, int(quality.get("score", 0) or 0)))
        pagination_truncated = bool(quality.get("pagination_truncated"))
        ok = bool(raw.get("ok"))
        needs_review = ok and (quality_score < 70 or pagination_truncated)
        source_totals[source]["total"] += 1
        source_totals[source]["success" if ok else "failed"] += 1
        if needs_review:
            source_totals[source]["needs_review"] += 1
        if source == "qianchuan" and raw.get("account_label") and not account_label:
            account_label = str(raw.get("account_label"))[:80]
        results.append(
            {
                "id": page_id,
                "label": str(raw.get("label") or page_id)[:80],
                "source": source,
                "ok": ok,
                "page_type": str(raw.get("page_type") or "")[:48],
                "quality_score": quality_score,
                "metric_count": max(0, int(quality.get("metric_count", 0) or 0)),
                "row_count": max(0, int(quality.get("row_count", 0) or 0)),
                "pagination_truncated": pagination_truncated,
                "needs_review": needs_review,
                "error": str(raw.get("error") or "")[:300],
                "captured_at": int(raw.get("captured_at", 0) or 0),
            }
        )

    total = max(int(scan.get("total", 0) or 0), len(results))
    success = sum(1 for item in results if item["ok"])
    failed = sum(1 for item in results if not item["ok"])
    needs_review = sum(1 for item in results if item["needs_review"])
    completed = len(results)
    coverage_rate = round(completed / total * 100) if total else 0
    status = str(scan.get("status") or "idle")
    if status == "running":
        readiness = "running"
        readiness_label = "正在采集"
    elif not results:
        readiness = "empty"
        readiness_label = "等待巡查"
    elif status == "completed" and not failed and not needs_review and coverage_rate == 100:
        readiness = "ready"
        readiness_label = "数据可用于分析"
    else:
        readiness = "attention"
        readiness_label = "需要补采或复核"

    warnings: list[str] = []
    if failed:
        warnings.append(f"{failed} 个页面读取失败，可在体检单中单独重试。")
    truncated = sum(1 for item in results if item.get("pagination_truncated"))
    if needs_review:
        warnings.append(f"{needs_review} 个页面质量不足或列表被截断，相关建议需先补采再作为可执行结论。")
    if truncated:
        warnings.append(f"{truncated} 个页面列表超过采集页数上限，止损/预算方案已锁定。")
    if total and completed < total:
        warnings.append(f"巡查仅覆盖 {completed}/{total} 个页面，数据不完整。")
    if status in {"cancelled", "error"} and scan.get("error"):
        warnings.append(str(scan.get("error"))[:300])

    return {
        "generated_at": _now_label(),
        "scan_status": status,
        "readiness": readiness,
        "readiness_label": readiness_label,
        "analysis_ready": readiness == "ready",
        "account_key": str(scan.get("account_key") or "")[:80],
        "account_label": account_label,
        "started_at": int(scan.get("started_at", 0) or 0),
        "finished_at": int(scan.get("finished_at", 0) or 0),
        "summary": {
            "total": total,
            "completed": completed,
            "success": success,
            "failed": failed,
            "needs_review": needs_review,
            "coverage_rate": coverage_rate,
            "row_count": sum(item["row_count"] for item in results),
        },
        "sources": source_totals,
        "results": results,
        "failed_page_ids": [item["id"] for item in results if not item["ok"]],
        "warnings": warnings,
        "mode": "read_only",
    }


def _timestamp_seconds(value: Any) -> int:
    """Normalize browser millisecond timestamps and server second timestamps."""
    try:
        timestamp = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return timestamp // 1000 if timestamp > 10_000_000_000 else timestamp


def build_operation_context(
    catalog: dict[str, Any] | None = None,
    receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the identity and data-trust gate shown before any recommendation."""
    catalog = catalog or build_store_catalog()
    receipt = receipt or build_scan_receipt()
    stores = catalog.get("stores") if isinstance(catalog.get("stores"), list) else []
    selected_key = str(catalog.get("selected_store_key") or catalog.get("selected_account_key") or "")
    selected = next(
        (
            item
            for item in stores
            if isinstance(item, dict) and str(item.get("key") or "") == selected_key
        ),
        None,
    )
    receipt_key = str(receipt.get("account_key") or "")
    identity_match = not receipt_key or not selected_key or receipt_key == selected_key
    updated_at = max(
        _timestamp_seconds(selected.get("updated_at") if selected else 0),
        _timestamp_seconds(receipt.get("finished_at")),
    )
    age_seconds = max(0, int(time.time()) - updated_at) if updated_at else None
    fresh = age_seconds is not None and age_seconds <= 24 * 60 * 60
    official_ready = bool(
        selected
        and selected.get("channel") == "official_api"
        and selected.get("state") == "ready"
        and int(selected.get("page_count") or 0) > 0
    )
    browser_ready = bool(receipt.get("analysis_ready"))
    coverage_rate = int((receipt.get("summary") or {}).get("coverage_rate") or 0)
    blockers: list[str] = []
    warnings: list[str] = []

    if not selected:
        blockers.append("尚未选择当前店铺")
    elif selected.get("state") == "not_linked":
        blockers.append("当前店铺未关联可用的千川广告账户")
    elif selected.get("state") == "empty":
        blockers.append("当前店铺还没有可分析的数据")
    if not identity_match:
        blockers.append("最近巡检账号与当前店铺不一致")
    if selected and not fresh:
        warnings.append("当前店铺数据已超过 24 小时或缺少更新时间")
    if selected and selected.get("channel") == "browser" and not browser_ready:
        warnings.append("网页巡检尚未完整通过，相关建议需要人工复核")
    warnings.extend(
        str(item)[:200]
        for item in receipt.get("warnings", [])
        if isinstance(item, str)
    )

    if blockers:
        state = "blocked"
        state_label = "暂停经营判断"
        decision_policy = "blocked"
        next_action = "先确认店铺、千川账户和数据来源，再生成经营建议。"
    elif fresh and identity_match and (official_ready or browser_ready):
        state = "ready"
        state_label = "数据可信，可进入处理"
        decision_policy = "reviewable"
        next_action = "可以处理今日任务；涉及预算、启停和资金的动作仍需执行前复核。"
    else:
        state = "review"
        state_label = "建议仅供人工复核"
        decision_policy = "manual_review"
        next_action = "先同步官方数据或完成一次全店巡检，补齐后再进入投放执行准备。"

    source_label = "尚未绑定"
    if selected:
        source_label = "千川官方 API" if selected.get("channel") == "official_api" else "浏览器网页"
    freshness_label = "暂无更新时间"
    if age_seconds is not None:
        if age_seconds < 60:
            freshness_label = "刚刚更新"
        elif age_seconds < 60 * 60:
            freshness_label = f"{max(1, age_seconds // 60)} 分钟前"
        elif age_seconds < 24 * 60 * 60:
            freshness_label = f"{max(1, age_seconds // 3600)} 小时前"
        else:
            freshness_label = f"{max(1, age_seconds // 86400)} 天前"

    return {
        "generated_at": _now_label(),
        "state": state,
        "state_label": state_label,
        "decision_policy": decision_policy,
        "analysis_allowed": state != "blocked",
        "execution_review_allowed": state == "ready",
        "selected_store": {
            "key": selected_key,
            "label": str(selected.get("label") or "未命名店铺") if selected else "尚未选择",
            "state": str(selected.get("state") or "empty") if selected else "empty",
            "state_label": str(selected.get("state_label") or "暂无数据") if selected else "尚未绑定",
            "channel": str(selected.get("channel") or "") if selected else "",
            "advertiser_count": selected.get("advertiser_count") if selected else None,
        },
        "identity_match": identity_match,
        "source_label": source_label,
        "freshness": {
            "updated_at": updated_at or None,
            "age_seconds": age_seconds,
            "fresh": fresh,
            "label": freshness_label,
        },
        "coverage": {
            "rate": coverage_rate,
            "label": f"{coverage_rate}% 网页覆盖" if receipt.get("summary") else "未生成网页体检",
            "official_ready": official_ready,
            "browser_ready": browser_ready,
        },
        "blockers": blockers,
        "warnings": list(dict.fromkeys(warnings))[:8],
        "next_action": next_action,
        "mode": "read_only",
    }


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


def build_stop_loss_queue(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Turn plan diagnostics into a ranked, operator-friendly loss-control queue."""

    settings = settings or load_agent_settings()
    mode = str(settings.get("execution_mode") or "observe")
    min_spend = max(1.0, float(settings.get("min_spend_for_action") or 100))
    queue: list[dict[str, Any]] = []
    for item in build_plan_recommendations(settings):
        action_type = str(item.get("action_type") or "")
        if action_type not in {"stop_loss", "reduce_budget", "optimize", "inspect_plans"}:
            continue
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        spend = float(evidence.get("spend") or 0)
        roi = evidence.get("roi")
        target = float(evidence.get("roi_target") or settings.get("roi_target") or 1.5)
        roi_gap = 0.0 if not isinstance(roi, (int, float)) or target <= 0 else max(0.0, (target - float(roi)) / target)
        severity = {"stop_loss": 45, "reduce_budget": 35, "optimize": 18, "inspect_plans": 10}.get(action_type, 0)
        spend_component = min(30, round(spend / min_spend * 12.0))
        roi_component = min(25, round(roi_gap * 25.0))
        confidence_component = 0 if item.get("confidence") == "low" else 5 if item.get("confidence") == "medium" else 10
        risk_score = min(100, severity + spend_component + roi_component + confidence_component)
        reduction = 0.30 if action_type == "stop_loss" else 0.20 if action_type == "reduce_budget" else 0.0
        saving_high = round(spend * reduction, 2)
        saving_low = round(saving_high * 0.5, 2)
        if item.get("confidence") == "low" or action_type == "inspect_plans":
            bucket = "data_missing"
        elif action_type in {"stop_loss", "reduce_budget"} and risk_score >= 60:
            bucket = "must_handle"
        else:
            bucket = "observe"
        queue.append({
            **item,
            "risk_score": risk_score,
            "risk_components": [
                {"key": "action", "label": "动作风险", "score": severity, "max_score": 45},
                {"key": "spend", "label": "消耗风险", "score": spend_component, "max_score": 30},
                {"key": "roi_gap", "label": "ROI偏差", "score": roi_component, "max_score": 25},
                {"key": "confidence", "label": "数据可信度", "score": confidence_component, "max_score": 10},
            ],
            "bucket": bucket,
            "bucket_label": {"must_handle": "必须处理", "observe": "继续观察", "data_missing": "补齐数据"}[bucket],
            "estimated_savings_low": saving_low,
            "estimated_savings_high": saving_high,
            "estimated_savings_label": f"预计可避免继续无效消耗 ¥{saving_low:g}–¥{saving_high:g}" if saving_high else "暂不估算可避免消耗",
            "execution_mode": mode,
            "can_start_execution": mode == "supervised" and action_type in {"stop_loss", "reduce_budget"},
        })
    bucket_order = {"must_handle": 0, "observe": 1, "data_missing": 2}
    queue.sort(key=lambda item: (bucket_order[item["bucket"]], -item["risk_score"], -(item.get("evidence", {}).get("spend") or 0)))
    return {
        "generated_at": _now_label(),
        "execution_mode": mode,
        "execution_mode_label": {"observe": "观察模式", "shadow": "影子模式", "supervised": "受控执行"}[mode],
        "items": queue[:10],
        "summary": {
            "must_handle": sum(1 for item in queue if item["bucket"] == "must_handle"),
            "observe": sum(1 for item in queue if item["bucket"] == "observe"),
            "data_missing": sum(1 for item in queue if item["bucket"] == "data_missing"),
            "estimated_savings_low": round(sum(item["estimated_savings_low"] for item in queue), 2),
            "estimated_savings_high": round(sum(item["estimated_savings_high"] for item in queue), 2),
        },
        "estimate_note": "金额按当前消耗与建议降幅保守估算，表示可能避免的后续无效消耗，不代表实际结算结果。",
    }

