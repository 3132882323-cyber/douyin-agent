"""Build a conservative, product-scoped operating graph for Douyin commerce.

The graph deliberately links rows only through stable identifiers.  Product
names remain display metadata and are never used as an automatic join key.
This keeps recommendations useful without inventing cross-channel causality.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any


SCHEMA_VERSION = 1

_IDENTITY_ALIASES: dict[str, tuple[str, ...]] = {
    "product_id": ("商品id", "抖音商品id", "productid", "product_id", "goodsid", "goods_id"),
    "sku_id": ("skuid", "sku_id", "规格id", "商品skuid"),
    "merchant_code": ("商家编码", "商品编码", "货号", "merchantcode", "merchant_code"),
}

_NAME_ALIASES = ("商品名称", "商品名", "商品", "productname", "product_name", "goodsname", "goods_name")
_PLAN_ID_ALIASES = ("计划id", "广告计划id", "项目id", "planid", "plan_id")
_PLAN_HEADER_ENTITY_PATTERN = (
    r"(?:(?:商品|直播|广告|推广|投放|全域推广|标准推广|乘方)?计划|"
    r"(?:广告|推广|投放)?项目|广告组|单元)"
)

_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "exposure": ("商品曝光人数", "曝光人数", "曝光量", "展示次数", "展现量", "impressions"),
    "clicks": ("商品点击人数", "点击人数", "点击次数", "点击量", "clicks"),
    "click_rate": ("商品点击率", "点击率", "ctr"),
    "views": ("观看次数", "播放量", "视频播放量", "进入直播间人数", "进房人数", "views"),
    "orders": ("净成交订单数", "成交订单数", "订单量", "成交单量", "orders"),
    "gmv": ("净成交金额", "成交金额", "支付金额", "商品成交金额", "gmv"),
    "conversion_rate": ("成交转化率", "点击成交率", "支付转化率", "cvr"),
    "spend": ("整体消耗", "消耗", "广告消耗", "视频消耗", "spend"),
    "roi": ("净成交roi", "整体支付roi", "支付roi", "综合营销roi", "roi"),
    "stock": ("可售库存", "可售", "库存", "库存数量", "总库存", "stock"),
    "refund_rate": ("退款率", "成交退款率", "1小时内退款率", "refundrate", "refund_rate"),
    "break_even_roi": ("保本roi", "盈亏平衡roi", "breakevenroi", "break_even_roi"),
    "profit_margin": ("毛利率", "净利率", "profitmargin", "profit_margin"),
}

_METRIC_UNITS = {
    "exposure": "count",
    "clicks": "count",
    "click_rate": "percent",
    "views": "count",
    "orders": "count",
    "gmv": "cny",
    "conversion_rate": "percent",
    "spend": "cny",
    "roi": "ratio",
    "stock": "count",
    "refund_rate": "percent",
    "break_even_roi": "ratio",
    "profit_margin": "percent",
}

_PRIMARY_ENTITY_PRIORITY = (
    "douyin_product_id",
    "qianchuan_product_id",
    "douyin_sku_id",
    "merchant_product_code",
    "qianchuan_plan_id",
    "qianchuan_material_id",
    "douyin_content_id",
    "douyin_live_session_id",
    "douyin_live_room_id",
)

_PAGE_CHANNELS: dict[str, str] = {
    "shelf": "shelf",
    "mall": "shelf",
    "overview": "shelf",
    "products": "inventory",
    "inventory": "inventory",
    "live": "live",
    "live_dashboard": "live",
    "qianchuan_live": "live",
    "short_video": "content",
    "image_text": "content",
    "recommend_card": "content",
    "video_library": "content",
    "materials": "content",
    "campaigns": "ads",
    "plans": "ads",
    "report": "ads",
    "qianchuan_campaigns": "ads",
    "qianchuan_report": "ads",
    "orders": "after_sale",
    "refunds": "after_sale",
    "reviews": "after_sale",
}

_CHANNEL_LABELS = {
    "shelf": "货架",
    "inventory": "库存",
    "live": "直播",
    "content": "内容",
    "ads": "千川",
    "after_sale": "售后",
}


def _label_key(value: Any) -> str:
    return re.sub(r"[\s\-_/（）()：:·.]", "", str(value or "").strip().lower())


def _value(record: dict[str, Any], aliases: tuple[str, ...]) -> Any | None:
    alias_keys = {_label_key(alias) for alias in aliases}
    for label, value in record.items():
        if _label_key(label) in alias_keys and str(value or "").strip() not in {"", "-", "--", "[已隐藏]"}:
            return value
    return None


def _labeled_value(record: dict[str, Any], aliases: tuple[str, ...]) -> tuple[str, Any | None]:
    alias_keys = {_label_key(alias) for alias in aliases}
    for label, value in record.items():
        if _label_key(label) in alias_keys and str(value or "").strip() not in {"", "-", "--", "[已隐藏]"}:
            return str(label), value
    return "", None


def _identifier(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text or text in {"-", "--", "[已隐藏]"}:
        return ""
    if not re.fullmatch(r"[a-z0-9_-]{4,96}", text):
        return ""
    return text


def _plan_identifier_value(record: dict[str, Any]) -> str:
    """Read an explicit plan ID column using the browser collection contract."""

    for label, value in record.items():
        normalized = unicodedata.normalize("NFKC", str(label or "")).lower()
        normalized = re.sub(r"(?:升序|降序|可排序|排序|筛选)", "", normalized)
        normalized = re.sub(r"[\s\-_/（）()：:·|]", "", normalized)
        if re.fullmatch(rf"{_PLAN_HEADER_ENTITY_PATTERN}(?:id|编号)", normalized, re.IGNORECASE):
            identifier = _identifier(value)
            if identifier:
                return identifier
    return _identifier(_value(record, _PLAN_ID_ALIASES))


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("￥", "").replace("¥", "")
    if not text or text in {"-", "--", "暂无数据", "[已隐藏]"}:
        return None
    multiplier = 100000000.0 if "亿" in text else 10000.0 if "万" in text else 1.0
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return round(float(match.group(0)) * multiplier, 6)
    except ValueError:
        return None


def _channel(entry: dict[str, Any]) -> str:
    page_type = str(entry.get("page_type") or "").lower()
    if page_type == "overview" and str(entry.get("source") or "") == "qianchuan":
        return "ads"
    return _PAGE_CHANNELS.get(page_type, "other")


def extract_commerce_observations(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract exact-row metric points from a bridge-resolved snapshot.

    The function consumes only installation-local entity keys emitted by the
    bridge. A row is attributed to one explicit primary entity; relations are
    used separately to connect SKU, plan, content and product entities.
    Unknown time windows remain ``unknown`` and therefore cannot later be used
    as automatic comparison evidence.
    """

    tables = data.get("tables")
    if not isinstance(tables, list):
        return []
    source = str(data.get("source") or "")
    page_type = str(data.get("page_type") or "")
    channel = _channel({"source": source, "page_type": page_type})
    try:
        captured_at_ms = int(data.get("captured_at") or data.get("timestamp") or 0)
    except (TypeError, ValueError):
        captured_at_ms = 0
    quality = data.get("quality") if isinstance(data.get("quality"), dict) else {}
    quality_score = max(0, min(100, int(quality.get("score") or 0)))
    raw_window = data.get("window_key") or data.get("date_range") or data.get("time_range") or "unknown"
    window_key = re.sub(r"[^a-z0-9_:-]", "_", str(raw_window).strip().lower())[:64] or "unknown"
    observations: list[dict[str, Any]] = []

    for table_index, table in enumerate(tables[:8]):
        if not isinstance(table, dict):
            continue
        headers = [str(value or "") for value in table.get("headers", [])]
        rows = table.get("rows")
        entity_rows = table.get("entity_rows")
        if not headers or not isinstance(rows, list) or not isinstance(entity_rows, list):
            continue
        entity_by_row = {
            int(item.get("row_index")): item.get("entities", [])
            for item in entity_rows
            if isinstance(item, dict) and isinstance(item.get("entities"), list)
        }
        for row_index, row in enumerate(rows[:500]):
            if not isinstance(row, list):
                continue
            row_entities = [item for item in entity_by_row.get(row_index, []) if isinstance(item, dict)]
            primary = next(
                (
                    item for entity_type in _PRIMARY_ENTITY_PRIORITY for item in row_entities
                    if item.get("entity_type") == entity_type and _identifier(item.get("entity_key"))
                ),
                None,
            )
            if not primary:
                continue
            record = {header: row[index] if index < len(row) else "" for index, header in enumerate(headers)}
            for metric, aliases in _METRIC_ALIASES.items():
                label, raw_value = _labeled_value(record, aliases)
                value = _number(raw_value)
                if value is None:
                    continue
                observations.append({
                    "entity_key": str(primary["entity_key"]),
                    "entity_type": str(primary.get("entity_type") or ""),
                    "metric": metric,
                    "metric_contract": f"{metric}:{hashlib.sha256(_label_key(label).encode('utf-8')).hexdigest()[:12]}",
                    "value": value,
                    "unit": _METRIC_UNITS.get(metric, "number"),
                    "channel": channel,
                    "window_key": window_key,
                    "captured_at_ms": captured_at_ms,
                    "quality_score": quality_score,
                    "attribution_scope": "exact_row",
                    "evidence": {
                        "source": source,
                        "page_type": page_type,
                        "table_index": table_index,
                        "row_index": row_index,
                        "metric_label": label[:80],
                    },
                })
    return observations


