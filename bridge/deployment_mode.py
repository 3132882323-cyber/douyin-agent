"""Deployment-mode policy for local and Doudian marketplace editions.

The marketplace edition must never silently fall back to browser scraping or
DOM execution.  Unknown mode values therefore resolve to a fail-closed policy.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from urllib.parse import urlparse


LOCAL_MODES = {"local", "internal"}
MARKETPLACE_MODES = {"doudian_marketplace", "cloud"}
KNOWN_MODES = LOCAL_MODES | MARKETPLACE_MODES


@dataclass(frozen=True)
class DeploymentPolicy:
    requested_mode: str
    mode: str
    valid: bool
    browser_page_capture: bool
    browser_dom_execution: bool
    local_companion: bool
    official_open_api: bool
    marketplace_release: bool

    def public_status(self) -> dict[str, object]:
        value = asdict(self)
        value["fail_closed"] = not self.browser_page_capture and not self.browser_dom_execution
        return value


def resolve_deployment_policy(mode: str | None = None) -> DeploymentPolicy:
    requested = str(mode if mode is not None else os.environ.get("DIAN_AGENT_DEPLOYMENT_MODE", "local"))
    requested = requested.strip().lower() or "local"
    valid = requested in KNOWN_MODES
    if requested in LOCAL_MODES:
        return DeploymentPolicy(
            requested_mode=requested,
            mode=requested,
            valid=True,
            browser_page_capture=True,
            browser_dom_execution=True,
            local_companion=True,
            official_open_api=False,
            marketplace_release=False,
        )
    mode_name = requested if requested in MARKETPLACE_MODES else "invalid"
    return DeploymentPolicy(
        requested_mode=requested,
        mode=mode_name,
        valid=valid,
        browser_page_capture=False,
        browser_dom_execution=False,
        local_companion=False,
        official_open_api=requested in MARKETPLACE_MODES,
        marketplace_release=requested in MARKETPLACE_MODES,
    )


BROWSER_CAPTURE_POST_PATHS = frozenset({
    "/push",
    "/scan-status",
    "/distribution/extension-source",
    "/stores/link",
    "/stores/select",
    "/onboarding/update",
})

BROWSER_DOM_EXECUTION_POST_PATHS = frozenset({
    "/actions/preflight/authorize",
    "/actions/preflight/consume",
    "/actions/preflight/preview",
    "/actions/execution/result",
    "/actions/execution/verify",
    "/actions/rollback/create",
})


def blocked_browser_capability(path: str, policy: DeploymentPolicy | None = None) -> str | None:
    active = policy or resolve_deployment_policy()
    if path in BROWSER_CAPTURE_POST_PATHS and not active.browser_page_capture:
        return "browser_page_capture_disabled"
    if path in BROWSER_DOM_EXECUTION_POST_PATHS and not active.browser_dom_execution:
        return "browser_dom_execution_disabled"
    return None


def allowed_web_origins() -> frozenset[str]:
    """Return exact HTTPS origins explicitly configured for the cloud UI."""
    values: set[str] = set()
    for raw in os.environ.get("DIAN_AGENT_ALLOWED_WEB_ORIGINS", "").split(","):
        candidate = raw.strip().lower().rstrip("/")
        parsed = urlparse(candidate)
        if parsed.scheme == "https" and parsed.netloc and not parsed.path and not parsed.query and not parsed.fragment:
            values.add(candidate)
    return frozenset(values)


def request_origin_allowed(origin: str, policy: DeploymentPolicy | None = None) -> bool:
    active = policy or resolve_deployment_policy()
    normalized = str(origin or "").strip().lower().rstrip("/")
    if active.local_companion:
        return normalized.startswith(("chrome-extension://", "moz-extension://", "safari-web-extension://"))
    return normalized in allowed_web_origins()


__all__ = [
    "BROWSER_CAPTURE_POST_PATHS",
    "BROWSER_DOM_EXECUTION_POST_PATHS",
    "DeploymentPolicy",
    "blocked_browser_capability",
    "allowed_web_origins",
    "request_origin_allowed",
    "resolve_deployment_policy",
]
