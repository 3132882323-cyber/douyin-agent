"""Privacy-minimized, deterministic context packs for external AI models.

The model never receives raw page text, browser credentials, customer/order
records, or real store/account identifiers.  This module deliberately accepts
larger source dictionaries but projects them onto a small, documented
allowlist before hashing or returning anything.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

AI_CONTEXT_SCHEMA_VERSION = 1
MAX_AI_EVIDENCE_ITEMS = 100

SUPPORTED_AI_ACTIONS = frozenset({"hold", "decrease_budget", "pause", "restore"})

# Aggregates only.  Order, customer and creative records are intentionally not
# represented.  ``orders`` and ``refund_orders`` are counts, never row data.
ALLOWED_METRIC_KEYS = frozenset(
    {
        "spend",
        "budget",
        "bid",
        "balance",
        "roi",
        "pay_roi",
        "gmv",
        "live_gmv",
        "revenue",
        "orders",
        "refund_orders",
        "conversions",
        "impressions",
        "clicks",
        "ctr",
        "cvr",
        "cpc",
        "cpm",
        "cost_per_order",
        "refund_rate",
        "gross_margin_rate",
        "inventory",
        "viewer_count",
        "status",
    }
)

_STATUS_VALUES = frozenset({"active", "paused", "ended", "draft", "limited", "unknown"})
_SAFE_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_SAFE_PLAN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")

_DEFAULT_CONSTRAINTS: dict[str, Any] = {
    "allowed_actions": ["hold"],
    "max_budget_decrease_percent": 10.0,
    "max_actions_per_hour": 3,
    "max_daily_budget_impact": 0.0,
    "min_quality_score": 70,
    "max_data_age_seconds": 600,
    "min_confidence": 0.75,
    "proposal_ttl_seconds": 300,
}

_CONTEXT_FIELDS = frozenset({
    "schema_version", "generated_at_ms", "expires_at_ms", "identity", "evidence",
    "constraints", "privacy_contract", "context_hash", "context_id",
})
_IDENTITY_FIELDS = frozenset({"store_ref", "account_ref", "identity_bound"})
_EVIDENCE_FIELDS = frozenset({
    "evidence_ref", "source", "page_type", "account_ref", "plan_id",
    "captured_at_ms", "data_age_seconds", "quality", "metrics", "snapshot_hash",
})
_QUALITY_FIELDS = frozenset({"score", "completeness", "confidence", "fresh", "future_timestamp"})
_CONSTRAINT_FIELDS = frozenset({
    *tuple(_DEFAULT_CONSTRAINTS),
    "budget_increase_allowed", "human_confirmation_required", "preflight_reread_required",
    "postflight_verification_required", "execution_enabled",
})
_PRIVACY_FIELDS = frozenset({
    "raw_identity_included", "page_text_included", "credentials_included",
    "order_rows_included", "aggregate_metrics_only",
})


def _canonical_value(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {str(key): _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def _sha256_json(value: Any) -> str:
    raw = json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return round(number, 6) if math.isfinite(number) else None


def _bounded_integer(value: Any, *, default: int, low: int, high: int) -> int:
    number = _finite_number(value)
    if number is None:
        return default
    return max(low, min(high, int(number)))


def _bounded_number(value: Any, *, default: float, low: float, high: float) -> float:
    number = _finite_number(value)
    if number is None:
        return default
    return round(max(low, min(high, number)), 6)


def _safe_token(value: Any, *, fallback: str, limit: int = 64) -> str:
    token = str(value or "").strip()
    if not token or not _SAFE_REFERENCE_RE.fullmatch(token[:limit]):
        return fallback
    return token[:limit]


def _anonymization_key(secret: str | bytes) -> bytes:
    if isinstance(secret, str):
        key = secret.encode("utf-8")
    elif isinstance(secret, bytes):
        key = secret
    else:
        raise TypeError("anonymization_secret must be str or bytes")
    if len(key) < 16:
        raise ValueError("anonymization_secret must contain at least 16 bytes")
    return key


def anonymized_ref(kind: str, value: Any, *, secret: str | bytes) -> str:
    """Return a deterministic, installation-scoped pseudonym."""

    key = _anonymization_key(secret)
    normalized_kind = "store" if kind == "store" else "account"
    normalized_value = str(value or "").strip().lower()
    # Missing identities must not all masquerade as one verified identity.
    if not normalized_value:
        return f"{normalized_kind}_unbound"
    digest = hmac.new(
        key,
        f"dian-ai-context-v1:{normalized_kind}:{normalized_value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{normalized_kind}_{digest[:20]}"


def _normalize_constraints(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    source = raw if isinstance(raw, Mapping) else {}
    requested_actions = source.get("allowed_actions")
    allowed_actions = {"hold"}
    if isinstance(requested_actions, (list, tuple, set, frozenset)):
        allowed_actions.update(str(item).strip() for item in requested_actions)
    allowed_actions.intersection_update(SUPPORTED_AI_ACTIONS)

    return {
        "allowed_actions": sorted(allowed_actions),
        # These caps can be tightened by configuration but never relaxed past
        # the hard product guardrails.
        "max_budget_decrease_percent": _bounded_number(
            source.get("max_budget_decrease_percent"),
            default=float(_DEFAULT_CONSTRAINTS["max_budget_decrease_percent"]),
            low=0.0,
            high=30.0,
        ),
        "max_actions_per_hour": _bounded_integer(
            source.get("max_actions_per_hour"),
            default=int(_DEFAULT_CONSTRAINTS["max_actions_per_hour"]),
            low=0,
            high=20,
        ),
        "max_daily_budget_impact": _bounded_number(
            source.get("max_daily_budget_impact"),
            default=float(_DEFAULT_CONSTRAINTS["max_daily_budget_impact"]),
            low=0.0,
            high=1_000_000.0,
        ),
        "min_quality_score": _bounded_integer(
            source.get("min_quality_score"),
            default=int(_DEFAULT_CONSTRAINTS["min_quality_score"]),
            low=70,
            high=100,
        ),
        "max_data_age_seconds": _bounded_integer(
            source.get("max_data_age_seconds"),
            default=int(_DEFAULT_CONSTRAINTS["max_data_age_seconds"]),
            low=30,
            high=600,
        ),
        "min_confidence": _bounded_number(
            source.get("min_confidence"),
            default=float(_DEFAULT_CONSTRAINTS["min_confidence"]),
            low=0.75,
            high=1.0,
        ),
        "proposal_ttl_seconds": _bounded_integer(
            source.get("proposal_ttl_seconds"),
            default=int(_DEFAULT_CONSTRAINTS["proposal_ttl_seconds"]),
            low=30,
            high=600,
        ),
        "budget_increase_allowed": False,
        "human_confirmation_required": True,
        "preflight_reread_required": True,
        "postflight_verification_required": True,
        "execution_enabled": False,
    }


def _normalize_metrics(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, Mapping) else {}
    metrics: dict[str, Any] = {}
    for key in sorted(ALLOWED_METRIC_KEYS):
        if key not in source:
            continue
        if key == "status":
            status = str(source.get(key) or "").strip().lower()
            metrics[key] = status if status in _STATUS_VALUES else "unknown"
            continue
        number = _finite_number(source.get(key))
        if number is not None:
            metrics[key] = number
    return metrics


def _normalize_observation(
    raw: Mapping[str, Any],
    *,
    index: int,
    now_ms: int,
    max_data_age_seconds: int,
    account_ref: str,
) -> dict[str, Any] | None:
    plan_id = str(raw.get("plan_id") or "").strip()
    if plan_id and not _SAFE_PLAN_ID_RE.fullmatch(plan_id):
        return None

    captured_number = _finite_number(raw.get("captured_at_ms"))
    captured_at_ms = int(captured_number or 0)
    future_timestamp = captured_at_ms > now_ms + 60_000
    age_seconds = (
        max(0, int((now_ms - captured_at_ms) / 1000))
        if captured_at_ms > 0 and not future_timestamp
        else None
    )
    quality_score = _bounded_integer(raw.get("quality_score"), default=0, low=0, high=100)
    completeness = _bounded_number(raw.get("completeness"), default=0.0, low=0.0, high=1.0)
    confidence = str(raw.get("confidence") or "unknown").strip().lower()
    if confidence not in {"low", "medium", "high"}:
        confidence = "unknown"

    source = _safe_token(raw.get("source"), fallback="unknown")
    page_type = _safe_token(raw.get("page_type"), fallback="unknown")
    supplied_ref = str(raw.get("evidence_ref") or "").strip()
    evidence_ref = (
        supplied_ref
        if _SAFE_REFERENCE_RE.fullmatch(supplied_ref)
        else f"{source}/{page_type}/{captured_at_ms or index}"
    )
    metrics = _normalize_metrics(raw.get("metrics"))
    if not metrics:
        return None

    safe_record: dict[str, Any] = {
        "evidence_ref": evidence_ref,
        "source": source,
        "page_type": page_type,
        "account_ref": account_ref,
        "plan_id": plan_id or None,
        "captured_at_ms": captured_at_ms,
        "data_age_seconds": age_seconds,
        "quality": {
            "score": quality_score,
            "completeness": completeness,
            "confidence": confidence,
            "fresh": bool(
                captured_at_ms > 0
                and not future_timestamp
                and age_seconds is not None
                and age_seconds <= max_data_age_seconds
            ),
            "future_timestamp": future_timestamp,
        },
        "metrics": metrics,
    }
    safe_record["snapshot_hash"] = _sha256_json(safe_record)
    return safe_record


def context_integrity_hash(context_pack: Mapping[str, Any]) -> str:
    """Hash the immutable model-visible context fields."""

    return _sha256_json(
        {
            "schema_version": context_pack.get("schema_version"),
            "generated_at_ms": context_pack.get("generated_at_ms"),
            "expires_at_ms": context_pack.get("expires_at_ms"),
            "identity": context_pack.get("identity"),
            "evidence": context_pack.get("evidence"),
            "constraints": context_pack.get("constraints"),
            "privacy_contract": context_pack.get("privacy_contract"),
        }
    )


def _build_context_from_observations(
    *,
    store_id: Any,
    account_id: Any,
    observations: Iterable[Mapping[str, Any]],
    constraints: Mapping[str, Any] | None = None,
    anonymization_secret: str | bytes,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Build an integrity-bound, model-safe operating context.

    Unknown observation fields are ignored instead of recursively copied.  In
    particular this prevents ``page_text``, cookies/tokens and order rows from
    reaching any external model even if callers pass a complete page snapshot.
    """

    if now_ms is None:
        now_ms = int(time.time() * 1000)
    elif not isinstance(now_ms, int) or isinstance(now_ms, bool) or now_ms <= 0:
        raise ValueError("now_ms must be a positive integer timestamp")
    normalized_constraints = _normalize_constraints(constraints)
    store_ref = anonymized_ref("store", store_id, secret=anonymization_secret)
    account_ref = anonymized_ref("account", account_id, secret=anonymization_secret)
    normalized_observations: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    for index, raw in enumerate(observations or (), start=1):
        if not isinstance(raw, Mapping):
            continue
        item = _normalize_observation(
            raw,
            index=index,
            now_ms=now_ms,
            max_data_age_seconds=int(normalized_constraints["max_data_age_seconds"]),
            account_ref=account_ref,
        )
        if item is None or item["evidence_ref"] in seen_refs:
            continue
        # External models must not reason from expired, future-dated, or
        # below-threshold evidence.  The local product can still display those
        # rows, but they do not belong in an AI decision context.  Filtering
        # here also prevents one unrelated historical plan from expiring an
        # otherwise fresh context pack.
        quality = item.get("quality") if isinstance(item.get("quality"), Mapping) else {}
        if (
            not bool(quality.get("fresh"))
            or bool(quality.get("future_timestamp"))
            or int(quality.get("score") or 0) < int(normalized_constraints["min_quality_score"])
        ):
            continue
        seen_refs.add(item["evidence_ref"])
        normalized_observations.append(item)

    normalized_observations.sort(
        key=lambda item: (
            -int(item.get("captured_at_ms") or 0),
            -int((item.get("quality") or {}).get("score") or 0),
            str(item.get("plan_id") or ""),
        )
    )
    normalized_observations = normalized_observations[:MAX_AI_EVIDENCE_ITEMS]
    normalized_observations.sort(
        key=lambda item: (str(item.get("plan_id") or ""), str(item["evidence_ref"]))
    )
    expiry_candidates = [
        int(item["captured_at_ms"])
        + int(normalized_constraints["max_data_age_seconds"]) * 1000
        for item in normalized_observations
        if int(item["captured_at_ms"] or 0) > 0
        and not bool(item["quality"]["future_timestamp"])
    ]

    context: dict[str, Any] = {
        "schema_version": AI_CONTEXT_SCHEMA_VERSION,
        "generated_at_ms": now_ms,
        "expires_at_ms": min(expiry_candidates) if expiry_candidates else now_ms,
        "identity": {
            "store_ref": store_ref,
            "account_ref": account_ref,
            "identity_bound": bool(str(store_id or "").strip() and str(account_id or "").strip()),
        },
        "evidence": normalized_observations,
        "constraints": normalized_constraints,
        "privacy_contract": {
            "raw_identity_included": False,
            "page_text_included": False,
            "credentials_included": False,
            "order_rows_included": False,
            "aggregate_metrics_only": True,
        },
    }
    integrity_hash = context_integrity_hash(context)
    context["context_hash"] = integrity_hash
    context["context_id"] = f"ai-context-{integrity_hash[:24]}"
    return context


