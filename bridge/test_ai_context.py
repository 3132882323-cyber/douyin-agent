import json
import unittest

try:
    from .ai_context import build_ai_context_pack, context_integrity_hash, validate_ai_context_pack
except ImportError:
    from ai_context import build_ai_context_pack, context_integrity_hash, validate_ai_context_pack


class AIContextTests(unittest.TestCase):
    NOW_MS = 1_800_000_000_000

    def _settings(self, **updates):
        settings = {
            "store_id": "real-store-1001",
            "account_id": "real-account-2002",
            "anonymization_secret": "installation-secret-32-bytes-long",
            "constraints": {
                "allowed_actions": ["decrease_budget", "pause", "delete_plan"],
                "max_budget_decrease_percent": 999,
                "min_quality_score": 1,
                "max_data_age_seconds": 9999,
            },
        }
        settings.update(updates)
        return settings

    def _build(self, *, secret_value="do-not-send"):
        return build_ai_context_pack(
            snapshots=[
                {
                    "evidence_ref": "qianchuan/campaigns/1",
                    "source": "qianchuan",
                    "page_type": "campaigns",
                    "account_key": "real-account-2002",
                    "plan_id": "plan-1",
                    "captured_at_ms": self.NOW_MS - 1_000,
                    "quality_score": 92,
                    "completeness": 0.9,
                    "confidence": "high",
                    "page_text": secret_value,
                    "cookie": secret_value,
                    "token": secret_value,
                    "orders_detail": [{"buyer": secret_value}],
                    "metrics": {
                        "spend": 100.5,
                        "roi": 2.2,
                        "orders": 7,
                        "cookie": secret_value,
                        "order_rows": [{"buyer": secret_value}],
                    },
                }
            ],
            plan_console={
                "rows": [
                    {
                        "plan_id": "plan-2",
                        "plan_name": secret_value,
                        "account_key": "real-account-2002",
                        "captured_at_ms": self.NOW_MS - 2_000,
                        "quality_score": 88,
                        "budget": 500,
                        "spend": 120,
                        "roi": 1.8,
                        "orders": 5,
                        "ctr": 0.04,
                        "delivery_status": "投放中",
                        "stale": False,
                    }
                ]
            },
            insights={"items": [{"recommendation": secret_value}]},
            settings=self._settings(),
            now_ms=self.NOW_MS,
        )

    def test_projects_only_aggregate_allowlist_and_anonymous_identity(self):
        context = self._build()
        rendered = json.dumps(context, ensure_ascii=False)

        self.assertNotIn("do-not-send", rendered)
        self.assertNotIn("real-store-1001", rendered)
        self.assertNotIn("real-account-2002", rendered)
        self.assertTrue(context["identity"]["store_ref"].startswith("store_"))
        self.assertTrue(context["identity"]["account_ref"].startswith("account_"))
        self.assertEqual({"orders", "roi", "spend"}, set(context["evidence"][0]["metrics"]))
        self.assertEqual("active", context["evidence"][1]["metrics"]["status"])
        self.assertFalse(context["privacy_contract"]["credentials_included"])
        self.assertFalse(context["privacy_contract"]["order_rows_included"])

    def test_forbidden_source_fields_do_not_change_context_or_snapshot_hash(self):
        first = self._build(secret_value="first-secret")
        second = self._build(secret_value="second-secret")
        self.assertEqual(first["context_hash"], second["context_hash"])
        self.assertEqual(first["evidence"][0]["snapshot_hash"], second["evidence"][0]["snapshot_hash"])

    def test_policy_can_be_tightened_but_not_relaxed_past_guardrails(self):
        constraints = self._build()["constraints"]
        self.assertEqual(["decrease_budget", "hold", "pause"], constraints["allowed_actions"])
        self.assertEqual(30, constraints["max_budget_decrease_percent"])
        self.assertEqual(70, constraints["min_quality_score"])
        self.assertEqual(600, constraints["max_data_age_seconds"])
        self.assertFalse(constraints["execution_enabled"])
        self.assertTrue(constraints["human_confirmation_required"])
        self.assertTrue(constraints["preflight_reread_required"])

    def test_context_integrity_and_expiry_are_validated(self):
        context = self._build()
        self.assertEqual([], validate_ai_context_pack(context, now_ms=self.NOW_MS))
        tampered = {**context, "constraints": {**context["constraints"], "execution_enabled": True}}
        codes = {item["code"] for item in validate_ai_context_pack(tampered, now_ms=self.NOW_MS)}
        self.assertIn("CONTEXT_INTEGRITY_FAILED", codes)
        self.assertNotEqual(context_integrity_hash(tampered), context["context_hash"])

    def test_requires_installation_scoped_anonymization_secret(self):
        with self.assertRaises(ValueError):
            build_ai_context_pack(
                snapshots=[],
                plan_console={},
                insights=[],
                settings={"store_id": "s", "account_id": "a"},
                now_ms=self.NOW_MS,
            )

    def test_plan_rows_from_other_accounts_are_excluded_fail_closed(self):
        context = build_ai_context_pack(
            snapshots=[],
            plan_console={
                "rows": [
                    {
                        "account_key": "real-account-2002",
                        "plan_id": "plan-a",
                        "captured_at_ms": self.NOW_MS - 1_000,
                        "quality_score": 90,
                        "budget": 500,
                        "spend": 100,
                        "roi": 1.8,
                    },
                    {
                        "account_key": "other-account-9999",
                        "plan_id": "plan-b",
                        "captured_at_ms": self.NOW_MS - 1_000,
                        "quality_score": 99,
                        "budget": 9999,
                        "spend": 8888,
                        "roi": 9.9,
                    },
                ]
            },
            insights=[],
            settings=self._settings(),
            now_ms=self.NOW_MS,
        )

        rendered = json.dumps(context, ensure_ascii=False)
        self.assertEqual(["plan-a"], [item["plan_id"] for item in context["evidence"]])
        self.assertNotIn("plan-b", rendered)
        self.assertNotIn('"budget":9999', rendered)
        self.assertTrue(all(
            item["account_ref"] == context["identity"]["account_ref"]
            for item in context["evidence"]
        ))

    def test_stale_unrelated_plan_cannot_expire_fresh_context(self):
        settings = self._settings()
        context = build_ai_context_pack(
            snapshots=[],
            plan_console={
                "rows": [
                    {
                        "account_key": "real-account-2002",
                        "plan_id": "fresh-plan",
                        "captured_at_ms": self.NOW_MS - 1_000,
                        "quality_score": 90,
                        "budget": 500,
                        "roi": 1.8,
                    },
                    {
                        "account_key": "real-account-2002",
                        "plan_id": "historical-plan",
                        "captured_at_ms": self.NOW_MS - 700_000,
                        "quality_score": 95,
                        "budget": 200,
                        "roi": 0.3,
                    },
                ]
            },
            insights=[],
            settings=settings,
            now_ms=self.NOW_MS,
        )

        self.assertEqual(["fresh-plan"], [item["plan_id"] for item in context["evidence"]])
        self.assertEqual([], validate_ai_context_pack(context, now_ms=self.NOW_MS))

    def test_malformed_timestamps_and_nonfinite_context_fail_closed(self):
        for field, value, code in (
            ("generated_at_ms", [], "CONTEXT_GENERATED_AT_INVALID"),
            ("generated_at_ms", "1800000000000", "CONTEXT_GENERATED_AT_INVALID"),
            ("expires_at_ms", [], "CONTEXT_EXPIRES_AT_INVALID"),
            ("expires_at_ms", "1800000001000", "CONTEXT_EXPIRES_AT_INVALID"),
        ):
            with self.subTest(field=field, value=value):
                context = {**self._build(), field: value}
                codes = {item["code"] for item in validate_ai_context_pack(context, now_ms=self.NOW_MS)}
                self.assertIn(code, codes)

        context = self._build()
        malformed = {
            **context,
            "constraints": {**context["constraints"], "max_daily_budget_impact": float("nan")},
        }
        codes = {item["code"] for item in validate_ai_context_pack(malformed, now_ms=self.NOW_MS)}
        self.assertIn("CONTEXT_INTEGRITY_INVALID", codes)
        self.assertIn("CONTEXT_INTEGRITY_FAILED", codes)

        for value in ([], {}):
            with self.subTest(confidence=value):
                context = self._build()
                context["evidence"][0]["quality"]["confidence"] = value
                context["context_hash"] = context_integrity_hash(context)
                context["context_id"] = f"ai-context-{context['context_hash'][:24]}"
                codes = {item["code"] for item in validate_ai_context_pack(context, now_ms=self.NOW_MS)}
                self.assertIn("CONTEXT_QUALITY_INVALID", codes)

        context = self._build()
        context["constraints"]["allowed_actions"] = [[]]
        context["context_hash"] = context_integrity_hash(context)
        context["context_id"] = f"ai-context-{context['context_hash'][:24]}"
        codes = {item["code"] for item in validate_ai_context_pack(context, now_ms=self.NOW_MS)}
        self.assertIn("CONTEXT_CONSTRAINTS_INVALID", codes)


if __name__ == "__main__":
    unittest.main()
