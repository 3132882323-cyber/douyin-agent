from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import http_receiver
from activation_status import (
    MAX_EXTENSION_MANIFEST_BYTES,
    build_activation_status,
    read_development_extension_version,
    read_installed_extension_version,
)
from install_verification import complete_late_install_verification
from local_api_auth import AUTH_HEADER


EXTENSION_ID = "a" * 32
OTHER_EXTENSION_ID = "b" * 32


class ActivationStatusContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.extension = self.root / "extension-current"
        self.extension.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _manifest(self, value: object) -> None:
        (self.extension / "manifest.json").write_text(
            json.dumps(value), encoding="utf-8"
        )

    def test_versions_distinguish_active_reload_and_unconfirmed_browser(self) -> None:
        self._manifest({"version": "4.14.0", "name": "Dian Agent"})

        active = build_activation_status(
            self.root,
            agent_version="4.14.0",
            reported_extension_version="4.14.0",
        )
        self.assertEqual(
            {
                "activation_contract_version": 1,
                "agent_version": "4.14.0",
                "required_extension_version": "4.14.0",
                "installed_extension_version": "4.14.0",
                "activation": {
                    "state": "active",
                    "ready": True,
                    "reload_required": False,
                },
            },
            active,
        )

        stale = build_activation_status(
            self.root,
            agent_version="4.14.0",
            reported_extension_version="4.13.5",
        )
        self.assertEqual("reload_required", stale["activation"]["state"])
        self.assertTrue(stale["activation"]["reload_required"])
        self.assertFalse(stale["activation"]["ready"])

        unconfirmed = build_activation_status(self.root, agent_version="4.14.0")
        self.assertEqual("installed_unconfirmed", unconfirmed["activation"]["state"])
        self.assertFalse(unconfirmed["activation"]["reload_required"])
        self.assertFalse(unconfirmed["activation"]["ready"])

    def test_missing_invalid_mismatched_and_oversized_manifests_fail_closed(self) -> None:
        missing = build_activation_status(self.root, agent_version="4.14.0")
        self.assertIsNone(missing["installed_extension_version"])
        self.assertEqual("install_or_repair_required", missing["activation"]["state"])

        for document in (
            [],
            {"version": 4.136},
            {"version": "4.13"},
            {"version": "../../secret"},
        ):
            with self.subTest(document=document):
                self._manifest(document)
                self.assertIsNone(read_installed_extension_version(self.root))

        (self.extension / "manifest.json").write_bytes(
            b"{" + (b" " * MAX_EXTENSION_MANIFEST_BYTES) + b"}"
        )
        self.assertIsNone(read_installed_extension_version(self.root))

        self._manifest({"version": "4.13.5"})
        mismatch = build_activation_status(
            self.root,
            agent_version="4.14.0",
            reported_extension_version="4.13.5",
        )
        self.assertEqual("4.13.5", mismatch["installed_extension_version"])
        self.assertEqual("installed_version_mismatch", mismatch["activation"]["state"])
        self.assertFalse(mismatch["activation"]["reload_required"])

    def test_manifest_pointer_cannot_escape_install_root(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside"
        outside.mkdir()
        (outside / "manifest.json").write_text(
            json.dumps({"version": "99.0.0"}), encoding="utf-8"
        )
        shutil.rmtree(self.extension)
        try:
            os.symlink(outside, self.extension, target_is_directory=True)
        except OSError as error:
            shutil.rmtree(outside, ignore_errors=True)
            self.skipTest(f"directory symlinks unavailable: {error}")
        try:
            self.assertIsNone(read_installed_extension_version(self.root))
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_in_root_atomic_extension_pointer_is_supported(self) -> None:
        versioned = self.root / "extension" / "4.14.0"
        versioned.mkdir(parents=True)
        (versioned / "manifest.json").write_text(
            json.dumps({"version": "4.14.0"}), encoding="utf-8"
        )
        shutil.rmtree(self.extension)
        try:
            os.symlink(versioned, self.extension, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"directory symlinks unavailable: {error}")
        self.assertEqual("4.14.0", read_installed_extension_version(self.root))

    def test_development_manifest_is_exact_regular_repo_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source_root = Path(temporary)
            extension = source_root / "extension"
            extension.mkdir()
            manifest = extension / "manifest.json"
            manifest.write_text(json.dumps({"version": "4.14.0"}), encoding="utf-8")
            self.assertEqual(
                "4.14.0", read_development_extension_version(source_root)
            )

            manifest.write_text(json.dumps({"version": "../../secret"}), encoding="utf-8")
            self.assertIsNone(read_development_extension_version(source_root))

            outside = source_root.parent / f"{source_root.name}-outside-manifest.json"
            outside.write_text(json.dumps({"version": "99.0.0"}), encoding="utf-8")
            manifest.unlink()
            try:
                os.symlink(outside, manifest)
            except OSError as error:
                outside.unlink(missing_ok=True)
                self.skipTest(f"file symlinks unavailable: {error}")
            try:
                self.assertIsNone(read_development_extension_version(source_root))
            finally:
                outside.unlink(missing_ok=True)


class ActivationStatusDefaultSourcePathTests(unittest.TestCase):
    def test_real_default_source_path_uses_repository_extension_manifest(self) -> None:
        expected_data = http_receiver.BASE_DIR / "data"
        self.assertEqual(expected_data.resolve(), http_receiver.DATA_DIR.resolve())
        with patch.dict(
            os.environ,
            {"DIAN_AGENT_DATA_DIR": "", "DIAN_AGENT_INSTALL_ROOT": ""},
            clear=False,
        ):
            self.assertEqual(
                http_receiver.BASE_DIR.parent,
                http_receiver._activation_development_source_root(),
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/activation/status",
                    headers={
                        "X-Dian-Agent": "2",
                        "X-Dian-Agent-Extension-Version": http_receiver.AGENT_VERSION,
                    },
                )
                response = urllib.request.urlopen(request, timeout=3)
                body = json.loads(response.read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        manifest_version = str(json.loads(
            (http_receiver.BASE_DIR.parent / "extension" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )["version"])
        self.assertEqual(http_receiver.AGENT_VERSION, manifest_version)
        self.assertEqual(manifest_version, body["installed_extension_version"])
        self.assertEqual("active", body["activation"]["state"])
        self.assertTrue(body["activation"]["ready"])

    def test_source_fallback_is_disabled_for_frozen_custom_or_overridden_layouts(self) -> None:
        with patch.object(http_receiver.sys, "frozen", True, create=True):
            self.assertIsNone(http_receiver._activation_development_source_root())

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            http_receiver, "DATA_DIR", Path(temporary) / "data"
        ):
            http_receiver.DATA_DIR.mkdir()
            self.assertIsNone(http_receiver._activation_development_source_root())

        for variable in ("DIAN_AGENT_DATA_DIR", "DIAN_AGENT_INSTALL_ROOT"):
            with self.subTest(variable=variable), patch.dict(
                os.environ, {variable: "C:\\not-the-repository"}, clear=False
            ):
                self.assertIsNone(http_receiver._activation_development_source_root())


class InstallVerificationClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        extension = self.root / "extension-current"
        extension.mkdir()
        (extension / "manifest.json").write_text(
            json.dumps({"version": "4.14.0"}), encoding="utf-8"
        )
        self.verification = self.root / "data" / "runtime" / "install-verification.json"
        self.verification.parent.mkdir(parents=True)
        self.started_at = "2026-08-26T08:00:00+00:00"
        self.checked_at = "2026-08-26T08:01:00+00:00"
        self.reported_at = "2026-08-26T09:00:00+00:00"
        self._pending()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _pending(self, **updates: object) -> None:
        value: dict[str, object] = {
            "schema_version": 1,
            "state": "extension_report_required",
            "target_version": "4.14.0",
            "started_at": self.started_at,
            "checked_at": self.checked_at,
            "accepted_extension_id": None,
            "reason": "report_stale",
        }
        value.update(updates)
        self.verification.write_text(json.dumps(value), encoding="utf-8")

    def _report(self, **updates: object) -> dict[str, object]:
        value: dict[str, object] = {
            "version": "4.14.0",
            "extension_id": EXTENSION_ID,
            "reported_at": self.reported_at,
            "origin_verified": True,
        }
        value.update(updates)
        return value

    def _complete(self, report: dict[str, object] | None = None, **updates: object) -> bool:
        values = {
            "authenticated_subject": EXTENSION_ID,
            "origin_extension_id": EXTENSION_ID,
            "origin_trusted": True,
        }
        values.update(updates)
        return complete_late_install_verification(
            self.root,
            report or self._report(),
            **values,
        )

    def test_late_authenticated_report_atomically_closes_pending_audit(self) -> None:
        self.assertTrue(self._complete())
        value = json.loads(self.verification.read_text(encoding="utf-8"))
        self.assertEqual(
            {
                "schema_version": 1,
                "state": "verified",
                "target_version": "4.14.0",
                "started_at": self.started_at,
                "checked_at": self.checked_at,
                "reported_at": self.reported_at,
                "accepted_extension_id": EXTENSION_ID,
                "accepted_scope": "late_authenticated_report",
            },
            value,
        )

    def test_public_activation_read_cannot_mutate_pending_audit(self) -> None:
        before = self.verification.read_bytes()
        status = build_activation_status(
            self.root,
            agent_version="4.14.0",
            reported_extension_version="4.14.0",
        )
        self.assertTrue(status["activation"]["ready"])
        self.assertEqual(before, self.verification.read_bytes())

    def test_untrusted_mismatched_or_unverified_reports_cannot_close_audit(self) -> None:
        cases = (
            ({"origin_trusted": False}, self._report()),
            ({"authenticated_subject": OTHER_EXTENSION_ID}, self._report()),
            ({"origin_extension_id": OTHER_EXTENSION_ID}, self._report()),
            ({}, self._report(origin_verified=False)),
            ({}, self._report(extension_id=OTHER_EXTENSION_ID)),
            ({}, self._report(version="4.13.5")),
            ({}, self._report(reported_at="not-a-time")),
        )
        for arguments, report in cases:
            with self.subTest(arguments=arguments, report=report):
                self._pending()
                before = self.verification.read_bytes()
                self.assertFalse(self._complete(report, **arguments))
                self.assertEqual(before, self.verification.read_bytes())

    def test_target_installed_version_and_pending_schema_must_all_match(self) -> None:
        self._pending(target_version="4.13.5")
        self.assertFalse(self._complete())

        self._pending(schema_version=2)
        self.assertFalse(self._complete())

        self._pending(state="health_checking")
        self.assertFalse(self._complete())

        self._pending()
        (self.root / "extension-current" / "manifest.json").write_text(
            json.dumps({"version": "4.13.5"}), encoding="utf-8"
        )
        self.assertFalse(self._complete())

    def test_existing_verified_receipt_is_immutable(self) -> None:
        self._pending(
            state="verified",
            reported_at="2026-08-26T08:02:00+00:00",
            accepted_extension_id=EXTENSION_ID,
            accepted_scope="manifest_id_fresh_install",
        )
        before = self.verification.read_bytes()
        self.assertFalse(self._complete())
        self.assertEqual(before, self.verification.read_bytes())


class ActivationStatusHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        extension = self.root / "extension-current"
        extension.mkdir()
        (extension / "manifest.json").write_text(
            json.dumps({"version": http_receiver.AGENT_VERSION}), encoding="utf-8"
        )
        config = self.root / "config"
        config.mkdir()
        (config / "trusted_extension_ids.json").write_text(
            json.dumps({"schema_version": 1, "extension_ids": [EXTENSION_ID]}),
            encoding="utf-8",
        )
        self.original_data_dir = http_receiver.DATA_DIR
        http_receiver.DATA_DIR = self.root / "data"
        http_receiver.DATA_DIR.mkdir()
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

    def _request(self, *, origin: str, version: str, method: str = "GET") -> tuple[int, dict, dict]:
        request = urllib.request.Request(
            self.base_url + "/activation/status",
            headers={
                "Origin": origin,
                "X-Dian-Agent": "2",
                "X-Dian-Agent-Extension-Version": version,
            },
            method=method,
        )
        response = urllib.request.urlopen(request, timeout=3)
        raw = response.read()
        return response.status, json.loads(raw) if raw else {}, dict(response.headers)

    def _post(self, path: str, payload: dict, *, token: str = "") -> tuple[int, dict]:
        headers = {
            "Origin": f"chrome-extension://{EXTENSION_ID}",
            "X-Dian-Agent": "2",
            "X-Dian-Agent-Extension-Version": http_receiver.AGENT_VERSION,
            "Content-Type": "application/json",
        }
        if token:
            headers[AUTH_HEADER] = token
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        response = urllib.request.urlopen(request, timeout=3)
        return response.status, json.loads(response.read())

    def test_public_endpoint_is_minimal_tokenless_and_cors_origin_bound(self) -> None:
        trusted_origin = f"chrome-extension://{EXTENSION_ID}"
        status, body, headers = self._request(
            origin=trusted_origin,
            version=http_receiver.AGENT_VERSION,
        )
        self.assertEqual(200, status)
        self.assertEqual(
            {
                "activation_contract_version",
                "agent_version",
                "required_extension_version",
                "installed_extension_version",
                "activation",
            },
            set(body),
        )
        self.assertTrue(body["activation"]["ready"])
        self.assertEqual(trusted_origin, headers.get("Access-Control-Allow-Origin"))
        serialized = json.dumps(body)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(EXTENSION_ID, serialized)
        for forbidden in ("token", "shop", "store", "account", "path"):
            self.assertNotIn(forbidden, serialized.lower())

        untrusted_origin = f"chrome-extension://{OTHER_EXTENSION_ID}"
        status, body, headers = self._request(
            origin=untrusted_origin,
            version=http_receiver.AGENT_VERSION,
        )
        self.assertEqual(200, status)
        self.assertTrue(body["activation"]["ready"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_stale_browser_gets_reload_signal_and_preflight_allows_version_header(self) -> None:
        origin = f"chrome-extension://{EXTENSION_ID}"
        status, body, _headers = self._request(origin=origin, version="4.13.5")
        self.assertEqual(200, status)
        self.assertEqual("reload_required", body["activation"]["state"])
        self.assertTrue(body["activation"]["reload_required"])

        status, _body, headers = self._request(
            origin=origin,
            version="4.13.5",
            method="OPTIONS",
        )
        self.assertEqual(204, status)
        self.assertEqual(origin, headers.get("Access-Control-Allow-Origin"))
        self.assertIn(
            "X-Dian-Agent-Extension-Version",
            headers.get("Access-Control-Allow-Headers", ""),
        )

    def test_marketplace_mode_does_not_expose_local_installation_state(self) -> None:
        with patch.dict(
            os.environ,
            {"DIAN_AGENT_DEPLOYMENT_MODE": "doudian_marketplace"},
            clear=False,
        ):
            request = urllib.request.Request(self.base_url + "/activation/status")
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(request, timeout=3)
        self.assertEqual(403, context.exception.code)

    def test_authenticated_extension_report_closes_late_install_audit(self) -> None:
        verification = self.root / "data" / "runtime" / "install-verification.json"
        verification.parent.mkdir(parents=True)
        verification.write_text(
            json.dumps({
                "schema_version": 1,
                "state": "extension_report_required",
                "target_version": http_receiver.AGENT_VERSION,
                "started_at": "2026-08-26T08:00:00+00:00",
                "checked_at": "2026-08-26T08:01:00+00:00",
                "accepted_extension_id": None,
                "reason": "report_stale",
            }),
            encoding="utf-8",
        )
        before = verification.read_bytes()
        status, activation, _headers = self._request(
            origin=f"chrome-extension://{EXTENSION_ID}",
            version=http_receiver.AGENT_VERSION,
        )
        self.assertEqual(200, status)
        self.assertTrue(activation["activation"]["ready"])
        self.assertEqual(before, verification.read_bytes())
        with self.assertRaises(urllib.error.HTTPError) as context:
            self._post("/activation/status", {"state": "verified"})
        self.assertEqual(401, context.exception.code)
        self.assertEqual(before, verification.read_bytes())

        status, session = self._post(
            "/auth/session",
            {
                "extension_id": EXTENSION_ID,
                "extension_version": http_receiver.AGENT_VERSION,
            },
        )
        self.assertEqual(200, status)
        status, report = self._post(
            "/distribution/extension-source",
            {
                "source": "unpacked",
                "browser": "chrome",
                "version": http_receiver.AGENT_VERSION,
                "extension_id": EXTENSION_ID,
            },
            token=str(session["access_token"]),
        )
        self.assertEqual(200, status)
        self.assertTrue(report["extension"]["origin_verified"])
        audit = json.loads(verification.read_text(encoding="utf-8"))
        self.assertEqual("verified", audit["state"])
        self.assertEqual("late_authenticated_report", audit["accepted_scope"])
        self.assertEqual(EXTENSION_ID, audit["accepted_extension_id"])


if __name__ == "__main__":
    unittest.main()
