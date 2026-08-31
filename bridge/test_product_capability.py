import unittest
from pathlib import Path
import sys

BRIDGE_DIR = str(Path(__file__).resolve().parent)
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)
from product_capability import build_product_capability_diagnostic


class ProductCapabilityDiagnosticTests(unittest.TestCase):
    def build(self, **overrides):
        values = {
            "onboarding": {"store_confirmed": False},
            "operation_context": {"state": "blocked", "analysis_allowed": False},
            "plan_console": {"rows": [], "summary": {}},
            "automation_readiness": {"summary": {}, "execution_enabled": False},
            "preflight": {"state": "idle", "execution_enabled": False},
            "effectiveness": {"summary": {}},
        }
        values.update(overrides)
        return build_product_capability_diagnostic(**values)

    def test_setup_required_never_claims_execution(self):
        result = self.build()
        self.assertEqual("setup_required", result["level"])
        self.assertEqual("connection-guide", result["next_action"]["target"])
        self.assertFalse(result["truth"]["execution_enabled"])

    def test_read_only_plan_is_reported_truthfully(self):
        result = self.build(
            onboarding={"store_confirmed": True},
            operation_context={"state": "review", "state_label": "可诊断，需复核", "analysis_allowed": True},
            plan_console={"rows": [{"plan_id": "1"}], "summary": {"total": 1, "binding_ready": 0, "stale": 0}, "freshness_status": "fresh", "platform_write_enabled": False},
            automation_readiness={"summary": {"manual_only": 1}, "execution_enabled": False},
        )
        self.assertEqual("plan_read_only", result["level"])
        self.assertEqual("identity", result["next_action"]["stage_id"])
        self.assertIn("真实投放调整尚未进入短时效人工授权", result["truth"]["not_available_yet"])

    def test_supervised_preflight_requires_real_readiness_evidence(self):
        result = self.build(
            onboarding={"store_confirmed": True},
            operation_context={"state": "ready", "analysis_allowed": True},
            plan_console={"summary": {"total": 3, "binding_ready": 2, "stale": 0}, "freshness_status": "fresh"},
            automation_readiness={"summary": {"preflight_ready": 1}, "execution_enabled": False},
            preflight={"state": "idle", "execution_enabled": False},
        )
        self.assertEqual("supervised_ready", result["level"])
        self.assertFalse(result["truth"]["execution_enabled"])
        self.assertEqual("readback", result["next_action"]["stage_id"])

    def test_closed_loop_requires_evaluated_result(self):
        result = self.build(
            onboarding={"store_confirmed": True},
            operation_context={"state": "ready", "analysis_allowed": True},
            plan_console={"summary": {"total": 2, "binding_ready": 2, "stale": 0}, "freshness_status": "fresh"},
            automation_readiness={"summary": {"preflight_ready": 1}, "execution_enabled": False},
            effectiveness={"summary": {"total": 1, "evaluated": 1, "effective": 1}},
        )
        self.assertEqual("closed_loop", result["level"])
        self.assertIn("执行效果回读与复盘", result["truth"]["available_now"])

    def test_stale_plan_and_blocked_preflight_never_claim_supervised_ready(self):
        result = self.build(
            onboarding={"store_confirmed": True},
            operation_context={"state": "ready", "analysis_allowed": True},
            plan_console={"rows": [{"account_key": "acct-a", "stale": True}], "summary": {"total": 1, "binding_ready": 0, "stale": 1}, "freshness_status": "stale"},
            automation_readiness={"summary": {"preflight_ready": 1}, "execution_enabled": False},
            preflight={"state": "blocked", "execution_enabled": False},
            effectiveness={"items": [{"account_key": "acct-other", "status": "effective"}], "summary": {"evaluated": 1}},
        )
        self.assertEqual("diagnosis_ready", result["level"])
        stages = {item["id"]: item for item in result["stages"]}
        self.assertEqual("blocked", stages["plans"]["status"])
        self.assertEqual("inactive", stages["identity"]["status"])
        self.assertEqual("inactive", stages["action"]["status"])
        self.assertEqual("inactive", stages["readback"]["status"])
        self.assertEqual("plans", result["next_action"]["stage_id"])
        self.assertIn("过期", result["next_action"]["reason"])
        self.assertNotIn("执行效果回读与复盘", result["truth"]["available_now"])


    def test_preflight_from_another_store_or_account_is_never_reused(self):
        common = {
            "onboarding": {"store_confirmed": True, "store_key": "store-b"},
            "operation_context": {
                "state": "ready",
                "analysis_allowed": True,
                "selected_store": {"key": "store-b"},
            },
            "plan_console": {
                "rows": [{"plan_id": "plan-b", "account_key": "account-b"}],
                "summary": {"total": 1, "binding_ready": 1, "stale": 0},
                "freshness_status": "fresh",
            },
            "automation_readiness": {"summary": {}, "execution_enabled": True},
        }
        stale = self.build(
            **common,
            preflight={
                "state": "authorized",
                "execution_enabled": True,
                "session": {"store_key": "store-a", "account_key": "account-a"},
            },
        )
        self.assertEqual("local_management", stale["level"])
        stages = {item["id"]: item for item in stale["stages"]}
        self.assertEqual("blocked", stages["action"]["status"])
        self.assertFalse(stale["truth"]["execution_enabled"])

        matching = self.build(
            **common,
            preflight={
                "state": "authorized",
                "execution_enabled": True,
                "session": {"store_key": "store-b", "account_key": "account-b"},
            },
        )
        self.assertEqual("supervised_ready", matching["level"])
        self.assertTrue(matching["truth"]["execution_enabled"])

    def test_nonfinite_counts_fail_closed_without_crashing(self):
        result = self.build(
            onboarding={"store_confirmed": True},
            operation_context={"state": "ready", "analysis_allowed": True},
            plan_console={
                "rows": [],
                "summary": {"total": float("inf"), "binding_ready": float("nan")},
                "freshness_status": "fresh",
            },
        )
        self.assertEqual("diagnosis_ready", result["level"])
        self.assertEqual("attention", {item["id"]: item for item in result["stages"]}["plans"]["status"])


if __name__ == "__main__":
    unittest.main()
