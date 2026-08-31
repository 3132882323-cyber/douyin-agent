from __future__ import annotations

import base64
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_extension_distribution",
    ROOT / "tools" / "check_extension_distribution.py",
)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)


class ExtensionDistributionGateTests(unittest.TestCase):
    def test_checked_in_development_identity_is_stable(self) -> None:
        result = gate.audit_distribution(ROOT, profile="internal")
        self.assertTrue(result["ok"], result["findings"])
        self.assertEqual("obpbbgjamjfkambmhidbjnaoiehfndcj", result["derived_extension_id"])

    def test_current_consumer_channel_is_truthfully_blocked(self) -> None:
        result = gate.audit_distribution(ROOT, profile="consumer", selected_store="chrome_web_store")
        self.assertFalse(result["ok"])
        codes = {finding["code"] for finding in result["findings"]}
        self.assertIn("CONSUMER_CHANNEL_DISABLED", codes)
        self.assertIn("MANUAL_RELOAD_INSTALLER_PATH", codes)
        self.assertIn("EXACT_PRODUCT_VERSION_LOCKSTEP", codes)
        self.assertIn("OFFICIAL_STORE_ID_MISSING", codes)

    def test_store_ready_fixture_requires_store_identity_protocol_and_no_unpacked_installer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("extension", "bridge", "tools"):
                (root / relative).mkdir(parents=True)
            public_key = base64.b64encode(b"test-public-key").decode("ascii")
            extension_id = gate.derive_extension_id(public_key)
            (root / "extension" / "manifest.json").write_text(
                json.dumps({"manifest_version": 3, "version": "9.0.0", "key": public_key}),
                encoding="utf-8",
            )
            (root / "extension" / "distribution.channels.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "development": {
                            "distribution_mode": "unpacked_development",
                            "extension_id": extension_id,
                        },
                        "consumer_release": {
                            "enabled": True,
                            "distribution_mode": "browser_store",
                            "update_owner": "browser_store",
                            "manual_reload_required": False,
                            "includes_unpacked_extension": False,
                            "required_stores": ["chrome_web_store"],
                        },
                        "protocol": {
                            "mode": "negotiated_protocol",
                            "current": 2,
                            "minimum_agent_protocol": 1,
                            "maximum_agent_protocol": 2,
                            "exact_product_version_required": False,
                        },
                        "stores": {
                            "chrome_web_store": {
                                "status": "published",
                                "extension_id": extension_id,
                                "listing_url": f"https://chromewebstore.google.com/detail/dian-agent/{extension_id}",
                                "update_url": "https://clients2.google.com/service/update2/crx",
                            }
                        },
                        "native_messaging": {
                            "enabled": False,
                            "role": "transport_only",
                            "solves_extension_install_or_update": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / "bridge" / "promotion_readiness.py").write_text(
                "OFFICIAL_EXTENSION_IDS_BY_STORE = "
                f"{{'chrome_web_store': frozenset({{{extension_id!r}}})}}\n",
                encoding="utf-8",
            )
            (root / "bridge" / "activation_status.py").write_text(
                "required_extension_protocol = 2\n",
                encoding="utf-8",
            )
            (root / "bridge" / "http_receiver.py").write_text(
                "PROTOCOL_HEADER = 'X-Dian-Agent-Protocol'\n",
                encoding="utf-8",
            )
            (root / "extension" / "bridge-auth.js").write_text(
                "const protocolHeader = 'X-Dian-Agent-Protocol';\n",
                encoding="utf-8",
            )
            (root / "tools" / "install_release.ps1").write_text(
                "Write-Host 'Open the official browser-store listing'\n",
                encoding="utf-8",
            )
            (root / "tools" / "build_release_core.ps1").write_text(
                "Write-Host 'Build signed store upload artifact'\n",
                encoding="utf-8",
            )

            result = gate.audit_distribution(
                root,
                profile="consumer",
                selected_store="chrome_web_store",
            )
            self.assertTrue(result["ok"], result["findings"])


if __name__ == "__main__":
    unittest.main()
