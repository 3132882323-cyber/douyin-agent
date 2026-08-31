from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from check_public_release import scan_target  # noqa: E402
from prepare_private_local_source import prepare_private_source  # noqa: E402
from prepare_public_source import PublicSourceError, prepare_public_source  # noqa: E402


class BuildDistributionTests(unittest.TestCase):
    def test_runtime_and_build_security_dependencies_are_exactly_pinned(self) -> None:
        runtime = (ROOT / "bridge" / "requirements.txt").read_text(encoding="utf-8").splitlines()
        agent = (ROOT / "bridge" / "requirements-agent.txt").read_text(encoding="utf-8").splitlines()
        build = (ROOT / "bridge" / "requirements-build.txt").read_text(encoding="utf-8").splitlines()
        build_script = (ROOT / "tools" / "build_agent.ps1").read_text(encoding="utf-8")

        self.assertIn("mcp==1.29.0", runtime)
        self.assertIn("cryptography==50.0.0", runtime)
        self.assertEqual(["cryptography==50.0.0"], agent)
        self.assertIn("-r requirements.txt", build)
        self.assertIn("pyinstaller==6.21.0", build)
        self.assertIn("pyinstaller-hooks-contrib==2026.6", build)
        self.assertIn("pip-audit==2.10.1", build)
        for expected in ("6.21.0", "2026.6", "50.0.0", "1.29.0"):
            self.assertIn(expected, build_script)

    def test_windows_agent_embeds_the_canonical_release_version(self) -> None:
        spec = (ROOT / "bridge" / "dian_agent.spec").read_text(encoding="utf-8")
        self.assertIn('(bridge_dir / "version.py").read_text', spec)
        self.assertIn('StringStruct("FileVersion", agent_version)', spec)
        self.assertIn('StringStruct("ProductVersion", agent_version)', spec)
        self.assertIn("version=windows_version", spec)

    def test_public_staging_omits_only_registered_commercial_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "checkout"
            destination = root / "public"
            (source / "bridge").mkdir(parents=True)
            (source / "bridge" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
            (source / "bridge" / "chengfang_autopilot.py").write_text(
                "VALUE = 'internal'\n", encoding="utf-8"
            )
            (source / "bridge" / "backup").mkdir()
            (source / "bridge" / "backup" / "shop.db").write_bytes(b"private shop data")
            (source / "bridge" / "knowledge").mkdir()
            (source / "bridge" / "knowledge" / "remote-pack.json").write_text(
                '{"shop_key":"private"}\n', encoding="utf-8"
            )

            excluded = prepare_public_source(source, destination)

            self.assertEqual(["bridge/chengfang_autopilot.py"], excluded)
            self.assertTrue((destination / "bridge" / "core.py").is_file())
            self.assertFalse((destination / "bridge" / "chengfang_autopilot.py").exists())
            self.assertFalse((destination / "bridge" / "backup").exists())
            self.assertFalse((destination / "bridge" / "knowledge").exists())
            self.assertEqual([], scan_target(destination, source=True))

    def test_public_staging_rejects_unregistered_secret_material(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "checkout"
            source.mkdir()
            (source / "merchant.license").write_text("secret\n", encoding="utf-8")

            with self.assertRaisesRegex(PublicSourceError, "unexpected private material"):
                prepare_public_source(source, root / "public")

    def test_private_staging_is_explicitly_internal_and_keeps_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "checkout"
            destination = root / "private"
            bridge = source / "bridge"
            bridge.mkdir(parents=True)
            for name in (
                "chengfang_autopilot.py",
                "chengfang_autopilot_runtime.py",
                "chengfang_evidence.py",
                "chengfang_official_contract.py",
                "chengfang_official_adapter.py",
                "chengfang_production_controller.py",
                "chengfang_production_targets.py",
            ):
                (bridge / name).write_text("VALUE = 1\n", encoding="utf-8")
            (bridge / "build_flavor.py").write_text("BUILD_FLAVOR='public_community'\n", encoding="utf-8")

            prepare_private_source(source, destination)

            flavor = (destination / "bridge" / "build_flavor.py").read_text(encoding="utf-8")
            self.assertIn('BUILD_FLAVOR = "private_commercial"', flavor)
            self.assertIn("REDISTRIBUTABLE = False", flavor)
            self.assertTrue((destination / "bridge" / "chengfang_autopilot_runtime.py").is_file())

    def test_private_staging_still_rejects_embedded_signing_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "checkout"
            bridge = source / "bridge"
            bridge.mkdir(parents=True)
            marker = "-----BEGIN " + "PRIVATE KEY-----"
            for name in (
                "chengfang_autopilot.py",
                "chengfang_autopilot_runtime.py",
                "chengfang_evidence.py",
                "chengfang_official_contract.py",
                "chengfang_official_adapter.py",
                "chengfang_production_controller.py",
                "chengfang_production_targets.py",
            ):
                content = marker if name == "chengfang_evidence.py" else "VALUE = 1"
                (bridge / name).write_text(content + "\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "embedded private-key material"):
                prepare_private_source(source, root / "private")

    def test_public_fallback_is_read_only_and_fail_closed(self) -> None:
        from chengfang_public_fallback import ChengfangAutopilotRuntime

        runtime = ChengfangAutopilotRuntime("unused")
        summary = runtime.summary()
        self.assertEqual("community", summary["edition"])
        self.assertFalse(summary["commercial_runtime_available"])
        self.assertFalse(summary["write_automation"]["execution_allowed"])
        self.assertEqual("skipped", runtime.evaluate()["status"])
        with self.assertRaisesRegex(ValueError, "commercial runtime"):
            runtime.configure_a2_pilot({})

    def test_build_entries_have_distinct_flavors_and_public_provenance(self) -> None:
        public = (ROOT / "tools" / "build_release.ps1").read_text(encoding="utf-8")
        core = (ROOT / "tools" / "build_release_core.ps1").read_text(encoding="utf-8")
        private = (ROOT / "tools" / "build_private_local.ps1").read_text(encoding="utf-8")
        agent = (ROOT / "tools" / "build_agent.ps1").read_text(encoding="utf-8")
        offline = (ROOT / "tools" / "build_release_bundle.ps1").read_text(encoding="utf-8")

        self.assertIn("prepare_public_source.py", public)
        self.assertIn("verified_sanitized_source", core)
        self.assertIn("commercial_modules_included = $false", core)
        self.assertIn('version_evidence = "exact_match"', core)
        self.assertIn("agent_file_version = $agentFileVersion", core)
        self.assertIn("modern_extension_version = $modernExtensionVersion", core)
        self.assertIn("Assert-BuildPathChainNoReparsePoints", core)
        self.assertIn("prepare_private_local_source.py", private)
        self.assertIn("INTERNAL_COMMERCIAL_BUILD.json", private)
        self.assertIn("redistributable = $false", private)
        self.assertIn('version_evidence = "exact_match"', private)
        self.assertIn("agent_product_version = $agentProductVersion", private)
        self.assertIn("New-SecureBuildDirectory", private)
        self.assertIn("/inheritance:r", private)
        self.assertIn("New-SecureBuildDirectory", public)
        self.assertIn("/inheritance:r", public)
        self.assertIn('Remove-Item -LiteralPath (Join-Path $distDir "PUBLIC_BUILD.json")', agent)
        self.assertIn('dist\\agent\\PUBLIC_BUILD.json', offline)
        self.assertIn('source_boundary -ne "verified_sanitized_source"', offline)
        self.assertIn('commercial_modules_included -ne $false', offline)
        self.assertIn('Copy-Item -LiteralPath $publicBuildSource', offline)

    def test_internal_marker_is_rejected_from_public_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "INTERNAL_COMMERCIAL_BUILD.json").write_text(
                json.dumps({"redistributable": False}), encoding="utf-8"
            )
            findings = scan_target(root)
            self.assertEqual("internal commercial build marker", findings[0].reason)


if __name__ == "__main__":
    unittest.main()
