from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys


BRIDGE_DIR = str(Path(__file__).resolve().parent)
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

from chengfang_schedule_control import (  # noqa: E402
    ChengfangScheduleControl,
    build_schedule_draft,
    build_schedule_transition_preview,
    validate_schedule_time_ranges,
)


CST = timezone(timedelta(hours=8))


class ScheduleValidationTests(unittest.TestCase):
    def test_valid_ranges_are_sorted_and_keep_explicit_timezone(self) -> None:
        result = validate_schedule_time_ranges([
            {"start": "18:00", "end": "22:00"},
            {"start": "09:00", "end": "12:00"},
        ])
        self.assertTrue(result["valid"])
        self.assertEqual([
            {"start": "09:00", "end": "12:00"},
            {"start": "18:00", "end": "22:00"},
        ], result["time_ranges"])
        self.assertEqual("Asia/Shanghai", result["timezone"])

    def test_overlap_cross_day_empty_and_too_many_ranges_fail_closed(self) -> None:
        overlap = validate_schedule_time_ranges([
            {"start": "09:00", "end": "12:00"},
            {"start": "11:00", "end": "13:00"},
        ])
        self.assertIn("TIME_RANGES_OVERLAP", overlap["blockers"])
        cross_day = validate_schedule_time_ranges([{"start": "22:00", "end": "02:00"}])
        self.assertIn("TIME_RANGE_1_CROSS_DAY_MUST_SPLIT", cross_day["blockers"])
        empty = validate_schedule_time_ranges([{"start": "09:00", "end": "09:00"}])
        self.assertIn("TIME_RANGE_1_EMPTY", empty["blockers"])
        too_many = validate_schedule_time_ranges([
            {"start": f"{hour:02d}:00", "end": f"{hour:02d}:30"} for hour in range(11)
        ])
        self.assertIn("TOO_MANY_TIME_RANGES", too_many["blockers"])

    def test_disabled_schedule_may_have_no_ranges(self) -> None:
        result = validate_schedule_time_ranges([], enabled=False)
        self.assertTrue(result["valid"])

    def test_preview_emits_future_enable_pause_events_without_platform_write(self) -> None:
        now = datetime(2026, 8, 18, 8, 30, tzinfo=CST)
        config = {"enabled": True, "time_ranges": [{"start": "09:00", "end": "12:00"}]}
        events = build_schedule_transition_preview(config, now_ms=int(now.timestamp() * 1000), hours=24)
        self.assertEqual(["ENABLE", "PAUSE"], [item["operation"] for item in events])
        self.assertTrue(all(item["platform_write_enabled"] is False for item in events))


class ScheduleControlLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.center = ChengfangScheduleControl(self.temp.name, "scope-opaque", "raw-plan-secret")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def payload() -> dict:
        return {
            "enabled": True,
            "time_ranges": [
                {"start": "09:00", "end": "12:00"},
                {"start": "18:00", "end": "22:00"},
            ],
        }

    def test_draft_is_versioned_private_and_requires_review(self) -> None:
        result = self.center.create_draft(self.payload())
        revision = result["revision"]
        self.assertEqual("review_required", revision["state"])
        self.assertFalse(revision["platform_write_enabled"])
        self.assertFalse(revision["production_allowed"])
        stored = self.center.path.read_text(encoding="utf-8")
        self.assertNotIn("raw-plan-secret", stored)
        self.assertIn("plan_scope_fingerprint", stored)
        duplicate = self.center.create_draft(self.payload())
        self.assertTrue(duplicate["deduplicated"])

    def test_review_simulate_readback_activates_only_local_monitor(self) -> None:
        revision_id = self.center.create_draft(self.payload())["revision"]["revision_id"]
        with self.assertRaisesRegex(ValueError, "显式确认"):
            self.center.review(revision_id, "accept", confirm=False)
        approved = self.center.review(revision_id, "accept", confirm=True)
        self.assertEqual("approved", approved["state"])
        now = datetime(2026, 8, 18, 8, 30, tzinfo=CST)
        simulated = self.center.simulate(revision_id, now_ms=int(now.timestamp() * 1000))
        self.assertEqual("awaiting_readback", simulated["revision"]["state"])
        receipt = simulated["revision"]["simulation_receipt"]
        self.assertGreater(len(receipt["events"]), 0)
        self.assertFalse(receipt["platform_write_attempted"])
        verified = self.center.readback(revision_id)
        self.assertEqual("verified", verified["revision"]["state"])
        self.assertTrue(verified["revision"]["readback"]["matched"])
        self.assertFalse(verified["revision"]["readback"]["platform_write_observed"])
        summary = self.center.summary(now_ms=int(now.timestamp() * 1000))
        self.assertTrue(summary["local_monitor_enabled"])
        self.assertFalse(summary["production_scheduler_enabled"])
        self.assertFalse(summary["platform_write_enabled"])

    def test_invalid_draft_is_blocked_and_cannot_be_reviewed(self) -> None:
        result = self.center.create_draft({
            "enabled": True,
            "time_ranges": [{"start": "23:00", "end": "02:00"}],
        })
        revision = result["revision"]
        self.assertEqual("blocked", revision["state"])
        with self.assertRaisesRegex(ValueError, "待复核"):
            self.center.review(revision["revision_id"], "accept", confirm=True)


class ScheduleDraftTests(unittest.TestCase):
    def test_draft_diff_and_idempotency_do_not_store_raw_plan(self) -> None:
        previous = {"enabled": False, "timezone": "Asia/Shanghai", "time_ranges": []}
        draft = build_schedule_draft(
            {"enabled": True, "time_ranges": [{"start": "09:00", "end": "18:00"}]},
            "scope-opaque",
            "raw-plan-secret",
            revision=2,
            now_ms=1_700_000_000_000,
            previous=previous,
        )
        self.assertEqual(2, draft["revision"])
        self.assertEqual(2, draft["changed_count"])
        self.assertTrue(draft["idempotency_key"])
        self.assertNotIn("raw-plan-secret", json.dumps(draft, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
