import unittest

from bot.binance_collector_manifest_pipeline_v1 import validate_collector_receipts
from bot.binance_coverage_manifest_verifier_v1 import verify_coverage_manifest


def check(records=None, terminal=None):
    return validate_collector_receipts(
        records_by_source=records if records is not None else {
            "BTCUSDT": [{"time": 1001, "id": 1}], "income": []},
        required_sources=["BTCUSDT", "income"], start_ms=1000, end_ms=1010,
        terminal_by_source=terminal if terminal is not None else {
            "BTCUSDT": True, "income": True})


class CollectorManifestPipelineTests(unittest.TestCase):
    def test_end_to_end(self):
        result = check()
        self.assertEqual(result["status"], "COLLECTOR_RECEIPTS_SHAPE_VALID")
        self.assertFalse(result["live_allowed"])
        self.assertEqual(set(result["manifests"]), {"BTCUSDT", "income"})

    def test_missing_source(self):
        self.assertEqual(check(records={"BTCUSDT": []})["status"], "PROOF_MISSING")

    def test_unverified_terminal(self):
        self.assertEqual(check(terminal={"BTCUSDT": True, "income": False})["status"],
                         "PROOF_MISSING")

    def test_tampering_after_receipt(self):
        result = check()
        self.assertEqual(verify_coverage_manifest(
            manifests=result["manifests"], required_sources=["BTCUSDT", "income"],
            start_ms=1000, end_ms=1010,
            records_by_source={"BTCUSDT": [{"time": 1001, "id": 99}],
                               "income": []})["status"], "PROOF_MISSING")


if __name__ == "__main__":
    unittest.main()
