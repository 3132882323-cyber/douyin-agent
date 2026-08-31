from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
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
        self.assertNotIn("account_id", status["accounts"][0])
        self.assertEqual(status["accounts"][0]["account_hint"], "•••• 0000")
        self.assertNotIn("access_token", status)
        self.assertNotIn("refresh_token", status)
        self.assertFalse(status["secrets_exposed"])

    def test_callback_rejects_non_string_tokens_and_missing_or_invalid_expiry(self) -> None:
        valid = {
            "access_token": "access-token-value",
            "refresh_token": "refresh-token-value",
            "expires_in": 86400,
            "refresh_token_expires_in": 2592000,
            "advertiser_ids": [],
        }
        invalid_rows = (
            {**valid, "access_token": 12345},
            {**valid, "refresh_token": True},
            {key: value for key, value in valid.items() if key != "expires_in"},
            {**valid, "expires_in": 0},
            {**valid, "expires_in": -1},
            {**valid, "expires_in": 1.5},
            {
                key: value
                for key, value in valid.items()
                if key != "refresh_token_expires_in"
            },
            {**valid, "refresh_token_expires_in": 0},
        )
        for data in invalid_rows:
            with self.subTest(data=data):
                started = self.oauth.start_authorization(
                    "1871942906223351", "local-only-secret"
                )
                state = parse_qs(urlparse(started["authorize_url"]).query)["state"][0]
                with patch.object(
                    oceanengine_oauth,
                    "_request_json",
                    return_value={"code": 0, "data": data},
                ), self.assertRaises(ValueError):
                    self.oauth.complete_authorization("one-time-code", state)
                self.assertEqual({}, self.oauth._load_tokens())
                self.assertFalse(self.oauth.status()["connected"])

    def test_refresh_rejects_malformed_token_response_without_overwriting_record(self) -> None:
        self.oauth.save_credentials("1871942906223351", "local-only-secret")
        original = {
            "access_token": "expired-access-token",
            "refresh_token": "valid-refresh-token",
            "expires_at": 1,
            "refresh_token_expires_at": 4102444800,
            "accounts": [],
        }
        self.oauth._store_tokens(original)
        with patch.object(
            oceanengine_oauth,
            "_request_json",
            return_value={
                "code": 0,
                "data": {
                    "access_token": 12345,
                    "refresh_token": "replacement-refresh-token",
                    "expires_in": 86400,
                    "refresh_token_expires_in": 2592000,
                },
            },
        ), self.assertRaises(ValueError):
            self.oauth.get_valid_access_token()
        self.assertEqual(original, self.oauth._load_tokens())

    def test_expired_access_token_refreshes_with_bounded_typed_response(self) -> None:
        self.oauth.save_credentials("1871942906223351", "local-only-secret")
        self.oauth._store_tokens(
            {
                "access_token": "expired-access-token",
                "refresh_token": "valid-refresh-token",
                "expires_at": 1,
                "refresh_token_expires_at": 4102444800,
                "accounts": [],
            }
        )
        status = self.oauth.status()
        self.assertFalse(status["connected"])
        self.assertTrue(status["needs_refresh"])
        self.assertTrue(status["refresh_available"])
        self.assertNotIn("refresh_token", status)
        with patch.object(
            oceanengine_oauth,
            "_request_json",
            return_value={
                "code": 0,
                "data": {
                    "access_token": "replacement-access-token",
                    "refresh_token": "replacement-refresh-token",
                    "expires_in": "86400",
                    "refresh_token_expires_in": 2592000,
                },
            },
        ):
            token = self.oauth.get_valid_access_token()

        self.assertEqual("replacement-access-token", token)
        saved = self.oauth._load_tokens()
        self.assertGreater(saved["expires_at"], int(oceanengine_oauth.time.time()))
        self.assertGreater(
            saved["refresh_token_expires_at"], int(oceanengine_oauth.time.time())
        )

    def test_callback_rejects_wrong_state_before_network_request(self) -> None:
        self.oauth.start_authorization("1871942906223351", "local-only-secret")
        with patch.object(oceanengine_oauth, "_request_json") as request:
            with self.assertRaisesRegex(ValueError, "状态不匹配"):
                self.oauth.complete_authorization("one-time-code", "wrong-state")
        request.assert_not_called()

    def test_oauth_redirect_is_rejected_before_credentials_can_be_forwarded(self) -> None:
        handler = oceanengine_oauth._RejectOceanEngineRedirects()
        response = Mock()
        with self.assertRaisesRegex(URLError, "拒绝转发凭证"):
            handler.redirect_request(
                object(), response, 302, "Found", {}, "https://attacker.example/token"
            )
        response.close.assert_called_once_with()

    def test_oauth_http_error_closes_response_before_reraising(self) -> None:
        response = Mock()
        error = HTTPError("https://api.oceanengine.com/token", 503, "unavailable", {}, response)
        with patch.object(oceanengine_oauth, "urlopen", side_effect=error):
            with self.assertRaises(HTTPError):
                oceanengine_oauth._request_json("https://api.oceanengine.com/token")
        response.close.assert_called_once_with()

    def test_oauth_response_is_bounded_and_rejects_nonfinite_json(self) -> None:
        class Response:
            def __init__(self, raw: bytes):
                self.raw = raw

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, limit: int) -> bytes:
                return self.raw[:limit]

        oversized = b"x" * (oceanengine_oauth.MAX_OAUTH_RESPONSE_BYTES + 1)
        with patch.object(oceanengine_oauth, "urlopen", return_value=Response(oversized)):
            with self.assertRaisesRegex(ValueError, "too large"):
                oceanengine_oauth._request_json("https://api.oceanengine.com/token")

        for raw in (
            b'{"code":0,"data":{"expires_in":NaN}}',
            b'{"code":0,"data":{"expires_in":Infinity}}',
            b'{"code":0,"data":{"expires_in":1e9999}}',
        ):
            with self.subTest(raw=raw), patch.object(
                oceanengine_oauth, "urlopen", return_value=Response(raw)
            ), self.assertRaises(ValueError):
                oceanengine_oauth._request_json("https://api.oceanengine.com/token")

        with patch.object(
            oceanengine_oauth,
            "urlopen",
            return_value=Response(
                b'{"code":0,"data":{"access_token":"broken-\xff-token"}}'
            ),
        ), self.assertRaisesRegex(ValueError, "UTF-8"):
            oceanengine_oauth._request_json("https://api.oceanengine.com/token")


class MacOSKeychainOAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.oauth = OceanEngineOAuth(Path(self.temp.name))
        self.keychain: dict[str, dict] = {}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_macos_saves_secret_and_tokens_in_keychain(self) -> None:
        def store(service: str, value: dict) -> None:
            self.keychain[service] = json.loads(json.dumps(value))

        with (
            patch.object(oceanengine_oauth.sys, "platform", "darwin"),
            patch.object(oceanengine_oauth, "_macos_keychain_store", side_effect=store),
            patch.object(
                oceanengine_oauth,
                "_macos_keychain_load",
                side_effect=lambda service: json.loads(json.dumps(self.keychain.get(service, {}))),
            ),
        ):
            self.oauth.save_credentials("1871942906223351", "mac-local-secret")
            self.oauth._store_tokens(
                {
                    "access_token": "access-token-value",
                    "expires_at": 4102444800,
                    "accounts": [],
                }
            )
            status = self.oauth.status()
            self.assertTrue(status["secret_saved"])
            self.assertEqual("macos_keychain", status["secret_storage"])
            self.assertTrue(status["connected"])
            self.assertFalse(self.oauth.secret_path.exists())
            self.assertFalse(self.oauth.token_path.exists())
            self.assertEqual(
                "mac-local-secret",
                self.keychain[oceanengine_oauth.MACOS_APP_SECRET_SERVICE]["app_secret"],
            )


if __name__ == "__main__":
    unittest.main()
