"""Fail-closed Chengfang surface used by the public/community build.

The community Agent keeps read-only product routes stable while the commercial
decision, evidence and execution runtime remains outside the public artifact.
No method in this module can create or execute a platform write.
"""

from __future__ import annotations

from typing import Any


EVALUATION_INTERVAL_SECONDS = 5 * 60
_UNAVAILABLE = "COMMERCIAL_RUNTIME_NOT_INCLUDED"


def build_official_control_task_contract() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "not_included_in_community_edition",
        "verified": False,
        "production_adapter_enabled": False,
        "execution_enabled": False,
        "operations": [],
        "blockers": [_UNAVAILABLE],
    }


def hydrate_chengfang_profile(
    profile: Any,
    snapshots: Any,
    *,
    expected_scope: Any = None,
) -> dict[str, Any]:
    del snapshots, expected_scope
    return {
        "status": "unavailable",
        "profile": dict(profile) if isinstance(profile, dict) else {},
        "saved": False,
        "execution_enabled": False,
        "blockers": [_UNAVAILABLE],
    }


class ChengfangAutopilotRuntime:
    """Shape-compatible, non-persistent and non-executing community runtime."""

    scope_fingerprint = ""
    pilot_plan_key = ""

    def __init__(self, data_dir: Any, promotion_context: Any = None) -> None:
        del data_dir, promotion_context

    @staticmethod
    def _blocked_result(operation: str) -> dict[str, Any]:
        return {
            "status": "skipped",
            "operation": operation,
            "execution_enabled": False,
            "platform_write_enabled": False,
            "blockers": [_UNAVAILABLE],
        }

    @staticmethod
    def _reject(operation: str) -> None:
        raise ValueError(
            f"{operation} is unavailable: the commercial runtime is not included "
            "in the community edition"
        )

    def load(self) -> dict[str, Any]:
        return {"schema_version": 1, "candidates": [], "executions": []}

    def summary(self, contract_registry: Any = None) -> dict[str, Any]:
        registry = contract_registry if isinstance(contract_registry, dict) else {}
        discovered_capabilities = registry.get("official_capability_discovery")
        if not isinstance(discovered_capabilities, list):
            discovered_capabilities = []
        readiness = {
            "schema_version": 1,
            "current_level": "L0",
            "execution_allowed": False,
            "adapter_policy": "official_api_only",
            "dom_write_fallback_allowed": False,
            "official_api": {
                "registry_verified": False,
                "contract_version": str(registry.get("contract_version") or ""),
                "verified_write_operations": [],
                "executable_operations": [],
                "required_contract_evidence": [],
                "discovered_capabilities": discovered_capabilities,
            },
            "blockers": [_UNAVAILABLE],
            "next_gate": {
                "level": "commercial_edition",
                "ready": False,
                "blockers": [_UNAVAILABLE],
            },
        }
        return {
            "schema_version": 1,
            "edition": "community",
            "commercial_runtime_available": False,
            "storage": "disabled_in_community_edition",
            "scope_fingerprint": "",
            "scope_bound": False,
            "decision_automation": {
                "mode": "off",
                "running": False,
                "interval_seconds": EVALUATION_INTERVAL_SECONDS,
                "platform_write_enabled": False,
            },
            "write_automation": {
                "mode": "disabled",
                "execution_allowed": False,
                "official_api_only": True,
                "dom_write_fallback_allowed": False,
            },
            "profile": {},
            "profile_validation": {"ready": False, "missing": [_UNAVAILABLE]},
            "policy": {"kill_switch": True, "allowed_actions": []},
            "policy_validation": {"status": "unavailable", "missing": [_UNAVAILABLE]},
            "shadow": {"enabled": False, "status": "unavailable"},
            "shadow_evidence": {},
            "emergency_stop": {"active": True, "reason": _UNAVAILABLE},
            "latest_evaluation": None,
            "evaluations": [],
            "evaluation_count": 0,
            "candidates": [],
            "candidate_count": 0,
            "readiness": readiness,
            "runtime_capabilities": {
                "commercial_runtime_available": False,
                "audit_supported": True,
                "readback_supported": False,
                "emergency_stop_available": True,
            },
            "a2_pilot": self.a2_pilot_summary(),
            "storage_warning": _UNAVAILABLE,
        }

    def a2_pilot_summary(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "level": "unavailable",
            "master_enabled": False,
            "simulation": False,
            "live_execution_available": False,
            "platform_write_enabled": False,
            "stop_active": True,
            "stop_reason": _UNAVAILABLE,
            "candidates": [],
            "executions": [],
            "execution_count": 0,
            "blockers": [_UNAVAILABLE],
        }

    def enforce_a2_safety(self, *, now_ms: int | None = None) -> dict[str, Any]:
        del now_ms
        return self._blocked_result("enforce_a2_safety")

    def evaluate(self, *, trigger: str = "manual", now_ms: int | None = None) -> dict[str, Any]:
        del now_ms
        return {**self._blocked_result("evaluate"), "trigger": trigger}

    def save_profile(self, value: Any, *, trusted_evidence: bool = False) -> dict[str, Any]:
        del value, trusted_evidence
        self._reject("save_profile")

    def set_shadow(self, enabled: bool, profile: Any = None) -> dict[str, Any]:
        del enabled, profile
        self._reject("set_shadow")

    def configure_a2_pilot(self, value: Any) -> dict[str, Any]:
        del value
        self._reject("configure_a2_pilot")

    def review_candidate(self, candidate_id: Any, decision: Any, *, note: Any = None) -> dict[str, Any]:
        del candidate_id, decision, note
        self._reject("review_candidate")

    def execute_candidate(self, candidate_id: Any, **kwargs: Any) -> dict[str, Any]:
        del candidate_id, kwargs
        self._reject("execute_candidate")

    def readback_execution(self, execution_id: Any, **kwargs: Any) -> dict[str, Any]:
        del execution_id, kwargs
        self._reject("readback_execution")

    def stop_a2_pilot(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self._reject("stop_a2_pilot")

    def record_feedback(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        self._reject("record_feedback")

    def emergency_stop(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return self._blocked_result("emergency_stop")

    def reset(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return self._blocked_result("reset")
