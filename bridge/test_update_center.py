import base64
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

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
    RollbackError,
    UpdateCenter,
    UpdateError,
    _secure_download,
    canonical_pack_bytes,
    channel_allows,
    compute_pack_sha256,
    create_opt_in_telemetry,
    locate_default_pack_path,
    merge_knowledge_packs,
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

    def _builtin(self, **overrides):
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
        pack.update(overrides)
        pack["sha256"] = compute_pack_sha256(pack)
        return pack

    def test_secure_download_rejects_https_redirect_downgrade(self):
        class FakeResponse:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return "http://updates.example.test/pack.json"

            def read(self, _limit):
                raise AssertionError("downgraded response body must not be read")

        with patch("update_center.urlopen", return_value=FakeResponse()):
            with self.assertRaisesRegex(UpdateError, "redirect must remain on HTTPS"):
                _secure_download("https://updates.example.test/manifest.json", 5, 1024)

    def test_secure_download_rejects_embedded_url_credentials(self):
        with self.assertRaisesRegex(UpdateError, "HTTPS URL"):
            _secure_download("https://user:password@updates.example.test/manifest.json", 5, 1024)

    def test_secure_download_closes_http_error_response(self):
        response = Mock()
        error = HTTPError("https://updates.example.test/manifest.json", 503, "unavailable", {}, response)
        with patch("update_center.urlopen", side_effect=error):
            with self.assertRaisesRegex(UpdateError, "download failed"):
                _secure_download("https://updates.example.test/manifest.json", 5, 1024)
        response.close.assert_called_once_with()

    @staticmethod
    def _rule(rule_id, title, *, overridable=None):
        rule = {
            "rule_id": rule_id,
            "conditions": {"field": "metric", "operator": "exists"},
            "result": {
                "level": "medium",
                "title": title,
                "action": {"type": "manual_review", "label": "人工复核"},
            },
        }
        if overridable is not None:
            rule["overridable"] = overridable
        return rule

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

    def test_update_json_and_signed_content_reject_nonfinite_numbers(self):
        pack = self._pack()
        pack["metadata"] = {"score": float("nan")}
        with self.assertRaisesRegex(PackValidationError, "finite JSON"):
            compute_pack_sha256(pack)

        with tempfile.TemporaryDirectory() as temp:
            center = UpdateCenter(
                temp,
                current_agent_version="3.8.0",
                public_key=self.public_key,
                downloader=lambda url, timeout, maximum: b'{"score":NaN}',
                now=lambda: NOW,
            )
            with self.assertRaisesRegex(UpdateError, "valid UTF-8 JSON"):
                center._get_json("https://updates.example/manifest.json")

    def test_post_commit_backup_prune_failure_reports_success_with_warning(self):
        old_pack = self._pack("1.0.0")
        new_pack = self._pack("2.0.0")
        with tempfile.TemporaryDirectory() as temp:
            center = UpdateCenter(
                temp,
                current_agent_version="3.8.0",
                public_key=self.public_key,
                now=lambda: NOW,
            )
            center.store.activate(old_pack, backup_current=False)

            with patch.object(center.store, "_prune", side_effect=OSError("backup is busy")):
                result = center.install_local(new_pack)

            self.assertTrue(result["ok"])
            self.assertEqual("activated", result["status"])
            self.assertEqual("2.0.0", result["pack_version"])
            self.assertEqual("2.0.0", center.store.read_active()["pack_version"])
            self.assertEqual(
                "BACKUP_PRUNE_DEFERRED",
                result["maintenance_warnings"][0]["code"],
            )

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

    def test_writable_active_pack_cannot_self_claim_builtin_trust(self):
        fallback = self._builtin(pack_version="1.0.0")
        forged = self._builtin(pack_version="2.0.0")
        with tempfile.TemporaryDirectory() as temp:
            builtin_path = Path(temp) / "bundled-default.json"
            builtin_path.write_text(json.dumps(fallback), encoding="utf-8")
            center = UpdateCenter(temp, current_agent_version="3.8.0", now=lambda: NOW)
            center.store.active_path.parent.mkdir(parents=True, exist_ok=True)
            center.store.active_path.write_text(json.dumps(forged), encoding="utf-8")

            effective = center.load_effective_pack(builtin_path)

            self.assertEqual("1.0.0", effective["pack_version"])
            raw, verified = center._validated_active_general()
            self.assertEqual("2.0.0", raw["pack_version"])
            self.assertIsNone(verified)

    def test_writable_backup_cannot_self_claim_builtin_trust(self):
        forged = self._builtin(pack_version="2.0.0")
        with tempfile.TemporaryDirectory() as temp:
            center = UpdateCenter(temp, current_agent_version="3.8.0", now=lambda: NOW)
            center.store.backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = center.store.backup_dir / "forged-2.0.0.json"
            backup_path.write_text(json.dumps(forged), encoding="utf-8")

            candidates = center.rollback_candidates()

            self.assertEqual(1, len(candidates))
            self.assertFalse(candidates[0]["usable"])
            self.assertIn("cannot claim", candidates[0]["reason"])
            with self.assertRaisesRegex(RollbackError, "no valid"):
                center.rollback(pack_version="2.0.0")

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

    def test_local_signed_industry_packs_install_without_changing_global_base(self):
        first = self._pack("1.0.0", pack_id="vendor.apparel", industry="apparel")
        second = self._pack("1.1.0", pack_id="vendor.beauty", metadata={"industry": "beauty"})
        with tempfile.TemporaryDirectory() as temp:
            center = UpdateCenter(
                temp,
                current_agent_version="4.6.0",
                public_key=self.public_key,
                now=lambda: NOW,
            )
            first_result = center.import_industry_pack(first)
            second_result = center.import_industry_pack(second)
            self.assertEqual("installed", first_result["status"])
            self.assertEqual("apparel", first_result["industry"])
            self.assertEqual("beauty", second_result["industry"])
            self.assertIsNone(center.store.read_active())

            center.bind_industry_pack("store_a", "vendor.apparel")
            center.bind_industry_pack("store_b", "vendor.beauty")
            self.assertEqual("vendor.apparel", center.store.read_binding("store_a")["pack_id"])
            self.assertEqual("vendor.beauty", center.store.read_binding("store_b")["pack_id"])

    def test_post_commit_installed_pack_prune_failure_reports_success_with_warning(self):
        industry = self._pack(
            "1.0.0", pack_id="vendor.apparel", industry="apparel"
        )
        with tempfile.TemporaryDirectory() as temp:
            center = UpdateCenter(
                temp,
                current_agent_version="4.6.0",
                public_key=self.public_key,
                now=lambda: NOW,
            )
            with patch.object(
                center.store,
                "_prune_installed",
                side_effect=OSError("installed-pack cleanup is busy"),
            ):
                result = center.import_industry_pack(industry)

            self.assertTrue(result["ok"])
            self.assertEqual("installed", result["status"])
            self.assertEqual(1, len(center.store.installed_paths()))
            self.assertEqual(
                "INSTALLED_PACK_PRUNE_DEFERRED",
                result["maintenance_warnings"][0]["code"],
            )
            self.assertTrue(
                result["maintenance_warnings"][0]["pack_install_committed"]
            )

    def test_legacy_industry_active_pack_never_becomes_unbound_store_base(self):
        general_rule = self._rule("base.keep", "通用规则")
        industry_rule = self._rule("apparel.only", "服饰规则")
        builtin = self._builtin(
            industry="general",
            pack_id="general",
            rules=[general_rule],
        )
        legacy_industry = self._pack(
            "2.0.0",
            industry="apparel",
            pack_id="legacy.apparel",
            rules=[industry_rule],
        )
        with tempfile.TemporaryDirectory() as temp:
            builtin_path = Path(temp) / "builtin.json"
            builtin_path.write_text(json.dumps(builtin), encoding="utf-8")
            center = UpdateCenter(
                temp,
                current_agent_version="4.6.0",
                public_key=self.public_key,
                now=lambda: NOW,
            )
            center.store.activate(legacy_industry, backup_current=False)

            for store_key in ("store_a", "store_b"):
                with self.subTest(store_key=store_key):
                    effective, status = center.resolve_effective_pack(
                        store_key=store_key,
                        builtin_path=builtin_path,
                    )
                    self.assertEqual("general", effective.get("industry", "general"))
                    self.assertEqual("1.0.0", effective["pack_version"])
                    self.assertEqual(["base.keep"], [rule["rule_id"] for rule in effective["rules"]])
                    self.assertEqual("general", status["active_pack_id"])
                    self.assertEqual("industry_not_selected", status["fallback_reason"])

    def test_industry_pack_cannot_enter_global_base_through_legacy_entrypoints(self):
        general = self._builtin(industry="general", pack_id="general")
        industry = self._pack(
            "2.0.0",
            industry="apparel",
            pack_id="vendor.apparel",
        )
        raw = json.dumps(industry).encode("utf-8")
        manifest = {
            "pack_version": industry["pack_version"],
            "channel": industry["channel"],
            "min_agent_version": industry["min_agent_version"],
            "url": "https://updates.example/industry.json",
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

        with tempfile.TemporaryDirectory() as temp:
            center = UpdateCenter(
                temp,
                current_agent_version="3.8.0",
                public_key=self.public_key,
                downloader=lambda url, timeout, maximum: raw,
                now=lambda: NOW,
            )
            center.store.activate(general, backup_current=False)
            with self.assertRaisesRegex(UpdateError, "industry packs"):
                center.install(manifest)
            self.assertEqual("general", center.store.read_active().get("industry", "general"))
            self.assertEqual("1.0.0", center.store.read_active()["pack_version"])

        with tempfile.TemporaryDirectory() as temp:
            center = UpdateCenter(
                temp,
                current_agent_version="3.8.0",
                public_key=self.public_key,
                now=lambda: NOW,
            )
            center.store.activate(general, backup_current=False)
            with self.assertRaisesRegex(UpdateError, "industry packs"):
                center.install_local(industry)
            self.assertEqual("general", center.store.read_active().get("industry", "general"))
            self.assertEqual("1.0.0", center.store.read_active()["pack_version"])

        with tempfile.TemporaryDirectory() as temp:
            center = UpdateCenter(
                temp,
                current_agent_version="3.8.0",
                public_key=self.public_key,
                now=lambda: NOW,
            )
            center.store.activate(industry, backup_current=False)
            center.store.activate(general)
            with self.assertRaisesRegex(RollbackError, "no valid"):
                center.rollback(pack_version="2.0.0")
            self.assertEqual("general", center.store.read_active().get("industry", "general"))
            self.assertEqual("1.0.0", center.store.read_active()["pack_version"])

    def test_merge_only_overrides_explicitly_overridable_base_rules_and_keeps_layer_versions(self):
        base = self._builtin(
            industry="general",
            pack_id="general",
            rules=[
                self._rule("system.safety", "系统基座"),
                self._rule("qianchuan.locked", "不可覆盖"),
                self._rule("qianchuan.allowed", "允许覆盖", overridable=True),
            ],
        )
        dedupe_shadow = self._rule("apparel.shadow", "通过去重键覆盖")
        dedupe_shadow["result"]["dedupe_key"] = "qianchuan.locked"
        industry = self._pack(
            "2.3.0",
            industry="apparel",
            pack_id="vendor.apparel",
            rules=[
                self._rule("system.safety", "恶意系统覆盖"),
                self._rule("qianchuan.locked", "恶意普通覆盖"),
                dedupe_shadow,
                self._rule("qianchuan.allowed", "行业明确覆盖"),
                self._rule("apparel.new", "行业新增"),
            ],
        )

        merged = merge_knowledge_packs(base, industry)
        rules = {rule["rule_id"]: rule for rule in merged["rules"]}

        self.assertEqual("系统基座", rules["system.safety"]["result"]["title"])
        self.assertEqual("不可覆盖", rules["qianchuan.locked"]["result"]["title"])
        self.assertEqual("行业明确覆盖", rules["qianchuan.allowed"]["result"]["title"])
        self.assertEqual("行业新增", rules["apparel.new"]["result"]["title"])
        self.assertNotIn("apparel.shadow", rules)
        for rule_id in ("system.safety", "qianchuan.locked"):
            self.assertEqual("general", rules[rule_id]["knowledge_layer"])
            self.assertEqual("1.0.0", rules[rule_id]["knowledge_pack_version"])
        for rule_id in ("qianchuan.allowed", "apparel.new"):
            self.assertEqual("industry", rules[rule_id]["knowledge_layer"])
            self.assertEqual("2.3.0", rules[rule_id]["knowledge_pack_version"])

    def test_selecting_general_removes_an_unavailable_binding(self):
        builtin = self._builtin(industry="general", pack_id="general")
        industry = self._pack(
            "2.0.0",
            industry="apparel",
            pack_id="vendor.apparel",
        )
        with tempfile.TemporaryDirectory() as temp:
            builtin_path = Path(temp) / "builtin.json"
            builtin_path.write_text(json.dumps(builtin), encoding="utf-8")
            center = UpdateCenter(
                temp,
                current_agent_version="4.6.0",
                public_key=self.public_key,
                now=lambda: NOW,
            )
            center.import_industry_pack(industry)
            center.bind_industry_pack("store_a", "vendor.apparel")
            for path in center.store.installed_paths():
                path.unlink()

            _, stale_status = center.resolve_effective_pack(
                store_key="store_a",
                builtin_path=builtin_path,
            )
            self.assertIsNotNone(center.store.read_binding("store_a"))
            self.assertEqual("bound_pack_unavailable", stale_status["fallback_reason"])

            result = center.bind_industry_pack("store_a", "general")
            effective, cleared_status = center.resolve_effective_pack(
                store_key="store_a",
                builtin_path=builtin_path,
            )
            self.assertEqual("general", result["pack_id"])
            self.assertIsNone(center.store.read_binding("store_a"))
            self.assertEqual("general", effective.get("industry", "general"))
            self.assertEqual("industry_not_selected", cleared_status["fallback_reason"])

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
                center.import_industry_pack(signed_beta)
            with self.assertRaisesRegex(UpdateError, "signature"):
                center.import_industry_pack(unsigned)

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
