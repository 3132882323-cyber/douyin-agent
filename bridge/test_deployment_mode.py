from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import http_receiver
from deployment_mode import blocked_browser_capability, request_origin_allowed, resolve_deployment_policy


class DeploymentModeTests(unittest.TestCase):
    def test_local_is_backward_compatible(self) -> None:
        policy = resolve_deployment_policy("local")
        self.assertTrue(policy.local_companion)
        self.assertTrue(policy.browser_page_capture)
        self.assertTrue(policy.browser_dom_execution)
        self.assertIsNone(blocked_browser_capability("/push", policy))

    def test_marketplace_and_unknown_modes_fail_closed(self) -> None:
        for mode in ("doudian_marketplace", "cloud", "typo"):
            with self.subTest(mode=mode):
                policy = resolve_deployment_policy(mode)
                self.assertFalse(policy.browser_page_capture)
                self.assertFalse(policy.browser_dom_execution)
                self.assertEqual(blocked_browser_capability("/push", policy), "browser_page_capture_disabled")
                self.assertEqual(
                    blocked_browser_capability("/actions/preflight/consume", policy),
                    "browser_dom_execution_disabled",
                )

    def test_marketplace_cors_uses_exact_https_allowlist(self) -> None:
        env = {
            "DIAN_AGENT_DEPLOYMENT_MODE": "doudian_marketplace",
            "DIAN_AGENT_ALLOWED_WEB_ORIGINS": "https://console.example.com, http://unsafe.example.com",
        }
        with patch.dict("os.environ", env, clear=False):
            self.assertTrue(request_origin_allowed("https://console.example.com"))
            self.assertFalse(request_origin_allowed("https://evil.example.com"))
            self.assertFalse(request_origin_allowed("chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"))

    def test_marketplace_http_rejects_snapshot_and_all_legacy_writes(self) -> None:
        with patch.dict("os.environ", {"DIAN_AGENT_DEPLOYMENT_MODE": "doudian_marketplace"}, clear=False):
            server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"

            def rejected(path: str) -> tuple[int, dict]:
                request = urllib.request.Request(
                    base_url + path,
                    data=b"{}",
                    headers={"Content-Type": "application/json", "X-Dian-Agent": "2"},
                    method="POST",
                )
                try:
                    urllib.request.urlopen(request)
                except urllib.error.HTTPError as error:
                    return error.code, json.loads(error.read())
                self.fail(f"{path} was not rejected")

            try:
                code, body = rejected("/push")
                self.assertEqual(code, 403)
                self.assertEqual(body["error"], "browser_page_capture_disabled")
                code, body = rejected("/stores/link")
                self.assertEqual(code, 403)
                self.assertEqual(body["error"], "browser_page_capture_disabled")
                code, body = rejected("/settings")
                self.assertEqual(code, 403)
                self.assertEqual(body["error"], "marketplace_authenticated_gateway_required")
                status = json.loads(urllib.request.urlopen(base_url + "/marketplace/readiness").read())
                self.assertFalse(status["ready_for_public_release"])
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(base_url + "/catalog")
                self.assertEqual(context.exception.code, 403)
                health = json.loads(urllib.request.urlopen(base_url + "/health").read())
                self.assertEqual(health["health_contract_version"], 2)
                self.assertEqual(health["mode"], "doudian_marketplace")
                self.assertEqual(health["mode_kind"], "deployment")
                self.assertEqual(health["details"]["status"], "deferred")
                self.assertEqual(health["details"]["endpoint"], "/system/status")
                self.assertNotIn("snapshot_count", health)
                self.assertNotIn("execution_enabled", health)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_health_and_live_alias_never_touch_diagnostics_or_database(self) -> None:
        blocked = AssertionError("liveness endpoint attempted a deferred check")
        patches = (
            patch.object(http_receiver, "list_snapshots", side_effect=blocked),
            patch.object(http_receiver, "_schema_version_check", side_effect=blocked),
            patch.object(http_receiver, "_disk_usage_check", side_effect=blocked),
            patch.object(http_receiver, "_local_store", side_effect=blocked),
            patch.object(http_receiver, "load_agent_settings", side_effect=blocked),
        )
        for active_patch in patches:
            active_patch.start()
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            for path in ("/health", "/health/live"):
                with self.subTest(path=path):
                    response = urllib.request.urlopen(base_url + path, timeout=1)
                    self.assertEqual(response.status, 200)
                    health = json.loads(response.read())
                    self.assertEqual(health["status"], "ok")
                    self.assertEqual(health["health_contract_version"], 2)
                    self.assertEqual(health["probe"], "liveness")
                    self.assertEqual(health["bridge_protocol_version"], 2)
                    self.assertEqual(health["details"], {
                        "status": "deferred",
                        "endpoint": "/system/status",
                        "sections": ["execution", "database", "storage", "scan"],
                    })
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            for active_patch in reversed(patches):
                active_patch.stop()


if __name__ == "__main__":
    unittest.main()
