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


if __name__ == "__main__":
    unittest.main()
