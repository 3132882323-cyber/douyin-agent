import unittest

from reconcile import official_plan_index, reconcile_plan_against_official


class ReconcileTests(unittest.TestCase):
    def test_budget_mismatch_caps_confidence(self):
        index = official_plan_index(
            {
                "data": {
                    "channel": "official_api",
                    "tables": [
                        {
                            "headers": ["计划名称", "预算", "消耗"],
                            "rows": [["夏季测新计划", "500", "300"]],
                        }
                    ],
                }
            }
        )
        report = reconcile_plan_against_official(
            plan_name="夏季测新计划",
            browser_budget=800,
            browser_spend=300,
            official_index=index,
        )
        self.assertTrue(report["matched"])
        self.assertEqual(report["confidence_cap"], "medium")
        self.assertIn("budget_mismatch", report["reasons"])

    def test_missing_official_snapshot_is_noop(self):
        report = reconcile_plan_against_official(
            plan_name="任意计划",
            browser_budget=500,
            browser_spend=100,
            official_index={},
        )
        self.assertFalse(report["available"])
        self.assertIsNone(report["confidence_cap"])

    def test_duplicate_names_are_ambiguous_unless_plan_id_matches(self):
        index = official_plan_index(
            {
                "data": {
                    "channel": "official_api",
                    "tables": [
                        {
                            "headers": ["计划ID", "计划名称", "预算", "消耗"],
                            "rows": [
                                ["plan_a", "同名计划", "500", "100"],
                                ["plan_b", "同名计划", "800", "200"],
                            ],
                        }
                    ],
                }
            }
        )
        ambiguous = reconcile_plan_against_official(
            plan_name="同名计划",
            browser_budget=500,
            browser_spend=100,
            official_index=index,
        )
        self.assertFalse(ambiguous["matched"])
        self.assertIn("ambiguous_plan_name", ambiguous["reasons"])
        self.assertEqual("medium", ambiguous["confidence_cap"])

        by_id = reconcile_plan_against_official(
            plan_name="同名计划",
            plan_id="plan_a",
            browser_budget=500,
            browser_spend=100,
            official_index=index,
        )
        self.assertTrue(by_id["matched"])
        self.assertEqual("plan_id", by_id["match_key"])
        self.assertEqual(500, by_id["official_budget"])


if __name__ == "__main__":
    unittest.main()
