from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys


BRIDGE_DIR = str(Path(__file__).resolve().parent)
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

from chengfang_control_tasks import (  # noqa: E402
    ChengfangControlTaskCenter,
    build_control_task_draft,
    canonical_control_status,
)


class ChengfangControlTaskDraftTests(unittest.TestCase):
    def test_known_statuses_are_normalized_and_unknown_status_fails_closed(self) -> None:
        self.assertEqual("ACTIVE", canonical_control_status("调控中"))
        self.assertEqual("PAUSED", canonical_control_status("DISABLE"))
        self.assertEqual("OFFLINE_BUDGET", canonical_control_status("预算已花完"))
        self.assertEqual("UNKNOWN", canonical_control_status("新的未知状态"))

        draft = build_control_task_draft({
            "family": "status",
            "operation": "PAUSE",
            "current_value": "新的未知状态",
            "target_value": "暂停",
            "plan_key": "raw-plan-123",
        }, "scope-1", now_ms=1_700_000_000_000)
        self.assertEqual("blocked", draft["state"])
        self.assertIn("CURRENT_STATUS_UNVERIFIED", draft["simulation_blockers"])
        self.assertFalse(draft["production_allowed"])

    def test_budget_decrease_is_one_variable_and_never_production_enabled(self) -> None:
        draft = build_control_task_draft({
            "family": "budget",
            "operation": "DECREASE",
            "current_value": 1000,
            "target_value": 900,
            "plan_key": "raw-plan-123",
            "candidate_id": "candidate-opaque",
            "reasons": ["ROI_BELOW_BREAK_EVEN"],
        }, "scope-1", now_ms=1_700_000_000_000)
        self.assertEqual("review_required", draft["state"])
        self.assertEqual({"budget": 900.0}, draft["change"])
        self.assertTrue(draft["single_variable_only"])
        self.assertFalse(draft["platform_write_enabled"])
        self.assertFalse(draft["platform_write_attempted"])
        self.assertNotEqual("raw-plan-123", draft["plan_scope_fingerprint"])
        self.assertNotIn("raw-plan-123", json.dumps(draft, ensure_ascii=False))

    def test_budget_increase_and_duration_remain_production_blocked(self) -> None:
        increase = build_control_task_draft({
            "family": "budget", "operation": "INCREASE",
            "current_value": 100, "target_value": 120, "plan_key": "plan-1",
        }, "scope-1", now_ms=1)
        duration = build_control_task_draft({
            "family": "duration", "operation": "EXTEND",
            "current_value": 60, "target_value": 90, "plan_key": "plan-1",
        }, "scope-1", now_ms=1)
        self.assertTrue(increase["simulation_allowed"])
        self.assertIn("FIRST_RELEASE_DECREASE_ONLY", increase["production_blockers"])
        self.assertTrue(duration["simulation_allowed"])
        self.assertIn("DURATION_WRITE_CONTRACT_UNVERIFIED", duration["production_blockers"])


class ChengfangControlTaskCenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.center = ChengfangControlTaskCenter(self.temp.name, "scope-opaque")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def budget_payload() -> dict:
        return {
            "family": "budget",
            "operation": "DECREASE",
            "current_value": 1000,
            "target_value": 900,
            "plan_key": "plan-secret-123",
            "candidate_id": "candidate-1",
            "evidence": {"source": "a1_shadow", "captured_at_ms": 1_700_000_000_000},
        }

    def test_full_local_lifecycle_is_idempotent_and_readback_verified(self) -> None:
        created = self.center.create_draft(self.budget_payload())
        duplicate = self.center.create_draft(self.budget_payload())
        self.assertFalse(created["deduplicated"])
        self.assertTrue(duplicate["deduplicated"])
        task_id = created["task"]["task_id"]

        with self.assertRaisesRegex(ValueError, "显式确认"):
            self.center.review(task_id, "accept", confirm=False)
        approved = self.center.review(task_id, "accept", confirm=True)
        self.assertEqual("approved", approved["state"])

        simulated = self.center.simulate(task_id)
        self.assertEqual("awaiting_readback", simulated["task"]["state"])
        self.assertFalse(simulated["task"]["simulation_receipt"]["platform_write_attempted"])
        duplicate_simulation = self.center.simulate(task_id)
        self.assertTrue(duplicate_simulation["deduplicated"])

        readback = self.center.readback(task_id)
        self.assertEqual("verified", readback["task"]["state"])
        self.assertTrue(readback["task"]["readback"]["matched"])
        self.assertFalse(readback["task"]["readback"]["platform_write_observed"])
        summary = self.center.summary(importable_candidates=[{"candidate_id": "candidate-2"}])
        self.assertEqual(1, summary["verified_count"])
        self.assertFalse(summary["platform_write_enabled"])
        self.assertEqual(1, summary["importable_candidate_count"])
        self.assertGreaterEqual(len(summary["activity"]), 4)

    def test_readback_mismatch_fails_closed(self) -> None:
        task_id = self.center.create_draft(self.budget_payload())["task"]["task_id"]
        self.center.review(task_id, "accept", confirm=True)
        self.center.simulate(task_id)
        task = self.center.load()["tasks"][0]
        result = self.center.readback(task_id, {
            "source": "simulation_override",
            "observed_value": 950,
            "platform_write_observed": False,
            "task_id": task_id,
            "idempotency_key": task["idempotency_key"],
            "receipt_id": task["simulation_receipt"]["receipt_id"],
        })
        self.assertEqual("readback_failed", result["task"]["state"])
        self.assertEqual("SIMULATION_READBACK_MISMATCH", result["task"]["failure_reason"])

    def test_state_file_from_another_scope_fails_closed(self) -> None:
        self.center.create_draft(self.budget_payload())
        state = json.loads(self.center.path.read_text(encoding="utf-8"))
        state["scope_fingerprint"] = "scope-other"
        self.center.path.write_text(json.dumps(state), encoding="utf-8")
        loaded = self.center.load()
        self.assertEqual("CONTROL_TASK_STATE_SCOPE_MISMATCH", loaded["storage_warning"])
        self.assertEqual([], loaded["tasks"])

    def test_task_tampering_and_cross_task_readback_are_rejected(self) -> None:
        created = self.center.create_draft(self.budget_payload())
        task_id = created["task"]["task_id"]
        state = self.center.load()
        state["tasks"][0]["target_value"] = 100
        self.center._write_unlocked(state)
        with self.assertRaisesRegex(ValueError, "CONTROL_TASK_INTEGRITY_INVALID"):
            self.center.review(task_id, "accept", confirm=True)

        center = ChengfangControlTaskCenter(self.temp.name, "scope-clean")
        clean = center.create_draft(self.budget_payload())["task"]
        center.review(clean["task_id"], "accept", confirm=True)
        center.simulate(clean["task_id"])
        with self.assertRaisesRegex(ValueError, "SIMULATION_READBACK_BINDING_MISMATCH"):
            center.readback(clean["task_id"], {
                "source": "simulation_override",
                "platform_write_observed": False,
                "observed_value": clean["target_value"],
                "task_id": "cf-control-other",
            })

    def test_storage_never_contains_raw_plan_identifier(self) -> None:
        self.center.create_draft(self.budget_payload())
        stored = self.center.path.read_text(encoding="utf-8")
        self.assertNotIn("plan-secret-123", stored)
        self.assertIn("plan_scope_fingerprint", stored)


if __name__ == "__main__":
    unittest.main()
