"""Strict, non-executable decision contract for untrusted AI output."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

try:
    from .ai_context import SUPPORTED_AI_ACTIONS, validate_ai_context_pack
except ImportError:
    from ai_context import SUPPORTED_AI_ACTIONS, validate_ai_context_pack

DECISION_PROPOSAL_SCHEMA_VERSION = 1
MAX_PROPOSAL_TTL_SECONDS = 600
MAX_FUTURE_SKEW_MS = 30_000

REASON_CODES = frozenset(
    {
        "roi_below_floor",
        "cost_rising",
        "no_conversion",
        "budget_pacing_fast",
        "inventory_risk",
        "goal_met",
        "performance_recovered",
        "risk_guardrail",
    }
)

_FIELDS = frozenset(
    {
        "schema_version",
        "context_id",
        "account_ref",
        "plan_id",
        "action",
        "delta_percent",
        "evidence_refs",
        "snapshot_hash",
        "confidence",
        "reason_codes",
        "observe_minutes",
        "rollback_condition",
        "created_at_ms",
        "ttl_seconds",
    }
)
_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")

# Provider adapters can pass this schema directly to strict JSON/tool output.
# Runtime validation below remains authoritative even when a provider claims
# to support strict schemas.
DECISION_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": sorted(_FIELDS),
    "properties": {
        "schema_version": {"type": "integer", "const": DECISION_PROPOSAL_SCHEMA_VERSION},
        "context_id": {"type": "string", "pattern": _REFERENCE_RE.pattern},
        "account_ref": {"type": "string", "pattern": _REFERENCE_RE.pattern},
        "plan_id": {"type": "string", "pattern": _REFERENCE_RE.pattern},
        "action": {"type": "string", "enum": sorted(SUPPORTED_AI_ACTIONS)},
        "delta_percent": {"type": ["number", "null"]},
        "evidence_refs": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "uniqueItems": True,
            "items": {"type": "string", "pattern": _REFERENCE_RE.pattern},
        },
        "snapshot_hash": {"type": "string", "pattern": _HASH_RE.pattern},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason_codes": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "uniqueItems": True,
            "items": {"type": "string", "enum": sorted(REASON_CODES)},
        },
        "observe_minutes": {"type": "integer", "minimum": 5, "maximum": 240},
        "rollback_condition": {"type": "string", "minLength": 1, "maxLength": 300},
        "created_at_ms": {"type": "integer", "minimum": 1},
        "ttl_seconds": {"type": "integer", "minimum": 30, "maximum": MAX_PROPOSAL_TTL_SECONDS},
    },
}


def _error(code: str, message: str, field: str | None = None) -> dict[str, str]:
    item = {"code": code, "message": message}
    if field:
        item["field"] = field
    return item


def _raw_hash(value: Any) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        raw = repr(type(value))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _safe_model_label(value: Any, *, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    label = value.strip()[:80]
    return label if label and _REFERENCE_RE.fullmatch(label) else fallback


class ProposalSchemaError(ValueError):
    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("invalid DecisionProposalV1")
        self.errors = errors


@dataclass(frozen=True, slots=True)
class DecisionProposalV1:
    """AI intent only; it deliberately contains no current or target value."""

    schema_version: int
    context_id: str
    account_ref: str
    plan_id: str
    action: str
    delta_percent: float | None
    evidence_refs: tuple[str, ...]
    snapshot_hash: str
    confidence: float
    reason_codes: tuple[str, ...]
    observe_minutes: int
    rollback_condition: str
    created_at_ms: int
    ttl_seconds: int

    @classmethod
    def from_mapping(cls, value: Any) -> "DecisionProposalV1":
        errors: list[dict[str, str]] = []
        if not isinstance(value, Mapping):
            raise ProposalSchemaError([_error("PROPOSAL_NOT_OBJECT", "Proposal must be an object.")])

        unknown = sorted(str(key) for key in value.keys() if key not in _FIELDS)
        missing = sorted(key for key in _FIELDS if key not in value)
        if unknown:
            errors.append(_error("UNKNOWN_FIELDS", "Proposal contains unsupported fields."))
        if missing:
            errors.append(_error("MISSING_FIELDS", "Proposal is missing required fields."))

        if value.get("schema_version") != DECISION_PROPOSAL_SCHEMA_VERSION:
            errors.append(_error("SCHEMA_VERSION_MISMATCH", "Proposal schema is unsupported.", "schema_version"))

        for field in ("context_id", "account_ref", "plan_id"):
            item = value.get(field)
            if not isinstance(item, str) or not _REFERENCE_RE.fullmatch(item):
                errors.append(_error("INVALID_REFERENCE", "Reference format is invalid.", field))

        action = value.get("action")
        if not isinstance(action, str) or action not in SUPPORTED_AI_ACTIONS:
            errors.append(_error("INVALID_ACTION", "Action is not supported.", "action"))

        delta = value.get("delta_percent")
        if action == "decrease_budget":
            if not _is_number(delta) or float(delta) >= 0:
                errors.append(_error("INVALID_DELTA", "Budget decrease must be a negative percentage.", "delta_percent"))
        elif delta is not None:
            errors.append(_error("UNEXPECTED_DELTA", "This action must not supply a delta.", "delta_percent"))

        evidence_refs = value.get("evidence_refs")
        if (
            not isinstance(evidence_refs, list)
            or not 1 <= len(evidence_refs) <= 8
            or any(not isinstance(item, str) or not _REFERENCE_RE.fullmatch(item) for item in evidence_refs)
            or len(set(evidence_refs)) != len(evidence_refs)
        ):
            errors.append(_error("INVALID_EVIDENCE_REFS", "Evidence references are invalid.", "evidence_refs"))

        snapshot_hash = value.get("snapshot_hash")
        if not isinstance(snapshot_hash, str) or not _HASH_RE.fullmatch(snapshot_hash):
            errors.append(_error("INVALID_SNAPSHOT_HASH", "Snapshot hash is invalid.", "snapshot_hash"))

        confidence = value.get("confidence")
        if not _is_number(confidence) or not 0 <= float(confidence) <= 1:
            errors.append(_error("INVALID_CONFIDENCE", "Confidence must be between 0 and 1.", "confidence"))

        reason_codes = value.get("reason_codes")
        if (
            not isinstance(reason_codes, list)
            or not 1 <= len(reason_codes) <= 8
            or any(not isinstance(item, str) or item not in REASON_CODES for item in reason_codes)
            or (
                all(isinstance(item, str) for item in reason_codes)
                and len(set(reason_codes)) != len(reason_codes)
            )
        ):
            errors.append(_error("INVALID_REASON_CODES", "Reason codes are invalid.", "reason_codes"))

        observe_minutes = value.get("observe_minutes")
        if (
            not isinstance(observe_minutes, int)
            or isinstance(observe_minutes, bool)
            or not 5 <= observe_minutes <= 240
        ):
            errors.append(_error("INVALID_OBSERVE_WINDOW", "Observe window is invalid.", "observe_minutes"))

        rollback_condition = value.get("rollback_condition")
        if (
            not isinstance(rollback_condition, str)
            or not rollback_condition.strip()
            or len(rollback_condition) > 300
            or any(ord(char) < 32 and char not in "\t\n" for char in rollback_condition)
        ):
            errors.append(_error("INVALID_ROLLBACK_CONDITION", "Rollback condition is invalid.", "rollback_condition"))

        created_at_ms = value.get("created_at_ms")
        if not isinstance(created_at_ms, int) or isinstance(created_at_ms, bool) or created_at_ms <= 0:
            errors.append(_error("INVALID_CREATED_AT", "Proposal timestamp is invalid.", "created_at_ms"))

        ttl_seconds = value.get("ttl_seconds")
        if (
            not isinstance(ttl_seconds, int)
            or isinstance(ttl_seconds, bool)
            or not 30 <= ttl_seconds <= MAX_PROPOSAL_TTL_SECONDS
        ):
            errors.append(_error("INVALID_TTL", "Proposal TTL is invalid.", "ttl_seconds"))

        if errors:
            raise ProposalSchemaError(errors)

        return cls(
            schema_version=DECISION_PROPOSAL_SCHEMA_VERSION,
            context_id=str(value["context_id"]),
            account_ref=str(value["account_ref"]),
            plan_id=str(value["plan_id"]),
            action=str(action),
            delta_percent=round(float(delta), 6) if delta is not None else None,
            evidence_refs=tuple(evidence_refs),
            snapshot_hash=str(snapshot_hash),
            confidence=round(float(confidence), 6),
            reason_codes=tuple(reason_codes),
            observe_minutes=int(observe_minutes),
            rollback_condition=rollback_condition.strip(),
            created_at_ms=int(created_at_ms),
            ttl_seconds=int(ttl_seconds),
        )


def _audit_metadata(
    *,
    raw_proposal: Any,
    context_pack: Mapping[str, Any] | Any,
    model_metadata: Mapping[str, Any] | None,
    received_at_ms: int,
) -> dict[str, Any]:
    metadata = model_metadata if isinstance(model_metadata, Mapping) else {}
    request_value = metadata.get("request_id")
    request_id = request_value if isinstance(request_value, str) else ""
    context_hash_value = context_pack.get("context_hash") if isinstance(context_pack, Mapping) else ""
    return {
        "received_at_ms": received_at_ms,
        "proposal_hash": _raw_hash(raw_proposal),
        "context_hash": context_hash_value if isinstance(context_hash_value, str) else "",
        "provider": _safe_model_label(metadata.get("provider"), fallback="unknown"),
        "model": _safe_model_label(metadata.get("model"), fallback="unknown"),
        "prompt_version": _safe_model_label(metadata.get("prompt_version"), fallback="unspecified"),
        "request_ref": hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:16] if request_id else None,
        "raw_prompt_stored": False,
        "raw_response_stored": False,
    }


def validate_decision_proposal(
    raw_proposal: Any,
    context_pack: Mapping[str, Any] | Any,
    *,
    model_metadata: Mapping[str, Any] | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Return a review candidate; never return an executable command."""

    now_ms = int(now_ms or time.time() * 1000)
    schema_errors: list[dict[str, str]] = []
    proposal: DecisionProposalV1 | None = None
    try:
        proposal = DecisionProposalV1.from_mapping(raw_proposal)
    except ProposalSchemaError as exc:
        schema_errors = exc.errors

    context_errors = validate_ai_context_pack(context_pack, now_ms=now_ms)
    blocked: list[dict[str, str]] = []
    context = context_pack if isinstance(context_pack, Mapping) else {}
    constraints = context.get("constraints") if isinstance(context.get("constraints"), Mapping) else {}
    identity = context.get("identity") if isinstance(context.get("identity"), Mapping) else {}
    account_ref_value = identity.get("account_ref")
    authoritative_account_ref = account_ref_value if isinstance(account_ref_value, str) else ""

    if proposal is not None and not context_errors:
        if proposal.context_id != context.get("context_id"):
            blocked.append(_error("PROPOSAL_CONTEXT_MISMATCH", "Proposal belongs to a different context."))
        if proposal.account_ref != authoritative_account_ref:
            blocked.append(_error("PROPOSAL_ACCOUNT_MISMATCH", "Proposal account does not match bound context."))
        if proposal.created_at_ms > now_ms + MAX_FUTURE_SKEW_MS:
            blocked.append(_error("PROPOSAL_TIME_IN_FUTURE", "Proposal timestamp is in the future."))
        if proposal.created_at_ms + proposal.ttl_seconds * 1000 <= now_ms:
            blocked.append(_error("PROPOSAL_EXPIRED", "Proposal has expired."))
        if proposal.ttl_seconds > int(constraints.get("proposal_ttl_seconds") or 0):
            blocked.append(_error("PROPOSAL_TTL_EXCEEDS_POLICY", "Proposal TTL exceeds the context policy."))
        if proposal.action not in set(constraints.get("allowed_actions") or []):
            blocked.append(_error("ACTION_NOT_ALLOWED", "Action is not allowed by the current policy."))
        if proposal.confidence < float(constraints.get("min_confidence") or 1):
            blocked.append(_error("CONFIDENCE_BELOW_POLICY", "Confidence is below the current policy."))
        if proposal.action == "decrease_budget" and abs(float(proposal.delta_percent or 0)) > float(
            constraints.get("max_budget_decrease_percent") or 0
        ):
            blocked.append(_error("DECREASE_EXCEEDS_POLICY", "Requested decrease exceeds the current policy."))

        evidence = context.get("evidence") if isinstance(context.get("evidence"), list) else []
        evidence_by_ref = {
            str(item.get("evidence_ref")): item
            for item in evidence
            if isinstance(item, Mapping) and item.get("evidence_ref")
        }
        selected = [evidence_by_ref[ref] for ref in proposal.evidence_refs if ref in evidence_by_ref]
        if len(selected) != len(proposal.evidence_refs):
            blocked.append(_error("EVIDENCE_REF_NOT_FOUND", "Proposal references evidence outside the context."))
        target_evidence = [item for item in selected if str(item.get("plan_id") or "") == proposal.plan_id]
        if not target_evidence:
            blocked.append(_error("TARGET_PLAN_NOT_EVIDENCED", "Target plan is not bound to selected evidence."))
        elif any(str(item.get("account_ref") or "") != authoritative_account_ref for item in target_evidence):
            blocked.append(_error("PROPOSAL_ACCOUNT_MISMATCH", "Target evidence belongs to a different account."))
        elif proposal.snapshot_hash not in {str(item.get("snapshot_hash") or "") for item in target_evidence}:
            blocked.append(_error("SNAPSHOT_HASH_MISMATCH", "Proposal snapshot hash does not match target evidence."))
        for item in target_evidence:
            quality = item.get("quality") if isinstance(item.get("quality"), Mapping) else {}
            if not quality.get("fresh"):
                blocked.append(_error("TARGET_EVIDENCE_STALE", "Target evidence is stale."))
                break
            if int(quality.get("score") or 0) < int(constraints.get("min_quality_score") or 100):
                blocked.append(_error("TARGET_EVIDENCE_LOW_QUALITY", "Target evidence quality is below policy."))
                break

    valid_for_review = not schema_errors and not context_errors and not blocked
    audit = _audit_metadata(
        raw_proposal=raw_proposal,
        context_pack=context_pack,
        model_metadata=model_metadata,
        received_at_ms=now_ms,
    )
    candidate_id = f"ai-candidate-{audit['proposal_hash'][:24]}"
    return {
        "schema_version": DECISION_PROPOSAL_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "mode": "proposal_only",
        "can_execute": False,
        "eligible_for_human_review": valid_for_review,
        "intent": (
            {
                "plan_id": proposal.plan_id,
                "requested_action": proposal.action,
                "requested_delta_percent": proposal.delta_percent,
                "evidence_refs": list(proposal.evidence_refs),
                "reason_codes": list(proposal.reason_codes),
                "observe_minutes": proposal.observe_minutes,
                "rollback_condition": proposal.rollback_condition,
            }
            if proposal is not None
            else None
        ),
        "authoritative_resolution": {
            "account_ref": authoritative_account_ref,
            "current_value": None,
            "target_value": None,
            "source": "agent_preflight_reread_required",
        },
        "validation": {
            "schema_valid": not schema_errors,
            "context_valid": not context_errors,
            "schema_errors": schema_errors,
            "context_errors": context_errors,
            "blocked_reasons": blocked,
        },
        "audit": audit,
    }


__all__ = [
    "DECISION_PROPOSAL_SCHEMA",
    "DECISION_PROPOSAL_SCHEMA_VERSION",
    "DecisionProposalV1",
    "ProposalSchemaError",
    "REASON_CODES",
    "validate_decision_proposal",
]
