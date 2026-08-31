"""Local, fail-closed Chengfang control-task rehearsal center.

The public product pattern separates status, budget and duration changes into
independent control tasks.  This module implements that useful separation for
the local Agent without guessing a production API or enabling platform writes.

Every task changes exactly one variable and follows the same lifecycle:

    review_required -> approved -> awaiting_readback -> verified

Rejected, blocked and readback-failed tasks are terminal.  The persisted
records contain only scope/plan fingerprints and opaque candidate identifiers;
raw advertiser, shop, plan and task identifiers are never stored here.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
MAX_TASKS = 120
MAX_LIFECYCLE_EVENTS = 24
TASK_FAMILIES = ("status", "budget", "duration")
FAMILY_LABELS = {
    "status": "启停状态",
    "budget": "任务预算",
    "duration": "投放时长",
}
FAMILY_OPERATIONS = {
    "status": frozenset({"ENABLE", "PAUSE", "CLOSE"}),
    "budget": frozenset({"DECREASE", "INCREASE"}),
    "duration": frozenset({"SHORTEN", "EXTEND"}),
}
TERMINAL_STATES = frozenset({"verified", "rejected", "blocked", "readback_failed"})
STATUS_ALIASES = {
    "ACTIVE": "ACTIVE",
    "RUNNING": "ACTIVE",
    "PROCESSING": "ACTIVE",
    "投放中": "ACTIVE",
    "调控中": "ACTIVE",
    "ENABLE": "ACTIVE",
    "ENABLED": "ACTIVE",
    "PAUSED": "PAUSED",
    "PAUSE": "PAUSED",
    "DISABLE": "PAUSED",
    "DISABLED": "PAUSED",
    "手动关停": "PAUSED",
    "全域投放已暂停": "PAUSED",
    "暂停": "PAUSED",
    "CLOSED": "CLOSED",
    "CLOSE": "CLOSED",
    "DELETED": "CLOSED",
    "已删除": "CLOSED",
    "调控结束": "CLOSED",
    "COMPLETED": "CLOSED",
    "OFFLINE_BALANCE": "OFFLINE_BALANCE",
    "账户余额不足": "OFFLINE_BALANCE",
    "OFFLINE_BUDGET": "OFFLINE_BUDGET",
    "预算已花完": "OFFLINE_BUDGET",
    "OFFLINE_TIME": "OFFLINE_TIME",
    "到达投放时间": "OFFLINE_TIME",
    "ROI2_DISABLE": "ROI2_DISABLE",
    "FROZEN": "FROZEN",
    "直播间未开播": "LIVE_NOT_STARTED",
}
_LOCK = threading.RLock()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result <= 0 or result > 100_000_000:
        return None
    return result


def canonical_control_status(value: Any) -> str:
    """Normalize known public/official labels without guessing unknown states."""

    text = str(value or "").strip()
    if not text:
        return ""
    return STATUS_ALIASES.get(text, STATUS_ALIASES.get(text.upper(), "UNKNOWN"))


def _clean_reason_codes(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        clean = "".join(character for character in str(item or "").upper() if character.isalnum() or character in {"_", "-"})[:80]
        if clean and clean not in result:
            result.append(clean)
    return result[:8]


def _task_integrity_payload(task: dict[str, Any], scope_fingerprint: str) -> dict[str, Any]:
    return {
        "schema_version": task.get("schema_version"),
        "scope_fingerprint": scope_fingerprint,
        "task_id": task.get("task_id"),
        "family": task.get("family"),
        "operation": task.get("operation"),
        "plan_scope_fingerprint": task.get("plan_scope_fingerprint"),
        "source_candidate_id": task.get("source_candidate_id"),
        "current_value": task.get("current_value"),
        "target_value": task.get("target_value"),
        "reason_codes": task.get("reason_codes"),
        "evidence": task.get("evidence"),
        "single_variable_only": task.get("single_variable_only"),
        "change": task.get("change"),
        "simulation_allowed": task.get("simulation_allowed"),
        "simulation_blockers": task.get("simulation_blockers"),
        "production_allowed": task.get("production_allowed"),
        "production_blockers": task.get("production_blockers"),
        "idempotency_key": task.get("idempotency_key"),
        "platform_write_enabled": task.get("platform_write_enabled"),
        "created_at": task.get("created_at"),
    }


def _normalize_family_values(family: str, operation: str, current: Any, target: Any) -> tuple[Any, Any, list[str]]:
    blockers: list[str] = []
    if family == "status":
        normalized_current = canonical_control_status(current)
        implied_target = {"ENABLE": "ACTIVE", "PAUSE": "PAUSED", "CLOSE": "CLOSED"}.get(operation, "")
        normalized_target = canonical_control_status(target) if target is not None and target != "" else implied_target
        if normalized_current in {"", "UNKNOWN"}:
            blockers.append("CURRENT_STATUS_UNVERIFIED")
        if normalized_target in {"", "UNKNOWN"} or normalized_target != implied_target:
            blockers.append("TARGET_STATUS_INVALID")
        if normalized_current == normalized_target:
            blockers.append("NO_EFFECT_CHANGE")
        if normalized_current not in {"ACTIVE", "PAUSED", "CLOSED"}:
            blockers.append("CURRENT_STATUS_NOT_ACTIONABLE")
        if normalized_current == "CLOSED":
            blockers.append("CLOSED_TASK_IS_TERMINAL")
        return normalized_current, normalized_target, blockers

    normalized_current = _number(current)
    normalized_target = _number(target)
    if normalized_current is None or normalized_target is None:
        blockers.append("POSITIVE_NUMERIC_VALUE_REQUIRED")
        return normalized_current, normalized_target, blockers
    if family == "duration":
        if not normalized_current.is_integer() or not normalized_target.is_integer():
            blockers.append("DURATION_MINUTES_MUST_BE_INTEGER")
        normalized_current = int(normalized_current)
        normalized_target = int(normalized_target)
    if normalized_current == normalized_target:
        blockers.append("NO_EFFECT_CHANGE")
    if operation in {"DECREASE", "SHORTEN"} and normalized_target >= normalized_current:
        blockers.append("TARGET_MUST_BE_LOWER")
    if operation in {"INCREASE", "EXTEND"} and normalized_target <= normalized_current:
        blockers.append("TARGET_MUST_BE_HIGHER")
    return normalized_current, normalized_target, blockers


def build_control_task_draft(
    value: Any,
    scope_fingerprint: Any,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Build one privacy-preserving, single-variable rehearsal draft."""

    raw = value if isinstance(value, dict) else {}
    captured_at = int(now_ms or _now_ms())
    family = str(raw.get("family") or "").strip().lower()
    operation = str(raw.get("operation") or "").strip().upper()
    clean_scope = str(scope_fingerprint or "").strip()
    raw_plan_key = str(raw.get("plan_key") or "").strip()
    source_candidate_id = str(raw.get("candidate_id") or raw.get("source_candidate_id") or "").strip()[:160]
    simulation_blockers: list[str] = []
    if family not in TASK_FAMILIES:
        simulation_blockers.append("UNKNOWN_ACTION_FAMILY")
    if operation not in FAMILY_OPERATIONS.get(family, frozenset()):
        simulation_blockers.append("OPERATION_NOT_ALLOWED_FOR_FAMILY")
    if not clean_scope:
        simulation_blockers.append("SCOPE_BINDING_INCOMPLETE")
    if not raw_plan_key:
        simulation_blockers.append("PLAN_SCOPE_REQUIRED")

    current, target, value_blockers = _normalize_family_values(
        family,
        operation,
        raw.get("current_value"),
        raw.get("target_value"),
    ) if family in TASK_FAMILIES and operation in FAMILY_OPERATIONS.get(family, frozenset()) else (None, None, [])
    simulation_blockers.extend(value_blockers)
    simulation_blockers = sorted(set(simulation_blockers))

    plan_scope_fingerprint = _stable_hash({"scope": clean_scope, "plan": raw_plan_key}) if clean_scope and raw_plan_key else ""
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
    try:
        evidence_captured_at = int(evidence.get("captured_at_ms") or raw.get("evidence_captured_at_ms") or captured_at)
    except (TypeError, ValueError):
        evidence_captured_at = captured_at
        simulation_blockers = sorted(set([*simulation_blockers, "EVIDENCE_TIMESTAMP_INVALID"]))
    evidence_fingerprint = _stable_hash({
        "scope": clean_scope,
        "plan": plan_scope_fingerprint,
        "candidate": source_candidate_id,
        "captured_at": evidence_captured_at,
        "family": family,
        "current": current,
    }) if clean_scope and plan_scope_fingerprint else ""
    idempotency_key = _stable_hash({
        "scope": clean_scope,
        "plan": plan_scope_fingerprint,
        "family": family,
        "operation": operation,
        "target": target,
        "evidence": evidence_fingerprint,
    }) if not simulation_blockers else ""
    task_id = f"cf-control-{idempotency_key[:20]}" if idempotency_key else f"cf-blocked-{_stable_hash({'value': raw, 'at': captured_at})[:20]}"

    production_blockers = ["PRODUCTION_WRITE_DISABLED", "ACCOUNT_WRITE_CONTRACT_UNVERIFIED"]
    if family == "budget" and operation == "INCREASE":
        production_blockers.append("FIRST_RELEASE_DECREASE_ONLY")
    if family == "status":
        production_blockers.append("STATUS_WRITE_CONTRACT_NOT_ACCOUNT_VERIFIED")
    if family == "status" and operation == "CLOSE":
        production_blockers.append("CLOSE_SEMANTICS_UNVERIFIED")
    if family == "duration":
        production_blockers.append("DURATION_WRITE_CONTRACT_UNVERIFIED")

    state = "blocked" if simulation_blockers else "review_required"
    task = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "task_label": f"{FAMILY_LABELS.get(family, '未知动作')} · {plan_scope_fingerprint[-6:].upper() or '待绑定'}",
        "family": family,
        "family_label": FAMILY_LABELS.get(family, "未知动作"),
        "operation": operation,
        "plan_scope_fingerprint": plan_scope_fingerprint,
        "source_candidate_id": source_candidate_id,
        "current_value": current,
        "target_value": target,
        "reason_codes": _clean_reason_codes(raw.get("reason_codes") or raw.get("reasons")),
        "evidence": {
            "source": str(evidence.get("source") or raw.get("evidence_source") or "local_candidate")[:80],
            "captured_at_ms": evidence_captured_at,
            "fingerprint": evidence_fingerprint,
        },
        "single_variable_only": True,
        "change": {family: target} if family in TASK_FAMILIES and target is not None else {},
        "state": state,
        "simulation_allowed": not simulation_blockers,
        "simulation_blockers": simulation_blockers,
        "production_allowed": False,
        "production_blockers": sorted(set(production_blockers)),
        "idempotency_key": idempotency_key,
        "platform_write_enabled": False,
        "platform_write_attempted": False,
        "created_at": captured_at,
        "updated_at": captured_at,
        "lifecycle": [{"state": state, "at": captured_at, "actor": "local_control_center"}],
    }
    task["integrity_fingerprint"] = _stable_hash(_task_integrity_payload(task, clean_scope))
    return task


