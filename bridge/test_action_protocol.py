import unittest

from action_protocol import assess_automation_readiness, build_action_draft, transition_action, validate_action_draft


class ActionProtocolTests(unittest.TestCase):
    def _draft(self, **overrides):
        values = {
            "operation_type": "adjust_budget",
            "operation_label": "降低预算 20%",
            "target_kind": "qianchuan_plan",
            "target_id": "plan-123",
            "target_name": "测试计划",
            "account_key": "account-1",
            "account_label": "主账户",
            "field": "预算",
            "current_value": 500.0,
            "target_value": 400.0,
            "source": "qianchuan",
            "page_type": "campaigns",
            "captured_at_ms": 1_000_000,
            "quality_score": 90,
            "confidence": "high",
            "evidence": {"spend": 200, "roi": 0.8},
            "copy_text": "测试计划 | 预算 500 → 400",
            "now_ms": 1_001_000,
        }
        values.update(overrides)
        return build_action_draft(**values)

    def test_valid_budget_reduction_can_be_confirmed_but_not_executed(self):
        draft = self._draft()
        self.assertTrue(draft["can_confirm"])
        self.assertFalse(draft["can_execute"])
        self.assertFalse(draft["policy"]["execution_enabled"])
        self.assertEqual([], validate_action_draft(draft, now_ms=1_001_000))

    def test_missing_budget_never_becomes_pause(self):
        draft = self._draft(current_value=None, target_value=None)
        self.assertEqual("adjust_budget", draft["operation_type"])
        self.assertFalse(draft["can_confirm"])
        self.assertIn("CURRENT_VALUE_MISSING", {item["code"] for item in draft["blocked_reasons"]})

    def test_missing_account_or_plan_id_blocks_confirmation(self):
        draft = self._draft(account_key="", target_id="")
        codes = {item["code"] for item in draft["blocked_reasons"]}
        self.assertIn("ACCOUNT_NOT_LOCKED", codes)
        self.assertIn("TARGET_ID_MISSING", codes)
        self.assertFalse(draft["can_confirm"])

    def test_stale_or_low_quality_data_blocks_confirmation(self):
        draft = self._draft(captured_at_ms=1, now_ms=700_000, quality_score=60)
        codes = {item["code"] for item in draft["blocked_reasons"]}
        self.assertIn("DATA_STALE", codes)
        self.assertIn("DATA_QUALITY_LOW", codes)

    def test_truncated_snapshot_blocks_confirmation_and_asks_for_rescan(self):
        draft = self._draft(pagination_truncated=True)
        codes = {item["code"] for item in draft["blocked_reasons"]}
        self.assertIn("SNAPSHOT_TRUNCATED", codes)
        self.assertFalse(draft["can_confirm"])
        readiness = assess_automation_readiness(draft)
        self.assertEqual("blocked", readiness["status"])
        self.assertIn("补采", readiness["next_step"])

    def test_change_limits_are_enforced(self):
        increase = self._draft(target_value=600)
        decrease = self._draft(target_value=300)
        self.assertIn("INCREASE_LIMIT_EXCEEDED", {item["code"] for item in increase["blocked_reasons"]})
        self.assertIn("DECREASE_LIMIT_EXCEEDED", {item["code"] for item in decrease["blocked_reasons"]})

    def test_integrity_change_is_detected(self):
        draft = self._draft()
        draft["change"]["target_value"] = 300
        self.assertIn("INTEGRITY_CHECK_FAILED", {item["code"] for item in validate_action_draft(draft, now_ms=1_001_000)})

    def test_safety_policy_tampering_is_detected(self):
        draft = self._draft()
        draft["policy"]["execution_enabled"] = True
        draft["blocked_reasons"] = []
        self.assertIn("INTEGRITY_CHECK_FAILED", {item["code"] for item in validate_action_draft(draft, now_ms=1_001_000)})

    def test_same_snapshot_and_parameters_keep_stable_action_id(self):
        first = self._draft(now_ms=1_001_000)
        second = self._draft(now_ms=1_002_000)
        self.assertEqual(first["action_id"], second["action_id"])

    def test_action_id_tampering_is_detected(self):
        draft = self._draft()
        draft["action_id"] = "0" * 24
        self.assertIn("ACTION_ID_MISMATCH", {item["code"] for item in validate_action_draft(draft, now_ms=1_001_000)})

    def test_execution_transition_is_disabled(self):
        confirmed = transition_action(self._draft(), "confirmed")
        with self.assertRaisesRegex(ValueError, "execution is disabled"):
            transition_action(confirmed, "executing")

    def test_readiness_separates_confirmation_preflight_and_blocked(self):
        confirmable = assess_automation_readiness(self._draft())
        self.assertEqual("confirmable", confirmable["status"])
        self.assertFalse(confirmable["can_enter_preflight"])

        confirmed = transition_action(self._draft(), "confirmed")
        preflight = assess_automation_readiness(confirmed)
        self.assertEqual("preflight_ready", preflight["status"])
        self.assertTrue(preflight["can_enter_preflight"])
        self.assertFalse(preflight["execution_enabled"])

        blocked = assess_automation_readiness(self._draft(account_key="", target_id=""))
        self.assertEqual("blocked", blocked["status"])
        self.assertIn("账号", blocked["next_step"])

    def test_non_executable_recommendation_stays_manual(self):
        manual = assess_automation_readiness(self._draft(operation_type="replace_creative"))
        self.assertEqual("manual_only", manual["status"])
        self.assertFalse(manual["execution_enabled"])

    def test_restore_budget_requires_verified_source_and_allows_original_value(self):
        missing_source = self._draft(operation_type="restore_budget", current_value=400, target_value=500)
        self.assertIn("ROLLBACK_SOURCE_MISSING", {item["code"] for item in missing_source["blocked_reasons"]})
        restore = self._draft(
            operation_type="restore_budget",
            current_value=400,
            target_value=500,
            evidence={"rollback_of_action_id": "a" * 24},
        )
        self.assertTrue(restore["can_confirm"])
        self.assertEqual(25.0, restore["change"]["change_percent"])

    def test_pause_plan_requires_active_status_and_pause_target(self):
        draft = self._draft(
            operation_type="pause_plan",
            operation_label="暂停计划",
            field="投放状态",
            current_value="投放中",
            target_value="暂停",
            copy_text="测试计划 | 投放状态 投放中 → 暂停",
        )
        self.assertTrue(draft["can_confirm"])
        self.assertEqual("high", draft["policy"]["risk_level"])
        inactive = self._draft(
            operation_type="pause_plan",
            field="投放状态",
            current_value="已暂停",
            target_value="暂停",
        )
        self.assertIn("CURRENT_STATUS_UNVERIFIED", {item["code"] for item in inactive["blocked_reasons"]})
        bad_target = self._draft(
            operation_type="pause_plan",
            field="投放状态",
            current_value="投放中",
            target_value="启用",
        )
        self.assertIn("TARGET_STATUS_INVALID", {item["code"] for item in bad_target["blocked_reasons"]})


if __name__ == "__main__":
    unittest.main()
