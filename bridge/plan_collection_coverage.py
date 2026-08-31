"""Fail-closed coverage receipts for read-only Qianchuan plan collection.

The browser snapshot is evidence of what was visible, not proof that every
platform row was collected.  This module deliberately keeps those concepts
separate so product surfaces can say "collected N rows" without claiming full
coverage unless an unambiguous platform total and all row identities/modes are
also verified.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


RECEIPT_SCHEMA_VERSION = 1
CONFIRMED_PROMOTION_MODES = frozenset({"standard", "full_domain", "chengfang", "suixintui"})
_MAX_SAFE_TOTAL = 1_000_000
_TOTAL_FIELD_NAMES = (
    "platform_total",
    "platform_plan_total",
    "plan_total",
    "total_plans",
)
_TOTAL_METRIC_KEYS = frozenset({
    "计划总数",
    "计划数量",
    "投放计划数",
    "投放计划总数",
    "当前计划",
    "全部计划",
})
_TOTAL_TEXT_PATTERNS = (
    re.compile(r"共\s*([0-9][0-9,，]*)\s*(?:条|个)?\s*(?:投放)?计划", re.IGNORECASE),
    re.compile(r"(?:计划总数|计划数量|投放计划数|投放计划总数|当前计划|全部计划)\s*[:：]?\s*([0-9][0-9,，]*)\s*(?:条|个)?", re.IGNORECASE),
)


def _safe_non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not value.is_integer():
            return None
        parsed = int(value)
    else:
        text = str(value or "").strip().replace(",", "").replace("，", "")
        match = re.fullmatch(r"([0-9]+)(?:\.0+)?(?:\s*(?:条|个))?", text)
        if not match:
            return None
        parsed = int(match.group(1))
    return parsed if 0 <= parsed <= _MAX_SAFE_TOTAL else None


def _normalized_metric_key(value: Any) -> str:
    return re.sub(r"[\s_\-:：()（）]", "", str(value or "")).strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _snapshot_payload(snapshot: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    wrapped = _mapping(snapshot.get("snapshot"))
    envelope = wrapped or snapshot
    data = _mapping(envelope.get("data"))
    return envelope, data or envelope


def _structured_total_candidates(data: Mapping[str, Any]) -> list[tuple[int, str]]:
    quality = _mapping(data.get("quality"))
    nested_containers = (
        (data, "snapshot"),
        (quality, "quality"),
        (_mapping(data.get("collection_receipt")), "collection_receipt"),
        (_mapping(data.get("plan_collection")), "plan_collection"),
        (_mapping(data.get("plan_collection_coverage")), "plan_collection_coverage"),
        (_mapping(quality.get("collection_receipt")), "quality.collection_receipt"),
        (_mapping(quality.get("plan_collection")), "quality.plan_collection"),
        (_mapping(quality.get("plan_collection_coverage")), "quality.plan_collection_coverage"),
        (_mapping(quality.get("plan_identity_coverage")), "quality.plan_identity_coverage"),
    )
    candidates: list[tuple[int, str]] = []
    for container, prefix in nested_containers:
        for field in _TOTAL_FIELD_NAMES:
            if field not in container:
                continue
            parsed = _safe_non_negative_int(container.get(field))
            if parsed is not None:
                candidates.append((parsed, f"{prefix}.{field}"))
    return candidates


def _metric_total_candidates(data: Mapping[str, Any]) -> list[tuple[int, str]]:
    candidates: list[tuple[int, str]] = []
    for container_name in ("safe_metrics", "metrics"):
        for label, value in _mapping(data.get(container_name)).items():
            if _normalized_metric_key(label) not in _TOTAL_METRIC_KEYS:
                continue
            parsed = _safe_non_negative_int(value)
            if parsed is not None:
                candidates.append((parsed, container_name))
    return candidates


def _safe_text_values(data: Mapping[str, Any]) -> Iterable[str]:
    signals = data.get("signals")
    if isinstance(signals, list):
        for value in signals[:100]:
            if isinstance(value, (str, int, float)):
                yield str(value)[:500]
    page_text = data.get("page_text")
    if isinstance(page_text, str) and page_text:
        yield page_text[:120_000]
    tables = data.get("tables")
    if not isinstance(tables, list):
        return
    for table in tables[:80]:
        rows = table.get("rows") if isinstance(table, Mapping) else None
        if not isinstance(rows, list):
            continue
        for row in rows[:2_000]:
            if not isinstance(row, list):
                continue
            for value in row[:80]:
                text = str(value or "").strip()
                # Only compact summary cells are candidates.  This avoids
                # scanning arbitrary plan names or large DOM fragments.
                if text and len(text) <= 80:
                    yield text


def _text_total_candidates(data: Mapping[str, Any]) -> list[tuple[int, str]]:
    candidates: list[tuple[int, str]] = []
    for text in _safe_text_values(data):
        for pattern in _TOTAL_TEXT_PATTERNS:
            for match in pattern.finditer(text):
                parsed = _safe_non_negative_int(match.group(1))
                if parsed is not None:
                    candidates.append((parsed, "visible_total_label"))
    return candidates


def snapshot_plan_coverage_evidence(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Extract only numeric/safe coverage evidence from one stored snapshot."""

    envelope, data = _snapshot_payload(snapshot)
    quality = _mapping(data.get("quality"))
    server_identity = _mapping(quality.get("server_plan_identity_coverage"))
    plan_collection = _mapping(data.get("plan_collection"))
    identity_quarantined = bool(
        (_safe_non_negative_int(server_identity.get("quarantined_rows")) or 0) > 0
        or (_safe_non_negative_int(plan_collection.get("quarantined_rows")) or 0) > 0
        or _mapping(data.get("plan_row_quarantine")).get("active") is True
    )
    groups = (
        _structured_total_candidates(data),
        _metric_total_candidates(data),
        _text_total_candidates(data),
    )
    # Structured totals outrank metrics and visible labels.  Lower-priority
    # page-size labels must not contradict a server/API-provided platform total.
    candidates = next((group for group in groups if group), [])
    totals = sorted({value for value, _source in candidates})
    sources = sorted({source for _value, source in candidates})
    promotion_context = _mapping(data.get("promotion_context"))
    account_scope = _mapping(promotion_context.get("account_scope"))
    account = _mapping(data.get("account"))
    captured_at = data.get("captured_at") or envelope.get("timestamp") or 0
    try:
        captured_at_ms = int(float(captured_at) * (1000 if float(captured_at) < 10_000_000_000 else 1))
    except (TypeError, ValueError, OverflowError):
        captured_at_ms = 0
    return {
        "source": str(snapshot.get("source") or data.get("source") or envelope.get("source") or "")[:32],
        "page_type": str(snapshot.get("page_type") or data.get("page_type") or envelope.get("page_type") or "")[:48],
        "account_key": str(account.get("key") or account_scope.get("account_id") or "").lower()[:128],
        "promotion_mode": str(promotion_context.get("promotion_mode") or "unknown")[:32],
        "captured_at_ms": max(0, captured_at_ms),
        "platform_totals": totals,
        "platform_total_sources": sources,
        "platform_total_conflict": len(totals) > 1,
        "pagination_truncated": quality.get("pagination_truncated") is True
        or data.get("pagination_truncated") is True,
        "identity_quarantined": identity_quarantined,
        "pages_scanned": max(0, _safe_non_negative_int(quality.get("pages_scanned")) or 0),
    }


