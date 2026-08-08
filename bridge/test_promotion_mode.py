import unittest

from promotion_mode import build_chengfang_readiness, build_promotion_context, legacy_execution_guard


class PromotionModeTests(unittest.TestCase):
    def test_unknown_is_default_and_blocks_legacy_writes(self):
        context = build_promotion_context()
        self.assertEqual("unknown", context["promotion_mode"])
        guard = legacy_execution_guard("adjust_budget", context)
        self.assertFalse(guard["allowed"])
        self.assertEqual("PROMOTION_MODE_UNVERIFIED", guard["code"])

    def test_chengfang_blocks_budget_pause_and_restore(self):
        for operation in ("adjust_budget", "pause_plan", "restore_budget"):
            guard = legacy_execution_guard(operation, {"promotion_mode": "chengfang"})
            self.assertFalse(guard["allowed"])
            self.assertEqual("UNSUPPORTED_FOR_CHENGFANG", guard["code"])

    def test_standard_and_full_domain_keep_legacy_path_available(self):
        for mode in ("standard", "full_domain"):
            self.assertTrue(legacy_execution_guard("adjust_budget", {"promotion_mode": mode})["allowed"])

    def test_metric_and_cost_contract_is_backward_compatible(self):
        context = build_promotion_context({
            "promotion_mode": "乘方",
            "strategy_id": "strategy-1",
            "metric": {"definition": "pay_roi", "value": 2.3},
            "cost_ledger": {"ad_spend": 100, "refund": 10, "unsupported": 99},
        })
        self.assertEqual("chengfang", context["promotion_mode"])
        self.assertEqual({"ad_spend": 100, "refund": 10}, context["cost_ledger"])
        self.assertTrue(context["data_ready"])

    def test_readiness_never_claims_write_support(self):
        readiness = build_chengfang_readiness({"promotion_mode": "chengfang", "strategy_id": "s-1"})
        self.assertFalse(readiness["ready_for_chengfang_write"])
        self.assertFalse(readiness["capabilities"]["chengfang_write"])
        self.assertFalse(readiness["capabilities"]["official_api_adapter"])


if __name__ == "__main__":
    unittest.main()
