import unittest

from rule_engine import HARD_SAFETY_GUARDRAILS, RuleEngine, RulePackError


class RuleEngineTests(unittest.TestCase):
    def _pack(self):
        return {
            "schema_version": 1,
            "pack_version": "2026.08.02.1",
            "rules": [
                {
                    "rule_id": "qianchuan.roi_loss",
                    "version": 3,
                    "priority": 10,
                    "conditions": {
                        "all": [
                            {"field": "spend", "operator": ">=", "value_from": "min_spend"},
                            {"field": "roi", "operator": "<", "formula": "roi_target * 0.8"},
                            {"field": "data_age_minutes", "operator": "<=", "value": 30},
                        ]
                    },
                    "result": {
                        "level": "high",
                        "title": "ROI 持续低于目标",
                        "message": "先降低预算并观察。",
                        "action": {
                            "type": "reduce_budget",
                            "change_percent": -15,
                            "observation_minutes": 30,
                        },
                    },
                }
            ],
        }

    def test_local_formula_rule_matches_without_ai(self):
        result = RuleEngine(self._pack()).evaluate(
            {"spend": 500, "roi": 1.0, "data_age_minutes": 5},
            {"min_spend": 100, "roi_target": 2.0},
        )
        self.assertEqual("deterministic_local", result["mode"])
        self.assertFalse(result["ai_required"])
        self.assertEqual(1, result["matched_count"])
        self.assertEqual(-15.0, result["diagnostics"][0]["action"]["change_percent"])
        self.assertFalse(result["diagnostics"][0]["action"]["can_execute"])

    def test_pack_cannot_override_hard_safety_policy(self):
        pack = self._pack()
        pack["safety"] = {"execution_enabled": True, "max_increase_percent": 999}
        pack["rules"][0]["result"]["policy"] = {"execution_enabled": True}
        pack["rules"][0]["result"]["action"].update(
            {"execution_enabled": True, "max_decrease_percent": 100, "can_execute": True}
        )
        result = RuleEngine(pack).evaluate(
            {"spend": 500, "roi": 1.0, "data_age_minutes": 5},
            {"min_spend": 100, "roi_target": 2.0},
        )
        diagnosis = result["diagnostics"][0]
        self.assertFalse(result["safety_policy"]["execution_enabled"])
        self.assertEqual(30.0, result["safety_policy"]["max_decrease_percent"])
        self.assertFalse(diagnosis["action"]["execution_enabled"])
        self.assertIn("can_execute", diagnosis["guardrail_overrides_ignored"])
        self.assertIn("policy", diagnosis["guardrail_overrides_ignored"])

    def test_unsafe_change_is_blocked_not_silently_clamped(self):
        pack = self._pack()
        pack["rules"][0]["result"]["action"]["change_percent"] = -80
        action = RuleEngine(pack).evaluate(
            {"spend": 500, "roi": 1.0, "data_age_minutes": 5},
            {"min_spend": 100, "roi_target": 2.0},
        )["diagnostics"][0]["action"]
        self.assertEqual(-80.0, action["change_percent"])
        self.assertFalse(action["eligible_for_action_draft"])
        self.assertIn("DECREASE_LIMIT_EXCEEDED", action["blocked_reasons"])

    def test_invalid_formula_is_rejected_before_evaluation(self):
        pack = self._pack()
        pack["rules"][0]["conditions"]["all"][1]["formula"] = "__import__('os').system('x')"
        with self.assertRaisesRegex(RulePackError, "unsupported expression"):
            RuleEngine(pack)

    def test_deterministic_order_and_dedupe(self):
        pack = self._pack()
        duplicate = dict(pack["rules"][0])
        duplicate["rule_id"] = "qianchuan.roi_loss.secondary"
        duplicate["priority"] = 20
        duplicate["result"] = dict(duplicate["result"], dedupe_key="roi-loss")
        pack["rules"][0]["result"]["dedupe_key"] = "roi-loss"
        pack["rules"].append(duplicate)
        engine = RuleEngine(pack)
        first = engine.evaluate(
            {"spend": 500, "roi": 1.0, "data_age_minutes": 5},
            {"min_spend": 100, "roi_target": 2.0},
        )
        second = engine.evaluate(
            {"spend": 500, "roi": 1.0, "data_age_minutes": 5},
            {"min_spend": 100, "roi_target": 2.0},
        )
        self.assertEqual(first, second)
        self.assertEqual(["qianchuan.roi_loss"], [item["rule_id"] for item in first["diagnostics"]])

    def test_hard_guardrails_are_read_only(self):
        with self.assertRaises(TypeError):
            HARD_SAFETY_GUARDRAILS["execution_enabled"] = True

    def test_manual_operations_never_become_action_drafts(self):
        pack = self._pack()
        pack["rules"][0]["result"]["action"] = {"type": "check_inventory"}
        action = RuleEngine(pack).evaluate(
            {"spend": 500, "roi": 1.0, "data_age_minutes": 5},
            {"min_spend": 100, "roi_target": 2.0},
        )["diagnostics"][0]["action"]
        self.assertFalse(action["eligible_for_action_draft"])
        self.assertFalse(action["can_execute"])

    def test_invalid_rule_id_rejected(self):
        pack = self._pack()
        pack["rules"][0]["rule_id"] = "bad id"
        with self.assertRaises(RulePackError):
            RuleEngine(pack)


if __name__ == "__main__":
    unittest.main()
