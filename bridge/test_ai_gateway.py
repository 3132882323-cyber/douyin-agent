from __future__ import annotations

import json
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ai_gateway
from ai_context import build_ai_context_pack
from ai_gateway import AIGateway, AIContextRejectedError, sanitize_ai_context


class _FakeClient:
    def request_proposal(self, **request):
        schema = request.get("proposal_schema") or {}
        if "probe" in (schema.get("properties") or {}):
            return {
                "provider_id": "deepseek",
                "model": "deepseek-chat",
                "proposal_candidates": [
                    {
                        "source": "tool_call",
                        "tool_name": "submit_ai_proposal",
                        "call_id": "probe-call",
                        "arguments": {"probe": "ok"},
                    }
                ],
                "usage": {},
            }
        context = request["context_pack"]
        evidence = context["evidence"][0]
        return {
            "provider_id": "deepseek",
            "model": "deepseek-chat",
            "proposal_candidates": [
                {
                    "source": "tool_call",
                    "tool_name": "submit_ai_proposal",
                    "call_id": "call-1",
                    "arguments": {
                        "schema_version": 1,
                        "context_id": context["context_id"],
                        "account_ref": context["identity"]["account_ref"],
                        "plan_id": evidence["plan_id"],
                        "action": "hold",
                        "delta_percent": None,
                        "evidence_refs": [evidence["evidence_ref"]],
                        "snapshot_hash": evidence["snapshot_hash"],
                        "confidence": 0.9,
                        "reason_codes": ["risk_guardrail"],
                        "observe_minutes": 30,
                        "rollback_condition": "Re-read current plan metrics before any action.",
                        "created_at_ms": context["generated_at_ms"],
                        "ttl_seconds": 120,
                    },
                }
            ],
            "usage": {"input_tokens": 12},
            "execution_performed": False,
        }


class _FakeRegistry:
    def __init__(self):
        self.configured = None

    @staticmethod
    def catalog():
        return {"default_network_mode": "offline", "execution_supported": False}

    @staticmethod
    def health(provider_id=None):
        return {
            "provider_id": provider_id,
            "network_checked": False,
            "providers": [{"state": "ready"}],
        }

    def configure(self, provider_id, **settings):
        self.configured = (provider_id, settings)
        return {"providers": [{"provider_id": provider_id, "secrets_exposed": False}]}

    @staticmethod
    def client(_provider_id):
        return _FakeClient()


class AIGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self.registry = _FakeRegistry()
        self.gateway = AIGateway(self.data_dir, registry=self.registry)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _context(now_ms: int = 1_700_000_000_000) -> dict:
        return build_ai_context_pack(
            snapshots=[{
                "evidence_ref": "qianchuan/campaigns/plan-1",
                "source": "qianchuan",
                "page_type": "campaigns",
                "account_key": "account-1",
                "plan_id": "plan-1",
                "captured_at_ms": now_ms - 1_000,
                "quality_score": 92,
                "completeness": 0.9,
                "confidence": "high",
                "metrics": {"budget": 500, "spend": 100, "roi": 1.2, "orders": 2},
            }],
            plan_console={},
            insights=[],
            settings={
                "store_id": "store-1",
                "account_id": "account-1",
                "anonymization_secret": "installation-secret-32-bytes-long",
                "constraints": {"allowed_actions": ["hold"], "proposal_ttl_seconds": 180},
            },
            now_ms=now_ms,
        )

    def test_minimal_integration_surface_is_available(self) -> None:
        self.assertFalse(self.gateway.catalog()["execution_supported"])
        self.assertFalse(self.gateway.status("deepseek")["network_checked"])
        tested = self.gateway.test("deepseek")
        self.assertEqual("configuration_only", tested["test_mode"])
        self.assertFalse(tested["network_checked"])
        probed = self.gateway.test_provider("deepseek")
        self.assertTrue(probed["ok"])
        self.assertTrue(probed["network_checked"])
        self.assertEqual("synthetic_only", probed["probe_data_scope"])
        self.assertFalse(probed["proposal_persisted"])
        self.assertEqual(0, self.gateway.list_proposals()["count"])
        configured = self.gateway.configure(
            {"provider_id": "deepseek", "enabled": False, "model": ""}
        )
        self.assertEqual("deepseek", configured["providers"][0]["provider_id"])
        self.assertEqual("deepseek", self.registry.configured[0])
        self.assertFalse(self.registry.configured[1]["enabled"])

    def test_unsaved_configuration_probe_never_persists_credentials_or_config(self) -> None:
        def fake_ephemeral(**request):
            self.assertEqual("unsaved-provider-key", request["api_key"])
            self.assertEqual(
                {
                    "probe": "connectivity",
                    "synthetic": True,
                    "contains_business_data": False,
                },
                request["context_pack"],
            )
            return {
                "proposal_candidates": [
                    {
                        "arguments": {"probe": "ok"},
                        "tool_name": "submit_ai_proposal",
                    }
                ]
            }

        with patch.object(ai_gateway, "request_ephemeral_proposal", side_effect=fake_ephemeral):
            result = self.gateway.test_configuration(
                {
                    "provider_id": "deepseek",
                    "model": "deepseek-chat",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "unsaved-provider-key",
                    "remote_access_approved": True,
                    "timeout_seconds": 5,
                }
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["network_checked"])
        self.assertFalse(result["configuration_persisted"])
        self.assertFalse(result["credential_persisted"])
        self.assertFalse(result["proposal_persisted"])
        self.assertNotIn("unsaved-provider-key", json.dumps(result))
        self.assertFalse(self.data_dir.joinpath("ai_providers.json").exists())
        self.assertFalse(self.data_dir.joinpath("ai_provider_keys.dpapi").exists())
        self.assertEqual(0, self.gateway.list_proposals()["count"])

    def test_unsaved_probe_unexpected_error_cannot_echo_key_or_body(self) -> None:
        with patch.object(
            ai_gateway,
            "request_ephemeral_proposal",
            side_effect=RuntimeError("unsaved-provider-key response-body"),
        ):
            result = self.gateway.test_configuration(
                {
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "api_key": "unsaved-provider-key",
                    "remote_access_approved": True,
                }
            )
        encoded = json.dumps(result)
        self.assertFalse(result["ok"])
        self.assertNotIn("unsaved-provider-key", encoded)
        self.assertNotIn("response-body", encoded)
        self.assertFalse(self.data_dir.joinpath("ai_providers.json").exists())

    def test_run_shadow_returns_non_executable_envelope_and_persists_it(self) -> None:
        result = self.gateway.run_shadow(
            provider_id="deepseek",
            context_pack=self._context(1_700_000_000_000),
            instruction="给出保守建议",
            now_ms=1_700_000_000_000,
        )
        self.assertEqual("validated_ai_proposal", result["state"])
        self.assertEqual("validated", result["validation_state"])
        self.assertFalse(result["validation_errors"])
        self.assertTrue(result["shadow_mode"])
        self.assertFalse(result["can_confirm"])
        self.assertFalse(result["can_execute"])
        self.assertFalse(result["execution_performed"])
        self.assertFalse(result["requires_local_validation"])
        self.assertEqual(64, len(result["context_hash"]))
        self.assertNotIn("context_pack", result)
        listed = self.gateway.list_proposals()
        self.assertEqual(1, listed["count"])
        self.assertEqual(result["proposal_id"], listed["proposals"][0]["proposal_id"])

        restarted = AIGateway(self.data_dir, registry=self.registry)
        self.assertEqual(1, restarted.list_proposals()["count"])
        persisted = self.data_dir.joinpath("ai", "proposals.json").read_text(encoding="utf-8")
        self.assertNotIn("api_key", persisted.lower())
        persisted_value = json.loads(persisted)
        audit = persisted_value["proposals"][0]["validated_proposals"][0]["audit"]
        self.assertFalse(audit["raw_prompt_stored"])
        self.assertFalse(audit["raw_response_stored"])

    def test_submit_proposal_only_queues_untrusted_data(self) -> None:
        result = self.gateway.submit_proposal(
            {"plan_id": "plan-2", "action": "decrease_budget", "delta_percent": -10},
            provider_id="codex",
            context_hash="snapshot-hash",
            now_ms=123,
        )
        self.assertEqual("external_submission", result["proposal_candidates"][0]["source"])
        self.assertEqual("validation_pending", result["validation_state"])
        self.assertEqual("CONTEXT_PACK_REQUIRED", result["validation_errors"][0]["code"])
        self.assertFalse(result["proposal_candidates"][0]["validated"])
        self.assertFalse(result["proposal_candidates"][0]["executable"])
        self.assertFalse(result["can_execute"])
        self.assertEqual(1, self.gateway.list_proposals()["count"])

    def test_sensitive_context_is_rejected_before_provider_call(self) -> None:
        for value in (
            {"cookie": "session=secret"},
            {"nested": {"access-token": "secret"}},
            {"customer": {"phone": "13800000000"}},
        ):
            with self.subTest(value=value):
                with self.assertRaises(AIContextRejectedError):
                    self.gateway.run_shadow(
                        provider_id="deepseek", context_pack=value, instruction="test"
                    )
        self.assertEqual(0, self.gateway.list_proposals()["count"])

    def test_non_ai_context_shape_is_rejected_before_provider_call(self) -> None:
        with patch.object(self.registry, "client", wraps=self.registry.client) as client:
            with self.assertRaises(AIContextRejectedError):
                self.gateway.run_shadow(
                    provider_id="deepseek",
                    context_pack={"page_text": "not-a-contract", "roi": 1.2},
                    instruction="test",
                )
        client.assert_not_called()

    def test_valid_context_with_smuggled_fields_is_rejected_before_provider_call(self) -> None:
        for path in ("top", "evidence", "metrics"):
            context = copy.deepcopy(self._context())
            if path == "top":
                context["page_text"] = "must-not-leave"
            elif path == "evidence":
                context["evidence"][0]["shipping_address"] = "must-not-leave"
            else:
                context["evidence"][0]["metrics"]["apiKey"] = "must-not-leave"
            with self.subTest(path=path):
                with patch.object(self.registry, "client", wraps=self.registry.client) as client:
                    with self.assertRaises(AIContextRejectedError):
                        self.gateway.run_shadow(
                            provider_id="deepseek",
                            context_pack=context,
                            instruction="test",
                        )
                client.assert_not_called()

    def test_sanitizer_returns_json_copy_and_rejects_unsupported_values(self) -> None:
        source = {"metrics": [{"roi": 1.2, "orders": 3}]}
        sanitized = sanitize_ai_context(source)
        self.assertEqual(source, sanitized)
        self.assertIsNot(source, sanitized)
        with self.assertRaises(AIContextRejectedError):
            sanitize_ai_context({"value": object()})

    def test_nonfinite_internal_proposals_are_rejected_before_persistence(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(AIContextRejectedError):
                    self.gateway.submit_proposal(
                        {"plan_id": "plan-2", "action": "hold", "confidence": value},
                        provider_id="internal",
                        now_ms=123,
                    )
        self.assertEqual(0, self.gateway.list_proposals()["count"])
        self.assertFalse(self.data_dir.joinpath("ai", "proposals.json").exists())

    def test_persisted_proposal_contains_no_provider_response_or_credentials(self) -> None:
        self.gateway.run_shadow(
            provider_id="deepseek",
            context_pack=self._context(1_700_000_000_000),
            instruction="hold",
            now_ms=1_700_000_000_000,
        )
        value = json.loads(self.data_dir.joinpath("ai", "proposals.json").read_text("utf-8"))
        encoded = json.dumps(value)
        self.assertNotIn("provider_usage", encoded.replace('"provider_usage": {"input_tokens": 12}', ""))
        self.assertNotIn("api_key", encoded.lower())
        self.assertNotIn("access_token", encoded.lower())
        audit = value["proposals"][0]["validated_proposals"][0]["audit"]
        self.assertFalse(audit["raw_prompt_stored"])
        self.assertFalse(audit["raw_response_stored"])


if __name__ == "__main__":
    unittest.main()
