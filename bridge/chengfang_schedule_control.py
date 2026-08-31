"""Fail-closed local schedule orchestration for Chengfang control tasks.

The schedule is a local rehearsal package, not a platform timer.  It validates
daily time ranges, produces deterministic enable/pause previews and records a
review/simulation/readback lifecycle.  No browser DOM or platform write path is
present in this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
MAX_REVISIONS = 30
MAX_TIME_RANGES = 10
BUSINESS_TIMEZONE = timezone(timedelta(hours=8))
BUSINESS_TIMEZONE_LABEL = "Asia/Shanghai"
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
TERMINAL_STATES = frozenset({"verified", "rejected", "blocked", "readback_failed"})
_LOCK = threading.RLock()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _minute_of_day(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def validate_schedule_time_ranges(value: Any, *, enabled: bool = True) -> dict[str, Any]:
    """Normalize daily ranges and reject ambiguity, overlap and cross-day spans."""

    ranges = value if isinstance(value, list) else []
    blockers: list[str] = []
    normalized = []
    if len(ranges) > MAX_TIME_RANGES:
        blockers.append("TOO_MANY_TIME_RANGES")
    for index, item in enumerate(ranges[:MAX_TIME_RANGES]):
        raw = item if isinstance(item, dict) else {}
        start = str(raw.get("start") or "").strip()
        end = str(raw.get("end") or "").strip()
        if not TIME_PATTERN.fullmatch(start) or not TIME_PATTERN.fullmatch(end):
            blockers.append(f"TIME_RANGE_{index + 1}_INVALID")
            continue
        start_minute = _minute_of_day(start)
        end_minute = _minute_of_day(end)
        if start_minute == end_minute:
            blockers.append(f"TIME_RANGE_{index + 1}_EMPTY")
            continue
        if start_minute > end_minute:
            blockers.append(f"TIME_RANGE_{index + 1}_CROSS_DAY_MUST_SPLIT")
            continue
        normalized.append({
            "start": start,
            "end": end,
            "start_minute": start_minute,
            "end_minute": end_minute,
        })
    normalized.sort(key=lambda item: (item["start_minute"], item["end_minute"]))
    for index in range(1, len(normalized)):
        previous = normalized[index - 1]
        current = normalized[index]
        if current["start_minute"] < previous["end_minute"]:
            blockers.append("TIME_RANGES_OVERLAP")
    if enabled and not normalized:
        blockers.append("AT_LEAST_ONE_TIME_RANGE_REQUIRED")
    public_ranges = [{"start": item["start"], "end": item["end"]} for item in normalized]
    return {
        "valid": not blockers,
        "time_ranges": public_ranges,
        "count": len(public_ranges),
        "blockers": sorted(set(blockers)),
        "maximum": MAX_TIME_RANGES,
        "cross_day_supported": False,
        "timezone": BUSINESS_TIMEZONE_LABEL,
    }


def build_schedule_draft(
    value: Any,
    scope_fingerprint: Any,
    plan_key: Any,
    *,
    revision: int = 1,
    now_ms: int | None = None,
    previous: Any = None,
) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    captured_at = int(now_ms or _now_ms())
    clean_scope = str(scope_fingerprint or "").strip()
    clean_plan = str(plan_key or "").strip()
    enabled = raw.get("enabled") is True
    validation = validate_schedule_time_ranges(raw.get("time_ranges"), enabled=enabled)
    blockers = list(validation["blockers"])
    if not isinstance(raw.get("enabled"), bool):
        blockers.append("ENABLED_BOOLEAN_REQUIRED")
    if not clean_scope:
        blockers.append("SCOPE_BINDING_INCOMPLETE")
    if not clean_plan:
        blockers.append("PLAN_SCOPE_REQUIRED")
    plan_scope_fingerprint = _stable_hash({"scope": clean_scope, "plan": clean_plan}) if clean_scope and clean_plan else ""
    core = {
        "schema_version": SCHEMA_VERSION,
        "scope_fingerprint": clean_scope,
        "plan_scope_fingerprint": plan_scope_fingerprint,
        "enabled": enabled,
        "timezone": BUSINESS_TIMEZONE_LABEL,
        "time_ranges": validation["time_ranges"],
        "single_plan_only": True,
        "action_family": "status",
        "transition_operations": ["ENABLE", "PAUSE"],
    }
    config_fingerprint = _stable_hash(core) if not blockers else ""
    revision_id = f"cf-schedule-{config_fingerprint[:20]}" if config_fingerprint else f"cf-schedule-blocked-{_stable_hash({'raw': raw, 'at': captured_at})[:20]}"
    previous_config = previous if isinstance(previous, dict) else {}
    before = {
        "enabled": previous_config.get("enabled") is True,
        "timezone": str(previous_config.get("timezone") or BUSINESS_TIMEZONE_LABEL),
        "time_ranges": previous_config.get("time_ranges") if isinstance(previous_config.get("time_ranges"), list) else [],
    }
    after = {"enabled": enabled, "timezone": BUSINESS_TIMEZONE_LABEL, "time_ranges": validation["time_ranges"]}
    changes = []
    for key, label in (("enabled", "定时启停"), ("timezone", "时区"), ("time_ranges", "每日时段")):
        if before[key] != after[key]:
            changes.append({"field": key, "label": label, "before": before[key], "after": after[key]})
    blockers = sorted(set(blockers))
    state = "blocked" if blockers else "review_required"
    return {
        **core,
        "revision_id": revision_id,
        "revision": max(1, int(revision or 1)),
        "state": state,
        "state_label": "已阻止" if blockers else "等待人工复核",
        "validation": {**validation, "blockers": blockers, "valid": not blockers},
        "changes": changes,
        "changed_count": len(changes),
        "config_fingerprint": config_fingerprint,
        "idempotency_key": config_fingerprint,
        "simulation_allowed": not blockers,
        "production_allowed": False,
        "production_blockers": [
            "PRODUCTION_WRITE_DISABLED",
            "SCHEDULE_WRITE_CONTRACT_UNVERIFIED",
            "ACCOUNT_WRITE_PERMISSION_UNVERIFIED",
        ],
        "platform_write_enabled": False,
        "platform_write_attempted": False,
        "created_at": captured_at,
        "updated_at": captured_at,
        "lifecycle": [{"state": state, "at": captured_at, "actor": "local_schedule_center"}],
    }


def build_schedule_transition_preview(config: Any, *, now_ms: int | None = None, hours: int = 48) -> list[dict[str, Any]]:
    item = config if isinstance(config, dict) else {}
    if item.get("enabled") is not True:
        return []
    validation = validate_schedule_time_ranges(item.get("time_ranges"), enabled=True)
    if not validation["valid"]:
        return []
    captured_at = int(now_ms or _now_ms())
    now = datetime.fromtimestamp(captured_at / 1000, tz=BUSINESS_TIMEZONE)
    deadline = now + timedelta(hours=max(1, min(168, int(hours or 48))))
    events = []
    for day_offset in range(0, 8):
        target_date = (now + timedelta(days=day_offset)).date()
        for time_range in validation["time_ranges"]:
            for operation, time_value in (("ENABLE", time_range["start"]), ("PAUSE", time_range["end"])):
                hour, minute = (int(part) for part in time_value.split(":"))
                event_time = datetime.combine(target_date, datetime_time(hour, minute), tzinfo=BUSINESS_TIMEZONE)
                if event_time <= now or event_time > deadline:
                    continue
                events.append({
                    "operation": operation,
                    "operation_label": "启用" if operation == "ENABLE" else "暂停",
                    "scheduled_at_ms": int(event_time.timestamp() * 1000),
                    "scheduled_at": event_time.isoformat(timespec="minutes"),
                    "timezone": BUSINESS_TIMEZONE_LABEL,
                    "platform_write_enabled": False,
                })
    events.sort(key=lambda event: (event["scheduled_at_ms"], event["operation"]))
    return events[:MAX_TIME_RANGES * 4]


class ChengfangScheduleControl:
    """Own schedule revisions for one opaque Chengfang scope."""

    def __init__(self, data_dir: str | os.PathLike[str], scope_fingerprint: Any, plan_key: Any) -> None:
        self.data_dir = Path(data_dir)
        self.scope_fingerprint = str(scope_fingerprint or "").strip()
        self.plan_key = str(plan_key or "").strip()
        scope_key = self.scope_fingerprint or "unscoped"
        self.path = self.data_dir / "chengfang_schedule_control" / f"{scope_key}.json"

    def _default_state(self) -> dict[str, Any]:
        now_ms = _now_ms()
        return {
            "schema_version": SCHEMA_VERSION,
            "scope_fingerprint": self.scope_fingerprint,
            "revisions": [],
            "active_revision_id": "",
            "created_at": now_ms,
            "updated_at": now_ms,
        }

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default_state()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = self._default_state()
            state["storage_warning"] = "SCHEDULE_STATE_UNREADABLE"
            return state
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            state = self._default_state()
            state["storage_warning"] = "SCHEDULE_STATE_SCHEMA_UNSUPPORTED"
            return state
        value["scope_fingerprint"] = self.scope_fingerprint
        value["revisions"] = value.get("revisions") if isinstance(value.get("revisions"), list) else []
        value["active_revision_id"] = str(value.get("active_revision_id") or "")
        return value

    def _write_unlocked(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state["scope_fingerprint"] = self.scope_fingerprint
        state["updated_at"] = _now_ms()
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2)
        descriptor, temporary_name = tempfile.mkstemp(prefix="schedule-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    @staticmethod
    def _find(state: dict[str, Any], revision_id: Any) -> dict[str, Any] | None:
        clean_id = str(revision_id or "").strip()
        return next((item for item in state.get("revisions", []) if isinstance(item, dict) and item.get("revision_id") == clean_id), None)

    @staticmethod
    def _transition(revision: dict[str, Any], target: str, actor: str, now_ms: int) -> None:
        revision["state"] = target
        revision["state_label"] = {
            "approved": "已批准，等待模拟",
            "awaiting_readback": "等待模拟回读",
            "verified": "本机时段演练已启用",
            "rejected": "人工已拒绝",
            "readback_failed": "模拟回读失败",
        }.get(target, target)
        revision["updated_at"] = now_ms
        history = revision.get("lifecycle") if isinstance(revision.get("lifecycle"), list) else []
        revision["lifecycle"] = [*history, {"state": target, "at": now_ms, "actor": actor}][-24:]

    def load(self) -> dict[str, Any]:
        with _LOCK:
            return self._load_unlocked()

    def create_draft(self, value: Any) -> dict[str, Any]:
        now_ms = _now_ms()
        with _LOCK:
            state = self._load_unlocked()
            active = self._find(state, state.get("active_revision_id"))
            revision_number = max((int(item.get("revision") or 0) for item in state.get("revisions", []) if isinstance(item, dict)), default=0) + 1
            draft = build_schedule_draft(
                value,
                self.scope_fingerprint,
                self.plan_key,
                revision=revision_number,
                now_ms=now_ms,
                previous=active,
            )
            existing = next((
                item for item in state.get("revisions", [])
                if isinstance(item, dict)
                and draft.get("idempotency_key")
                and item.get("idempotency_key") == draft["idempotency_key"]
                and item.get("state") not in {"rejected", "readback_failed"}
            ), None)
            if existing:
                return {"revision": existing, "deduplicated": True}
            state["revisions"] = [*state.get("revisions", []), draft][-MAX_REVISIONS:]
            self._write_unlocked(state)
            return {"revision": draft, "deduplicated": False}

    def review(self, revision_id: Any, decision: Any, *, confirm: bool = False) -> dict[str, Any]:
        clean_decision = str(decision or "").strip().lower()
        if clean_decision not in {"accept", "reject"}:
            raise ValueError("decision 必须为 accept 或 reject")
        if clean_decision == "accept" and confirm is not True:
            raise ValueError("批准定时启停草稿前必须显式确认")
        now_ms = _now_ms()
        with _LOCK:
            state = self._load_unlocked()
            revision = self._find(state, revision_id)
            if revision is None:
                raise ValueError("未找到定时启停草稿")
            target = "approved" if clean_decision == "accept" else "rejected"
            if revision.get("state") == target:
                return revision
            if revision.get("state") != "review_required":
                raise ValueError("当前草稿不在待复核状态")
            self._transition(revision, target, "human_review", now_ms)
            self._write_unlocked(state)
            return revision

    def simulate(self, revision_id: Any, *, now_ms: int | None = None) -> dict[str, Any]:
        captured_at = int(now_ms or _now_ms())
        with _LOCK:
            state = self._load_unlocked()
            revision = self._find(state, revision_id)
            if revision is None:
                raise ValueError("未找到定时启停草稿")
            if revision.get("state") in {"awaiting_readback", "verified"} and isinstance(revision.get("simulation_receipt"), dict):
                return {"revision": revision, "deduplicated": True}
            if revision.get("state") != "approved":
                raise ValueError("定时启停草稿必须先通过人工复核")
            if revision.get("platform_write_enabled") is not False or revision.get("production_allowed") is not False:
                raise ValueError("检测到不安全的生产写标记")
            events = build_schedule_transition_preview(revision, now_ms=captured_at, hours=48)
            receipt_core = {
                "revision_id": revision["revision_id"],
                "config_fingerprint": revision["config_fingerprint"],
                "captured_at": captured_at,
                "events": events,
                "execution_kind": "simulation",
            }
            revision["simulation_receipt"] = {
                **receipt_core,
                "receipt_id": f"cf-schedule-sim-{_stable_hash(receipt_core)[:20]}",
                "event_fingerprint": _stable_hash(events),
                "platform_write_attempted": False,
            }
            self._transition(revision, "awaiting_readback", "local_schedule_simulator", captured_at)
            self._write_unlocked(state)
            return {"revision": revision, "deduplicated": False}

    def readback(self, revision_id: Any) -> dict[str, Any]:
        now_ms = _now_ms()
        with _LOCK:
            state = self._load_unlocked()
            revision = self._find(state, revision_id)
            if revision is None:
                raise ValueError("未找到定时启停草稿")
            if revision.get("state") in {"verified", "readback_failed"} and isinstance(revision.get("readback"), dict):
                return {"revision": revision, "deduplicated": True}
            if revision.get("state") != "awaiting_readback":
                raise ValueError("定时启停草稿尚未进入回读阶段")
            receipt = revision.get("simulation_receipt") if isinstance(revision.get("simulation_receipt"), dict) else {}
            events = receipt.get("events") if isinstance(receipt.get("events"), list) else []
            matched = (
                receipt.get("execution_kind") == "simulation"
                and receipt.get("platform_write_attempted") is False
                and str(receipt.get("config_fingerprint") or "") == str(revision.get("config_fingerprint") or "")
                and str(receipt.get("event_fingerprint") or "") == _stable_hash(events)
            )
            revision["readback"] = {
                "source": "simulation",
                "matched": matched,
                "event_count": len(events),
                "event_fingerprint": _stable_hash(events),
                "platform_write_observed": False,
                "captured_at": now_ms,
            }
            self._transition(revision, "verified" if matched else "readback_failed", "simulation_readback", now_ms)
            if matched:
                state["active_revision_id"] = revision["revision_id"]
                revision["activated_at"] = now_ms
            else:
                revision["failure_reason"] = "SIMULATION_READBACK_MISMATCH"
            self._write_unlocked(state)
            return {"revision": revision, "deduplicated": False}

    def summary(self, *, now_ms: int | None = None) -> dict[str, Any]:
        state = self.load()
        revisions = [item for item in state.get("revisions", []) if isinstance(item, dict)]
        active = self._find(state, state.get("active_revision_id"))
        latest = revisions[-1] if revisions else None
        next_events = build_schedule_transition_preview(active, now_ms=now_ms, hours=48) if active else []
        return {
            "schema_version": SCHEMA_VERSION,
            "scope_bound": bool(self.scope_fingerprint and self.plan_key),
            "storage": "local_scope_partitioned",
            "timezone": BUSINESS_TIMEZONE_LABEL,
            "maximum_time_ranges": MAX_TIME_RANGES,
            "cross_day_supported": False,
            "platform_write_enabled": False,
            "automatic_platform_submit": False,
            "active_revision_id": str(state.get("active_revision_id") or ""),
            "active": active,
            "latest": latest,
            "revisions": revisions[-10:][::-1],
            "revision_count": len(revisions),
            "next_events": next_events[:8],
            "next_event": next_events[0] if next_events else None,
            "local_monitor_enabled": bool(active and active.get("enabled") is True),
            "production_scheduler_enabled": False,
            "notice": "定时启停当前只在本机生成时段演练与状态候选，不会按时间自动提交千川。",
            "storage_warning": state.get("storage_warning"),
        }


__all__ = [
    "BUSINESS_TIMEZONE_LABEL",
    "ChengfangScheduleControl",
    "MAX_TIME_RANGES",
    "build_schedule_draft",
    "build_schedule_transition_preview",
    "validate_schedule_time_ranges",
]
