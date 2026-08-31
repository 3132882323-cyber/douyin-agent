from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class StartupHealthContractTests(unittest.TestCase):
    def test_windows_startup_and_repair_probes_use_liveness_endpoint(self) -> None:
        watchdog = (ROOT / "bridge" / "watchdog.ps1").read_text(encoding="utf-8-sig")
        setup = (ROOT / "bridge" / "setup_windows.ps1").read_text(encoding="utf-8-sig")

        self.assertIn('$healthUrl = "http://127.0.0.1:$Port/health/live"', watchdog)
        setup_probes = [line for line in setup.splitlines() if "Invoke-RestMethod" in line]
        self.assertGreaterEqual(len(setup_probes), 2)
        self.assertTrue(
            all("http://127.0.0.1:8765/health/live" in line for line in setup_probes),
            setup_probes,
        )
        # The full diagnostic endpoint remains discoverable for users after setup.
        self.assertIn('Health check: http://127.0.0.1:8765/health"', setup)

    def test_windows_watchdogs_preserve_creation_date_startup_budget(self) -> None:
        release_watchdog = (ROOT / "tools" / "watchdog_release.ps1").read_text(
            encoding="utf-8-sig"
        )
        source_watchdog = (ROOT / "bridge" / "watchdog.ps1").read_text(
            encoding="utf-8-sig"
        )

        for name, source, wait_function in (
            (
                "release",
                release_watchdog,
                "Wait-ForExactAgentStartupBudget",
            ),
            (
                "source",
                source_watchdog,
                "Wait-ForExactRuntimeStartupBudget",
            ),
        ):
            with self.subTest(watchdog=name):
                self.assertIn("[int]$StartupTimeoutSeconds", source)
                self.assertIn("CreationDate", source)
                self.assertIn(f"function {wait_function}", source)
                self.assertIn(".AddSeconds($StartupTimeoutSeconds)", source)
                self.assertLess(
                    source.index(f"{wait_function} $"),
                    source.index(
                        "Stop-Process -Id ([int]$stalledProcess.ProcessId)"
                    ),
                )

        self.assertIn(
            "$StartupTimeoutSeconds * 2",
            source_watchdog,
        )

    def test_macos_installers_use_shared_authenticated_readiness(self) -> None:
        verifier = (ROOT / "tools" / "macos" / "verify_local_api.sh").read_text(
            encoding="utf-8"
        )
        for name in (
            "install_dian_agent.command",
            "install_dian_agent_source.command",
        ):
            with self.subTest(script=name):
                source = (ROOT / "tools" / "macos" / name).read_text(encoding="utf-8")
                self.assertIn('source "$VERIFY_HELPER"', source)
                self.assertIn('dian_verify_local_api "$TRUST_RECEIPT" "$VERSION"', source)
        self.assertIn("http://127.0.0.1:8765/health/live", verifier)
        self.assertIn("http://127.0.0.1:8765/auth/session", verifier)
        self.assertIn("http://127.0.0.1:8765/auth/status", verifier)
        self.assertIn("X-Dian-Agent-Extension-Id: $extension_id", verifier)
        self.assertIn("X-Dian-Agent-Extension-Version: $expected_version", verifier)
        self.assertIn("extension_version", verifier)

    def test_macos_repair_uses_liveness_then_authenticated_readiness(self) -> None:
        repair = (ROOT / "tools" / "macos" / "repair_dian_agent.command").read_text(
            encoding="utf-8"
        )
        verifier = (ROOT / "tools" / "macos" / "verify_local_api.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('source "$VERIFY_HELPER"', repair)
        self.assertIn('dian_verify_local_api "$TRUST_RECEIPT" "$VERSION"', repair)
        self.assertIn("http://127.0.0.1:8765/health/live", verifier)
        self.assertIn("http://127.0.0.1:8765/auth/session", verifier)
        self.assertIn("http://127.0.0.1:8765/auth/status", verifier)
        self.assertIn("X-Dian-Agent-Token: $token", verifier)
        self.assertLess(verifier.index("/health/live"), verifier.index("/auth/session"))
        self.assertLess(verifier.index("/auth/session"), verifier.index("/auth/status"))


if __name__ == "__main__":
    unittest.main()
