import base64
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    serialization = None
    Ed25519PrivateKey = None
    CRYPTOGRAPHY_AVAILABLE = False

from update_center import (
    DEFAULT_PACK_PATH,
    UpdateCenter,
    UpdateError,
    canonical_pack_bytes,
    compute_pack_sha256,
    merge_knowledge_packs,
)


NOW = datetime(2026, 8, 22, 6, 0, tzinfo=timezone.utc)


class BundledIndustryPackTests(unittest.TestCase):
    def test_bundled_industries_bind_per_store_and_keep_general_guardrails(self):
        with tempfile.TemporaryDirectory() as temp:
            center = UpdateCenter(temp, current_agent_version="4.6.0", now=lambda: NOW)
            apparel = center.bind_industry_pack("store_a", "industry.apparel")
            beauty = center.bind_industry_pack("store_b", "industry.beauty")

            self.assertEqual("apparel", apparel["industry"])
            self.assertEqual("beauty", beauty["industry"])
            apparel_pack = center.load_effective_pack(store_key="store_a")
            beauty_pack = center.load_effective_pack(store_key="store_b")
            apparel_ids = {rule["rule_id"] for rule in apparel_pack["rules"]}
            beauty_ids = {rule["rule_id"] for rule in beauty_pack["rules"]}

            self.assertIn("system.data_stale", apparel_ids)
            self.assertIn("system.data_stale", beauty_ids)
            self.assertIn("apparel.inventory.size_break_risk", apparel_ids)
            self.assertNotIn("beauty.inventory.hero_sku_risk", apparel_ids)
            self.assertIn("beauty.inventory.hero_sku_risk", beauty_ids)
            self.assertNotIn("apparel.inventory.size_break_risk", beauty_ids)

            reopened = UpdateCenter(temp, current_agent_version="4.6.0", now=lambda: NOW)
            self.assertEqual("industry.apparel", reopened.knowledge_catalog(store_key="store_a")["active_pack_id"])
            self.assertEqual("industry.beauty", reopened.knowledge_catalog(store_key="store_b")["active_pack_id"])

    def test_unselected_and_unbound_store_fall_back_to_general(self):
        with tempfile.TemporaryDirectory() as temp:
            center = UpdateCenter(temp, current_agent_version="4.6.0", now=lambda: NOW)
            no_store = center.knowledge_catalog(store_key="")
            unbound = center.knowledge_catalog(store_key="store_a")
            self.assertEqual("general", no_store["active_pack_id"])
            self.assertEqual("store_not_selected", no_store["fallback_reason"])
            self.assertEqual("general", unbound["active_pack_id"])
            self.assertEqual("industry_not_selected", unbound["fallback_reason"])

    def test_industry_cannot_override_protected_system_rule(self):
        base = json.loads(DEFAULT_PACK_PATH.read_text(encoding="utf-8"))
        industry = json.loads((DEFAULT_PACK_PATH.parent / "industry" / "apparel.json").read_text(encoding="utf-8"))
        industry["rules"].append({
            "rule_id": "system.data_stale",
            "conditions": {"field": "data_age_minutes", "operator": ">", "value": 999999},
            "result": {"level": "low", "title": "unsafe override"},
        })
        merged = merge_knowledge_packs(base, industry)
        protected = next(rule for rule in merged["rules"] if rule["rule_id"] == "system.data_stale")
        self.assertEqual("经营数据已过期", protected["result"]["title"])
        self.assertEqual("general", protected["knowledge_layer"])