def _scope_plan_type(page_type: Any) -> str:
    normalized = str(page_type or "").strip().lower()
    if normalized in {"campaigns", "qianchuan_campaigns"}:
        return "product"
    if normalized == "qianchuan_live":
        return "live"
    return "unknown"


def _scope_key(account_key: Any, promotion_mode: Any, plan_type: Any) -> str:
    account = str(account_key or "").strip().lower()[:128] or "unknown-account"
    mode = str(promotion_mode or "unknown").strip().lower()[:32]
    if mode not in CONFIRMED_PROMOTION_MODES:
        mode = "unknown"
    kind = str(plan_type or "unknown").strip().lower()[:24]
    if kind not in {"live", "product"}:
        kind = "unknown"
    return f"{account}|{mode}|{kind}"


def build_scoped_plan_collection_receipts(
    rows: Iterable[Mapping[str, Any]],
    snapshots: Iterable[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Build independently verifiable receipts for one account/mode/type scope.

    Product, live and mixed report pages expose different platform totals.  A
    global receipt must stay fail-closed, while a narrowly selected workbench
    view can still prove its own coverage without comparing unrelated totals.
    """

    normalized_rows = [row for row in rows if isinstance(row, Mapping)]
    normalized_snapshots = [snapshot for snapshot in snapshots if isinstance(snapshot, Mapping)]
    row_groups: dict[str, list[Mapping[str, Any]]] = {}
    snapshot_groups: dict[str, list[Mapping[str, Any]]] = {}
    scope_values: dict[str, dict[str, str]] = {}

    for row in normalized_rows:
        account = str(row.get("account_key") or "").strip().lower()[:128]
        mode = str(row.get("promotion_mode") or "unknown").strip().lower()[:32]
        plan_type = str(row.get("plan_type") or "unknown").strip().lower()[:24]
        key = _scope_key(account, mode, plan_type)
        row_groups.setdefault(key, []).append(row)
        scope_values[key] = {
            "account_key": account,
            "promotion_mode": mode if mode in CONFIRMED_PROMOTION_MODES else "unknown",
            "plan_type": plan_type if plan_type in {"live", "product"} else "unknown",
        }

    for snapshot in normalized_snapshots:
        evidence = snapshot_plan_coverage_evidence(snapshot)
        account = str(evidence.get("account_key") or "").strip().lower()[:128]
        mode = str(evidence.get("promotion_mode") or "unknown").strip().lower()[:32]
        plan_type = _scope_plan_type(evidence.get("page_type"))
        key = _scope_key(account, mode, plan_type)
        snapshot_groups.setdefault(key, []).append(snapshot)
        scope_values[key] = {
            "account_key": account,
            "promotion_mode": mode if mode in CONFIRMED_PROMOTION_MODES else "unknown",
            "plan_type": plan_type,
        }

    receipts: list[dict[str, Any]] = []
    for key in sorted(set(row_groups) | set(snapshot_groups)):
        scoped_rows = row_groups.get(key, [])
        scoped_snapshots = snapshot_groups.get(key, [])
        receipt = build_plan_collection_receipt(scoped_rows, scoped_snapshots)
        scope = scope_values[key]
        scope_identity_verified = bool(
            scope["account_key"]
            and scope["promotion_mode"] in CONFIRMED_PROMOTION_MODES
            and scope["plan_type"] in {"live", "product"}
        )
        diagnosis_ready = bool(receipt["safe_to_claim_complete"] and scope_identity_verified)
        qualification_blockers: list[dict[str, str]] = []
        if not scope_identity_verified:
            qualification_blockers.append(_warning(
                "PLAN_SCOPE_IDENTITY_UNVERIFIED",
                "计划采集范围缺少已核验的广告账户、投放模式或计划类型。",
            ))
        if not receipt["safe_to_claim_complete"]:
            qualification_blockers.append(_warning(
                "PLAN_SCOPE_COVERAGE_INCOMPLETE",
                "当前账户、模式与计划类型的采集回执尚未完整，不能进入计划诊断或受监督草稿。",
            ))
        if diagnosis_ready and int(receipt.get("collected_rows") or 0) <= 0:
            qualification_blockers.append(_warning(
                "PLAN_SCOPE_CONFIRMED_EMPTY",
                "平台已确认当前范围没有计划，因此没有可生成的受监督草稿。",
            ))
        receipts.append({
            **receipt,
            "scope_key": key,
            "scope": scope,
            "scope_identity_verified": scope_identity_verified,
            "diagnosis_ready": diagnosis_ready,
            "supervised_draft_ready": bool(
                diagnosis_ready and int(receipt.get("collected_rows") or 0) > 0
            ),
            "qualification_blockers": qualification_blockers,
        })
    return receipts


def _percentage(numerator: int, denominator: int, *, empty_complete: bool = False) -> int:
    if denominator <= 0:
        return 100 if empty_complete else 0
    return round(numerator / denominator * 100)


def _warning(code: str, label: str) -> dict[str, str]:
    return {"code": code, "label": label}


def _has_stable_plan_id(value: Any) -> bool:
    text = str(value or "").strip()
    if text.lower() in {
        "", "-", "--", "暂无", "未知", "unavailable", "unknown", "null", "none",
        "masked", "missing", "undefined", "n/a", "na", "[masked]", "[已隐藏]", "[标识无效]",
    }:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{4,80}", text))


def build_plan_collection_receipt(
    rows: Iterable[Mapping[str, Any]],
    snapshots: Iterable[Mapping[str, Any]] = (),
    *,
    displayed_rows: int | None = None,
) -> dict[str, Any]:
    """Build a UI-safe, read-only receipt without assuming complete coverage."""

    normalized_rows = [row for row in rows if isinstance(row, Mapping)]
    collected_rows = len(normalized_rows)
    displayed = collected_rows if displayed_rows is None else max(0, min(int(displayed_rows), collected_rows))
    stable_plan_ids = [
        str(row.get("plan_id") or "").strip()
        for row in normalized_rows
        if _has_stable_plan_id(row.get("plan_id"))
    ]
    stable_id_rows = len(stable_plan_ids)
    unique_stable_id_rows = len(set(stable_plan_ids))
    duplicate_plan_id_rows = stable_id_rows - unique_stable_id_rows
    mode_confirmed_rows = sum(
        str(row.get("promotion_mode") or "unknown") in CONFIRMED_PROMOTION_MODES
        for row in normalized_rows
    )

    evidence = [
        snapshot_plan_coverage_evidence(snapshot)
        for snapshot in snapshots
        if isinstance(snapshot, Mapping)
    ]
    observations = [
        (value, item)
        for item in evidence
        for value in item["platform_totals"]
    ]
    observed_values = sorted({value for value, _item in observations})
    internal_total_conflict = any(item["platform_total_conflict"] for item in evidence)
    total_conflict = internal_total_conflict or len(observed_values) > 1
    platform_total = observed_values[0] if len(observed_values) == 1 and not total_conflict else None
    scope_keys = {
        (
            item.get("account_key") or "unknown-account",
            item.get("promotion_mode") or "unknown",
            item.get("page_type") or "unknown-page",
        )
        for _value, item in observations
    }
    multiple_total_scopes = len(scope_keys) > 1
    pagination_truncated = any(item["pagination_truncated"] for item in evidence)
    identity_quarantined = any(item["identity_quarantined"] for item in evidence)
    list_truncated = displayed < collected_rows
    platform_total_observed = platform_total is not None
    explicit_empty = platform_total == 0 and collected_rows == 0
    stable_id_coverage = _percentage(stable_id_rows, collected_rows, empty_complete=explicit_empty)
    mode_confirmed_coverage = _percentage(mode_confirmed_rows, collected_rows, empty_complete=explicit_empty)
    if platform_total is None:
        platform_coverage = None
    elif platform_total == 0:
        platform_coverage = 100 if collected_rows == 0 else 0
    else:
        platform_coverage = round(collected_rows / platform_total * 100)

    warnings: list[dict[str, str]] = []
    if total_conflict:
        warnings.append(_warning("PLATFORM_TOTAL_CONFLICT", "页面中的计划总数证据不一致，不能判断是否全量采集。"))
    elif not platform_total_observed:
        warnings.append(_warning("PLATFORM_TOTAL_UNOBSERVED", "尚未可靠读取平台计划总数，不能宣称全量采集。"))
    if multiple_total_scopes:
        warnings.append(_warning("MULTIPLE_TOTAL_SCOPES", "检测到多个账户、模式或页面范围的总数，不能合并为一个全量口径。"))
    if pagination_truncated:
        warnings.append(_warning("PAGINATION_TRUNCATED", "分页采集已触达本轮上限，当前计划列表可能不完整。"))
    if list_truncated:
        warnings.append(_warning("LOCAL_LIST_TRUNCATED", "本地计划列表已达到展示上限，请缩小筛选范围。"))
    if platform_total is not None and collected_rows < platform_total:
        warnings.append(_warning("COLLECTED_ROWS_BELOW_PLATFORM_TOTAL", "已采集计划少于平台显示总数，请继续翻页或滚动后重试。"))
    if platform_total is not None and collected_rows > platform_total:
        warnings.append(_warning("COLLECTED_ROWS_EXCEED_PLATFORM_TOTAL", "已采集计划多于平台显示总数，可能混入了不同筛选范围或历史快照。"))
    if stable_id_rows < collected_rows:
        warnings.append(_warning("STABLE_PLAN_ID_INCOMPLETE", "部分计划缺少稳定计划 ID，不能用于绑定或自动操作。"))
    if duplicate_plan_id_rows:
        warnings.append(_warning(
            "DUPLICATE_PLAN_IDENTITIES",
            "同一计划 ID 被重复采集，当前行数不能证明平台计划覆盖完整，请刷新并重新采集。",
        ))
    if identity_quarantined:
        warnings.append(_warning(
            "PLAN_ROWS_QUARANTINED",
            "部分计划缺少稳定 ID；有效行已保留，异常行已隔离，自动操作继续关闭。",
        ))
    if mode_confirmed_rows < collected_rows:
        warnings.append(_warning("PROMOTION_MODE_INCOMPLETE", "部分计划的投放模式尚未验真，不能进入自动操作。"))

    coverage_complete = bool(
        platform_total_observed
        and not total_conflict
        and not multiple_total_scopes
        and not pagination_truncated
        and not identity_quarantined
        and not list_truncated
        and collected_rows == platform_total
        and stable_id_rows == collected_rows
        and unique_stable_id_rows == collected_rows
        and mode_confirmed_rows == collected_rows
    )
    if coverage_complete and platform_total == 0:
        status, status_label = "confirmed_empty", "平台已确认当前没有计划"
    elif coverage_complete:
        status, status_label = "complete", "计划采集覆盖已确认"
    elif total_conflict or duplicate_plan_id_rows or (platform_total is not None and collected_rows > platform_total):
        status, status_label = "inconsistent", "计划采集口径不一致"
    elif identity_quarantined or pagination_truncated or list_truncated or (platform_total is not None and collected_rows < platform_total):
        status, status_label = "partial", "计划尚未采集完整"
    elif collected_rows:
        status, status_label = "unverified", "已采集计划，覆盖范围待确认"
    else:
        status, status_label = "missing", "尚未采集到可确认的计划"

    platform_total_label = (
        f"平台显示 {platform_total} 条计划"
        if platform_total is not None
        else "平台计划总数未确认"
    )
    platform_coverage_label = (
        f"已采集 {collected_rows}/{platform_total} 条"
        if platform_total is not None
        else f"已采集 {collected_rows} 条，平台总数待确认"
    )
    labels = {
        "status": status_label,
        "platform_total": platform_total_label,
        "collected_rows": f"已采集 {collected_rows} 条计划",
        "displayed_rows": f"当前展示 {displayed} 条计划",
        "platform_coverage": platform_coverage_label,
        "stable_id_coverage": f"{stable_id_rows}/{collected_rows} 条具备稳定计划 ID" if collected_rows else "暂无计划 ID 可核验",
        "mode_confirmed_coverage": f"{mode_confirmed_rows}/{collected_rows} 条已确认投放模式" if collected_rows else "暂无投放模式可核验",
    }
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "read_only": True,
        "status": status,
        "status_label": status_label,
        "coverage_complete": coverage_complete,
        "safe_to_claim_complete": coverage_complete,
        "platform_total": platform_total,
        "platform_total_observed": platform_total_observed,
        "observed_platform_totals": observed_values,
        "platform_total_conflict": total_conflict,
        "platform_total_scope_count": len(scope_keys),
        "collected_rows": collected_rows,
        "displayed_rows": displayed,
        "stable_id_rows": stable_id_rows,
        "unique_stable_id_rows": unique_stable_id_rows,
        "duplicate_plan_id_rows": duplicate_plan_id_rows,
        "stable_id_coverage": stable_id_coverage,
        "mode_confirmed_rows": mode_confirmed_rows,
        "mode_confirmed_coverage": mode_confirmed_coverage,
        "platform_coverage": platform_coverage,
        "pagination_truncated": pagination_truncated,
        "identity_quarantined": identity_quarantined,
        "local_list_truncated": list_truncated,
        "coverage_warnings": warnings,
        "warning_labels": [warning["label"] for warning in warnings],
        "labels": labels,
    }
