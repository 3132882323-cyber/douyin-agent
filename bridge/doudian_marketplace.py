"""Fail-closed server boundary for a future Doudian marketplace app.

No official endpoint, signature algorithm, or response schema is guessed here.
Network access is possible only through an explicitly injected platform adapter
implemented from the contract granted to the actual marketplace application.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse


OAUTH_STATE_TTL_SECONDS = 10 * 60
_ADAPTER_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,63}$")
_CONTRACT_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_.-]{0,63}$")
_BUILD_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_adapter_lock = threading.Lock()
_registered_adapter: MarketplacePlatformAdapter | None = None


def _required_scope(value: str, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 128:
        raise ValueError(f"{label} is required and must be at most 128 characters")
    return normalized


@dataclass(frozen=True)
class ShopScope:
    tenant_id: str
    shop_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", _required_scope(self.tenant_id, "tenant_id"))
        object.__setattr__(self, "shop_id", _required_scope(self.shop_id, "shop_id"))


@dataclass(frozen=True)
class OAuthToken:
    access_token: str
    refresh_token: str
    expires_at: int
    refresh_expires_at: int | None = None
    granted_scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.access_token or "").strip():
            raise ValueError("access_token is required")
        if int(self.expires_at) <= 0:
            raise ValueError("expires_at is required")

    def public_status(self, now: int | None = None) -> dict[str, Any]:
        current = int(time.time() if now is None else now)
        return {
            "connected": True,
            "expires_at": int(self.expires_at),
            "expired": int(self.expires_at) <= current,
            "refresh_available": bool(self.refresh_token),
            "granted_scopes": list(self.granted_scopes),
        }


@dataclass(frozen=True)
class PlatformAuthorization:
    """Normalized result returned by an approved platform adapter."""

    shop_id: str
    token: OAuthToken

    def __post_init__(self) -> None:
        object.__setattr__(self, "shop_id", _required_scope(self.shop_id, "platform shop_id"))


class TokenStore(Protocol):
    """Tenant/shop-scoped storage. Production implementations encrypt at rest."""

    def put(self, scope: ShopScope, token: OAuthToken) -> None: ...
    def get(self, scope: ShopScope) -> OAuthToken | None: ...
    def delete(self, scope: ShopScope) -> None: ...


@dataclass(frozen=True)
class TenantRequestContext:
    """Authenticated cloud request context supplied by the SaaS gateway."""

    subject_id: str
    scope: ShopScope
    roles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "subject_id", _required_scope(self.subject_id, "subject_id"))


class RequestAuthenticator(Protocol):
    def authenticate(self, headers: Mapping[str, str]) -> TenantRequestContext: ...


class RejectingAuthenticator:
    """Safe default until a production identity provider is wired in."""

    production_safe = False

    def authenticate(self, headers: Mapping[str, str]) -> TenantRequestContext:
        raise PermissionError("marketplace_request_authenticator_not_configured")


class InMemoryTokenStore:
    """Non-persistent store for tests and local development only."""

    production_safe = False

    def __init__(self) -> None:
        self._values: dict[tuple[str, str], OAuthToken] = {}
        self._lock = threading.Lock()

    def put(self, scope: ShopScope, token: OAuthToken) -> None:
        with self._lock:
            self._values[(scope.tenant_id, scope.shop_id)] = token

    def get(self, scope: ShopScope) -> OAuthToken | None:
        with self._lock:
            return self._values.get((scope.tenant_id, scope.shop_id))

    def delete(self, scope: ShopScope) -> None:
        with self._lock:
            self._values.pop((scope.tenant_id, scope.shop_id), None)


@dataclass(frozen=True)
class DoudianAppConfig:
    app_key: str
    app_secret: str
    callback_url: str

    @classmethod
    def from_env(cls) -> "DoudianAppConfig":
        return cls(
            app_key=os.environ.get("DIAN_AGENT_DOUDIAN_APP_KEY", "").strip(),
            app_secret=os.environ.get("DIAN_AGENT_DOUDIAN_APP_SECRET", "").strip(),
            callback_url=os.environ.get("DIAN_AGENT_DOUDIAN_CALLBACK_URL", "").strip(),
        )

    def validate(self, *, production: bool) -> list[str]:
        errors: list[str] = []
        if not self.app_key:
            errors.append("app_key_missing")
        if not self.app_secret:
            errors.append("app_secret_missing")
        callback = urlparse(self.callback_url)
        if not callback.scheme or not callback.netloc:
            errors.append("callback_url_missing")
        elif production and callback.scheme != "https":
            errors.append("callback_url_must_use_https")
        if production and callback.hostname in {"localhost", "127.0.0.1", "::1"}:
            errors.append("callback_url_must_be_public")
        return errors

    def public_status(self, *, production: bool) -> dict[str, Any]:
        return {
            "app_key_configured": bool(self.app_key),
            "app_secret_configured": bool(self.app_secret),
            "callback_url": self.callback_url,
            "configuration_errors": self.validate(production=production),
        }


class OAuthStateStore:
    """One-time, tenant/shop-bound OAuth state store."""

    def __init__(self, ttl_seconds: int = OAUTH_STATE_TTL_SECONDS) -> None:
        self.ttl_seconds = max(60, min(int(ttl_seconds), 3600))
        self._states: dict[str, tuple[ShopScope, int]] = {}
        self._lock = threading.Lock()

    def issue(self, scope: ShopScope, now: int | None = None) -> str:
        current = int(time.time() if now is None else now)
        state = secrets.token_urlsafe(32)
        with self._lock:
            self._purge(current)
            self._states[state] = (scope, current + self.ttl_seconds)
        return state

    def consume(self, state: str, now: int | None = None) -> ShopScope:
        current = int(time.time() if now is None else now)
        candidate = str(state or "").strip()
        with self._lock:
            self._purge(current)
            value = self._states.pop(candidate, None)
        if value is None:
            raise ValueError("oauth_state_invalid_or_expired")
        return value[0]

    def _purge(self, now: int) -> None:
        for key, (_, expires_at) in list(self._states.items()):
            if expires_at <= now:
                self._states.pop(key, None)


class ExampleDraftHmacSigner:
    """Non-production example used only to test deterministic canonicalization.

    This is not asserted to be a Doudian signature algorithm and is never used
    by :class:`DoudianMarketplaceClient`. An approved adapter must implement the
    exact signing contract supplied for the real application.
    """

    production_safe = False
    algorithm = "EXAMPLE-HMAC-SHA256-NOT-A-PLATFORM-CONTRACT"

    @staticmethod
    def canonical_payload(method: str, timestamp: int, params: Mapping[str, Any]) -> bytes:
        normalized = {
            str(key): params[key]
            for key in sorted(params)
            if key not in {"sign", "access_token"} and params[key] is not None
        }
        param_json = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return f"{method.strip()}\n{int(timestamp)}\n{param_json}".encode("utf-8")

    @classmethod
    def sign(cls, secret: str, method: str, timestamp: int, params: Mapping[str, Any]) -> str:
        if not secret:
            raise ValueError("example secret is required")
        digest = hmac.new(secret.encode(), cls.canonical_payload(method, timestamp, params), hashlib.sha256).digest()
        return base64.b64encode(digest).decode("ascii")


class MarketplacePlatformAdapter(Protocol):
    """Approved platform contract injected by the cloud application."""

    def readiness_evidence(self) -> Mapping[str, Any]: ...
    def build_authorization_url(self, config: DoudianAppConfig, state: str) -> str: ...
    def exchange_authorization_code(
        self, config: DoudianAppConfig, code: str, expected_scope: ShopScope
    ) -> PlatformAuthorization: ...
    def call_open_api(
        self,
        config: DoudianAppConfig,
        token: OAuthToken,
        scope: ShopScope,
        method: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]: ...


def platform_adapter_status(adapter: MarketplacePlatformAdapter | None = None) -> dict[str, Any]:
    active = adapter
    if active is None:
        with _adapter_lock:
            active = _registered_adapter
    if active is None:
        return {"injected": False, "ready": False, "errors": ["platform_adapter_not_injected"]}
    try:
        evidence = dict(active.readiness_evidence())
    except Exception:
        return {"injected": True, "ready": False, "errors": ["adapter_evidence_unavailable"]}
    adapter_id = str(evidence.get("adapter_id") or "").strip()
    contract_version = str(evidence.get("contract_version") or "").strip()
    build_sha256 = str(evidence.get("build_sha256") or "").strip().lower()
    probe = evidence.get("deployment_probe") if isinstance(evidence.get("deployment_probe"), dict) else {}
    probe_checked_at = str(probe.get("checked_at") or "").strip()
    errors = []
    for method_name in (
        "build_authorization_url",
        "exchange_authorization_code",
        "call_open_api",
    ):
        if not callable(getattr(active, method_name, None)):
            errors.append(f"adapter_method_missing:{method_name}")
    if not _ADAPTER_ID.fullmatch(adapter_id):
        errors.append("adapter_id_invalid")
    if not _CONTRACT_VERSION.fullmatch(contract_version):
        errors.append("adapter_contract_version_invalid")
    if not _BUILD_SHA256.fullmatch(build_sha256):
        errors.append("adapter_build_sha256_invalid")
    if probe.get("passed") is not True or not probe_checked_at:
        errors.append("adapter_deployment_probe_missing_or_failed")
    return {
        "injected": True,
        "ready": not errors,
        "adapter_id": adapter_id,
        "contract_version": contract_version,
        "build_sha256": build_sha256,
        "deployment_probe": {"passed": probe.get("passed") is True, "checked_at": probe_checked_at or None},
        "errors": errors,
    }


def register_platform_adapter(adapter: MarketplacePlatformAdapter) -> dict[str, Any]:
    status = platform_adapter_status(adapter)
    if not status["ready"]:
        raise ValueError("platform_adapter_evidence_invalid:" + ",".join(status["errors"]))
    global _registered_adapter
    with _adapter_lock:
        _registered_adapter = adapter
    return status


def clear_registered_platform_adapter() -> None:
    """Clear process-level adapter registration, primarily for test isolation."""
    global _registered_adapter
    with _adapter_lock:
        _registered_adapter = None


class DoudianMarketplaceClient:
    def __init__(
        self,
        config: DoudianAppConfig,
        token_store: TokenStore,
        *,
        state_store: OAuthStateStore | None = None,
        adapter: MarketplacePlatformAdapter | None = None,
    ) -> None:
        self.config = config
        self.tokens = token_store
        self.states = state_store or OAuthStateStore()
        self.adapter = adapter

    def _approved_adapter(self) -> MarketplacePlatformAdapter:
        status = platform_adapter_status(self.adapter)
        if self.adapter is None:
            raise RuntimeError("marketplace_platform_adapter_not_configured")
        if not status["ready"]:
            raise RuntimeError("marketplace_platform_adapter_not_verified:" + ",".join(status["errors"]))
        return self.adapter

    def start_authorization(self, scope: ShopScope) -> dict[str, Any]:
        errors = self.config.validate(production=True)
        if errors:
            raise ValueError("marketplace_oauth_not_configured:" + ",".join(errors))
        adapter = self._approved_adapter()
        state = self.states.issue(scope)
        authorization_url = str(adapter.build_authorization_url(self.config, state) or "").strip()
        parsed = urlparse(authorization_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("platform_adapter_returned_invalid_authorization_url")
        return {
            "authorization_url": authorization_url,
            "state_expires_in": self.states.ttl_seconds,
            "tenant_id": scope.tenant_id,
            "shop_id": scope.shop_id,
        }

    def complete_authorization(self, code: str, state: str) -> dict[str, Any]:
        adapter = self._approved_adapter()
        auth_code = str(code or "").strip()
        if not auth_code:
            raise ValueError("authorization_code_missing")
        scope = self.states.consume(state)
        authorization = adapter.exchange_authorization_code(self.config, auth_code, scope)
        if not isinstance(authorization, PlatformAuthorization):
            raise TypeError("platform_adapter_returned_invalid_authorization")
        if authorization.shop_id != scope.shop_id:
            raise ValueError("oauth_shop_scope_mismatch")
        self.tokens.put(scope, authorization.token)
        return {
            "scope": {"tenant_id": scope.tenant_id, "shop_id": scope.shop_id},
            "token": authorization.token.public_status(),
        }

    def authorization_status(self, scope: ShopScope) -> dict[str, Any]:
        token = self.tokens.get(scope)
        return {
            "scope": {"tenant_id": scope.tenant_id, "shop_id": scope.shop_id},
            "token": token.public_status() if token else {"connected": False},
        }

    def call_api(self, scope: ShopScope, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        adapter = self._approved_adapter()
        token = self.tokens.get(scope)
        if token is None or token.expires_at <= int(time.time()):
            raise ValueError("shop_authorization_missing_or_expired")
        method_name = str(method or "").strip()
        if not method_name:
            raise ValueError("api_method_missing")
        result = adapter.call_open_api(self.config, token, scope, method_name, dict(params))
        if not isinstance(result, dict):
            raise TypeError("platform_adapter_returned_invalid_api_response")
        return result


class DoudianMarketplaceService:
    """Cloud-framework-neutral service that never trusts caller-supplied scope."""

    def __init__(self, client: DoudianMarketplaceClient, authenticator: RequestAuthenticator) -> None:
        self.client = client
        self.authenticator = authenticator

    def start_authorization(self, headers: Mapping[str, str]) -> dict[str, Any]:
        return self.client.start_authorization(self.authenticator.authenticate(headers).scope)

    def authorization_status(self, headers: Mapping[str, str]) -> dict[str, Any]:
        return self.client.authorization_status(self.authenticator.authenticate(headers).scope)

    def call_api(self, headers: Mapping[str, str], method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        return self.client.call_api(self.authenticator.authenticate(headers).scope, method, params)

    def complete_authorization(self, code: str, state: str) -> dict[str, Any]:
        return self.client.complete_authorization(code, state)


__all__ = [
    "DoudianAppConfig",
    "DoudianMarketplaceClient",
    "DoudianMarketplaceService",
    "ExampleDraftHmacSigner",
    "InMemoryTokenStore",
    "MarketplacePlatformAdapter",
    "OAuthStateStore",
    "OAuthToken",
    "PlatformAuthorization",
    "RejectingAuthenticator",
    "RequestAuthenticator",
    "ShopScope",
    "TenantRequestContext",
    "TokenStore",
    "clear_registered_platform_adapter",
    "platform_adapter_status",
    "register_platform_adapter",
]
