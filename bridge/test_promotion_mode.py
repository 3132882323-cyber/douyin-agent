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

    def test_suixintui_aliases_are_read_only_and_never_reuse_other_executors(self):
        aliases = ("suixintui", "sui-xin-tui", "随心推", "随心推推广", "小店随心推", "千川随心推", "巨量千川随心推")
        for alias in aliases:
            with self.subTest(alias=alias):
                self.assertEqual("suixintui", build_promotion_context(alias)["promotion_mode"])

        complete_context = {
            "promotion_mode": "随心推",
            "account_scope": {"store_id": "shop-1", "account_id": "ad-1", "binding_status": "verified"},
            "strategy_id": "strategy-1",
            "metric_contract": {"definition": "pay_roi", "version": "v1"},
        }
        for operation in ("adjust_budget", "pause_plan", "restore_budget"):
            with self.subTest(operation=operation):
                guard = legacy_execution_guard(operation, complete_context)
                self.assertFalse(guard["allowed"])
                self.assertEqual("UNSUPPORTED_FOR_SUIXINTUI", guard["code"])
                self.assertIn("独立的执行、回读和回滚合同", guard["reason"])
                self.assertIn("禁止复用标准、全域或乘方执行器", guard["reason"])

    def test_suixintui_can_pass_read_only_data_gate_when_contract_is_complete(self):
        report = assess_deterministic_data_gate({
            "promotion_mode": "小店随心推",
            "account_scope": {"store_id": "shop-1", "account_id": "ad-1", "binding_status": "verified"},
            "strategy_id": "strategy-1",
            "metric_contract": {"definition": "pay_roi", "version": "v1"},
            "data_quality": {"confidence": "high", "freshness_seconds": 30, "completeness": 0.9},
        })
        self.assertTrue(report["read_only"])
        self.assertTrue(report["deterministic_advice_allowed"])

    def test_standard_and_full_domain_keep_legacy_path_available(self):
        for mode in ("standard", "full_domain"):
            context = {
                "promotion_mode": mode,
                "account_scope": {"store_id": "shop-1", "account_id": "ad-1"},
                "strategy_id": "strategy-1",
                "metric_contract": {"definition": "pay_roi", "version": "v1"},
                "data_quality": {"confidence": "high", "freshness_seconds": 30, "completeness": 0.9},
            }
            self.assertTrue(legacy_execution_guard("adjust_budget", context)["allowed"])

    def test_legacy_guard_binds_expected_account_to_context(self):
        context = {
            "promotion_mode": "standard",
            "account_scope": {"store_id": "shop-1", "account_id": "Ad-1"},
            "strategy_id": "strategy-1",
            "metric_contract": {"definition": "pay_roi", "version": "v1"},
            "data_quality": {"confidence": "high", "freshness_seconds": 30, "completeness": 0.9},
        }
        self.assertTrue(legacy_execution_guard(
            "adjust_budget", context, expected_account_key=" ad-1 "
        )["allowed"])
        blocked = legacy_execution_guard(
            "adjust_budget", context, expected_account_key="ad-2"
        )
        self.assertFalse(blocked["allowed"])
        self.assertEqual("ACTION_CONTEXT_ACCOUNT_MISMATCH", blocked["code"])

    def test_legacy_writes_fail_closed_on_metric_conflict_or_stale_data(self):
        base = {
            "promotion_mode": "standard",
            "account_scope": {"store_id": "shop-1", "account_id": "ad-1"},
            "strategy_id": "strategy-1",
            "metric_contract": {"definition": "pay_roi", "version": "v1"},
            "data_quality": {"confidence": "high", "freshness_seconds": 30, "completeness": 0.9},
        }
        conflicted = legacy_execution_guard("adjust_budget", {
            **base,
            "data_quality": {**base["data_quality"], "metric_conflict": True},
        })
        self.assertFalse(conflicted["allowed"])
        self.assertEqual("DATA_CONTRACT_CONFLICT", conflicted["code"])
        stale = legacy_execution_guard("pause_plan", {
            **base,
            "data_quality": {**base["data_quality"], "freshness_seconds": 3600},
        })
        self.assertFalse(stale["allowed"])
        self.assertEqual("DATA_STALE_OR_UNTIMED", stale["code"])

    def test_raw_mode_conflict_survives_normalization_and_blocks_writes(self):
        context = {
            "promotion_mode": "standard",
            "account_scope": {"store_id": "shop-1", "account_id": "ad-1"},
            "strategy_id": "strategy-1",
            "metric_contract": {"definition": "pay_roi", "version": "v1"},
            "data_quality": {
                "confidence": "high",
                "freshness_seconds": 0,
                "completeness": 0.9,
                "mode_conflict": True,
            },
        }
        normalized = build_promotion_context(context)
        self.assertEqual("unknown", normalized["promotion_mode"])
        self.assertTrue(normalized["data_quality"]["mode_conflict"])
        guard = legacy_execution_guard("adjust_budget", context)
        self.assertFalse(guard["allowed"])
        self.assertIn(guard["code"], {"PROMOTION_MODE_UNVERIFIED", "DATA_CONTRACT_CONFLICT"})

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

    def test_zero_second_freshness_is_valid_only_when_explicit(self):
        base = {
            "promotion_mode": "standard",
            "account_scope": {"store_id": "shop-1", "account_id": "ad-1"},
            "strategy_id": "strategy-1",
            "metric_contract": {"definition": "pay_roi", "version": "v1"},
        }
        self.assertTrue(assess_deterministic_data_gate({
            **base,
            "data_quality": {"confidence": "high", "freshness_seconds": 0, "completeness": 0.9},
        })["deterministic_advice_allowed"])
        self.assertIn("DATA_STALE_OR_UNTIMED", assess_deterministic_data_gate({
            **base,
            "data_quality": {"confidence": "high", "completeness": 0.9},
        })["blocked_reasons"])

    def test_nonfinite_context_values_fail_closed_instead_of_looking_complete(self):
        invalid = {
            "promotion_mode": "standard",
            "account_scope": {"store_id": "shop-1", "account_id": "ad-1"},
            "promotion_mode_evidence": {"source": "official_api", "captured_at_ms": float("nan")},
            "strategy_id": "strategy-1",
            "strategy": {"strategy_id": "strategy-1", "total_budget": float("inf")},
            "metric_contract": {"definition": "net_revenue_roi", "version": "v1", "value": float("nan")},
            "cost_ledger": {key: float("nan") for key in (
                "ad_spend", "commission", "platform_fee", "discount",
                "refund", "product_cost", "fulfillment_cost",
            )},
            "result_ledger": {"net_revenue": float("inf")},
            "data_quality": {
                "confidence": "high",
                "freshness_seconds": float("nan"),
                "completeness": float("nan"),
            },
        }

        context = build_promotion_context(invalid)
        gate = assess_deterministic_data_gate(invalid)
        dashboard = build_chengfang_dashboard_summary(invalid)

        self.assertFalse(context["data_ready"])
        self.assertEqual({}, context["cost_ledger"])
        self.assertEqual({}, context["result_ledger"])
        self.assertIsNone(context["metric_contract"]["value"])
        self.assertIsNone(context["strategy"]["total_budget"])
        self.assertEqual(0, context["promotion_mode_evidence"]["captured_at_ms"])
        self.assertFalse(context["data_quality"]["freshness_provided"])
        self.assertEqual(0.0, context["data_quality"]["completeness"])
        self.assertFalse(gate["deterministic_advice_allowed"])
        self.assertIn("DATA_STALE_OR_UNTIMED", gate["blocked_reasons"])
        self.assertIn("DATA_COMPLETENESS_LOW", gate["blocked_reasons"])
        self.assertFalse(dashboard["profit_safety"]["calculable"])


if __name__ == "__main__":
    unittest.main()
