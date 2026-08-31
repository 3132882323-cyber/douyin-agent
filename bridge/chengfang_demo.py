"""Pure synthetic fixture for the one-click Chengfang A2 walkthrough.

This module deliberately has no filesystem, network, or runtime dependencies.
The returned object only mirrors the read model consumed by the extension so a
new user can see the complete candidate -> simulation -> readback lifecycle.
It must never be treated as platform evidence or persisted into an account
runtime.
"""

from __future__ import annotations

from typing import Any


DEMO_FIXTURE_ID = "demo_fixture:chengfang_a2_one_minute:v1"
DEMO_SCOPE_ID = "demo_fixture:synthetic_scope:not_a_real_account"


def _demo_markers() -> dict[str, Any]:
    return {
        "synthetic": True,
        "demo_fixture": True,
        "platform_write_attempted": False,
    }


def build_a2_demo_fixture(*, confirm: bool, generated_at_ms: int) -> dict[str, Any]:
    """Return a deterministic, non-persistent, fully closed A2 demo lifecycle."""

    if confirm is not True:
        raise ValueError("confirm 必须为 true 才能运行独立 A2 演示")
    if isinstance(generated_at_ms, bool) or not isinstance(generated_at_ms, int) or generated_at_ms <= 0:
        raise ValueError("generated_at_ms 必须为正整数")

    candidate_id = f"{DEMO_FIXTURE_ID}:candidate"
    execution_id = f"{DEMO_FIXTURE_ID}:execution"
    receipt_id = f"{DEMO_FIXTURE_ID}:receipt"
    current_budget = 1000.0
    target_budget = 900.0
    markers = _demo_markers()

    candidate = {
        **markers,
        "candidate_id": candidate_id,
        "plan_key": "demo_fixture:synthetic_plan:not_a_platform_plan",
        "state": "verified",
        "action": "reduce_total_budget",
        "action_type": "reduce_total_budget",
        "current_value": current_budget,
        "target_value": target_budget,
        "adjustment_pct": -10.0,
        "change_pct": -10.0,
        "generated_at_ms": generated_at_ms,
        "evidence_source": "demo_fixture",
        "notice": "合成候选，仅用于产品演示，不属于任何真实店铺或计划。",
    }
    receipt = {
        **markers,
        "ok": True,
        "receipt_id": receipt_id,
        "execution_kind": "simulation",
        "adapter_id": "demo_fixture:synthetic_adapter",
        "simulated_value": target_budget,
        "notice": "合成回执，未调用、填写或提交任何平台。",
    }
    readback = {
        **markers,
        "source": "demo_fixture",
        "execution_kind": "simulation",
        "observed_value": target_budget,
        "expected_value": target_budget,
        "matched": True,
        "platform_write_observed": False,
        "notice": "合成回读，仅验证演示链路展示，不是平台回读。",
    }
    execution = {
        **markers,
        "execution_id": execution_id,
        "candidate_id": candidate_id,
        "execution_kind": "simulation",
        "adapter_id": "demo_fixture:synthetic_adapter",
        "state": "verified",
        "current_value": current_budget,
        "target_value": target_budget,
        "started_at_ms": generated_at_ms,
        "completed_at_ms": generated_at_ms,
        "adapter_receipt": receipt,
        "readback": readback,
        "notice": "合成执行记录，不代表实际投放。",
    }
    a2_pilot = {
        **markers,
        "level": "A2_simulation",
        "environment": "demo",
        "execution_kind": "simulation",
        "master_enabled": False,
        "stop_active": True,
        "stop_reason": "DEMO_FIXTURE_COMPLETE",
        "platform_write_enabled": False,
        "live_execution_available": False,
        "runtime_persisted": False,
        "plan_key": candidate["plan_key"],
        "plan_whitelist": [candidate["plan_key"]],
        "budget_cap": current_budget,
        "evidence_gate": {
            **markers,
            "fresh": True,
            "source": "demo_fixture",
            "production_ready": False,
        },
        "candidates": [candidate],
        "executions": [execution],
        "notice": "独立合成 A2 状态；不会合并、覆盖或写入真实账户 runtime。",
    }
    runtime = {
        **markers,
        "fixture_id": DEMO_FIXTURE_ID,
        "environment": "demo",
        "runtime_persisted": False,
        "real_account_bound": False,
        "scope_fingerprint": DEMO_SCOPE_ID,
        "policy": {
            **markers,
            "daily_budget_cap": 1000.0,
            "max_daily_loss": 100.0,
            "max_single_adjustment_pct": 10.0,
            "max_daily_adjustment_pct": 20.0,
            "max_daily_actions": 1,
            "cooldown_minutes": 30,
            "authorization_ttl_seconds": 60,
        },
        "a2_pilot": a2_pilot,
        "notice": "SYNTHETIC / DEMO_FIXTURE / platform_write_attempted=false",
    }
    return {
        **markers,
        "fixture_id": DEMO_FIXTURE_ID,
        "environment": "demo",
        "execution_kind": "simulation",
        "generated_at_ms": generated_at_ms,
        "runtime_persisted": False,
        "real_account_bound": False,
        "platform_write_observed": False,
        "notice": "这是一分钟独立演示，不是实际投放；未读取、绑定或修改真实账户。",
        "timeline": [
            {**markers, "step": "candidate", "state": "complete", "label": "合成候选已生成"},
            {**markers, "step": "review", "state": "complete", "label": "演示审核已通过"},
            {**markers, "step": "simulation", "state": "complete", "label": "本机合成模拟已完成"},
            {**markers, "step": "readback", "state": "complete", "label": "合成回读已匹配"},
        ],
        "runtime": runtime,
    }
