import base64
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    serialization = None
    Ed25519PrivateKey = None
    CRYPTOGRAPHY_AVAILABLE = False

from update_center import (
    PackValidationError,
    DEFAULT_PACK_PATH,
    UpdateCenter,
    UpdateError,
    canonical_pack_bytes,
    channel_allows,
    compute_pack_sha256,
    create_opt_in_telemetry,
    locate_default_pack_path,
    validate_knowledge_pack,
)


NOW = datetime(2026, 8, 2, 6, 0, tzinfo=timezone.utc)


@unittest.skipUnless(CRYPTOGRAPHY_AVAILABLE, "cryptography is optional")
class UpdateCenterTests(unittest.TestCase):
    def setUp(self):
        self.private_key = Ed25519PrivateKey.generate()
        public_bytes = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.public_key = base64.b64encode(public_bytes).decode("ascii")

    def _pack(self, version="1.0.0", channel="stable", **overrides):
        pack = {
            "schema_version": 1,
            "pack_version": version,
            "channel": channel,
            "min_agent_version": "3.7.0",
            "published_at": "2026-08-01T00:00:00+00:00",
            "expires_at": "2027-08-01T00:00:00+00:00",
            "rules": [],
        }
        pack.update(overrides)
        pack["sha256"] = compute_pack_sha256(pack)
        pack["signature"] = base64.b64encode(self.private_key.sign(canonical_pack_bytes(pack))).decode("ascii")
        return pack

    def _builtin(self):
        pack = {
            "schema_version": 1,
            "pack_version": "1.0.0",
            "channel": "stable",
            "min_agent_version": "3.7.0",
            "published_at": "2026-08-01T00:00:00+00:00",
            "expires_at": "2027-08-01T00:00:00+00:00",
            "trusted_builtin": True,
            "rules": [],
        }
        pack["sha256"] = compute_pack_sha256(pack)
        return pack

    def test_remote_pack_requires_public_key_and_valid_signature(self):
        pack = self._pack()
        with self.assertRaisesRegex(PackValidationError, "public key"):
            validate_knowledge_pack(pack, current_agent_version="3.8.0", source="remote", now=NOW)
        verified = validate_knowledge_pack(
            pack,
            current_agent_version="3.8.0",
            source="remote",
            public_key=self.public_key,
            now=NOW,
        )
        self.assertEqual("1.0.0", verified["pack_version"])

    def test_tampering_expiry_and_minimum_agent_fail_closed(self):
        tampered = self._pack()
        tampered["rules"].append({"rule_id": "tampered"})
        with self.assertRaisesRegex(PackValidationError, "sha256"):
            validate_knowledge_pack(
                tampered,
                current_agent_version="3.8.0",
                source="remote",
                public_key=self.public_key,
                now=NOW,
            )
        resigned_hash_only = self._pack()
        resigned_hash_only["rules"].append({"rule_id": "changed"})
        resigned_hash_only["sha256"] = compute_pack_sha256(resigned_hash_only)
        with self.assertRaisesRegex(PackValidationError, "signature"):
            validate_knowledge_pack(
                resigned_hash_only,
                current_agent_version="3.8.0",
                source="remote",
                public_key=self.public_key,
                now=NOW,
            )
        expired = self._pack(expires_at="2026-08-02T05:00:00+00:00")
        with self.assertRaisesRegex(PackValidationError, "expired"):
            validate_knowledge_pack(
                expired,
                current_agent_version="3.8.0",
                source="remote",
                public_key=self.public_key,
                now=NOW,
            )
        future = self._pack(published_at="2026-08-03T00:00:00+00:00")
        with self.assertRaisesRegex(PackValidationError, "future"):
            validate_knowledge_pack(
                future,
                current_agent_version="3.8.0",
                source="remote",
                public_key=self.public_key,
                now=NOW,
            )
        too_new = self._pack(min_agent_version="4.0.0")
        with self.assertRaisesRegex(PackValidationError, "older than required"):
            validate_knowledge_pack(
                too_new,
                current_agent_version="3.8.0",
                source="remote",
                public_key=self.public_key,
                now=NOW,
            )

    def test_builtin_trust_cannot_be_claimed_by_remote(self):
        builtin = self._builtin()
        verified = validate_knowledge_pack(
            builtin, current_agent_version="3.8.0", source="builtin", now=NOW
        )
        self.assertTrue(verified["trusted_builtin"])
        with self.assertRaisesRegex(PackValidationError, "cannot claim"):
            validate_knowledge_pack(
                builtin,
                current_agent_version="3.8.0",
                source="remote",
                public_key=self.public_key,
                now=NOW,
            )

    def test_signed_but_structurally_invalid_rules_are_rejected(self):
        pack = self._pack(
            rules=[
                {
                    "rule_id": "bad.operator",
                    "conditions": {"field": "roi", "operator": "run", "value": 1},
                    "result": {"level": "high"},
                }
            ]
        )
        with self.assertRaisesRegex(PackValidationError, "rules are invalid"):
            validate_knowledge_pack(
                pack,
                current_agent_version="3.8.0",
                source="remote",
                public_key=self.public_key,
                now=NOW,
            )

    def test_channels_are_one_way(self):
        self.assertTrue(channel_allows("stable", "stable"))
        self.assertFalse(channel_allows("stable", "beta"))
        self.assertTrue(channel_allows("beta", "stable"))
        self.assertTrue(channel_allows("internal", "beta"))

    def test_download_activation_backup_and_rollback(self):
        first = self._pack("1.0.0")
        second = self._pack("1.1.0")
        payloads = {
            "https://updates.example/first.json": json.dumps(first).encode("utf-8"),
            "https://updates.example/second.json": json.dumps(second).encode("utf-8"),
        }

        def download(url, timeout, maximum):
            self.assertEqual(15, timeout)
            self.assertLessEqual(len(payloads[url]), maximum)
            return payloads[url]

        def manifest(url, pack):
            raw = payloads[url]
            return {
                "pack_version": pack["pack_version"],
                "channel": pack["channel"],
                "min_agent_version": pack["min_agent_version"],
                "url": url,
                "sha256": hashlib.sha256(raw).hexdigest(),
            }

        with tempfile.TemporaryDirectory() as temp:
            center = UpdateCenter(
                temp,
                current_agent_version="3.8.0",
                public_key=self.public_key,
                downloader=download,
                now=lambda: NOW,
            )
            center.install(manifest("https://updates.example/first.json", first))
            center.install(manifest("https://updates.example/second.json", second))
            self.assertEqual("1.1.0", center.store.read_active()["pack_version"])
            self.assertEqual(1, len(center.store.backups()))
            result = center.rollback(pack_version="1.0.0")
            self.assertEqual("1.0.0", result["pack_version"])
            self.assertEqual("1.0.0", center.store.read_active()["pack_version"])

    def test_bad_download_hash_does_not_replace_active(self):
        pack = self._pack("1.0.0")
        raw = json.dumps(pack).encode("utf-8")
        manifest = {
            "pack_version": "1.0.0",
            "channel": "stable",
            "min_agent_version": "3.7.0",
            "url": "https://updates.example/pack.json",
            "sha256": "0" * 64,
        }
        with tempfile.TemporaryDirectory() as temp:
            center = UpdateCenter(
                temp,
                current_agent_version="3.8.0",
                public_key=self.public_key,
                downloader=lambda url, timeout, maximum: raw,
                now=lambda: NOW,
            )
            with self.assertRaisesRegex(UpdateError, "hash"):
                center.install(manifest)
            self.assertIsNone(center.store.read_active())

    def test_local_signed_industry_pack_import_and_visible_rollback(self):
        first = self._pack("1.0.0", industry="apparel")
        second = self._pack("1.1.0", metadata={"industry": "beauty"})
        with tempfile.TemporaryDirectory() as temp:
            center = UpdateCenter(
                temp,
                current_agent_version="3.8.0",
                public_key=self.public_key,
                now=lambda: NOW,
            )
            first_result = center.install_local(first)
            second_result = center.install_local(second)
            self.assertEqual("local_signed_import", first_result["install_mode"])
            self.assertEqual("apparel", first_result["industry"])
            self.assertEqual("beauty", second_result["industry"])
            candidates = center.rollback_candidates()
            self.assertEqual("1.0.0", candidates[0]["pack_version"])
            self.assertEqual("apparel", candidates[0]["industry"])
            self.assertTrue(candidates[0]["usable"])

    def test_local_import_never_bypasses_signature_or_channel(self):
        signed_beta = self._pack("1.0.0", channel="beta", industry="apparel")
        unsigned = dict(self._pack("1.1.0", industry="apparel"))
        unsigned.pop("signature")
        with tempfile.TemporaryDirectory() as temp:
            center = UpdateCenter(
                temp,
                current_agent_version="3.8.0",
                public_key=self.public_key,
                channel="stable",
                now=lambda: NOW,
            )
            with self.assertRaisesRegex(UpdateError, "channel"):
                center.install_local(signed_beta)
            with self.assertRaisesRegex(UpdateError, "signature"):
                center.install_local(unsigned)

    def test_effective_pack_falls_back_to_builtin(self):
        builtin = self._builtin()
        with tempfile.TemporaryDirectory() as temp:
            builtin_path = Path(temp) / "builtin.json"
            builtin_path.write_text(json.dumps(builtin), encoding="utf-8")
            center = UpdateCenter(temp, current_agent_version="3.8.0", now=lambda: NOW)
            result = center.load_effective_pack(builtin_path)
            self.assertEqual("1.0.0", result["pack_version"])

    def test_corrupt_active_pack_falls_back_and_can_be_replaced(self):
        builtin = self._builtin()
        remote = self._pack("1.1.0")
        raw = json.dumps(remote).encode("utf-8")
        manifest = {
            "pack_version": remote["pack_version"],
            "channel": remote["channel"],
            "min_agent_version": remote["min_agent_version"],
            "url": "https://updates.example/pack.json",
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as temp:
            builtin_path = Path(temp) / "builtin.json"
            builtin_path.write_text(json.dumps(builtin), encoding="utf-8")
            center = UpdateCenter(
                temp,
                current_agent_version="3.8.0",
                public_key=self.public_key,
                downloader=lambda url, timeout, maximum: raw,
                now=lambda: NOW,
            )
            center.store.active_path.parent.mkdir(parents=True)
            center.store.active_path.write_text("not-json", encoding="utf-8")
            self.assertEqual("1.0.0", center.load_effective_pack(builtin_path)["pack_version"])
            center.install(manifest)
            self.assertEqual("1.1.0", center.store.read_active()["pack_version"])
            self.assertEqual(1, len(list(center.store.backup_dir.glob("*.invalid"))))

    def test_default_pack_path_supports_pyinstaller_onefile(self):
        with tempfile.TemporaryDirectory() as temp:
            bundled = Path(temp) / "assets" / "knowledge" / "default_pack.json"
            bundled.parent.mkdir(parents=True)
            bundled.write_text("{}", encoding="utf-8")
            with patch("update_center.sys._MEIPASS", temp, create=True):
                self.assertEqual(bundled, locate_default_pack_path())

    def test_telemetry_is_strictly_opt_in_and_whitelisted(self):
        payload = {
            "industry": "apparel",
            "rule_id": "qianchuan.roi_loss",
            "spend_band": "500-1000",
            "roi_band": "1.0-1.5",
            "accepted": True,
            "result": "improved",
            "pack_version": "2026.08.02.1",
            "agent_version": "4.0.0",
            "shop_name": "must-not-leave-device",
            "product_title": "must-not-leave-device",
        }
        self.assertIsNone(create_opt_in_telemetry(payload, opted_in=False))
        event = create_opt_in_telemetry(payload, opted_in=True)
        self.assertNotIn("shop_name", event)
        self.assertNotIn("product_title", event)
        self.assertEqual("explicit_opt_in", event["consent"])
        for unsafe in ("shop_13800138000", "服饰内衣旗舰店"):
            with self.subTest(industry=unsafe):
                with self.assertRaisesRegex(ValueError, "approved industry slug"):
                    create_opt_in_telemetry({**payload, "industry": unsafe}, opted_in=True)


class BundledPackTests(unittest.TestCase):
    def test_repository_default_pack_is_hash_valid_and_loadable(self):
        pack = json.loads(DEFAULT_PACK_PATH.read_text(encoding="utf-8"))
        verified = validate_knowledge_pack(
            pack,
            current_agent_version="4.0.0",
            source="builtin",
            now=NOW,
        )
        self.assertEqual("2026.08.02.1", verified["pack_version"])


if __name__ == "__main__":
    unittest.main()
