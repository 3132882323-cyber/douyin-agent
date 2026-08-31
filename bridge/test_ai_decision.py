import json
import unittest

try:
    from .ai_context import build_ai_context_pack
    from .ai_decision import DECISION_PROPOSAL_SCHEMA, DecisionProposalV1, ProposalSchemaError, validate_decision_proposal
except ImportError:
    from ai_context import build_ai_context_pack
    from ai_decision import DECISION_PROPOSAL_SCHEMA, DecisionProposalV1, ProposalSchemaError, validate_decision_proposal


class AIDecisionTests(unittest.TestCase):
    NOW_MS = 1_800_000_000_000

    def setUp(self):
        self.context = build_ai_context_pack(
            snapshots=[
                {
                    "evidence_ref": "qianchuan/campaigns/plan-1",
                    "source": "qianchuan",
                    "page_type": "campaigns",
                    "account_key": "account-1",
                    "plan_id": "plan-1",
                    "captured_at_ms": self.NOW_MS - 1_000,
                    "quality_score": 90,
                    "completeness": 0.95,
                    "confidence": "high",
                    "metrics": {"spend": 100, "budget": 500, "roi": 1.2, "orders": 2},
                }
            ],
            plan_console={},
            insights=[],
            settings={
                "store_id": "store-1",
                "account_id": "account-1",
                "anonymization_secret": "installation-secret-32-bytes-long",
                "constraints": {
                    "allowed_actions": ["decrease_budget", "pause", "restore"],
                    "max_budget_decrease_percent": 10,
                    "proposal_ttl_seconds": 180,
                },
            },
            now_ms=self.NOW_MS,
        )
        evidence = self.context["evidence"][0]
        self.proposal = {
            "schema_version": 1,
            "context_id": self.context["context_id"],
            "account_ref": self.context["identity"]["account_ref"],
            "plan_id": "plan-1",
            "action": "decrease_budget",
            "delta_percent": -10,
            "evidence_refs": [evidence["evidence_ref"]],
            "snapshot_hash": evidence["snapshot_hash"],
            "confidence": 0.9,
            "reason_codes": ["roi_below_floor"],
            "observe_minutes": 30,
            "rollback_condition": "ROI recovers above the guardrail",
            "created_at_ms": self.NOW_MS,
            "ttl_seconds": 120,
        }

    def _codes(self, candidate):
        validation = candidate["validation"]
        return {
            item["code"]
            for group in ("schema_errors", "context_errors", "blocked_reasons")
            for item in validation[group]
        }

    def test_valid_proposal_is_reviewable_but_never_executable(self):
        candidate = validate_decision_proposal(
            self.proposal,
            self.context,
            model_metadata={
                "provider": "deepseek",
                "model": "deepseek-chat",
                "prompt_version": "decision-v1",
                "request_id": "provider-secret-request-id",
                "access_token": "must-not-appear",
            },
            now_ms=self.NOW_MS,
        )
        self.assertTrue(candidate["eligible_for_human_review"])
        self.assertFalse(candidate["can_execute"])
        self.assertEqual("agent_preflight_reread_required", candidate["authoritative_resolution"]["source"])
        self.assertIsNone(candidate["authoritative_resolution"]["current_value"])
        self.assertIsNone(candidate["authoritative_resolution"]["target_value"])
        rendered = json.dumps(candidate, ensure_ascii=False)
        self.assertNotIn("provider-secret-request-id", rendered)
        self.assertNotIn("must-not-appear", rendered)

    def test_schema_rejects_ai_claimed_current_target_and_raw_account(self):
        unsafe = {
            **self.proposal,
            "current_value": 500,
            "target_value": 450,
            "account_id": "raw-account",
        }
        with self.assertRaises(ProposalSchemaError):
            DecisionProposalV1.from_mapping(unsafe)
        candidate = validate_decision_proposal(unsafe, self.context, now_ms=self.NOW_MS)
        self.assertFalse(candidate["validation"]["schema_valid"])
        self.assertIsNone(candidate["intent"])
        self.assertIn("UNKNOWN_FIELDS", self._codes(candidate))
        self.assertNotIn("raw-account", json.dumps(candidate))

    def test_target_evidence_and_snapshot_hash_must_match(self):
        wrong_plan = validate_decision_proposal(
            {**self.proposal, "plan_id": "plan-2"}, self.context, now_ms=self.NOW_MS
        )
        self.assertIn("TARGET_PLAN_NOT_EVIDENCED", self._codes(wrong_plan))
        wrong_hash = validate_decision_proposal(
            {**self.proposal, "snapshot_hash": "0" * 64}, self.context, now_ms=self.NOW_MS
        )
        self.assertIn("SNAPSHOT_HASH_MISMATCH", self._codes(wrong_hash))

    def test_confidence_ttl_and_action_policy_are_enforced(self):
        low_confidence = validate_decision_proposal(
            {**self.proposal, "confidence": 0.2}, self.context, now_ms=self.NOW_MS
        )
        self.assertIn("CONFIDENCE_BELOW_POLICY", self._codes(low_confidence))
        excessive_ttl = validate_decision_proposal(
            {**self.proposal, "ttl_seconds": 300}, self.context, now_ms=self.NOW_MS
        )
        self.assertIn("PROPOSAL_TTL_EXCEEDS_POLICY", self._codes(excessive_ttl))
        hold = validate_decision_proposal(
            {**self.proposal, "action": "hold", "delta_percent": None}, self.context, now_ms=self.NOW_MS
        )
        self.assertTrue(hold["eligible_for_human_review"])

    def test_schema_is_closed_and_matches_runtime_contract(self):
        self.assertFalse(DECISION_PROPOSAL_SCHEMA["additionalProperties"])
        self.assertEqual(set(DECISION_PROPOSAL_SCHEMA["required"]), set(self.proposal))
        parsed = DecisionProposalV1.from_mapping(self.proposal)
        self.assertEqual("plan-1", parsed.plan_id)

    def test_untrusted_array_and_object_enums_are_rejected_without_crashing(self):
        for updates in (
            {"action": []},
            {"action": {}},
            {"reason_codes": [[]]},
            {"reason_codes": [{}]},
            {"delta_percent": 10**400},
            {"confidence": 10**400},
        ):
            with self.subTest(updates=updates):
                candidate = validate_decision_proposal(
                    {**self.proposal, **updates}, self.context, now_ms=self.NOW_MS
                )
                self.assertFalse(candidate["validation"]["schema_valid"])
                self.assertFalse(candidate["eligible_for_human_review"])
                self.assertIsNone(candidate["intent"])

    def test_malformed_rehashed_context_constraints_fail_closed(self):
        from_context = dict(self.context)
        from_context["constraints"] = {
            **self.context["constraints"],
            "allowed_actions": [[]],
            "min_confidence": [],
        }
        try:
            from .ai_context import context_integrity_hash
        except ImportError:
            from ai_context import context_integrity_hash
        from_context["context_hash"] = context_integrity_hash(from_context)
        from_context["context_id"] = f"ai-context-{from_context['context_hash'][:24]}"
        candidate = validate_decision_proposal(self.proposal, from_context, now_ms=self.NOW_MS)
        self.assertFalse(candidate["validation"]["context_valid"])
        self.assertFalse(candidate["eligible_for_human_review"])
        self.assertIn("CONTEXT_CONSTRAINTS_INVALID", self._codes(candidate))


if __name__ == "__main__":
    unittest.main()
