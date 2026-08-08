from __future__ import annotations

import unittest
from unittest.mock import patch

from doudian_marketplace import clear_registered_platform_adapter, register_platform_adapter
from marketplace_readiness import build_marketplace_readiness


class ReadyAdapter:
    def readiness_evidence(self):
        return {
            "adapter_id": "doudian.approved",
            "contract_version": "contract-v1",
            "build_sha256": "b" * 64,
            "deployment_probe": {"passed": True, "checked_at": "2026-08-05T06:00:00Z"},
        }

    def build_authorization_url(self, config, state):
        return "https://platform.example.test/authorize"

    def exchange_authorization_code(self, config, code, expected_scope):
        raise NotImplementedError

    def call_open_api(self, config, token, scope, method, params):
        raise NotImplementedError


class MarketplaceReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_registered_platform_adapter()

    def tearDown(self) -> None:
        clear_registered_platform_adapter()

    def test_default_local_checkout_never_claims_marketplace_ready(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            result = build_marketplace_readiness()
        self.assertFalse(result["ready_for_submission"])
        self.assertFalse(result["ready_for_public_release"])
        self.assertGreater(result["blocker_count"], 0)
        self.assertIn("deployment_mode", {item["id"] for item in result["blockers"]})
        self.assertFalse(result["oauth"]["app_secret_configured"])
        self.assertNotIn("app_secret", result["oauth"])

    def test_complete_deployment_evidence_can_pass(self) -> None:
        env = {
            "DIAN_AGENT_DEPLOYMENT_MODE": "doudian_marketplace",
            "DIAN_AGENT_ALLOWED_WEB_ORIGINS": "https://console.example.com",
            "DIAN_AGENT_ENTERPRISE_NAME": "Example Technology Co Ltd",
            "DIAN_AGENT_ENTERPRISE_VERIFIED": "true",
            "DIAN_AGENT_DOUDIAN_PROVIDER_APPROVED": "true",
            "DIAN_AGENT_DOUDIAN_APP_APPROVED": "true",
            "DIAN_AGENT_DOUDIAN_APP_KEY": "app-key",
            "DIAN_AGENT_DOUDIAN_APP_SECRET": "secret",
            "DIAN_AGENT_DOUDIAN_CALLBACK_URL": "https://api.example.com/oauth/doudian/callback",
            "DIAN_AGENT_DOUDIAN_API_CONTRACT_APPROVED": "true",
            "DIAN_AGENT_TOKEN_STORE_BACKEND": "cloud_kms",
            "DIAN_AGENT_OAUTH_STATE_BACKEND": "redis",
            "DIAN_AGENT_AUTH_GATEWAY_CONFIGURED": "true",
            "DIAN_AGENT_PRIVACY_URL": "https://example.com/privacy",
            "DIAN_AGENT_TERMS_URL": "https://example.com/terms",
            "DIAN_AGENT_DATA_DELETION_URL": "https://example.com/delete",
            "DIAN_AGENT_SUPPORT_URL": "https://example.com/support",
            "DIAN_AGENT_CUSTOMER_SERVICE": "support@example.com",
            "DIAN_AGENT_ICP_FILING": "京ICP备12345678号",
            "DIAN_AGENT_SECURITY_REVIEW_PASSED": "true",
        }
        register_platform_adapter(ReadyAdapter())
        with patch.dict("os.environ", env, clear=True):
            result = build_marketplace_readiness()
        self.assertTrue(result["ready_for_submission"])
        self.assertEqual(result["blocker_count"], 0)
        self.assertFalse(result["deployment"]["browser_page_capture"])
        self.assertEqual(result["platform_adapter"]["contract_version"], "contract-v1")

    def test_environment_flags_cannot_replace_injected_adapter_evidence(self) -> None:
        env = {
            "DIAN_AGENT_DEPLOYMENT_MODE": "doudian_marketplace",
            "DIAN_AGENT_DOUDIAN_API_CONTRACT_APPROVED": "true",
            "DIAN_AGENT_DOUDIAN_API_ADAPTER_CONFIGURED": "true",
        }
        with patch.dict("os.environ", env, clear=True):
            result = build_marketplace_readiness()
        blockers = {item["id"] for item in result["blockers"]}
        self.assertIn("api_adapter", blockers)
        self.assertIn("api_contract", blockers)
        self.assertFalse(result["platform_adapter"]["injected"])

    def test_http_callback_and_memory_token_store_are_blockers(self) -> None:
        env = {
            "DIAN_AGENT_DEPLOYMENT_MODE": "doudian_marketplace",
            "DIAN_AGENT_DOUDIAN_APP_KEY": "app-key",
            "DIAN_AGENT_DOUDIAN_APP_SECRET": "secret",
            "DIAN_AGENT_DOUDIAN_CALLBACK_URL": "http://127.0.0.1/callback",
            "DIAN_AGENT_TOKEN_STORE_BACKEND": "memory",
        }
        with patch.dict("os.environ", env, clear=True):
            result = build_marketplace_readiness()
        blockers = {item["id"] for item in result["blockers"]}
        self.assertIn("oauth_configuration", blockers)
        self.assertIn("token_storage", blockers)


if __name__ == "__main__":
    unittest.main()
