from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import http_receiver
from local_api_auth import (
    AUTH_HEADER,
    issue_internal_session_token,
    issue_session_token,
    provision_local_api_trust,
    repair_local_api_trust,
)


EXTENSION_A = "a" * 32
EXTENSION_B = "b" * 32


class LocalHttpAuthenticationTests(unittest.TestCase):
    def test_installer_trust_receipt_identifies_the_packaged_agent_version(self) -> None:
        output = io.StringIO()
        receipt = {"install_id": "1" * 32, "extension_id": EXTENSION_A}
        with patch.object(http_receiver, "provision_local_api_trust", return_value=receipt), contextlib.redirect_stdout(output):
            result = http_receiver._run_local_api_trust_provisioner(
                ["--initialize-local-api-trust", "manifest.json", "install-root"]
            )

        self.assertEqual(0, result)
        document = json.loads(output.getvalue())
        self.assertTrue(document["ok"])
        self.assertEqual(http_receiver.AGENT_VERSION, document["agent_version"])
        self.assertEqual(receipt["install_id"], document["install_id"])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.original_data_dir = http_receiver.DATA_DIR
        http_receiver.DATA_DIR = Path(self.temp.name) / "data"
        http_receiver.DATA_DIR.mkdir(parents=True)
        active_extension = Path(self.temp.name) / "extension-current"
        active_extension.mkdir()
        (active_extension / "manifest.json").write_text(
            json.dumps({"version": http_receiver.AGENT_VERSION}),
            encoding="utf-8",
        )
        config = Path(self.temp.name) / "config"
        config.mkdir(parents=True)
        (config / "trusted_extension_ids.json").write_text(
            json.dumps({"schema_version": 1, "extension_ids": [EXTENSION_A, EXTENSION_B]}),
            encoding="utf-8",
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        http_receiver.DATA_DIR = self.original_data_dir
        self.temp.cleanup()

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict | None = None,
        origin: str = "",
        token: str = "",
        user_agent: str = "",
        bridge_protocol: str = "2",
        extension_version: str | None = None,
        extension_id_header: str = "",
    ) -> tuple[int, dict | str, dict]:
        headers = {"X-Dian-Agent": bridge_protocol}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if origin:
            headers["Origin"] = origin
        runtime_version = (
            http_receiver.AGENT_VERSION
            if origin and extension_version is None
            else str(extension_version or "")
        )
        if runtime_version:
            headers["X-Dian-Agent-Extension-Version"] = runtime_version
        if extension_id_header:
            headers[http_receiver.EXTENSION_ID_HEADER] = extension_id_header
        if token:
            headers[AUTH_HEADER] = token
        if user_agent:
            headers["User-Agent"] = user_agent
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                body: dict | str = json.loads(raw)
            except json.JSONDecodeError:
                body = raw.decode("utf-8", errors="replace")
            return error.code, body, dict(error.headers)
        raw = response.read()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = raw.decode("utf-8", errors="replace")
        return response.status, body, dict(response.headers)

    def _session(self, extension_id: str = EXTENSION_A) -> dict:
        status, body, _headers = self._request(
            "/auth/session",
            method="POST",
            payload={
                "extension_id": extension_id,
                "extension_version": http_receiver.AGENT_VERSION,
            },
            origin=f"chrome-extension://{extension_id}",
        )
        self.assertEqual(200, status, body)
        self.assertIsInstance(body, dict)
        self.assertEqual(
            http_receiver.AGENT_VERSION, body.get("extension_version")
        )
        return body

    def test_extension_session_fails_closed_until_all_versions_match(self) -> None:
        origin = f"chrome-extension://{EXTENSION_A}"
        status, body, _headers = self._request(
            "/auth/session",
            method="POST",
            payload={
                "extension_id": EXTENSION_A,
                "extension_version": http_receiver.AGENT_VERSION,
            },
            origin=origin,
            extension_version="",
        )
        self.assertEqual(409, status)
        self.assertEqual("agent_extension_version_mismatch", body["error"])

        current_session = self._session(EXTENSION_A)
        status, body, _headers = self._request(
            "/settings",
            origin=origin,
            token=current_session["access_token"],
            extension_version="",
        )
        self.assertEqual(409, status)
        self.assertEqual("agent_extension_version_mismatch", body["error"])

        status, body, _headers = self._request(
            "/auth/session",
            method="POST",
            payload={"extension_id": EXTENSION_A, "extension_version": "4.13.9"},
            origin=origin,
            extension_version="4.13.9",
        )
        self.assertEqual(409, status)
        self.assertEqual("reload_required", body["activation"]["state"])

        manifest = Path(self.temp.name) / "extension-current" / "manifest.json"
        manifest.write_text(json.dumps({"version": "4.13.9"}), encoding="utf-8")
        status, body, _headers = self._request(
            "/auth/session",
            method="POST",
            payload={
                "extension_id": EXTENSION_A,
                "extension_version": http_receiver.AGENT_VERSION,
            },
            origin=origin,
        )
        self.assertEqual(409, status)
        self.assertEqual("installed_version_mismatch", body["activation"]["state"])

    def test_upgrade_invalidates_old_extension_token_before_business_access(self) -> None:
        old_session = issue_session_token(
            Path(self.temp.name),
            EXTENSION_A,
            extension_version="4.13.9",
        )
        status, body, _headers = self._request(
            "/settings",
            origin=f"chrome-extension://{EXTENSION_A}",
            token=old_session["access_token"],
        )
        self.assertEqual(409, status)
        self.assertEqual(
            "agent_session_extension_version_mismatch", body["error"]
        )

    def test_powershell_utf8_bom_trust_registry_remains_authoritative(self) -> None:
        registry = http_receiver.DATA_DIR.parent / "config" / "trusted_extension_ids.json"
        registry.write_text(
            json.dumps({"schema_version": 1, "extension_ids": [EXTENSION_A]}),
            encoding="utf-8-sig",
        )

        session = self._session(EXTENSION_A)

        self.assertEqual(EXTENSION_A, session["subject"])
        status, body, _headers = self._request(
            "/auth/session",
            method="POST",
            payload={
                "extension_id": EXTENSION_B,
                "extension_version": http_receiver.AGENT_VERSION,
            },
            origin=f"chrome-extension://{EXTENSION_B}",
        )
        self.assertEqual(403, status)
        self.assertEqual("extension_pairing_not_authorized", body["error"])

    def test_corrupt_install_auth_returns_repairable_service_error(self) -> None:
        auth_path = http_receiver.DATA_DIR.parent / "config" / "local_api_auth.json"
        auth_path.write_text("{not-json", encoding="utf-8")

        status, body, _headers = self._request(
            "/auth/session",
            method="POST",
            payload={
                "extension_id": EXTENSION_A,
                "extension_version": http_receiver.AGENT_VERSION,
            },
            origin=f"chrome-extension://{EXTENSION_A}",
        )

        self.assertEqual(503, status)
        self.assertEqual("agent_install_auth_corrupt", body["error"])
        self.assertTrue(body["repair_required"])

    def test_corrupt_trust_fails_closed_then_explicit_repair_restores_full_pairing(self) -> None:
        old_session = self._session(EXTENSION_A)
        registry = http_receiver.DATA_DIR.parent / "config" / "trusted_extension_ids.json"
        registry.write_text(
            json.dumps({"schema_version": 999, "extension_ids": [EXTENSION_A]}),
            encoding="utf-8",
        )
        status, body, _headers = self._request(
            "/auth/session",
            method="POST",
            payload={
                "extension_id": EXTENSION_A,
                "extension_version": http_receiver.AGENT_VERSION,
            },
            origin=f"chrome-extension://{EXTENSION_A}",
        )
        self.assertEqual(403, status)
        self.assertEqual("extension_pairing_not_authorized", body["error"])

        receipt = repair_local_api_trust(
            http_receiver.DATA_DIR.parent,
            Path(__file__).resolve().parent.parent / "extension" / "manifest.json",
        )
        repaired_extension_id = str(receipt["extension_id"])
        self.assertTrue(receipt["repaired"])
        self.assertNotIn("secret", json.dumps(receipt))
        self.assertNotIn("access_token", json.dumps(receipt))

        status, body, _headers = self._request(
            "/system/status",
            origin=f"chrome-extension://{EXTENSION_A}",
            token=str(old_session["access_token"]),
        )
        self.assertEqual(403, status)
        self.assertEqual("extension_origin_not_trusted", body["error"])

        session = self._session(repaired_extension_id)
        self.assertEqual(receipt["install_id"], session["install_id"])
        status, body, _headers = self._request(
            "/system/status",
            origin=f"chrome-extension://{repaired_extension_id}",
            token=str(session["access_token"]),
        )
        self.assertEqual(200, status, body)
        self.assertEqual(http_receiver.AGENT_VERSION, body["agent_version"])

    def test_same_version_agent_from_another_install_root_has_different_install_id(self) -> None:
        manifest = Path(__file__).resolve().parent.parent / "extension" / "manifest.json"
        expected_root = Path(self.temp.name) / "expected-install"
        expected_receipt = provision_local_api_trust(expected_root, manifest)
        running_receipt = provision_local_api_trust(http_receiver.DATA_DIR.parent, manifest)
        extension_id = str(running_receipt["extension_id"])

        session = self._session(extension_id)

        self.assertEqual(running_receipt["install_id"], session["install_id"])
        self.assertNotEqual(expected_receipt["install_id"], session["install_id"])

    def test_health_and_oauth_callback_are_explicitly_exempt(self) -> None:
        status, body, _headers = self._request(
            "/health/live", origin=f"chrome-extension://{EXTENSION_A}"
        )
        self.assertEqual(200, status)
        self.assertEqual("ok", body["status"])

        status, body, _headers = self._request("/oauth/oceanengine/callback")
        self.assertEqual(400, status)
        self.assertIsInstance(body, str)
        self.assertNotIn("agent_session", body)

    def test_sensitive_extension_get_and_all_regular_posts_require_session(self) -> None:
        origin = f"chrome-extension://{EXTENSION_A}"
        status, body, _headers = self._request("/settings", origin=origin)
        self.assertEqual(401, status)
        self.assertEqual("agent_session_required", body["error"])

        status, body, _headers = self._request(
            "/settings", method="POST", payload={"roi_target": 2}, origin=origin
        )
        self.assertEqual(401, status)
        self.assertEqual("agent_session_required", body["error"])

        session = self._session()
        token = session["access_token"]
        self.assertNotIn("secret", session)
        self.assertFalse(session["installation_secret_exposed"])
        status, body, _headers = self._request("/settings", origin=origin, token=token)
        self.assertEqual(200, status)
        status, body, _headers = self._request(
            "/settings",
            method="POST",
            payload={"roi_target": 2},
            origin=origin,
            token=token,
        )
        self.assertEqual(200, status)
        self.assertEqual(2.0, body["settings"]["roi_target"])

    def test_authenticated_status_is_lightweight_and_bound_to_current_extension(self) -> None:
        origin = f"chrome-extension://{EXTENSION_A}"

        status, body, _headers = self._request("/auth/status", origin=origin)
        self.assertEqual(401, status)
        self.assertEqual("agent_session_required", body["error"])

        session = self._session(EXTENSION_A)
        with patch.object(
            http_receiver,
            "build_system_status",
            side_effect=AssertionError("aggregate diagnostics must not run for auth status"),
        ):
            status, body, _headers = self._request(
                "/auth/status",
                origin=origin,
                token=str(session["access_token"]),
            )

        self.assertEqual(200, status, body)
        self.assertEqual(1, body["authentication_contract_version"])
        self.assertTrue(body["authenticated"])
        self.assertEqual("browser_extension", body["client_kind"])
        self.assertEqual(EXTENSION_A, body["session_subject"])
        self.assertEqual(
            http_receiver.AGENT_VERSION, body["session_extension_version"]
        )
        self.assertEqual(
            http_receiver.AGENT_VERSION, body["required_extension_version"]
        )
        self.assertGreater(body["session_expires_at"], int(time.time()))
        self.assertNotIn("access_token", body)
        self.assertNotIn("secret", body)

        token_a = str(session["access_token"])
        status, body, _headers = self._request(
            "/auth/status",
            origin=f"chrome-extension://{EXTENSION_B}",
            token=token_a,
        )
        self.assertEqual(401, status)
        self.assertEqual("agent_session_invalid", body["error"])

    def test_originless_extension_get_uses_explicit_id_plus_signed_token_binding(self) -> None:
        session = self._session(EXTENSION_A)
        token = str(session["access_token"])

        status, body, _headers = self._request(
            "/auth/status",
            token=token,
            extension_id_header=EXTENSION_A,
            extension_version=http_receiver.AGENT_VERSION,
        )
        self.assertEqual(200, status, body)
        self.assertTrue(body["authenticated"])
        self.assertEqual(EXTENSION_A, body["session_subject"])

        status, body, _headers = self._request(
            "/auth/status",
            token=token,
            extension_id_header=EXTENSION_B,
            extension_version=http_receiver.AGENT_VERSION,
        )
        self.assertEqual(401, status)
        self.assertEqual("agent_session_invalid", body["error"])

        status, body, _headers = self._request(
            "/auth/status",
            token=token,
            extension_id_header="c" * 32,
            extension_version=http_receiver.AGENT_VERSION,
        )
        self.assertEqual(403, status)
        self.assertEqual("extension_origin_not_trusted", body["error"])

    def test_untrusted_or_cross_origin_sessions_fail_closed(self) -> None:
        status, body, _headers = self._request(
            "/auth/session",
            method="POST",
            payload={
                "extension_id": EXTENSION_A,
                "extension_version": http_receiver.AGENT_VERSION,
            },
            origin=f"chrome-extension://{EXTENSION_A}",
            bridge_protocol="1",
        )
        self.assertEqual(403, status)
        self.assertEqual("bridge_protocol_upgrade_required", body["error"])

        untrusted = "c" * 32
        status, body, _headers = self._request(
            "/auth/session",
            method="POST",
            payload={
                "extension_id": untrusted,
                "extension_version": http_receiver.AGENT_VERSION,
            },
            origin=f"chrome-extension://{untrusted}",
        )
        self.assertEqual(403, status)
        self.assertEqual("extension_pairing_not_authorized", body["error"])

        status, body, _headers = self._request(
            "/auth/session",
            method="POST",
            payload={
                "extension_id": EXTENSION_B,
                "extension_version": http_receiver.AGENT_VERSION,
            },
            origin=f"chrome-extension://{EXTENSION_A}",
        )
        self.assertEqual(403, status)
        self.assertEqual("extension_pairing_not_authorized", body["error"])

        token_a = self._session(EXTENSION_A)["access_token"]
        status, body, _headers = self._request(
            "/settings", origin=f"chrome-extension://{EXTENSION_B}", token=token_a
        )
        self.assertEqual(401, status)
        self.assertEqual("agent_session_invalid", body["error"])

        expired = issue_session_token(
            Path(self.temp.name),
            EXTENSION_A,
            extension_version=http_receiver.AGENT_VERSION,
            now=int(time.time()) - 61,
            ttl_seconds=60,
        )["access_token"]
        status, body, _headers = self._request(
            "/settings", origin=f"chrome-extension://{EXTENSION_A}", token=expired
        )
        self.assertEqual(401, status)
        self.assertEqual("agent_session_expired", body["error"])

        forged = token_a[:-1] + ("a" if token_a[-1] != "a" else "b")
        status, body, _headers = self._request(
            "/settings", origin=f"chrome-extension://{EXTENSION_A}", token=forged
        )
        self.assertEqual(401, status)
        self.assertEqual("agent_session_invalid", body["error"])

    def test_cors_preflight_never_reflects_an_untrusted_extension_origin(self) -> None:
        status, _body, headers = self._request(
            "/settings", method="OPTIONS", origin=f"chrome-extension://{'c' * 32}"
        )
        self.assertEqual(204, status)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

        status, _body, headers = self._request(
            "/settings", method="OPTIONS", origin=f"chrome-extension://{EXTENSION_A}"
        )
        self.assertEqual(204, status)
        self.assertEqual(
            f"chrome-extension://{EXTENSION_A}", headers.get("Access-Control-Allow-Origin")
        )
        self.assertIn(AUTH_HEADER, headers.get("Access-Control-Allow-Headers", ""))

    def test_sensitive_post_retry_is_coalesced_without_changing_extension_protocol(self) -> None:
        origin = f"chrome-extension://{EXTENSION_A}"
        token = self._session(EXTENSION_A)["access_token"]
        with patch.object(
            http_receiver,
            "test_integration",
            return_value={"platform": "feishu", "ok": True},
        ) as sender:
            first_status, first_body, _first_headers = self._request(
                "/integrations/test",
                method="POST",
                payload={"platform": "feishu"},
                origin=origin,
                token=token,
            )
            second_status, second_body, second_headers = self._request(
                "/integrations/test",
                method="POST",
                payload={"platform": "feishu"},
                origin=origin,
                token=token,
            )
        self.assertEqual(200, first_status, first_body)
        self.assertEqual(200, second_status, second_body)
        self.assertEqual("true", second_headers.get("X-Dian-Agent-Idempotent-Replay"))
        sender.assert_called_once_with("feishu")

    def test_generic_settings_route_cannot_replace_store_or_account_scope(self) -> None:
        origin = f"chrome-extension://{EXTENSION_A}"
        token = self._session(EXTENSION_A)["access_token"]
        status, body, _headers = self._request(
            "/settings",
            method="POST",
            payload={"store_key": "store_forged", "qianchuan_account_key": "account_forged"},
            origin=origin,
            token=token,
        )
        self.assertEqual(400, status)
        self.assertIn("/stores/select", body["error"])
        settings = http_receiver.load_agent_settings()
        self.assertEqual("", settings["store_key"])
        self.assertEqual("", settings["qianchuan_account_key"])

    def test_originless_compatibility_is_limited_to_redacted_maintenance_status(self) -> None:
        status, body, _headers = self._request("/settings")
        self.assertEqual(401, status)
        self.assertEqual("agent_session_required", body["error"])

        status, body, _headers = self._request("/system/status")
        self.assertEqual(200, status)
        self.assertTrue(body["authentication_required_for_details"])
        self.assertNotIn("database", body)
        self.assertNotIn("storage", body)

        status, body, _headers = self._request("/data/doudian/overview")
        self.assertEqual(401, status)
        self.assertEqual("agent_session_required", body["error"])

        status, body, _headers = self._request(
            "/settings", method="POST", payload={"roi_target": 2}
        )
        self.assertEqual(401, status)
        self.assertEqual("agent_session_required", body["error"])

        internal = issue_internal_session_token(Path(self.temp.name))["access_token"]
        status, body, _headers = self._request("/settings", token=internal)
        self.assertEqual(200, status)
        self.assertIsInstance(body, dict)
        status, body, _headers = self._request(
            "/settings", method="POST", payload={"roi_target": 2}, token=internal
        )
        self.assertEqual(200, status)
        self.assertEqual(2.0, body["settings"]["roi_target"])

        extension_token = self._session(EXTENSION_A)["access_token"]
        status, body, _headers = self._request("/settings", token=extension_token)
        self.assertEqual(401, status)
        self.assertEqual("agent_session_invalid", body["error"])

        status, body, _headers = self._request(
            "/settings", user_agent="Mozilla/5.0 Chrome/140.0"
        )
        self.assertEqual(401, status)
        self.assertEqual("agent_session_required", body["error"])


if __name__ == "__main__":
    unittest.main()
