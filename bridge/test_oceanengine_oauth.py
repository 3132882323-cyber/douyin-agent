from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import oceanengine_oauth
from oceanengine_oauth import OceanEngineOAuth


class OceanEngineOAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.oauth = OceanEngineOAuth(Path(self.temp.name))
        self.protect = patch.object(
            oceanengine_oauth, "_windows_protect", side_effect=lambda value, _description: value
        )
        self.unprotect = patch.object(
            oceanengine_oauth, "_windows_unprotect", side_effect=lambda value: value
        )
        self.platform = patch.object(oceanengine_oauth.sys, "platform", "win32")
        self.protect.start()
        self.unprotect.start()
        self.platform.start()

    def tearDown(self) -> None:
        self.platform.stop()
        self.unprotect.stop()
        self.protect.stop()
        self.temp.cleanup()

    def test_start_builds_qianchuan_url_without_exposing_secret(self) -> None:
        started = self.oauth.start_authorization(
            "1871942906223351", "local-only-secret"
        )
        parsed = urlparse(started["authorize_url"])
        query = parse_qs(parsed.query)
        self.assertEqual(
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
            oceanengine_oauth.QIANCHUAN_AUTHORIZE_URL,
        )
        self.assertEqual(query["app_id"], ["1871942906223351"])
        self.assertEqual(query["redirect_uri"], [oceanengine_oauth.PUBLIC_CALLBACK_URL])
        self.assertNotIn("local-only-secret", started["authorize_url"])
        status = self.oauth.status()
        self.assertTrue(status["secret_saved"])
        self.assertTrue(status["authorization_in_progress"])
        self.assertFalse(status["secrets_exposed"])

    def test_callback_validates_state_and_saves_account_without_exposing_token(self) -> None:
        started = self.oauth.start_authorization(
            "1871942906223351", "local-only-secret"
        )
        state = parse_qs(urlparse(started["authorize_url"]).query)["state"][0]

        def fake_request(url: str, **kwargs):
            if url == oceanengine_oauth.TOKEN_URL:
                return {
                    "code": 0,
                    "data": {
                        "access_token": "access-token-value",
                        "refresh_token": "refresh-token-value",
                        "expires_in": 86400,
                        "refresh_token_expires_in": 2592000,
                        "advertiser_ids": [26000000],
                    },
                }
            return {
                "code": 0,
                "data": {
                    "list": [
                        {
                            "advertiser_id": 26000000,
                            "advertiser_name": "测试千川账号",
                            "is_valid": True,
                        }
                    ]
                },
            }

        with patch.object(oceanengine_oauth, "_request_json", side_effect=fake_request):
            result = self.oauth.complete_authorization("one-time-code", state)
        self.assertTrue(result["ok"])
        self.assertEqual(result["account_count"], 1)
        status = self.oauth.status()
        self.assertTrue(status["connected"])
        self.assertEqual(status["accounts"][0]["account_name"], "测试千川账号")
        self.assertNotIn("access_token", status)
        self.assertNotIn("refresh_token", status)
        self.assertFalse(status["secrets_exposed"])

    def test_callback_rejects_wrong_state_before_network_request(self) -> None:
        self.oauth.start_authorization("1871942906223351", "local-only-secret")
        with patch.object(oceanengine_oauth, "_request_json") as request:
            with self.assertRaisesRegex(ValueError, "状态不匹配"):
                self.oauth.complete_authorization("one-time-code", "wrong-state")
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
