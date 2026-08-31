from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from check_public_release import scan_target  # noqa: E402
from prepare_private_local_source import prepare_private_source  # noqa: E402
from prepare_public_source import PublicSourceError, prepare_public_source  # noqa: E402


class PublicReleaseBoundaryTests(unittest.TestCase):
    def test_clean_source_and_documented_env_example_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "bridge").mkdir()
            (root / "bridge" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
            (root / ".env.marketplace.example").write_text(
                "CLIENT_ID=replace-me\n", encoding="utf-8"
            )
            self.assertEqual([], scan_target(root, source=True))

    def test_source_rejects_private_paths_and_module_imports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "private").mkdir()
            (root / "private" / "optimizer.py").write_text("pass\n", encoding="utf-8")
            (root / "bridge").mkdir()
            (root / "bridge" / "core.py").write_text(
                "from commercial_private.optimizer import decide\n", encoding="utf-8"
            )
            findings = scan_target(root, source=True)
            reasons = {finding.reason for finding in findings}
            self.assertTrue(any("private capability path" in item for item in reasons))
            self.assertIn("reference to a private capability module", reasons)

    def test_source_rejects_current_commercial_modules_until_they_are_split(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "bridge").mkdir()
            (root / "bridge" / "chengfang_autopilot_runtime.py").write_text(
                "def decide(): return 'commercial'\n", encoding="utf-8"
            )
            findings = scan_target(root, source=True)
            self.assertEqual(1, len(findings))
            self.assertIn("private repository", findings[0].reason)

    def test_javascript_side_effect_and_commonjs_private_imports_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "side-effect.js").write_text(
                "import './private/bootstrap.js';\n", encoding="utf-8"
            )
            (root / "common.cjs").write_text(
                "module.exports = " + "req" + "uire('./enterprise/rules.cjs');\n",
                encoding="utf-8",
            )
            findings = scan_target(root, source=True)
            self.assertEqual(
                2,
                sum(
                    item.reason == "reference to a private capability module"
                    for item in findings
                ),
            )

    def test_source_rejects_env_key_files_and_embedded_private_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env.production").write_text("TOKEN=secret\n", encoding="utf-8")
            (root / "signing.key").write_text("secret\n", encoding="utf-8")
            marker = "-----BEGIN " + "PRIVATE KEY-----"
            (root / "embedded.txt").write_text(marker + "\nsecret\n", encoding="utf-8")
            reasons = [finding.reason for finding in scan_target(root, source=True)]
            self.assertIn("environment/secret file", reasons)
            self.assertIn("credential or private-key file", reasons)
            self.assertIn("embedded private-key material", reasons)

    def test_source_preparers_reject_registered_commercial_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "checkout"
            bridge = source / "bridge"
            bridge.mkdir(parents=True)
            outside = root / "outside.py"
            outside.write_text("VALUE = 'outside'\n", encoding="utf-8")
            for name in (
                "chengfang_autopilot_runtime.py",
                "chengfang_evidence.py",
                "chengfang_official_contract.py",
            ):
                (bridge / name).write_text("VALUE = 1\n", encoding="utf-8")
            link = bridge / "chengfang_autopilot.py"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symbolic links are unavailable: {exc}")

            with self.assertRaisesRegex(PublicSourceError, "symbolic link or reparse"):
                prepare_public_source(source, root / "public")
            with self.assertRaisesRegex(RuntimeError, "symbolic link or reparse"):
                prepare_private_source(source, root / "private")
            self.assertFalse((root / "public").exists())
            self.assertFalse((root / "private").exists())

    def test_source_preparers_reject_a_symlinked_checkout_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "checkout"
            (source / "bridge").mkdir(parents=True)
            linked_source = root / "linked-checkout"
            try:
                linked_source.symlink_to(source, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symbolic links are unavailable: {exc}")

            with self.assertRaisesRegex(PublicSourceError, "symbolic link or reparse"):
                prepare_public_source(linked_source, root / "public")
            with self.assertRaisesRegex(RuntimeError, "symbolic link or reparse"):
                prepare_private_source(linked_source, root / "private")
            self.assertFalse((root / "public").exists())
            self.assertFalse((root / "private").exists())

    def test_public_source_copy_failure_removes_partial_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "checkout"
            (source / "bridge").mkdir(parents=True)
            (source / "bridge" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
            destination = root / "public"

            def fail_after_creating_destination(*args: object, **kwargs: object) -> None:
                destination.mkdir()
                (destination / "partial.txt").write_text("partial\n", encoding="utf-8")
                raise OSError("injected copy failure")

            with mock.patch("prepare_public_source.shutil.copytree", fail_after_creating_destination):
                with self.assertRaisesRegex(OSError, "injected copy failure"):
                    prepare_public_source(source, destination)
            self.assertFalse(destination.exists())

    def test_artifact_zip_is_scanned_by_member_name_and_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "release.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("app/readme.txt", "public\n")
                archive.writestr("enterprise/rules.json", "{}\n")
                archive.writestr(
                    "extension/background.js",
                    "import rules from './commercial-private/rules.js';\n",
                )
            findings = scan_target(archive_path)
            self.assertTrue(any("enterprise/rules.json" in item.location for item in findings))
            self.assertTrue(
                any(item.reason == "reference to a private capability module" for item in findings)
            )

    def test_source_excludes_generated_dist_but_artifact_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_output = root / "dist" / "private"
            private_output.mkdir(parents=True)
            (private_output / "optimizer.py").write_text("pass\n", encoding="utf-8")
            self.assertEqual([], scan_target(root, source=True))
            self.assertTrue(scan_target(root / "dist"))

    def test_release_builders_run_source_and_artifact_checks(self) -> None:
        windows = "\n".join(
            (ROOT / "tools" / name).read_text(encoding="utf-8")
            for name in ("build_release.ps1", "build_release_core.ps1")
        )
        mac_source = (ROOT / "tools" / "build_macos_source_release.ps1").read_text(
            encoding="utf-8"
        )
        mac_native = (ROOT / "tools" / "macos" / "build_release.sh").read_text(
            encoding="utf-8"
        )
        browser = (ROOT / "tools" / "build_browser_packages.ps1").read_text(
            encoding="utf-8"
        )
        offline = (ROOT / "tools" / "build_release_bundle.ps1").read_text(
            encoding="utf-8"
        )
        for source in (windows, mac_source, mac_native, browser):
            self.assertIn("check_public_release.py", source)
            self.assertIn("--source", source)
            self.assertIn("--artifact", source)
        self.assertIn("check_public_release.py", offline)
        self.assertIn("--artifact", offline)
        self.assertIn("PUBLIC_BUILD.json", offline)
        self.assertIn("verified_sanitized_source", offline)
        self.assertIn("prepare_public_source.py", mac_source)
        self.assertIn("--destination $publicSource", mac_source)
        self.assertIn('Join-Path $publicSource "bridge"', mac_source)
        self.assertNotIn('Join-Path $projectDir "bridge") -Filter "*.py"', mac_source)


if __name__ == "__main__":
    unittest.main()
