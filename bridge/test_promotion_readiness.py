from __future__ import annotations

import os
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import http_receiver
from promotion_readiness import (
    LocalAnonymousFeedbackQueue,
    OFFICIAL_EXTENSION_IDS_BY_STORE,
    build_distribution_status,
    build_release_readiness,
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
                "version": "4.0.0",
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
                {"source": "unpacked", "browser": "chrome", "version": "4.0.0", "token": "secret"},
            )

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
                "windows_authenticode",
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
                "version": "4.0.0",
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
        for source, version in (("unpacked", "4.0.0"), ("chrome_web_store", "3.9.0")):
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
            {"source": "chrome_web_store", "browser": "chrome", "version": "4.0.0", "extension_id": "a" * 32},
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
        check = next(item for item in result["checks"] if item["id"] == "windows_authenticode")
        self.assertFalse(check["ready"])
        self.assertEqual(
            {"installer_entry", "upgrade_entry", "maintenance_scripts"},
            {item["id"] for item in check["artifact_checks"] if not item["ready"]},
        )


class PromotionHttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.original_data_dir = http_receiver.DATA_DIR
        http_receiver.DATA_DIR = Path(self.temp.name) / "data"
        http_receiver.DATA_DIR.mkdir(parents=True)
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

    def _post(self, path: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Dian-Agent": "2",
                "Origin": f"chrome-extension://{'a' * 32}",
            },
        )
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())
        return response.status, json.loads(response.read())

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
        telemetry = json.loads(urllib.request.urlopen(self.base_url + "/telemetry/status").read())
        self.assertEqual(1, telemetry["queued_count"])
        status, body = self._post("/telemetry/queue/clear", {"confirm": True})
        self.assertEqual(200, status)
        self.assertEqual(1, body["removed"])
        self.assertFalse(body["shop_data_removed"])

    def test_extension_source_and_release_readiness_apis(self) -> None:
        status, body = self._post(
            "/distribution/extension-source",
            {"source": "unpacked", "browser": "edge", "version": "4.0.0", "extension_id": "a" * 32},
        )
        self.assertEqual(200, status)
        self.assertEqual("unpacked", body["extension"]["source"])
        self.assertTrue(body["extension"]["origin_verified"])
        distribution = json.loads(urllib.request.urlopen(self.base_url + "/distribution/status").read())
        self.assertEqual("edge", distribution["extension"]["browser"])
        readiness = json.loads(urllib.request.urlopen(self.base_url + "/release/readiness").read())
        self.assertFalse(readiness["ready_for_public_release"])
        self.assertIn("browser_store_publication", readiness["blockers"])


if __name__ == "__main__":
    unittest.main()