@unittest.skipUnless(CRYPTOGRAPHY_AVAILABLE, "cryptography is optional")
class SignedIndustryPackLibraryTests(unittest.TestCase):
    def setUp(self):
        self.private_key = Ed25519PrivateKey.generate()
        public_bytes = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.public_key = base64.b64encode(public_bytes).decode("ascii")

    def sign(self, value: dict) -> dict:
        value.pop("sha256", None)
        value.pop("signature", None)
        value["sha256"] = compute_pack_sha256(value)
        value["signature"] = base64.b64encode(
            self.private_key.sign(canonical_pack_bytes(value))
        ).decode("ascii")
        return value

    def pack(self, industry: str, version: str = "1.0.0", *, title: str = "行业提醒"):
        value = {
            "schema_version": 1,
            "pack_id": f"partner.{industry}",
            "industry": industry,
            "display_name": f"{industry} partner pack",
            "channel": "stable",
            "min_agent_version": "4.6.0",
            "pack_version": version,
            "published_at": "2026-08-21T00:00:00+00:00",
            "expires_at": "2027-08-21T00:00:00+00:00",
            "required_metrics": ["sales.last_24h"],
            "rules": [{
                "rule_id": f"{industry}.partner.signal",
                "conditions": {"field": "sales.last_24h", "operator": ">", "value": 0},
                "result": {"level": "medium", "title": title},
            }],
        }
        return self.sign(value)

    def test_import_installs_without_activation_and_same_versions_can_coexist(self):
        with tempfile.TemporaryDirectory() as temp:
            center = UpdateCenter(
                temp,
                current_agent_version="4.6.0",
                public_key=self.public_key,
                now=lambda: NOW,
            )
            apparel = center.import_industry_pack(self.pack("apparel"))
            beauty = center.import_industry_pack(self.pack("beauty"))
            self.assertEqual("installed", apparel["status"])
            self.assertTrue(apparel["activation_required"])
            self.assertEqual("installed", beauty["status"])
            self.assertIsNone(center.store.read_active())
            self.assertEqual(2, len(center.store.installed_paths()))
            self.assertEqual("general", center.knowledge_catalog(store_key="store_a")["active_pack_id"])

            center.bind_industry_pack("store_a", "partner.apparel", pack_version="1.0.0")
            self.assertEqual("partner.apparel", center.knowledge_catalog(store_key="store_a")["active_pack_id"])

    def test_same_pack_id_and_version_with_different_content_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            center = UpdateCenter(
                temp,
                current_agent_version="4.6.0",
                public_key=self.public_key,
                now=lambda: NOW,
            )
            center.import_industry_pack(self.pack("apparel", title="first"))
            with self.assertRaisesRegex(UpdateError, "different content"):
                center.import_industry_pack(self.pack("apparel", title="second"))
            self.assertEqual(1, len(center.store.installed_paths()))

    def test_import_rejects_general_and_protected_system_rules(self):
        general = self.pack("general")
        with tempfile.TemporaryDirectory() as temp:
            center = UpdateCenter(
                temp,
                current_agent_version="4.6.0",
                public_key=self.public_key,
                now=lambda: NOW,
            )
            with self.assertRaisesRegex(UpdateError, "general pack_id and general industry"):
                center.import_industry_pack(general)

            unsafe = self.pack("apparel")
            unsafe["rules"][0]["rule_id"] = "system.unsafe_override"
            self.sign(unsafe)
            with self.assertRaisesRegex(UpdateError, "protected"):
                center.import_industry_pack(unsafe)

    def test_metric_contract_rejects_unknown_declared_metric(self):
        value = self.pack("apparel")
        value["required_metrics"].append("sales.last_24hh")
        self.sign(value)
        with tempfile.TemporaryDirectory() as temp:
            center = UpdateCenter(
                temp,
                current_agent_version="4.6.0",
                public_key=self.public_key,
                now=lambda: NOW,
            )
            with self.assertRaisesRegex(UpdateError, "unsupported facts"):
                center.import_industry_pack(value)

    def test_metric_contract_rejects_unknown_fact_in_nested_operand(self):
        value = self.pack("beauty")
        value["rules"][0]["conditions"] = {
            "field": "sales.last_24h",
            "operator": ">",
            "right": {
                "add": [
                    {"field": "inventory.avaliable"},
                    1,
                ],
            },
        }
        self.sign(value)
        with tempfile.TemporaryDirectory() as temp:
            center = UpdateCenter(
                temp,
                current_agent_version="4.6.0",
                public_key=self.public_key,
                now=lambda: NOW,
            )
            with self.assertRaisesRegex(UpdateError, "unsupported facts"):
                center.import_industry_pack(value)

    def test_metric_contract_rejects_unknown_setting(self):
        value = self.pack("food")
        value["rules"][0]["conditions"] = {
            "field": "sales.last_24h",
            "operator": ">",
            "value_from": "min_spned",
        }
        self.sign(value)
        with tempfile.TemporaryDirectory() as temp:
            center = UpdateCenter(
                temp,
                current_agent_version="4.6.0",
                public_key=self.public_key,
                now=lambda: NOW,
            )
            with self.assertRaisesRegex(UpdateError, "unsupported settings"):
                center.import_industry_pack(value)

    def test_corrupt_installed_pack_is_repaired_by_reimport(self):
        value = self.pack("apparel")
        with tempfile.TemporaryDirectory() as temp:
            center = UpdateCenter(
                temp,
                current_agent_version="4.6.0",
                public_key=self.public_key,
                now=lambda: NOW,
            )
            center.import_industry_pack(value)
            installed_path = center.store.installed_paths()[0]
            corrupt = json.loads(installed_path.read_text(encoding="utf-8"))
            corrupt["signature"] = "A" * 128
            installed_path.write_text(json.dumps(corrupt), encoding="utf-8")

            repaired = center.import_industry_pack(value)

            self.assertFalse(repaired["idempotent"])
            restored = json.loads(installed_path.read_text(encoding="utf-8"))
            self.assertEqual(value["signature"], restored["signature"])
            catalog = center.knowledge_catalog(store_key="store_a")
            record = next(item for item in catalog["packs"] if item["pack_id"] == "partner.apparel")
            self.assertTrue(record["compatible"])

    def test_corrupt_active_general_pack_can_be_reinstalled_at_same_version(self):
        value = self.pack("apparel", version="2026.08.22.2")
        value.update({
            "pack_id": "general",
            "industry": "general",
            "display_name": "signed general pack",
        })
        self.sign(value)
        with tempfile.TemporaryDirectory() as temp:
            center = UpdateCenter(
                temp,
                current_agent_version="4.6.0",
                public_key=self.public_key,
                now=lambda: NOW,
            )
            center.install_local(value)
            corrupt = json.loads(center.store.active_path.read_text(encoding="utf-8"))
            corrupt["signature"] = "A" * 128
            center.store.active_path.write_text(json.dumps(corrupt), encoding="utf-8")

            repaired = center.install_local(value)

            self.assertEqual("activated", repaired["status"])
            restored = json.loads(center.store.active_path.read_text(encoding="utf-8"))
            self.assertEqual(value["signature"], restored["signature"])


if __name__ == "__main__":
    unittest.main()
