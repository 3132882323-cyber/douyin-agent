"""Execution adapter contract for the Chengfang A2 pilot.

The production runtime intentionally ships without a live adapter.  The only
concrete adapter in this module is an in-memory simulator whose receipts are
explicitly marked as simulation and can never be mistaken for a platform
write.  A future official-API adapter must implement this contract and pass the
same fail-closed checks before it can be registered by the runtime.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from typing import Any


class ChengfangAdapterError(RuntimeError):
    """Raised when an adapter cannot safely complete an operation."""


def _receipt_id(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class ChengfangExecutionAdapter(ABC):
    """Narrow write/readback boundary used by the A2 pilot runtime."""

    adapter_id = "unconfigured"
    execution_kind = "unavailable"
    platform_write_enabled = False

    def capabilities(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "execution_kind": self.execution_kind,
            "available": False,
            "platform_write_enabled": self.platform_write_enabled,
            "supports_idempotency": False,
            "supports_readback": False,
        }

    @abstractmethod
    def execute(self, intent: dict[str, Any]) -> dict[str, Any]:
        """Submit one already-authorized intent and return an untrusted receipt."""

    @abstractmethod
    def readback(self, execution: dict[str, Any]) -> dict[str, Any]:
        """Read the current value after an execution attempt."""


class ChengfangSimulationAdapter(ChengfangExecutionAdapter):
    """Deterministic A2 rehearsal adapter.  It never performs network I/O."""

    adapter_id = "chengfang_simulator_v1"
    execution_kind = "simulation"
    platform_write_enabled = False

    def capabilities(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "execution_kind": self.execution_kind,
            "available": True,
            "platform_write_enabled": False,
            "supports_idempotency": True,
            "supports_readback": True,
        }

    def execute(self, intent: dict[str, Any]) -> dict[str, Any]:
        if intent.get("execution_kind") != "simulation":
            raise ChengfangAdapterError("SIMULATION_INTENT_REQUIRED")
        receipt_id = _receipt_id({
            "adapter": self.adapter_id,
            "idempotency_key": intent.get("idempotency_key"),
            "target_value": intent.get("target_value"),
        })
        return {
            "ok": True,
            "receipt_id": f"cf-sim-{receipt_id}",
            "execution_kind": "simulation",
            "adapter_id": self.adapter_id,
            "platform_write_attempted": False,
            "simulated_value": intent.get("target_value"),
        }

    def readback(self, execution: dict[str, Any]) -> dict[str, Any]:
        receipt = execution.get("adapter_receipt") if isinstance(execution.get("adapter_receipt"), dict) else {}
        if receipt.get("execution_kind") != "simulation" or receipt.get("adapter_id") != self.adapter_id:
            raise ChengfangAdapterError("SIMULATION_RECEIPT_REQUIRED")
        return {
            "ok": True,
            "source": "simulation",
            "execution_kind": "simulation",
            "adapter_id": self.adapter_id,
            "receipt_id": receipt.get("receipt_id"),
            "execution_id": execution.get("execution_id"),
            "candidate_id": execution.get("candidate_id"),
            "scope_fingerprint": execution.get("scope_fingerprint"),
            "plan_key": execution.get("plan_key"),
            "idempotency_key": execution.get("idempotency_key"),
            "platform_write_observed": False,
            "observed_value": receipt.get("simulated_value"),
        }


class UnavailableOfficialChengfangAdapter(ChengfangExecutionAdapter):
    """Explicit production placeholder; it cannot execute or fabricate reads."""

    adapter_id = "official_api_unconfigured"
    execution_kind = "live"
    platform_write_enabled = False

    def execute(self, intent: dict[str, Any]) -> dict[str, Any]:
        raise ChengfangAdapterError("OFFICIAL_EXECUTION_ADAPTER_UNAVAILABLE")

    def readback(self, execution: dict[str, Any]) -> dict[str, Any]:
        raise ChengfangAdapterError("OFFICIAL_READBACK_ADAPTER_UNAVAILABLE")


__all__ = [
    "ChengfangAdapterError",
    "ChengfangExecutionAdapter",
    "ChengfangSimulationAdapter",
    "UnavailableOfficialChengfangAdapter",
]