class ChengfangControlTaskCenter:
    """Persist and rehearse control tasks for one opaque operating scope."""

    def __init__(self, data_dir: str | os.PathLike[str], scope_fingerprint: Any) -> None:
        self.data_dir = Path(data_dir)
        self.scope_fingerprint = str(scope_fingerprint or "").strip()
        scope_key = self.scope_fingerprint or "unscoped"
        self.path = self.data_dir / "chengfang_control_tasks" / f"{scope_key}.json"

    def _default_state(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "scope_fingerprint": self.scope_fingerprint,
            "tasks": [],
            "created_at": _now_ms(),
            "updated_at": _now_ms(),
        }

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default_state()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = self._default_state()
            state["storage_warning"] = "CONTROL_TASK_STATE_UNREADABLE"
            return state
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            state = self._default_state()
            state["storage_warning"] = "CONTROL_TASK_STATE_SCHEMA_UNSUPPORTED"
            return state
        if str(value.get("scope_fingerprint") or "") != self.scope_fingerprint:
            state = self._default_state()
            state["storage_warning"] = "CONTROL_TASK_STATE_SCOPE_MISMATCH"
            return state
        value["scope_fingerprint"] = self.scope_fingerprint
        value["tasks"] = value.get("tasks") if isinstance(value.get("tasks"), list) else []
        return value

    def _write_unlocked(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state["scope_fingerprint"] = self.scope_fingerprint
        state["updated_at"] = _now_ms()
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2)
        fd, temporary_name = tempfile.mkstemp(prefix="control-tasks-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    @staticmethod
    def _find(state: dict[str, Any], task_id: Any) -> dict[str, Any] | None:
        clean_id = str(task_id or "").strip()
        return next((item for item in state.get("tasks", []) if isinstance(item, dict) and item.get("task_id") == clean_id), None)

    def _validate_task_integrity(self, task: dict[str, Any]) -> None:
        expected = _stable_hash(_task_integrity_payload(task, self.scope_fingerprint))
        if not str(task.get("integrity_fingerprint") or "") or task.get("integrity_fingerprint") != expected:
            raise ValueError("CONTROL_TASK_INTEGRITY_INVALID")

    @staticmethod
    def _transition(task: dict[str, Any], target: str, actor: str, now_ms: int) -> None:
        task["state"] = target
        task["updated_at"] = now_ms
        history = task.get("lifecycle") if isinstance(task.get("lifecycle"), list) else []
        task["lifecycle"] = [*history, {"state": target, "at": now_ms, "actor": actor}][-MAX_LIFECYCLE_EVENTS:]

    def load(self) -> dict[str, Any]:
        with _LOCK:
            return self._load_unlocked()

    def create_draft(self, value: Any) -> dict[str, Any]:
        now_ms = _now_ms()
        draft = build_control_task_draft(value, self.scope_fingerprint, now_ms=now_ms)
        with _LOCK:
            state = self._load_unlocked()
            idempotency_key = str(draft.get("idempotency_key") or "")
            existing = next((
                item for item in state.get("tasks", [])
                if isinstance(item, dict) and idempotency_key and item.get("idempotency_key") == idempotency_key
            ), None)
            if existing:
                return {"task": existing, "deduplicated": True}
            state["tasks"] = [*state.get("tasks", []), draft][-MAX_TASKS:]
            self._write_unlocked(state)
            return {"task": draft, "deduplicated": False}

    def review(self, task_id: Any, decision: Any, *, confirm: bool = False, note: Any = None) -> dict[str, Any]:
        clean_decision = str(decision or "").strip().lower()
        if clean_decision not in {"accept", "reject"}:
            raise ValueError("decision 必须为 accept 或 reject")
        if clean_decision == "accept" and confirm is not True:
            raise ValueError("接受控制任务前必须显式确认")
        now_ms = _now_ms()
        with _LOCK:
            state = self._load_unlocked()
            task = self._find(state, task_id)
            if task is None:
                raise ValueError("未找到控制任务")
            self._validate_task_integrity(task)
            target_state = "approved" if clean_decision == "accept" else "rejected"
            if task.get("state") == target_state:
                return task
            if task.get("state") != "review_required":
                raise ValueError("当前控制任务不在待复核状态")
            if clean_decision == "accept" and task.get("simulation_allowed") is not True:
                raise ValueError("控制任务未通过本机模拟门槛")
            self._transition(task, target_state, "human_review", now_ms)
            task["review_note"] = str(note or "").strip()[:500]
            task["reviewed_at"] = now_ms
            self._write_unlocked(state)
            return task

    def simulate(self, task_id: Any) -> dict[str, Any]:
        now_ms = _now_ms()
        with _LOCK:
            state = self._load_unlocked()
            task = self._find(state, task_id)
            if task is None:
                raise ValueError("未找到控制任务")
            self._validate_task_integrity(task)
            if task.get("state") in {"awaiting_readback", "verified"} and isinstance(task.get("simulation_receipt"), dict):
                return {"task": task, "deduplicated": True}
            if task.get("state") != "approved":
                raise ValueError("控制任务必须先通过人工复核")
            change = task.get("change") if isinstance(task.get("change"), dict) else {}
            if list(change) != [task.get("family")]:
                raise ValueError("控制任务必须且只能修改一个变量")
            if task.get("platform_write_enabled") is not False or task.get("production_allowed") is not False:
                raise ValueError("本机模拟检测到不安全的生产写标记")
            receipt_id = f"cf-control-sim-{_stable_hash({'task': task['task_id'], 'idem': task['idempotency_key'], 'target': task['target_value']})[:20]}"
            task["simulation_receipt"] = {
                "receipt_id": receipt_id,
                "task_id": task["task_id"],
                "idempotency_key": task["idempotency_key"],
                "execution_kind": "simulation",
                "changed_field": task["family"],
                "simulated_value": task["target_value"],
                "platform_write_attempted": False,
                "created_at": now_ms,
            }
            task["platform_write_attempted"] = False
            self._transition(task, "awaiting_readback", "local_simulator", now_ms)
            self._write_unlocked(state)
            return {"task": task, "deduplicated": False}

    def readback(self, task_id: Any, observation: Any = None) -> dict[str, Any]:
        now_ms = _now_ms()
        with _LOCK:
            state = self._load_unlocked()
            task = self._find(state, task_id)
            if task is None:
                raise ValueError("未找到控制任务")
            self._validate_task_integrity(task)
            if task.get("state") in {"verified", "readback_failed"} and isinstance(task.get("readback"), dict):
                return {"task": task, "deduplicated": True}
            if task.get("state") != "awaiting_readback":
                raise ValueError("控制任务尚未进入回读阶段")
            receipt = task.get("simulation_receipt") if isinstance(task.get("simulation_receipt"), dict) else {}
            supplied = observation if isinstance(observation, dict) else {}
            expected_receipt_id = f"cf-control-sim-{_stable_hash({'task': task['task_id'], 'idem': task['idempotency_key'], 'target': task['target_value']})[:20]}"
            receipt_valid = (
                receipt.get("receipt_id") == expected_receipt_id
                and receipt.get("task_id") == task.get("task_id")
                and receipt.get("idempotency_key") == task.get("idempotency_key")
                and receipt.get("execution_kind") == "simulation"
                and receipt.get("platform_write_attempted") is False
            )
            if not receipt_valid:
                raise ValueError("SIMULATION_RECEIPT_BINDING_INVALID")
            if supplied and any(
                str(supplied.get(field) or "") != str(expected)
                for field, expected in {
                    "task_id": task.get("task_id"),
                    "idempotency_key": task.get("idempotency_key"),
                    "receipt_id": receipt.get("receipt_id"),
                }.items()
            ):
                raise ValueError("SIMULATION_READBACK_BINDING_MISMATCH")
            source = str(supplied.get("source") or "simulation")
            platform_write_observed = supplied.get("platform_write_observed") is True
            if source not in {"simulation", "simulation_override"} or platform_write_observed:
                raise ValueError("本机控制任务只接受明确标记的模拟回读")
            observed = supplied.get("observed_value", receipt.get("simulated_value"))
            expected = task.get("target_value")
            if task.get("family") == "status":
                observed = canonical_control_status(observed)
                matched = observed == expected and observed not in {"", "UNKNOWN"}
            elif task.get("family") == "duration":
                parsed = _number(observed)
                observed = int(parsed) if parsed is not None and parsed.is_integer() else None
                matched = observed == expected
            else:
                observed = _number(observed)
                matched = observed is not None and expected is not None and abs(observed - float(expected)) <= 0.01
            task["readback"] = {
                "source": source,
                "execution_kind": "simulation",
                "changed_field": task.get("family"),
                "expected_value": expected,
                "observed_value": observed,
                "matched": matched,
                "platform_write_observed": False,
                "captured_at": now_ms,
            }
            self._transition(task, "verified" if matched else "readback_failed", "simulation_readback", now_ms)
            if not matched:
                task["failure_reason"] = "SIMULATION_READBACK_MISMATCH"
            self._write_unlocked(state)
            return {"task": task, "deduplicated": False}

    def summary(self, *, importable_candidates: Any = None) -> dict[str, Any]:
        state = self.load()
        tasks = [item for item in state.get("tasks", []) if isinstance(item, dict)]
        candidates = importable_candidates if isinstance(importable_candidates, list) else []
        family_summaries = []
        for family in TASK_FAMILIES:
            family_tasks = [item for item in tasks if item.get("family") == family]
            family_summaries.append({
                "id": family,
                "label": FAMILY_LABELS[family],
                "task_count": len(family_tasks),
                "pending_count": sum(item.get("state") not in TERMINAL_STATES for item in family_tasks),
                "verified_count": sum(item.get("state") == "verified" for item in family_tasks),
                "simulation_available": True,
                "production_available": False,
                "contract_status": (
                    "documented_not_account_verified" if family in {"status", "budget"}
                    else "write_contract_not_verified"
                ),
            })
        lifecycle = []
        for task in tasks:
            for event in task.get("lifecycle", []):
                if isinstance(event, dict):
                    lifecycle.append({
                        "task_id": task.get("task_id"),
                        "family": task.get("family"),
                        "task_label": task.get("task_label"),
                        **event,
                    })
        lifecycle.sort(key=lambda item: int(item.get("at") or 0), reverse=True)
        return {
            "schema_version": SCHEMA_VERSION,
            "scope_bound": bool(self.scope_fingerprint),
            "scope_fingerprint": self.scope_fingerprint,
            "storage": "local_scope_partitioned",
            "single_variable_only": True,
            "execution_kind": "simulation",
            "platform_write_enabled": False,
            "automatic_platform_submit": False,
            "families": family_summaries,
            "tasks": tasks[-30:][::-1],
            "task_count": len(tasks),
            "pending_count": sum(item.get("state") not in TERMINAL_STATES for item in tasks),
            "verified_count": sum(item.get("state") == "verified" for item in tasks),
            "importable_candidates": candidates[:10],
            "importable_candidate_count": len(candidates),
            "activity": lifecycle[:100],
            "notice": "状态、预算、时长分开复核与回读；当前仅本机模拟，未启用千川写入。",
            "storage_warning": state.get("storage_warning"),
        }


__all__ = [
    "ChengfangControlTaskCenter",
    "FAMILY_LABELS",
    "FAMILY_OPERATIONS",
    "SCHEMA_VERSION",
    "TASK_FAMILIES",
    "build_control_task_draft",
    "canonical_control_status",
]