def _freshest_metric_candidate(channel_metrics: dict[str, dict[str, dict[str, Any]]], metric: str) -> dict[str, Any] | None:
    candidates = [values[metric] for values in channel_metrics.values() if metric in values]
    if not candidates:
        return None
    return max(candidates, key=lambda item: (int(item.get("captured_at_ms") or 0), int(item.get("quality_score") or 0)))


def _freshest_metric(channel_metrics: dict[str, dict[str, dict[str, Any]]], metric: str) -> float | None:
    candidate = _freshest_metric_candidate(channel_metrics, metric)
    return candidate.get("value") if candidate else None


def _display_number(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "--"
    rendered = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{rendered}{suffix}"


def _decision(
    product: dict[str, Any],
    *,
    roi_target: float,
    min_spend: float,
    low_inventory: float,
    refund_limit: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    channel_metrics = product["_metric_candidates"]
    stock_candidate = _freshest_metric_candidate(channel_metrics, "stock")
    spend_candidate = _freshest_metric_candidate(channel_metrics, "spend")
    roi_candidate = _freshest_metric_candidate(channel_metrics, "roi")
    break_even_candidate = _freshest_metric_candidate(channel_metrics, "break_even_roi")
    refund_candidate = _freshest_metric_candidate(channel_metrics, "refund_rate")
    orders_candidate = _freshest_metric_candidate(channel_metrics, "orders")
    stock = stock_candidate.get("value") if stock_candidate else None
    spend = spend_candidate.get("value") if spend_candidate else None
    roi = roi_candidate.get("value") if roi_candidate else None
    break_even_roi = break_even_candidate.get("value") if break_even_candidate else None
    refund_rate = refund_candidate.get("value") if refund_candidate else None
    exposure = _freshest_metric(channel_metrics, "exposure")
    clicks = _freshest_metric(channel_metrics, "clicks")
    orders = _freshest_metric(channel_metrics, "orders")
    views = _freshest_metric(channel_metrics, "views")
    channels = set(product["channels"])

    gate_reasons: list[str] = []
    if product["identity_confidence"] != "high":
        gate_reasons.append("缺少平台商品 ID")
    if stock is None:
        gate_reasons.append("缺少可售库存")
    elif stock <= low_inventory:
        gate_reasons.append("库存不足")
    if refund_rate is None:
        gate_reasons.append("缺少退款率")
    elif refund_rate >= refund_limit:
        gate_reasons.append("退款率超过安全线")
    if break_even_roi is None:
        gate_reasons.append("缺少保本 ROI")
    if roi is None:
        gate_reasons.append("缺少商品投放 ROI")
    elif break_even_roi is not None and roi < max(roi_target, break_even_roi):
        gate_reasons.append("ROI 未达到目标与保本线")
    if spend is None or spend < min_spend:
        gate_reasons.append("消耗样本不足")
    if orders_candidate is None or float(orders_candidate.get("value") or 0) < 3:
        gate_reasons.append("成交样本不足")
    if not product.get("_ads_plan_mapped"):
        gate_reasons.append("缺少精确计划 ID—商品映射")
    if not product.get("_ads_account_confirmed"):
        gate_reasons.append("千川账户未确认")
    required_candidates = [stock_candidate, refund_candidate, break_even_candidate, spend_candidate, roi_candidate]
    if any(item is not None and int(item.get("quality_score") or 0) < 60 for item in required_candidates):
        gate_reasons.append("关键证据质量不足")
    if spend_candidate and roi_candidate and int(spend_candidate.get("captured_at_ms") or 0) != int(roi_candidate.get("captured_at_ms") or 0):
        gate_reasons.append("ROI 与消耗口径未配对")

    promotion_gate = {
        "scale_allowed": not gate_reasons,
        "reasons": gate_reasons,
        "policy": "只有精确商品身份、库存安全、退款/保本线安全且已关联千川计划时才允许进入放量复核。",
    }

    if stock is not None and stock <= 0:
        return ({
            "level": "high", "stage": "inventory", "label": "库存已断货，先停止继续放量",
            "action": "先核对可售库存与补货时间；相关计划只进入暂停或降量人工复核，不继续加预算。",
            "acceptance": "可售库存恢复并确认履约时效后，重新同步商品与千川页面。",
            "evidence": f"可售库存 {_display_number(stock)}",
        }, promotion_gate)
    if roi is not None and break_even_roi is not None and roi < break_even_roi:
        return ({
            "level": "high", "stage": "profit", "label": "表面有成交，但仍低于商品保本线",
            "action": "先核对毛利、退款与履约成本，再决定降预算、改承接或暂停；不要按账户统一 ROI 盲目放量。",
            "acceptance": f"商品 ROI 达到保本线 {_display_number(break_even_roi)} 以上，且库存与退款门槛同时通过。",
            "evidence": f"当前 ROI {_display_number(roi)} · 保本 ROI {_display_number(break_even_roi)}",
        }, promotion_gate)
    if refund_rate is not None and refund_rate >= refund_limit:
        return ({
            "level": "high", "stage": "after_sale", "label": "退款风险正在吞噬投放价值",
            "action": "先核对商品描述、尺码/规格、履约和主播承诺，再评估是否限制该商品投放。",
            "acceptance": f"退款率降到 {_display_number(refund_limit, '%')} 以下，并用同一商品的新快照回读。",
            "evidence": f"退款率 {_display_number(refund_rate, '%')}",
        }, promotion_gate)
    if spend is not None and spend >= min_spend and roi is not None and roi < roi_target:
        return ({
            "level": "high", "stage": "ads", "label": "商品投放已过判断门槛，ROI 仍未达标",
            "action": "进入该商品关联计划逐条复核素材、出价和预算；只处理精确关联计划，并保留回读窗口。",
            "acceptance": f"同一商品 ROI 回到 {_display_number(roi_target)} 以上，或完成受控止损并取得新快照。",
            "evidence": f"消耗 ¥{_display_number(spend)} · ROI {_display_number(roi)}",
        }, promotion_gate)
    if len(channels) < 2 or "ads" not in channels:
        missing = "、".join(_CHANNEL_LABELS[key] for key in ("inventory", "shelf", "live", "content", "ads") if key not in channels)
        return ({
            "level": "warning", "stage": "mapping", "label": "经营链尚未完整，不能做跨渠道归因",
            "action": "同步同一商品的商品/库存与千川计划页；平台 ID 不完整时不要按同名商品自动合并。",
            "acceptance": "至少用平台商品 ID 精确关联两个经营环节，并显示数据来源与采集时间。",
            "evidence": f"已关联 {len(channels)} 个环节 · 待补 {missing or '关联证据'}",
        }, promotion_gate)
    if exposure is not None and exposure > 0 and (clicks is None or clicks <= 0):
        return ({
            "level": "warning", "stage": "shelf", "label": "有曝光但没有形成商品点击",
            "action": "优先复核主图、标题、价格利益点与人群匹配，不先扩大投放。",
            "acceptance": "同一商品点击人数或点击率在新快照中改善。",
            "evidence": f"曝光 {_display_number(exposure)} · 点击 {_display_number(clicks)}",
        }, promotion_gate)
    if clicks is not None and clicks > 0 and (orders is None or orders <= 0):
        return ({
            "level": "warning", "stage": "conversion", "label": "已经有人点击，但商品承接没有转成订单",
            "action": "复核详情页、到手价、评价、库存规格与直播讲解承诺，再决定是否继续引流。",
            "acceptance": "同一商品产生订单，或点击成交率在新快照中改善。",
            "evidence": f"点击 {_display_number(clicks)} · 订单 {_display_number(orders)}",
        }, promotion_gate)
    if views is not None and views > 0 and clicks is not None and clicks <= 0:
        return ({
            "level": "warning", "stage": "content", "label": "内容有观看，但没有带动商品兴趣",
            "action": "只改一个内容变量：前三秒钩子、利益点或商品露出，并保留原素材作为对照。",
            "acceptance": "同一商品关联内容的商品点击人数在下一观察窗改善。",
            "evidence": f"观看 {_display_number(views)} · 商品点击 {_display_number(clicks)}",
        }, promotion_gate)
    if promotion_gate["scale_allowed"] and roi is not None and roi >= roi_target:
        return ({
            "level": "opportunity", "stage": "scale", "label": "商品具备进入小步放量复核的条件",
            "action": "只对精确关联计划生成小幅加预算草稿；提交前再次检查库存、退款和保本 ROI。",
            "acceptance": "放量后按同一商品回读 ROI、订单与库存，任何安全线失守立即停止。",
            "evidence": f"ROI {_display_number(roi)} · 库存 {_display_number(stock)}",
        }, promotion_gate)
    return ({
        "level": "info", "stage": "observe", "label": "暂未发现可安全归因的单品异常",
        "action": "保持观察，下一次巡店继续补齐商品与千川、内容、直播的精确关联。",
        "acceptance": "至少两个可比较时间窗口径一致，且同一商品身份未发生冲突。",
        "evidence": f"已关联 {len(channels)} 个经营环节",
    }, promotion_gate)


def build_product_operating_graph(
    records: list[dict[str, Any]],
    *,
    store_key: str = "",
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize product rows and return a read-only, provenance-rich graph."""

    settings = settings or {}
    scope = str(store_key or "").strip().lower()
    products: dict[str, dict[str, Any]] = {}
    unresolved: list[dict[str, Any]] = []
    name_to_keys: dict[str, set[str]] = {}
    channel_coverage = {key: 0 for key in _CHANNEL_LABELS}

    # Learn only exact same-row SKU/merchant-code -> product-ID links. If one
    # secondary identifier ever points at multiple products, keep it unresolved
    # instead of silently merging catalog items.
    secondary_claims: dict[tuple[str, str], set[str]] = {}
    for entry in records:
        record = entry.get("record") if isinstance(entry, dict) else None
        if not isinstance(record, dict):
            continue
        product_id = _identifier(_value(record, _IDENTITY_ALIASES["product_id"]))
        if not product_id:
            continue
        for kind in ("sku_id", "merchant_code"):
            secondary_id = _identifier(_value(record, _IDENTITY_ALIASES[kind]))
            if secondary_id:
                secondary_claims.setdefault((kind, secondary_id), set()).add(product_id)
    secondary_to_product = {
        key: next(iter(product_ids))
        for key, product_ids in secondary_claims.items()
        if len(product_ids) == 1
    }
    identity_link_conflicts = [
        {"identity_kind": kind, "identity_value": value, "product_count": len(product_ids), "reason": "同一辅助标识关联多个商品 ID，系统未自动合并"}
        for (kind, value), product_ids in secondary_claims.items()
        if len(product_ids) > 1
    ]

    for entry in records:
        if not isinstance(entry, dict) or not isinstance(entry.get("record"), dict):
            continue
        record = entry["record"]
        name = str(_value(record, _NAME_ALIASES) or "").strip()[:120]
        identities: dict[str, str] = {}
        for candidate_kind in ("product_id", "sku_id", "merchant_code"):
            candidate = _identifier(_value(record, _IDENTITY_ALIASES[candidate_kind]))
            if candidate:
                identities[candidate_kind] = candidate
        identity_kind = ""
        identity_value = ""
        linked_by = ""
        if identities.get("product_id"):
            identity_kind, identity_value = "product_id", identities["product_id"]
        else:
            for secondary_kind in ("sku_id", "merchant_code"):
                secondary_value = identities.get(secondary_kind, "")
                linked_product = secondary_to_product.get((secondary_kind, secondary_value)) if secondary_value else None
                if linked_product:
                    identity_kind, identity_value, linked_by = "product_id", linked_product, secondary_kind
                    break
            if not identity_value:
                for candidate_kind in ("sku_id", "merchant_code"):
                    if identities.get(candidate_kind):
                        identity_kind, identity_value = candidate_kind, identities[candidate_kind]
                        break
        channel = _channel(entry)
        if channel == "other":
            continue
        if not identity_value:
            if name:
                unresolved.append({
                    "name": name,
                    "channel": channel,
                    "source": str(entry.get("source") or ""),
                    "page_type": str(entry.get("page_type") or ""),
                    "captured_at_ms": int(entry.get("captured_at_ms") or 0),
                    "reason": "缺少平台商品 ID、SKU ID 或商家编码",
                })
            continue

        canonical_ref = f"{identity_kind}:{identity_value}"
        product_key = hashlib.sha256(f"{scope or 'unconfirmed'}|{canonical_ref}".encode("utf-8")).hexdigest()[:24]
        product = products.setdefault(product_key, {
            "product_key": product_key,
            "entity_ref": canonical_ref,
            "identity_kind": identity_kind,
            "identity_confidence": "high" if identity_kind in {"product_id", "sku_id"} else "medium",
            "product_id": identity_value if identity_kind == "product_id" else None,
            "sku_id": identity_value if identity_kind == "sku_id" else None,
            "merchant_code": identity_value if identity_kind == "merchant_code" else None,
            "product_name": name or "未命名商品",
            "identity_aliases": [],
            "channels": [],
            "evidence": [],
            "_metric_candidates": {},
            "_latest_name_at": 0,
            "_ads_plan_mapped": False,
            "_ads_account_confirmed": False,
        })
        for alias_kind, alias_value in identities.items():
            alias = {"kind": alias_kind, "value": alias_value}
            if alias not in product["identity_aliases"]:
                product["identity_aliases"].append(alias)
        captured_at_ms = int(entry.get("captured_at_ms") or 0)
        if name and captured_at_ms >= int(product.get("_latest_name_at") or 0):
            product["product_name"] = name
            product["_latest_name_at"] = captured_at_ms
        if channel not in product["channels"]:
            product["channels"].append(channel)
        evidence = {
            "source": str(entry.get("source") or ""),
            "page_type": str(entry.get("page_type") or ""),
            "channel": channel,
            "captured_at_ms": captured_at_ms,
            "quality_score": int(entry.get("quality_score") or 0),
            "account_key": str(entry.get("account_key") or ""),
            "matched_by": identity_kind,
            "linked_by": linked_by,
            "table_index": int(entry.get("table_index") or 0),
            "row_index": int(entry.get("row_index") or 0),
        }
        plan_id = _plan_identifier_value(record)
        if channel == "ads" and plan_id:
            product["_ads_plan_mapped"] = True
            product["_ads_account_confirmed"] = bool(str(entry.get("account_key") or ""))
            evidence["plan_mapping"] = "exact_row"
        product["evidence"].append(evidence)
        metric_candidates = product["_metric_candidates"].setdefault(channel, {})
        for metric, aliases in _METRIC_ALIASES.items():
            value = _number(_value(record, aliases))
            if value is None:
                continue
            previous = metric_candidates.get(metric)
            candidate = {"value": value, "captured_at_ms": captured_at_ms, "quality_score": evidence["quality_score"]}
            if previous is None or (captured_at_ms, evidence["quality_score"]) >= (int(previous.get("captured_at_ms") or 0), int(previous.get("quality_score") or 0)):
                metric_candidates[metric] = candidate

        normalized_name = _label_key(name)
        if normalized_name:
            name_to_keys.setdefault(normalized_name, set()).add(product_key)

    rows: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []
    roi_target = max(0.01, float(settings.get("roi_target") or 1.5))
    min_spend = max(0.0, float(settings.get("min_spend_for_action") or 100.0))
    low_inventory = max(0.0, float(settings.get("low_inventory_threshold") or 10.0))
    refund_limit = max(0.0, float(settings.get("refund_rate_limit") or 30.0))
    priority = {"high": 0, "warning": 1, "opportunity": 2, "info": 3}

    for product in products.values():
        decision, promotion_gate = _decision(
            product,
            roi_target=roi_target,
            min_spend=min_spend,
            low_inventory=low_inventory,
            refund_limit=refund_limit,
        )
        metrics = {
            channel: {metric: candidate["value"] for metric, candidate in values.items()}
            for channel, values in product["_metric_candidates"].items()
        }
        desired_channel = {
            "inventory": "inventory",
            "profit": "ads",
            "after_sale": "after_sale",
            "ads": "ads",
            "shelf": "shelf",
            "conversion": "shelf",
            "content": "content",
            "scale": "ads",
        }.get(decision["stage"])
        relevant_evidence = [item for item in product["evidence"] if item.get("channel") == desired_channel]
        latest_evidence = max(relevant_evidence or product["evidence"], key=lambda item: int(item.get("captured_at_ms") or 0))
        row = {
            key: value for key, value in product.items()
            if not key.startswith("_")
        }
        row.update({
            "channels": sorted(product["channels"], key=lambda key: list(_CHANNEL_LABELS).index(key)),
            "channel_labels": [_CHANNEL_LABELS[key] for key in sorted(product["channels"], key=lambda key: list(_CHANNEL_LABELS).index(key))],
            "metrics": metrics,
            "decision": decision,
            "promotion_gate": promotion_gate,
            "evidence": sorted(product["evidence"], key=lambda item: int(item.get("captured_at_ms") or 0), reverse=True)[:12],
        })
        rows.append(row)
        recommendations.append({
            "level": decision["level"],
            "owner": "商品运营",
            "title": f"{row['product_name']} · {decision['label']}",
            "action": decision["action"],
            "acceptance": decision["acceptance"],
            "evidence": decision["evidence"],
            "confidence": row["identity_confidence"],
            "action_params": {
                "operation_type": "product_operating_review",
                "target_ref": {
                    "kind": "douyin_product",
                    "id": row["product_key"],
                    "name": row["product_name"],
                    "store_key": scope,
                },
                "evidence_ref": latest_evidence,
                "promotion_gate": promotion_gate,
            },
        })
        for channel in row["channels"]:
            channel_coverage[channel] += 1

    rows.sort(key=lambda item: (priority.get(item["decision"]["level"], 9), -len(item["channels"]), item["product_name"]))
    recommendations.sort(key=lambda item: (priority.get(item["level"], 9), item["title"]))
    same_name_conflicts = [
        {"name_key": name, "product_keys": sorted(keys), "reason": "同名商品存在多个稳定身份，系统未自动合并"}
        for name, keys in name_to_keys.items() if len(keys) > 1
    ]
    blockers: list[dict[str, Any]] = []
    if not scope:
        blockers.append({"code": "STORE_UNCONFIRMED", "message": "请先确认当前店铺，避免跨店串联商品。"})
    if not rows:
        blockers.append({"code": "PRODUCT_ID_MISSING", "message": "尚未读取到可稳定识别的商品 ID、SKU ID 或商家编码。"})
    if unresolved:
        blockers.append({"code": "UNRESOLVED_PRODUCT_ROWS", "message": f"{len(unresolved)} 行商品数据缺少稳定 ID，未参与跨渠道归因。"})
    if same_name_conflicts:
        blockers.append({"code": "SAME_NAME_CONFLICT", "message": f"发现 {len(same_name_conflicts)} 组同名不同商品，已保持分离。"})
    if identity_link_conflicts:
        blockers.append({"code": "IDENTITY_LINK_CONFLICT", "message": f"发现 {len(identity_link_conflicts)} 个 SKU/商家编码指向多个商品，已停止自动合并。"})
    cross_channel = sum(1 for item in rows if len(item["channels"]) >= 2)
    status = "missing" if not rows else "partial" if blockers or cross_channel < len(rows) else "ready"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "status_label": {"ready": "单品链已建立", "partial": "单品链待补齐", "missing": "等待商品身份"}[status],
        "store_key": scope,
        "summary": {
            "products": len(rows),
            "cross_channel_products": cross_channel,
            "actionable_products": sum(1 for item in rows if item["decision"]["level"] in {"high", "warning", "opportunity"}),
            "blocked_from_scaling": sum(1 for item in rows if not item["promotion_gate"]["scale_allowed"]),
            "unresolved_rows": len(unresolved),
            "same_name_conflicts": len(same_name_conflicts),
        },
        "channel_coverage": channel_coverage,
        "products": rows[:50],
        "recommendations": recommendations[:20],
        "unresolved": unresolved[:30],
        "identity_conflicts": same_name_conflicts[:20],
        "identity_link_conflicts": identity_link_conflicts[:20],
        "blockers": blockers,
        "mode": "read_only",
        "governance": {
            "name_only_join_allowed": False,
            "missing_values_default_to_zero": False,
            "platform_write_enabled": False,
            "raw_platform_ids_persisted": False,
        },
    }
