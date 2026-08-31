from __future__ import annotations

import os
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import http_receiver
import promotion_readiness
from promotion_readiness import (
    LocalAnonymousFeedbackQueue,
    OFFICIAL_EXTENSION_IDS_BY_STORE,
    build_distribution_status,
    build_release_readiness,
    configured_trusted_extension_ids,
    extension_origin_trusted,
    extension_pairing_allowed,
    save_extension_install_state,
)


class PromotionReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _event(self) -> dict[str, object]:
        return {
            "industry": "apparel",
            "rule_id": "qianchuan.roi_loss",
            "spend_band": "500-1000",
            "roi_band": "1.0-1.5",
            "accepted": True,
            "result": "improved",
            "pack_version": "2026.8.2",
            "agent_version": "4.0.0",
        }

    def test_extension_source_is_whitelisted_and_visible(self) -> None:
        saved = save_extension_install_state(
            self.temp.name,
            {
                "source": "release_bundle",
                "browser": "chrome",
                    "version": "4.14.9",
                "extension_id": "a" * 32,
            },
        )
        self.assertEqual("extension_self_reported", saved["evidence"])
        status = build_distribution_status(self.temp.name)
        self.assertEqual("release_bundle", status["extension"]["source"])
        self.assertFalse(status["extension"]["official_store_install"])
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            save_extension_install_state(
                self.temp.name,
                    {"source": "unpacked", "browser": "chrome", "version": "4.14.9", "token": "secret"},
            )

    def test_environment_flag_cannot_create_extension_trust(self) -> None:
        unknown_extension = "c" * 32
        with patch.dict(
            os.environ,
            {
                "DIAN_AGENT_ALLOW_EXTENSION_TOFU": "1",
                "DIAN_AGENT_TRUSTED_EXTENSION_IDS": unknown_extension,
            },
            clear=False,
        ):
            self.assertFalse(extension_pairing_allowed(self.temp.name, unknown_extension))

    def test_malformed_or_symlinked_registry_contributes_no_trust(self) -> None:
        extension_id = "d" * 32
        save_extension_install_state(
            self.temp.name,
            {
                "source": "release_bundle",
                "browser": "chrome",
                    "version": "4.14.9",
                "extension_id": extension_id,
            },
            origin_extension_id=extension_id,
        )
        registry = Path(self.temp.name) / "config" / "trusted_extension_ids.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(
            json.dumps({"schema_version": 999, "extension_ids": [extension_id]}),
            encoding="utf-8",
        )
        with patch.dict(OFFICIAL_EXTENSION_IDS_BY_STORE, {}, clear=True):
            self.assertEqual(frozenset(), configured_trusted_extension_ids(self.temp.name))
            self.assertFalse(extension_origin_trusted(self.temp.name, extension_id))

        registry.unlink()
        outside = Path(self.temp.name) / "outside-trust.json"
        outside.write_text(
            json.dumps({"schema_version": 1, "extension_ids": [extension_id]}),
            encoding="utf-8",
        )
        try:
            registry.symlink_to(outside)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink creation is unavailable: {error}")
        with patch.dict(OFFICIAL_EXTENSION_IDS_BY_STORE, {}, clear=True):
            self.assertEqual(frozenset(), configured_trusted_extension_ids(self.temp.name))
            self.assertFalse(extension_origin_trusted(self.temp.name, extension_id))

    def test_anonymous_feedback_is_off_by_default_and_never_uploads(self) -> None:
        queue = LocalAnonymousFeedbackQueue(self.temp.name)
        self.assertFalse(queue.status(consent_enabled=False)["enabled"])
        with self.assertRaisesRegex(ValueError, "explicit consent"):
            queue.enqueue(self._event(), consent_enabled=False)
        queued = queue.enqueue(self._event(), consent_enabled=True)
        self.assertEqual("explicit_opt_in", queued["consent"])
        status = queue.status(consent_enabled=True)
        self.assertEqual(1, status["queued_count"])
        self.assertFalse(status["upload_configured"])
        self.assertFalse(status["upload_attempted"])
        self.assertEqual("local_queue_only", status["mode"])

    def test_raw_shop_data_and_unknown_fields_are_rejected(self) -> None:
        queue = LocalAnonymousFeedbackQueue(self.temp.name)
        raw = {**self._event(), "shop_name": "secret shop"}
        with self.assertRaisesRegex(ValueError, "raw shop data"):
            queue.enqueue(raw, consent_enabled=True)
        nested = {**self._event(), "industry": {"shop": "secret"}}
        with self.assertRaisesRegex(ValueError, "coarse scalar"):
            queue.enqueue(nested, consent_enabled=True)
        for unsafe_industry in ("shop_13800138000", "兽醒纪男士活力裤", "apparel-store-8848"):
            with self.subTest(industry=unsafe_industry):
                with self.assertRaisesRegex(ValueError, "approved industry slug"):
                    queue.enqueue({**self._event(), "industry": unsafe_industry}, consent_enabled=True)
        self.assertEqual(0, queue.status(consent_enabled=True)["queued_count"])

    def test_clear_removes_only_anonymous_queue(self) -> None:
        queue = LocalAnonymousFeedbackQueue(self.temp.name)
        queue.enqueue(self._event(), consent_enabled=True)
        self.assertEqual(1, queue.clear())
        self.assertEqual(0, queue.status(consent_enabled=True)["queued_count"])

    def test_public_release_remains_blocked_without_real_evidence(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = build_release_readiness(
                self.temp.name,
                production_ed25519_trust=False,
                authenticode_artifacts={},
            )
        self.assertFalse(result["ready_for_public_release"])
        self.assertEqual(
            {
                "production_ed25519_trust",
                "platform_code_signature",
                "browser_store_publication",
            },
            set(result["blockers"]),
        )

    def test_public_release_requires_all_three_proofs(self) -> None:
        save_extension_install_state(
            self.temp.name,
            {
                "source": "chrome_web_store",
                "browser": "chrome",
                    "version": "4.14.9",
                "extension_id": "a" * 32,
            },
            origin_extension_id="a" * 32,
        )
        with patch.dict(OFFICIAL_EXTENSION_IDS_BY_STORE, {"chrome_web_store": frozenset({"a" * 32})}):
            with patch.dict(
                os.environ,
                {"DIAN_AGENT_PUBLISHED_BROWSER_STORES": "chrome_web_store"},
                clear=True,
            ):
                result = build_release_readiness(
                    self.temp.name,
                    production_ed25519_trust=True,
                    authenticode_artifacts={
                        "agent": True,
                        "updater": True,
                        "installer_entry": True,
                        "upgrade_entry": True,
                        "maintenance_scripts": True,
                    },
                )
        self.assertTrue(result["ready_for_public_release"])
        self.assertEqual([], result["blockers"])

    def test_public_release_rejects_unpacked_or_wrong_version_extension(self) -> None:
        signed = {
            "agent": True,
            "updater": True,
            "installer_entry": True,
            "upgrade_entry": True,
            "maintenance_scripts": True,
        }
        for source, version in (("unpacked", "4.14.9"), ("chrome_web_store", "3.9.0")):
            with self.subTest(source=source, version=version):
                save_extension_install_state(
                    self.temp.name,
                    {"source": source, "browser": "chrome", "version": version, "extension_id": "a" * 32},
                    origin_extension_id="a" * 32,
                )
                with patch.dict(OFFICIAL_EXTENSION_IDS_BY_STORE, {"chrome_web_store": frozenset({"a" * 32})}):
                    with patch.dict(os.environ, {"DIAN_AGENT_PUBLISHED_BROWSER_STORES": "chrome_web_store"}, clear=True):
                        result = build_release_readiness(
                            self.temp.name,
                            production_ed25519_trust=True,
                            authenticode_artifacts=signed,
                        )
                self.assertFalse(result["ready_for_public_release"])
                check = next(item for item in result["checks"] if item["id"] == "browser_store_publication")
                self.assertFalse(check["ready"])

    def test_authenticode_requires_every_release_artifact(self) -> None:
        save_extension_install_state(
            self.temp.name,
                    {"source": "chrome_web_store", "browser": "chrome", "version": "4.14.9", "extension_id": "a" * 32},
            origin_extension_id="a" * 32,
        )
        with patch.dict(OFFICIAL_EXTENSION_IDS_BY_STORE, {"chrome_web_store": frozenset({"a" * 32})}):
            with patch.dict(os.environ, {"DIAN_AGENT_PUBLISHED_BROWSER_STORES": "chrome_web_store"}, clear=True):
                result = build_release_readiness(
                    self.temp.name,
                    production_ed25519_trust=True,
                    authenticode_artifacts={"agent": True, "updater": True},
                )
        self.assertFalse(result["ready_for_public_release"])
        check = next(item for item in result["checks"] if item["id"] == "platform_code_signature")
        self.assertFalse(check["ready"])
        self.assertEqual(
            {"installer_entry", "upgrade_entry", "maintenance_scripts"},
            {item["id"] for item in check["artifact_checks"] if not item["ready"]},
        )

    def test_offline_versioned_program_layout_resolves_install_root(self) -> None:
        install_root = Path(self.temp.name) / "DianAgent"
        executable = install_root / "versions" / "4.14.9" / "program" / "DianAgent.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"packaged agent")

        with patch.object(sys, "frozen", True, create=True), patch.object(
            sys, "executable", str(executable)
        ):
            release_root = promotion_readiness._release_root_from_executable()

        self.assertEqual(install_root.resolve(), release_root)


class PromotionHttpApiTests(unittest.TestCase):
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
        self.trust_path = Path(self.temp.name) / "config" / "trusted_extension_ids.json"
        self.trust_path.parent.mkdir(parents=True, exist_ok=True)
        self.trust_path.write_text(
            json.dumps({"schema_version": 1, "extension_ids": ["a" * 32]}),
            encoding="utf-8",
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.session_token = self._bootstrap_session()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        http_receiver.DATA_DIR = self.original_data_dir
        self.temp.cleanup()

    def _post(self, path: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Dian-Agent": "2",
                "X-Dian-Agent-Extension-Version": http_receiver.AGENT_VERSION,
                "Origin": f"chrome-extension://{'a' * 32}",
                **({"X-Dian-Agent-Token": self.session_token} if self.session_token else {}),
            },
        )
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())
        return response.status, json.loads(response.read())

    def _get(self, path: str) -> dict[str, object]:
        request = urllib.request.Request(
            self.base_url + path,
            headers={
                "X-Dian-Agent": "2",
                "X-Dian-Agent-Extension-Version": http_receiver.AGENT_VERSION,
                "Origin": f"chrome-extension://{'a' * 32}",
                "X-Dian-Agent-Token": self.session_token,
            },
        )
        return json.loads(urllib.request.urlopen(request, timeout=3).read())

    def _bootstrap_session(self) -> str:
        request = urllib.request.Request(
            self.base_url + "/auth/session",
            data=json.dumps({
                "extension_id": "a" * 32,
                "extension_version": http_receiver.AGENT_VERSION,
            }).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Dian-Agent": "2",
                "X-Dian-Agent-Extension-Version": http_receiver.AGENT_VERSION,
                "Origin": f"chrome-extension://{'a' * 32}",
            },
        )
        return str(json.loads(urllib.request.urlopen(request, timeout=3).read())["access_token"])

    def _event(self) -> dict[str, object]:
        return {
            "industry": "apparel",
            "rule_id": "qianchuan.roi_loss",
            "spend_band": "500-1000",
            "roi_band": "1.0-1.5",
            "accepted": True,
            "result": "unknown",
            "agent_version": "4.0.0",
        }

    def test_feedback_api_requires_consent_queues_locally_and_clears(self) -> None:
        status, body = self._post("/telemetry/queue", self._event())
        self.assertEqual(400, status)
        self.assertIn("explicit consent", body["error"])
        self.assertEqual(200, self._post("/telemetry/settings", {"enabled": True})[0])
        status, body = self._post("/telemetry/queue", self._event())
        self.assertEqual(200, status)
        self.assertFalse(body["upload_attempted"])
        telemetry = self._get("/telemetry/status")
        self.assertEqual(1, telemetry["queued_count"])
        status, body = self._post("/telemetry/queue/clear", {"confirm": True})
        self.assertEqual(200, status)
        self.assertEqual(1, body["removed"])
        self.assertFalse(body["shop_data_removed"])

    def test_extension_source_and_release_readiness_apis(self) -> None:
        self.trust_path.unlink()
        self.session_token = ""
        status, body = self._post(
            "/distribution/extension-source",
                    {"source": "unpacked", "browser": "edge", "version": "4.14.9", "extension_id": "a" * 32},
        )
        self.assertEqual(403, status)
        self.assertEqual("extension_origin_not_trusted", body["error"])
        self.trust_path.write_text(
            json.dumps({"schema_version": 1, "extension_ids": ["a" * 32]}),
            encoding="utf-8",
        )
        self.session_token = self._bootstrap_session()
        status, body = self._post(
            "/distribution/extension-source",
                    {"source": "unpacked", "browser": "edge", "version": "4.14.9", "extension_id": "a" * 32},
        )
        self.assertEqual(200, status)
        self.assertEqual("unpacked", body["extension"]["source"])
        self.assertTrue(body["extension"]["origin_verified"])
        distribution = self._get("/distribution/status")
        self.assertEqual("edge", distribution["extension"]["browser"])
        readiness = self._get("/release/readiness")
        self.assertFalse(readiness["ready_for_public_release"])
        self.assertIn("browser_store_publication", readiness["blockers"])


if __name__ == "__main__":
    unittest.main()