def _nested_value(source: Mapping[str, Any], container: str, key: str) -> Any:
    nested = source.get(container)
    return nested.get(key) if isinstance(nested, Mapping) else None


def _plan_console_observations(plan_console: Any, *, account_id: Any) -> list[dict[str, Any]]:
    console = plan_console if isinstance(plan_console, Mapping) else {}
    rows = console.get("rows") if isinstance(console.get("rows"), list) else []
    selected_account = str(account_id or "").strip().lower()
    if not selected_account:
        return []
    observations: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        row_account = str(row.get("account_key") or row.get("account_id") or "").strip().lower()
        if not row_account or row_account != selected_account:
            continue
        plan_id = str(row.get("plan_id") or "").strip()
        captured_at_ms = int(_finite_number(row.get("captured_at_ms")) or 0)
        metrics = {key: row.get(key) for key in ALLOWED_METRIC_KEYS if key in row}
        delivery_status = str(row.get("delivery_status") or "").lower()
        if "暂停" in delivery_status or "停用" in delivery_status:
            metrics["status"] = "paused"
        elif "投放" in delivery_status or "运行" in delivery_status or "启用" in delivery_status:
            metrics["status"] = "active"
        known_metric_count = sum(metrics.get(key) is not None for key in ("budget", "spend", "roi", "orders", "ctr"))
        observations.append(
            {
                "evidence_ref": f"plan-console/{plan_id or 'unbound'}/{captured_at_ms}",
                "source": "plan_console",
                "page_type": str(row.get("plan_type") or "campaigns"),
                "plan_id": plan_id,
                "captured_at_ms": captured_at_ms,
                "quality_score": row.get("quality_score"),
                "completeness": known_metric_count / 5,
                "confidence": "high" if int(row.get("quality_score") or 0) >= 70 and not row.get("stale") else "low",
                "metrics": metrics,
            }
        )
    return observations


