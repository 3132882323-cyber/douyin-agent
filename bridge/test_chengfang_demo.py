import unittest
from pathlib import Path
import sys

BRIDGE_DIR = str(Path(__file__).resolve().parent)
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

from chengfang_demo import DEMO_FIXTURE_ID, build_a2_demo_fixture


class ChengfangDemoFixtureTests(unittest.TestCase):
    def test_requires_explicit_confirmation(self) -> None:
        with self.assertRaisesRegex(ValueError, "confirm 必须为 true"):
            build_a2_demo_fixture(confirm=False, generated_at_ms=1_700_000_000_000)

    def test_returns_closed_synthetic_lifecycle_without_runtime_persistence(self) -> None:
        result = build_a2_demo_fixture(confirm=True, generated_at_ms=1_700_000_000_000)

        self.assertEqual(DEMO_FIXTURE_ID, result["fixture_id"])
        self.assertTrue(result["synthetic"])
        self.assertTrue(result["demo_fixture"])
        self.assertFalse(result["platform_write_attempted"])
        self.assertFalse(result["platform_write_observed"])
        self.assertFalse(result["runtime_persisted"])
        self.assertFalse(result["real_account_bound"])
        self.assertEqual(
            ["candidate", "review", "simulation", "readback"],
            [item["step"] for item in result["timeline"]],
        )
        self.assertTrue(all(item["state"] == "complete" for item in result["timeline"]))

        runtime = result["runtime"]
        pilot = runtime["a2_pilot"]
        candidate = pilot["candidates"][0]
        execution = pilot["executions"][0]
        receipt = execution["adapter_receipt"]
        readback = execution["readback"]
        for layer in (runtime, pilot, candidate, execution, receipt, readback):
            self.assertTrue(layer["synthetic"])
            self.assertTrue(layer["demo_fixture"])
            self.assertFalse(layer["platform_write_attempted"])

        self.assertEqual("verified", execution["state"])
        self.assertEqual(1000.0, execution["current_value"])
        self.assertEqual(900.0, execution["target_value"])
        self.assertTrue(readback["matched"])
        self.assertFalse(readback["platform_write_observed"])


if __name__ == "__main__":
    unittest.main()
