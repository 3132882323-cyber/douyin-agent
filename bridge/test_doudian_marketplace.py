from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlencode, urlparse
from unittest.mock import patch

from doudian_marketplace import (
    DoudianAppConfig,
    DoudianMarketplaceClient,
    DoudianMarketplaceService,
    ExampleDraftHmacSigner,
    InMemoryTokenStore,
    OAuthStateStore,
    OAuthToken,
    PlatformAuthorization,
    RejectingAuthenticator,
    ShopScope,
    TenantRequestContext,
    clear_registered_platform_adapter,
    platform_adapter_status,
    register_platform_adapter,
)


class FakeApprovedAdapter:
    def __init__(self, shop_id: str = "shop-a") -> None:
        self.shop_id = shop_id
        self.last_call = None

    def readiness_evidence(self):
        return {
            "adapter_id": "test.approved-adapter",
            "contract_version": "test-contract-v1",
            "build_sha256": "a" * 64,
            "deployment_probe": {"passed": True, "checked_at": "2026-08-05T06:00:00Z"},
        }

    def build_authorization_url(self, config, state):
        return "https://platform.example.test/authorize?" + urlencode({"state": state, "redirect_uri": config.callback_url})

    def exchange_authorization_code(self, config, code, expected_scope):
        return PlatformAuthorization(
            self.shop_id,
            OAuthToken("access-private", "refresh-private", 4102444800, granted_scopes=("order.read",)),
        )

    def call_open_api(self, config, token, scope, method, params):
        self.last_call = {"token": token, "scope": scope, "method": method, "params": dict(params)}
        return {"data": {"ok": True}}


class DoudianMarketplaceTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_registered_platform_adapter()
        self.config = DoudianAppConfig(
            app_key="test-app",
            app_secret="test-secret",
            callback_url="https://app.example.com/oauth/doudian/callback",
        )

    def tearDown(self) -> None:
        clear_registered_platform_adapter()

    def test_example_hmac_is_explicitly_non_production(self) -> None:
        self.assertFalse(ExampleDraftHmacSigner.production_safe)
        first = ExampleDraftHmacSigner.sign("secret", "example", 1700000000, {"b": 2, "a": 1})
        second = ExampleDraftHmacSigner.sign("secret", "example", 1700000000, {"a": 1, "b": 2})
        self.assertEqual(first, second)
        self.assertIn("NOT-A-PLATFORM-CONTRACT", ExampleDraftHmacSigner.algorithm)

    def test_default_client_fails_closed_without_network(self) -> None:
        client = DoudianMarketplaceClient(self.config, InMemoryTokenStore())
        scope = ShopScope("tenant-a", "shop-a")
        state = client.states.issue(scope)
        with patch("urllib.request.urlopen") as network:
            with self.assertRaisesRegex(RuntimeError, "adapter_not_configured"):
                client.start_authorization(scope)
            with self.assertRaisesRegex(RuntimeError, "adapter_not_configured"):
                client.complete_authorization("code", state)
            with self.assertRaisesRegex(RuntimeError, "adapter_not_configured"):
                client.call_api(scope, "product.list", {})
            network.assert_not_called()

    def test_adapter_registration_requires_contract_build_and_probe_evidence(self) -> None:
        class IncompleteAdapter(FakeApprovedAdapter):
            def readiness_evidence(self):
                return {"adapter_id": "incomplete"}

        with self.assertRaisesRegex(ValueError, "evidence_invalid"):
            register_platform_adapter(IncompleteAdapter())
        self.assertFalse(platform_adapter_status()["ready"])
        result = register_platform_adapter(FakeApprovedAdapter())
        self.assertTrue(result["ready"])
        self.assertEqual(result["build_sha256"], "a" * 64)

    def test_oauth_state_is_one_time_and_shop_scoped(self) -> None:
        store = OAuthStateStore(ttl_seconds=60)
        scope = ShopScope("tenant-a", "shop-a")
        state = store.issue(scope, now=100)
        self.assertEqual(store.consume(state, now=110), scope)
        with self.assertRaisesRegex(ValueError, "invalid_or_expired"):
            store.consume(state, now=111)

    def test_injected_adapter_completes_oauth_without_exposing_token(self) -> None:
        token_store = InMemoryTokenStore()
        adapter = FakeApprovedAdapter()
        client = DoudianMarketplaceClient(self.config, token_store, adapter=adapter)
        scope = ShopScope("tenant-a", "shop-a")
        start = client.start_authorization(scope)
        self.assertTrue(start["authorization_url"].startswith("https://"))
        state = parse_qs(urlparse(start["authorization_url"]).query)["state"][0]
        result = client.complete_authorization("one-time-code", state)
        self.assertTrue(result["token"]["connected"])
        self.assertNotIn("access_token", result["token"])
        self.assertNotIn("refresh_token", result["token"])
        self.assertIsNotNone(token_store.get(scope))
        self.assertIsNone(token_store.get(ShopScope("tenant-a", "shop-b")))

    def test_callback_rejects_platform_shop_mismatch(self) -> None:
        client = DoudianMarketplaceClient(
            self.config,
            InMemoryTokenStore(),
            adapter=FakeApprovedAdapter("shop-b"),
        )
        state = client.states.issue(ShopScope("tenant-a", "shop-a"))
        with self.assertRaisesRegex(ValueError, "shop_scope_mismatch"):
            client.complete_authorization("code", state)

    def test_api_call_uses_injected_adapter_and_scoped_token(self) -> None:
        store = InMemoryTokenStore()
        scope = ShopScope("tenant-a", "shop-a")
        store.put(scope, OAuthToken("access-private", "", 4102444800))
        adapter = FakeApprovedAdapter()
        client = DoudianMarketplaceClient(self.config, store, adapter=adapter)
        result = client.call_api(scope, "product.list", {"page": 1})
        self.assertTrue(result["data"]["ok"])
        self.assertEqual(adapter.last_call["scope"], scope)
        self.assertEqual(adapter.last_call["method"], "product.list")

    def test_default_authenticator_rejects_every_cloud_request(self) -> None:
        with self.assertRaises(PermissionError):
            RejectingAuthenticator().authenticate({})

    def test_service_uses_authenticated_scope_not_request_parameters(self) -> None:
        scope = ShopScope("tenant-authenticated", "shop-authenticated")
        store = InMemoryTokenStore()
        store.put(scope, OAuthToken("access-private", "", 4102444800))

        class Authenticator:
            def authenticate(self, headers):
                return TenantRequestContext("user-1", scope, ("operator",))

        adapter = FakeApprovedAdapter("shop-authenticated")
        service = DoudianMarketplaceService(
            DoudianMarketplaceClient(self.config, store, adapter=adapter), Authenticator()
        )
        service.call_api({"X-Shop-Id": "shop-attacker"}, "product.list", {})
        self.assertEqual(adapter.last_call["scope"], scope)


if __name__ == "__main__":
    unittest.main()