def build_ai_context_pack(
    *,
    snapshots: Iterable[Mapping[str, Any]],
    plan_console: Mapping[str, Any] | None,
    insights: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None,
    settings: Mapping[str, Any],
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Build the public model context from existing product-domain objects.

    ``snapshots`` and ``insights`` are accepted only when an item already uses
    the explicit aggregate ``metrics`` shape.  Arbitrary text insights are
    intentionally ignored.  Identity, anonymization secret and policy are
    supplied through ``settings`` so provider adapters never need raw secrets.
    """

    if not isinstance(settings, Mapping):
        raise TypeError("settings must be a mapping")
    store_id = settings.get("store_id") or settings.get("store_key") or _nested_value(settings, "store", "key")
    account_id = settings.get("account_id") or settings.get("account_key") or _nested_value(settings, "account", "key")
    console = plan_console if isinstance(plan_console, Mapping) else {}
    secret = settings.get("anonymization_secret") or settings.get("installation_secret")
    if not isinstance(secret, (str, bytes)):
        raise ValueError("settings must contain anonymization_secret or installation_secret")
    constraints = settings.get("constraints") or settings.get("ai_policy")

    observations: list[Mapping[str, Any]] = []
    selected_account = str(account_id or "").strip().lower()
    if isinstance(snapshots, Iterable) and not isinstance(snapshots, (str, bytes, Mapping)):
        observations.extend(
            item
            for item in snapshots
            if isinstance(item, Mapping)
            and selected_account
            and str(item.get("account_key") or item.get("account_id") or "").strip().lower() == selected_account
        )
    observations.extend(_plan_console_observations(console, account_id=account_id))
    if isinstance(insights, Mapping):
        insight_items = insights.get("items") if isinstance(insights.get("items"), list) else []
    elif isinstance(insights, Iterable) and not isinstance(insights, (str, bytes)):
        insight_items = insights
    else:
        insight_items = []
    # Only structured aggregate observations survive the common projector.
    observations.extend(
        item
        for item in insight_items
        if isinstance(item, Mapping)
        and isinstance(item.get("metrics"), Mapping)
        and selected_account
        and str(item.get("account_key") or item.get("account_id") or "").strip().lower() == selected_account
    )

    return _build_context_from_observations(
        store_id=store_id,
        account_id=account_id,
        observations=observations,
        constraints=constraints if isinstance(constraints, Mapping) else None,
        anonymization_secret=secret,
        now_ms=now_ms,
    )


def validate_ai_context_pack(context_pack: Any, *, now_ms: int | None = None) -> list[dict[str, str]]:
    """Validate provenance and freshness before accepting a model proposal."""

    now_ms = int(now_ms or time.time() * 1000)
    errors: list[dict[str, str]] = []
    if not isinstance(context_pack, Mapping):
        return [{"code": "INVALID_CONTEXT", "message": "AI context must be an object."}]
    if set(context_pack) - _CONTEXT_FIELDS:
        errors.append({"code": "CONTEXT_UNKNOWN_FIELDS", "message": "AI context contains unsupported fields."})
    if context_pack.get("schema_version") != AI_CONTEXT_SCHEMA_VERSION:
        errors.append({"code": "CONTEXT_SCHEMA_MISMATCH", "message": "AI context schema is unsupported."})
    try:
        expected_hash = context_integrity_hash(context_pack)
    except (TypeError, ValueError, OverflowError):
        expected_hash = ""
        errors.append({"code": "CONTEXT_INTEGRITY_INVALID", "message": "AI context cannot be integrity hashed."})
    provided_hash = context_pack.get("context_hash")
    if not isinstance(provided_hash, str):
        provided_hash = ""
    if not expected_hash or not hmac.compare_digest(provided_hash, expected_hash):
        errors.append({"code": "CONTEXT_INTEGRITY_FAILED", "message": "AI context integrity check failed."})
    if not expected_hash or context_pack.get("context_id") != f"ai-context-{expected_hash[:24]}":
        errors.append({"code": "CONTEXT_ID_MISMATCH", "message": "AI context ID does not match its contents."})
    identity = context_pack.get("identity") if isinstance(context_pack.get("identity"), Mapping) else {}
    if not isinstance(context_pack.get("identity"), Mapping) or set(identity) - _IDENTITY_FIELDS:
        errors.append({"code": "CONTEXT_IDENTITY_INVALID", "message": "AI context identity shape is invalid."})
    elif (
        set(identity) != _IDENTITY_FIELDS
        or not isinstance(identity.get("store_ref"), str)
        or not _SAFE_REFERENCE_RE.fullmatch(identity.get("store_ref", ""))
        or not isinstance(identity.get("account_ref"), str)
        or not _SAFE_REFERENCE_RE.fullmatch(identity.get("account_ref", ""))
        or identity.get("identity_bound") is not True
    ):
        errors.append({"code": "CONTEXT_IDENTITY_INVALID", "message": "AI context identity values are invalid."})
    if identity.get("identity_bound") is not True:
        errors.append({"code": "CONTEXT_IDENTITY_UNBOUND", "message": "Store and account must be bound."})
    generated_at_ms = context_pack.get("generated_at_ms")
    expires_at_ms = context_pack.get("expires_at_ms")
    generated_at_valid = (
        isinstance(generated_at_ms, int)
        and not isinstance(generated_at_ms, bool)
        and 0 < generated_at_ms <= 10**15
    )
    expires_at_valid = (
        isinstance(expires_at_ms, int)
        and not isinstance(expires_at_ms, bool)
        and 0 < expires_at_ms <= 10**15
    )
    if not generated_at_valid:
        errors.append({"code": "CONTEXT_GENERATED_AT_INVALID", "message": "AI context generation timestamp is invalid."})
    if not expires_at_valid:
        errors.append({"code": "CONTEXT_EXPIRES_AT_INVALID", "message": "AI context expiry timestamp is invalid."})
    elif expires_at_ms <= now_ms:
        errors.append({"code": "CONTEXT_EXPIRED", "message": "AI context data is stale."})
    elif generated_at_valid and expires_at_ms <= generated_at_ms:
        errors.append({"code": "CONTEXT_EXPIRY_INVALID", "message": "AI context expiry must follow generation."})
    evidence = context_pack.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append({"code": "CONTEXT_EVIDENCE_EMPTY", "message": "AI context has no aggregate evidence."})
    elif len(evidence) > MAX_AI_EVIDENCE_ITEMS:
        errors.append({"code": "CONTEXT_EVIDENCE_LIMIT", "message": "AI context contains too many evidence items."})
    else:
        for item in evidence:
            if not isinstance(item, Mapping) or set(item) != _EVIDENCE_FIELDS:
                errors.append({"code": "CONTEXT_EVIDENCE_INVALID", "message": "AI evidence shape is invalid."})
                break
            quality = item.get("quality")
            metrics = item.get("metrics")
            if (
                not isinstance(item.get("evidence_ref"), str)
                or not _SAFE_REFERENCE_RE.fullmatch(item.get("evidence_ref", ""))
                or not isinstance(item.get("source"), str)
                or not _SAFE_REFERENCE_RE.fullmatch(item.get("source", ""))
                or not isinstance(item.get("page_type"), str)
                or not _SAFE_REFERENCE_RE.fullmatch(item.get("page_type", ""))
                or not isinstance(item.get("account_ref"), str)
                or not _SAFE_REFERENCE_RE.fullmatch(item.get("account_ref", ""))
                or (
                    item.get("plan_id") is not None
                    and (
                        not isinstance(item.get("plan_id"), str)
                        or not _SAFE_PLAN_ID_RE.fullmatch(item.get("plan_id", ""))
                    )
                )
                or not isinstance(item.get("captured_at_ms"), int)
                or isinstance(item.get("captured_at_ms"), bool)
                or item.get("captured_at_ms", 0) <= 0
                or not isinstance(item.get("data_age_seconds"), int)
                or isinstance(item.get("data_age_seconds"), bool)
                or item.get("data_age_seconds", -1) < 0
                or not isinstance(item.get("snapshot_hash"), str)
                or not _HASH_RE.fullmatch(item.get("snapshot_hash", ""))
            ):
                errors.append({"code": "CONTEXT_EVIDENCE_INVALID", "message": "AI evidence values are invalid."})
                break
            if not isinstance(quality, Mapping) or set(quality) != _QUALITY_FIELDS:
                errors.append({"code": "CONTEXT_QUALITY_INVALID", "message": "AI evidence quality shape is invalid."})
                break
            if (
                not isinstance(quality.get("score"), int)
                or isinstance(quality.get("score"), bool)
                or not 0 <= quality.get("score", -1) <= 100
                or _finite_number(quality.get("completeness")) is None
                or not 0 <= float(quality.get("completeness")) <= 1
                or not isinstance(quality.get("confidence"), str)
                or quality.get("confidence") not in {"low", "medium", "high", "unknown"}
                or quality.get("fresh") is not True
                or quality.get("future_timestamp") is not False
            ):
                errors.append({"code": "CONTEXT_QUALITY_INVALID", "message": "AI evidence quality values are invalid."})
                break
            if not isinstance(metrics, Mapping) or not metrics or set(metrics) - ALLOWED_METRIC_KEYS:
                errors.append({"code": "CONTEXT_METRICS_INVALID", "message": "AI evidence metrics shape is invalid."})
                break
            if any(
                (
                    not isinstance(value, str)
                    or value not in _STATUS_VALUES
                ) if key == "status" else _finite_number(value) is None
                for key, value in metrics.items()
            ):
                errors.append({"code": "CONTEXT_METRICS_INVALID", "message": "AI evidence metrics contain invalid values."})
                break
    constraints = context_pack.get("constraints")
    if not isinstance(constraints, Mapping) or set(constraints) != _CONSTRAINT_FIELDS:
        errors.append({"code": "CONTEXT_CONSTRAINTS_INVALID", "message": "AI context constraints shape is invalid."})
    else:
        allowed_actions = constraints.get("allowed_actions")
        integer_ranges = {
            "max_actions_per_hour": (0, 20),
            "min_quality_score": (70, 100),
            "max_data_age_seconds": (30, 600),
            "proposal_ttl_seconds": (30, 600),
        }
        number_ranges = {
            "max_budget_decrease_percent": (0.0, 30.0),
            "max_daily_budget_impact": (0.0, 1_000_000.0),
            "min_confidence": (0.75, 1.0),
        }
        constraints_valid = bool(
            isinstance(allowed_actions, list)
            and allowed_actions
            and all(isinstance(action, str) and action in SUPPORTED_AI_ACTIONS for action in allowed_actions)
            and len(allowed_actions) == len(set(allowed_actions))
            and all(
                isinstance(constraints.get(key), int)
                and not isinstance(constraints.get(key), bool)
                and low <= constraints.get(key) <= high
                for key, (low, high) in integer_ranges.items()
            )
            and all(
                _finite_number(constraints.get(key)) is not None
                and low <= float(constraints.get(key)) <= high
                for key, (low, high) in number_ranges.items()
            )
            and constraints.get("budget_increase_allowed") is False
            and constraints.get("human_confirmation_required") is True
            and constraints.get("preflight_reread_required") is True
            and constraints.get("postflight_verification_required") is True
            and constraints.get("execution_enabled") is False
        )
        if not constraints_valid:
            errors.append({"code": "CONTEXT_CONSTRAINTS_INVALID", "message": "AI context constraint values are invalid."})
    privacy = context_pack.get("privacy_contract")
    if not isinstance(privacy, Mapping) or set(privacy) != _PRIVACY_FIELDS:
        errors.append({"code": "CONTEXT_PRIVACY_INVALID", "message": "AI context privacy contract is invalid."})
    elif not (
        privacy.get("raw_identity_included") is False
        and privacy.get("page_text_included") is False
        and privacy.get("credentials_included") is False
        and privacy.get("order_rows_included") is False
        and privacy.get("aggregate_metrics_only") is True
    ):
        errors.append({"code": "CONTEXT_PRIVACY_INVALID", "message": "AI context privacy values are invalid."})
    return errors


__all__ = [
    "AI_CONTEXT_SCHEMA_VERSION",
    "MAX_AI_EVIDENCE_ITEMS",
    "ALLOWED_METRIC_KEYS",
    "SUPPORTED_AI_ACTIONS",
    "anonymized_ref",
    "build_ai_context_pack",
    "context_integrity_hash",
    "validate_ai_context_pack",
]
