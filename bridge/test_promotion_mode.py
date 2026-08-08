import unittest

from promotion_mode import assess_deterministic_data_gate, build_chengfang_dashboard_summary, build_chengfang_readiness, build_promotion_context, legacy_execution_guard


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
            context = {"promotion_mode": mode, "account_scope": {"store_id": "shop-1", "account_id": "ad-1"}, "strategy_id": "strategy-1", "metric_contract": {"definition": "pay_roi", "version": "v1"}}
            self.assertTrue(legacy_execution_guard("adjust_budget", context)["allowed"])

    def test_standard_mode_with_incomplete_or_conflicting_scope_is_read_only(self):
        missing = legacy_execution_guard("adjust_budget", {"promotion_mode": "standard"})
        self.assertFalse(missing["allowed"])
        self.assertEqual("PROMOTION_SCOPE_UNVERIFIED", missing["code"])
        conflict = legacy_execution_guard("pause_plan", {"promotion_mode": "standard", "account_scope": {"store_id": "shop-1", "account_id": "ad-1", "conflict": True}, "strategy_id": "s-1", "metric_contract": {"definition": "pay_roi", "version": "v1"}})
        self.assertFalse(conflict["allowed"])

    def test_metric_and_cost_contract_is_backward_compatible(self):
        context = build_promotion_context({
            "promotion_mode": "乘方",
            "strategy_id": "strategy-1",
            "account_scope": {"store_id": "store-1", "account_id": "account-1"},
            "metric": {"definition": "pay_roi", "version": "v1", "value": 2.3},
            "cost_ledger": {"ad_spend": 100, "refund": 10, "unsupported": 99},
            "result_ledger": {"pay_amount": 230, "orders": 3},
        })
        self.assertEqual("chengfang", context["promotion_mode"])
        self.assertEqual({"ad_spend": 100, "refund": 10}, context["cost_ledger"])
        self.assertTrue(context["data_ready"])

    def test_chinese_aliases_are_utf8_and_conflict_fails_closed(self):
        self.assertEqual("standard", build_promotion_context("标准推广")["promotion_mode"])
        self.assertEqual("full_domain", build_promotion_context("全域推广")["promotion_mode"])
        conflicted = build_promotion_context({
            "promotion_mode": "乘方",
            "promotion_mode_evidence": {"source": "visible_label", "label": "乘方 / 全域推广", "conflict": True},
        })
        self.assertEqual("unknown", conflicted["promotion_mode"])
        self.assertEqual("conflict", conflicted["data_quality"]["confidence"])
        self.assertFalse(legacy_execution_guard("pause_plan", conflicted)["allowed"])

    def test_v2_scope_metric_ledgers_and_quality_are_normalized(self):
        context = build_promotion_context({
            "promotion_mode": "chengfang",
            "account_scope": {"store_id": "shop-1", "account_id": "ad-1", "subject_id": "corp-1", "binding_status": "verified"},
            "promotion_mode_evidence": {"source": "visible_label", "label": "乘方", "confidence": "high", "captured_at_ms": 123},
            "strategy": {"strategy_id": "strategy-1", "goal": "保利润", "total_budget": 5000},
            "metric_contract": {"definition": "net_revenue_roi", "version": "2026-08", "numerator": "净成交", "denominator": "消耗", "refund_policy": "扣除退款"},
            "cost_ledger": {"ad_spend": 100, "product_cost": 40, "fulfillment_cost": 8},
            "result_ledger": {"net_revenue": 220, "contribution_margin": 72},
            "data_quality": {"confidence": "high", "freshness_seconds": 30, "completeness": 0.8},
        })
        self.assertEqual("shop-1", context["account_scope"]["store_id"])
        self.assertEqual("2026-08", context["metric_contract"]["version"])
        self.assertEqual(72, context["result_ledger"]["contribution_margin"])
        self.assertTrue(context["write_identity_complete"])

    def test_readiness_reports_metric_profit_gaps_and_next_step(self):
        readiness = build_chengfang_readiness({"promotion_mode": "chengfang", "strategy_id": "s-1"})
        self.assertEqual("unverified", readiness["summary"]["metric_status"])
        self.assertEqual("incomplete", readiness["summary"]["profit_status"])
        self.assertIn("product_cost", readiness["summary"]["missing_profit_fields"])
        self.assertTrue(readiness["next_step"])

    def test_readiness_never_claims_write_support(self):
        readiness = build_chengfang_readiness({"promotion_mode": "chengfang", "strategy_id": "s-1"})
        self.assertFalse(readiness["ready_for_chengfang_write"])
        self.assertFalse(readiness["capabilities"]["chengfang_write"])
        self.assertFalse(readiness["capabilities"]["official_api_adapter"])

    def test_dashboard_distinguishes_missing_from_real_zero(self):
        dashboard = build_chengfang_dashboard_summary({
            "promotion_mode": "chengfang",
            "cost_ledger": {"ad_spend": 0},
            "result_ledger": {"net_revenue": 0},
        })
        self.assertEqual("present", dashboard["metrics"]["ad_spend"]["status"])
        self.assertEqual(0, dashboard["metrics"]["ad_spend"]["value"])
        self.assertEqual("missing", dashboard["metrics"]["contribution_margin"]["status"])
        self.assertIsNone(dashboard["metrics"]["contribution_margin"]["value"])

    def test_deterministic_gate_requires_scope_fresh_complete_nonconflicting_data(self):
        allowed = assess_deterministic_data_gate({
            "promotion_mode": "chengfang",
            "account_scope": {"store_id": "shop-1", "account_id": "ad-1", "binding_status": "verified"},
            "promotion_mode_evidence": {"source": "visible_label", "label": "乘方", "confidence": "high"},
            "strategy_id": "strategy-1",
            "metric_contract": {"definition": "pay_roi", "version": "v1"},
            "data_quality": {"confidence": "high", "freshness_seconds": 30, "completeness": 0.9},
        })
        self.assertTrue(allowed["deterministic_advice_allowed"])
        blocked = assess_deterministic_data_gate({"promotion_mode": "chengfang", "account_scope": {"conflict": True}, "data_quality": {"freshness_seconds": 3600, "completeness": 0.2}})
        self.assertFalse(blocked["deterministic_advice_allowed"])
        self.assertIn("ACCOUNT_SCOPE_CONFLICT", blocked["blocked_reasons"])


if __name__ == "__main__":
    unittest.main()
