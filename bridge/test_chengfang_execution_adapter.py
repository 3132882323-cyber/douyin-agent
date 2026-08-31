import unittest

from chengfang_execution_adapter import (
    ChengfangAdapterError,
    ChengfangSimulationAdapter,
    UnavailableOfficialChengfangAdapter,
)


class ChengfangExecutionAdapterTests(unittest.TestCase):
    def test_simulator_is_strictly_labelled_and_never_reports_platform_write(self):
        adapter = ChengfangSimulationAdapter()
        intent = {
            "execution_kind": "simulation",
            "idempotency_key": "idem-1",
            "target_value": 900,
        }
        first = adapter.execute(intent)
        second = adapter.execute(intent)
        self.assertEqual(first["receipt_id"], second["receipt_id"])
        self.assertEqual("simulation", first["execution_kind"])
        self.assertFalse(first["platform_write_attempted"])
        readback = adapter.readback({"adapter_receipt": first})
        self.assertEqual(900, readback["observed_value"])
        self.assertFalse(readback["platform_write_observed"])

    def test_simulator_rejects_live_intent(self):
        with self.assertRaises(ChengfangAdapterError):
            ChengfangSimulationAdapter().execute({"execution_kind": "live"})

    def test_official_adapter_is_explicitly_unavailable(self):
        adapter = UnavailableOfficialChengfangAdapter()
        self.assertFalse(adapter.capabilities()["available"])
        with self.assertRaises(ChengfangAdapterError):
            adapter.execute({})


if __name__ == "__main__":
    unittest.main()
