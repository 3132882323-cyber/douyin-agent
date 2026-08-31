"""Proposal-only gateway between local business context and AI providers.

This boundary rejects obvious credentials and personal data, calls one
configured provider, and seals its output as an *untrusted* proposal envelope.
It intentionally has no authorize, confirm, preflight, or execute method.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
import time
from pathlib import Path
from typing import Any, Mapping

try:
    from .ai_provider import (
        AIProviderConfigurationError,
        AIProviderError,
        AIProviderRegistry,
        request_ephemeral_proposal,
    )
    from .ai_context import validate_ai_context_pack
    from .ai_decision import DECISION_PROPOSAL_SCHEMA, validate_decision_proposal
    from .oceanengine_oauth import _atomic_write_json
except ImportError:  # pragma: no cover - direct bridge execution
    from ai_provider import (
        AIProviderConfigurationError,
        AIProviderError,
        AIProviderRegistry,
        request_ephemeral_proposal,
    )
    from ai_context import validate_ai_context_pack
    from ai_decision import DECISION_PROPOSAL_SCHEMA, validate_decision_proposal
    from oceanengine_oauth import _atomic_write_json


AI_GATEWAY_SCHEMA_VERSION = 1
MAX_CONTEXT_BYTES = 256 * 1024
MAX_CONTEXT_DEPTH = 12
MAX_CONTEXT_STRING_LENGTH = 20_000

_CONNECTIVITY_PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["probe"],
    "properties": {"probe": {"type": "string", "enum": ["ok"]}},
}


class AIContextRejectedError(ValueError):
    """The context is not safe to send to a model."""


_FORBIDDEN_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "cookie",
    "cookies",
    "customer",
    "customers",
    "consumer",
    "consumers",
    "email",
    "id_card",
    "idcard",
    "mobile",
    "order_detail",
    "order_details",
    "password",
    "phone",
    "raw_html",
    "receiver",
    "refresh_token",
    "screenshot",
    "secret",
    "token",
}


def _normalized_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def sanitize_ai_context(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a bounded JSON copy or fail closed on sensitive field names."""

    if not isinstance(value, Mapping):
        raise AIContextRejectedError("AI 经营上下文必须是 JSON 对象。")

    def visit(item: Any, *, depth: int) -> Any:
        if depth > MAX_CONTEXT_DEPTH:
            raise AIContextRejectedError("AI 经营上下文嵌套过深。")
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise AIContextRejectedError("AI operating context contains a non-finite number.")
            return item
        if isinstance(item, str):
            if len(item) > MAX_CONTEXT_STRING_LENGTH:
                raise AIContextRejectedError("AI 经营上下文包含过长文本。")
            return item
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for key, child in item.items():
                normalized = _normalized_key(key)
                if normalized in _FORBIDDEN_KEYS or normalized.endswith("_token"):
                    raise AIContextRejectedError(f"AI 经营上下文包含禁止字段：{normalized}")
                result[str(key)[:160]] = visit(child, depth=depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            if len(item) > 2000:
                raise AIContextRejectedError("AI 经营上下文列表过长。")
            return [visit(child, depth=depth + 1) for child in item]
        raise AIContextRejectedError("AI 经营上下文包含不支持的数据类型。")

    sanitized = visit(value, depth=0)
    encoded = json.dumps(
        sanitized,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_CONTEXT_BYTES:
        raise AIContextRejectedError("AI 经营上下文超过本地安全上限。")
    return sanitized


def _context_hash(value: Mapping[str, Any]) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validated_ai_context(value: Mapping[str, Any], *, now_ms: int) -> dict[str, Any]:
    """Accept only the closed AIContextV1 contract before any model call."""

    sanitized = sanitize_ai_context(value)
    errors = validate_ai_context_pack(sanitized, now_ms=now_ms)
    if errors:
        codes = ",".join(str(item.get("code") or "INVALID_CONTEXT") for item in errors[:6])
        raise AIContextRejectedError(f"AI operating context was rejected ({codes}).")
    return sanitized


def _validation_errors(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    validation = result.get("validation") if isinstance(result.get("validation"), Mapping) else {}
    errors: list[dict[str, Any]] = []
    for field in ("schema_errors", "context_errors", "blocked_reasons"):
        rows = validation.get(field) if isinstance(validation.get(field), list) else []
        errors.extend(copy.deepcopy(row) for row in rows if isinstance(row, dict))
    return errors


def _validate_candidate_rows(
    candidates: list[dict[str, Any]],
    *,
    context_pack: Mapping[str, Any],
    provider_id: str,
    model: str,
    response_id: str = "",
    now_ms: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], bool]:
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        result = validate_decision_proposal(
            candidate["arguments"],
            context_pack,
            model_metadata={
                "provider": provider_id,
                "model": model,
                "prompt_version": "dian-ai-proposal-v1",
                "request_id": response_id,
            },
            now_ms=now_ms,
        )
        candidate["validation"] = copy.deepcopy(result)
        candidate["validated"] = bool(result.get("eligible_for_human_review"))
        results.append(result)
        for error in _validation_errors(result):
            errors.append({"candidate_index": index, **error})
    if len(candidates) != 1:
        errors.append(
            {
                "code": "PROPOSAL_COUNT_INVALID",
                "message": "一次影子决策必须且只能返回一个提案。",
            }
        )
    eligible = len(candidates) == 1 and bool(results[0].get("eligible_for_human_review"))
    return candidates, results, errors, eligible


class AIProposalGateway:
    """Small integration surface for ``http_receiver`` and MCP adapters."""

    def __init__(self, data_dir: Path, registry: AIProviderRegistry | None = None):
        self.data_dir = Path(data_dir)
        self.registry = registry or AIProviderRegistry(self.data_dir)
        self.proposals_path = self.data_dir / "ai" / "proposals.json"
        self._proposals: list[dict[str, Any]] = self._load_proposals()
        self._proposal_lock = threading.RLock()

    def _load_proposals(self) -> list[dict[str, Any]]:
        if not self.proposals_path.exists():
            return []
        try:
            value = json.loads(self.proposals_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        rows = value.get("proposals") if isinstance(value, dict) else None
        if not isinstance(rows, list):
            return []
        return [row for row in rows[-100:] if isinstance(row, dict)]

    def catalog(self) -> dict[str, Any]:
        return self.registry.catalog()

    def health(self, provider_id: str | None = None) -> dict[str, Any]:
        return self.registry.health(provider_id)

    def status(self, provider_id: str | None = None) -> dict[str, Any]:
        """Stable HTTP integration alias; this check never opens the network."""

        return self.health(provider_id)

    def configure_provider(self, provider_id: str, **settings: Any) -> dict[str, Any]:
        """Configure a provider without ever returning its API key."""

        return self.registry.configure(provider_id, **settings)

    def configure(
        self,
        provider_id: str | Mapping[str, Any],
        **settings: Any,
    ) -> dict[str, Any]:
        """Configure from keyword arguments or one local HTTP payload."""

        if isinstance(provider_id, Mapping):
            payload = dict(provider_id)
            resolved_id = str(payload.pop("provider_id", ""))
            payload.update(settings)
            return self.configure_provider(resolved_id, **payload)
        return self.configure_provider(str(provider_id), **settings)

    def test(self, provider_id: str | None = None) -> dict[str, Any]:
        """Configuration-only readiness test; deliberately performs no probe."""

        health = self.health(provider_id)
        return {
            **health,
            "test_mode": "configuration_only",
            "network_checked": False,
            "message": "配置已检查；为避免意外外发数据，网络能力请通过影子运行显式验证。",
        }

    def test_provider(self, provider_id: str) -> dict[str, Any]:
        """Run one real structured-tool probe containing no business data.

        This never changes provider configuration, never persists the probe,
        and never includes store/account identifiers or metrics.
        """

        started = time.perf_counter()
        try:
            response = self.registry.client(provider_id).request_proposal(
                context_pack={
                    "probe": "connectivity",
                    "synthetic": True,
                    "contains_business_data": False,
                },
                instruction="Connectivity test only. Return probe=ok through the required function.",
                proposal_schema=_CONNECTIVITY_PROBE_SCHEMA,
            )
            candidates = response.get("proposal_candidates")
            structured = bool(
                isinstance(candidates, list)
                and len(candidates) == 1
                and isinstance(candidates[0], dict)
                and isinstance(candidates[0].get("arguments"), dict)
                and candidates[0]["arguments"].get("probe") == "ok"
            )
            error = "" if structured else "Provider 已响应，但未正确支持结构化工具调用。"
            network_checked = True
        except AIProviderConfigurationError as exc:
            structured = False
            error = str(exc)
            network_checked = False
        except AIProviderError as exc:
            structured = False
            error = str(exc)
            network_checked = True
        except Exception:
            structured = False
            error = "Provider 连接测试出现未预期错误。"
            network_checked = False
        return {
            "schema_version": AI_GATEWAY_SCHEMA_VERSION,
            "provider_id": str(provider_id or "")[:80],
            "ok": structured,
            "state": "connected" if structured else "failed",
            "network_checked": network_checked,
            "latency_ms": max(0, round((time.perf_counter() - started) * 1000)),
            "structured_tools_working": structured,
            "probe_data_scope": "synthetic_only",
            "proposal_persisted": False,
            "error": error[:240],
            "secrets_exposed": False,
        }

    def test_configuration(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Test unsaved UI settings in memory with synthetic data only.

        Accepted fields are ``provider_id`` (or ``provider``), ``model``,
        ``base_url``, ``api_key``, ``remote_access_approved`` and optional
        ``timeout_seconds``.  Nothing in ``payload`` is written to disk or an
        operating-system secret store.
        """

        started = time.perf_counter()
        provider_id = ""
        try:
            if not isinstance(payload, Mapping):
                raise AIProviderConfigurationError("临时 Provider 配置必须是 JSON 对象。")
            allowed = {
                "provider_id",
                "provider",
                "model",
                "base_url",
                "api_key",
                "remote_access_approved",
                "timeout_seconds",
            }
            unknown = {str(key) for key in payload if str(key) not in allowed}
            if unknown:
                raise AIProviderConfigurationError("临时 Provider 配置包含不支持的字段。")
            provider_id = str(payload.get("provider_id") or payload.get("provider") or "")
            if not isinstance(payload.get("remote_access_approved", False), bool):
                raise AIProviderConfigurationError("远程数据传输授权必须是布尔值。")
            response = request_ephemeral_proposal(
                provider_id=provider_id,
                model=str(payload.get("model") or ""),
                base_url=str(payload.get("base_url") or ""),
                api_key=str(payload.get("api_key") or ""),
                remote_access_approved=bool(payload.get("remote_access_approved", False)),
                timeout_seconds=payload.get("timeout_seconds", 20),
                context_pack={
                    "probe": "connectivity",
                    "synthetic": True,
                    "contains_business_data": False,
                },
                instruction="Connectivity test only. Return probe=ok through the required function.",
                proposal_schema=_CONNECTIVITY_PROBE_SCHEMA,
            )
            candidates = response.get("proposal_candidates")
            structured = bool(
                isinstance(candidates, list)
                and len(candidates) == 1
                and isinstance(candidates[0], dict)
                and isinstance(candidates[0].get("arguments"), dict)
                and candidates[0]["arguments"].get("probe") == "ok"
            )
            error = "" if structured else "Provider 已响应，但未正确支持结构化工具调用。"
            network_checked = True
        except AIProviderConfigurationError as exc:
            structured = False
            error = str(exc)
            network_checked = False
        except AIProviderError as exc:
            structured = False
            error = str(exc)
            network_checked = True
        except Exception:
            structured = False
            error = "临时 Provider 连接测试出现未预期错误。"
            network_checked = False
        return {
            "schema_version": AI_GATEWAY_SCHEMA_VERSION,
            "provider_id": provider_id[:80],
            "ok": structured,
            "state": "connected" if structured else "failed",
            "network_checked": network_checked,
            "latency_ms": max(0, round((time.perf_counter() - started) * 1000)),
            "structured_tools_working": structured,
            "probe_data_scope": "synthetic_only",
            "configuration_persisted": False,
            "credential_persisted": False,
            "proposal_persisted": False,
            "error": error[:240],
            "secrets_exposed": False,
        }

    def _remember(self, envelope: dict[str, Any]) -> dict[str, Any]:
        with self._proposal_lock:
            self._proposals.append(copy.deepcopy(envelope))
            self._proposals = self._proposals[-100:]
            _atomic_write_json(
                self.proposals_path,
                {
                    "schema_version": AI_GATEWAY_SCHEMA_VERSION,
                    "proposals": self._proposals,
                },
            )
        return envelope

    def list_proposals(self, *, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(100, int(limit or 50)))
        with self._proposal_lock:
            rows = copy.deepcopy(self._proposals[-limit:])
        rows.reverse()
        return {
            "schema_version": AI_GATEWAY_SCHEMA_VERSION,
            "mode": "proposal_only",
            "count": len(rows),
            "proposals": rows,
            "execution_supported": False,
            "secrets_exposed": False,
        }

    def submit_proposal(
        self,
        proposal: Mapping[str, Any],
        *,
        context_pack: Mapping[str, Any] | None = None,
        provider_id: str = "external",
        model: str = "",
        context_hash: str = "",
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Queue an external raw proposal for validation, never execution."""

        safe = sanitize_ai_context({"proposal": proposal})["proposal"]
        created_at_ms = int(now_ms or time.time() * 1000)
        provider_label = str(provider_id or "external")[:80]
        model_label = str(model or "")[:120]
        candidate = {
            "source": "external_submission",
            "tool_name": "submit_ai_proposal",
            "call_id": "",
            "arguments": safe,
            "trusted": False,
            "validated": False,
            "executable": False,
        }
        if context_pack is None:
            context_digest = str(context_hash or "")[:128]
            validated_proposals: list[dict[str, Any]] = []
            validation_errors = [
                {
                    "code": "CONTEXT_PACK_REQUIRED",
                    "message": "外部 AI 提案缺少对应的脱敏经营上下文，暂不能进入人工复核。",
                }
            ]
            eligible = False
            validation_state = "validation_pending"
        else:
            sanitized_context = _validated_ai_context(context_pack, now_ms=created_at_ms)
            context_digest = str(sanitized_context.get("context_hash") or _context_hash(sanitized_context))
            rows, validated_proposals, validation_errors, eligible = _validate_candidate_rows(
                [candidate],
                context_pack=sanitized_context,
                provider_id=provider_label,
                model=model_label,
                now_ms=created_at_ms,
            )
            candidate = rows[0]
            validation_state = "validated" if eligible else "rejected"
        identity = {
            "provider_id": provider_label,
            "model": model_label,
            "context_hash": context_digest,
            "created_at_ms": created_at_ms,
            "arguments": safe,
        }
        proposal_id = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()[:24]
        envelope = {
            "schema_version": AI_GATEWAY_SCHEMA_VERSION,
            "proposal_id": proposal_id,
            "state": (
                "validated_ai_proposal"
                if eligible
                else "validation_pending"
                if validation_state == "validation_pending"
                else "rejected_ai_proposal"
            ),
            "validation_state": validation_state,
            "mode": "proposal_only",
            "provider_id": identity["provider_id"],
            "model": identity["model"],
            "context_hash": identity["context_hash"],
            "created_at_ms": created_at_ms,
            "proposal_candidates": [candidate],
            "validated_proposals": validated_proposals,
            "validation_errors": validation_errors,
            "eligible_for_human_review": eligible,
            "requires_local_validation": not eligible,
            "requires_user_or_policy_authorization": True,
            "can_confirm": False,
            "can_execute": False,
            "execution_performed": False,
            "provider_usage": {},
            "warnings": ["外部 AI 提案尚未经过本地规则、数据新鲜度和账户身份校验。"],
            "secrets_exposed": False,
        }
        return self._remember(envelope)

    def run_shadow(self, **request: Any) -> dict[str, Any]:
        """Explicitly call a model and retain the result as a non-executable shadow."""

        envelope = self.generate_proposal(**request)
        envelope["shadow_mode"] = True
        return self._remember(envelope)

    def generate_proposal(
        self,
        *,
        provider_id: str,
        context_pack: Mapping[str, Any],
        instruction: str = "",
        proposal_schema: Mapping[str, Any] | None = None,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Request and seal an untrusted proposal; never authorize or execute it."""

        created_at_ms = int(now_ms or time.time() * 1000)
        sanitized = _validated_ai_context(context_pack, now_ms=created_at_ms)
        schema = proposal_schema or DECISION_PROPOSAL_SCHEMA
        response = self.registry.client(provider_id).request_proposal(
            context_pack=sanitized,
            instruction=instruction,
            proposal_schema=schema,
        )
        candidates = response.get("proposal_candidates")
        if not isinstance(candidates, list) or not candidates:
            raise AIProviderConfigurationError("AI Provider 未返回可供校验的提案。")
        safe_candidates: list[dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict) or not isinstance(candidate.get("arguments"), dict):
                raise AIProviderConfigurationError("AI Provider 提案格式不正确。")
            safe_candidates.append(
                {
                    "source": str(candidate.get("source") or "")[:40],
                    "tool_name": str(candidate.get("tool_name") or "")[:80],
                    "call_id": str(candidate.get("call_id") or "")[:160],
                    "arguments": copy.deepcopy(candidate["arguments"]),
                    "trusted": False,
                    "validated": False,
                    "executable": False,
                }
            )
        provider_label = str(response.get("provider_id") or provider_id)
        model_label = str(response.get("model") or "")
        safe_candidates, validated_proposals, validation_errors, eligible = _validate_candidate_rows(
            safe_candidates,
            context_pack=sanitized,
            provider_id=provider_label,
            model=model_label,
            response_id=str(response.get("response_id") or ""),
            now_ms=created_at_ms,
        )
        context_digest = str(sanitized.get("context_hash") or _context_hash(sanitized))
        identity = {
            "provider_id": provider_label,
            "model": model_label,
            "context_hash": context_digest,
            "created_at_ms": created_at_ms,
            "proposal_candidates": safe_candidates,
        }
        proposal_id = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()[:24]
        return {
            "schema_version": AI_GATEWAY_SCHEMA_VERSION,
            "proposal_id": proposal_id,
            "state": "validated_ai_proposal" if eligible else "rejected_ai_proposal",
            "validation_state": "validated" if eligible else "rejected",
            "mode": "proposal_only",
            "provider_id": identity["provider_id"],
            "model": identity["model"],
            "context_hash": context_digest,
            "created_at_ms": created_at_ms,
            "proposal_candidates": safe_candidates,
            "validated_proposals": validated_proposals,
            "validation_errors": validation_errors,
            "eligible_for_human_review": eligible,
            "requires_local_validation": not eligible,
            "requires_user_or_policy_authorization": True,
            "can_confirm": False,
            "can_execute": False,
            "execution_performed": False,
            "provider_usage": copy.deepcopy(response.get("usage") or {}),
            "warnings": [
                "AI 输出属于不可信提案，必须重新读取账户、计划和指标后再进入本地规则校验。",
                "本模块未授权、未提交、未执行任何千川操作。",
            ],
            "secrets_exposed": False,
        }


__all__ = [
    "AI_GATEWAY_SCHEMA_VERSION",
    "AIContextRejectedError",
    "AIProposalGateway",
    "AIGateway",
    "sanitize_ai_context",
]


# Short integration name requested by the local HTTP layer.
AIGateway = AIProposalGateway
