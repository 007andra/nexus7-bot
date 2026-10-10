import unittest

from bot.binance_coverage_manifest_builder_v1 import build_manifest
from bot.binance_coverage_manifest_verifier_v1 import verify_coverage_manifest


class ManifestBuilderTests(unittest.TestCase):
    def test_build_verify_round_trip(self):
        records = [{"time": 1002, "id": 7, "commission": "0.01"}]
        result = build_manifest(records=records, start_ms=1000, end_ms=1010,
                                terminal_page_verified=True)
        self.assertEqual(result["status"], "MANIFEST_BUILT")
        verified = verify_coverage_manifest(
            manifests={"BTCUSDT": result["parts"]},
            required_sources=["BTCUSDT"], start_ms=1000, end_ms=1010,
            records_by_source={"BTCUSDT": records})
        self.assertEqual(verified["status"], "MANIFEST_SHAPE_VALID")
        self.assertFalse(verified["live_allowed"])

    def test_no_terminal_evidence(self):
        result = build_manifest(records=[], start_ms=1000, end_ms=1010,
                                terminal_page_verified=False)
        self.assertEqual(result["status"], "PROOF_MISSING")

    def test_outside_window(self):
        result = build_manifest(records=[{"time": 1011}], start_ms=1000,
                                end_ms=1010, terminal_page_verified=True)
        self.assertEqual(result["status"], "PROOF_MISSING")

    def test_mutation_invalidates_receipt(self):
        records = [{"time": 1002, "id": 7}]
        result = build_manifest(records=records, start_ms=1000, end_ms=1010,
                                terminal_page_verified=True)
        verified = verify_coverage_manifest(
            manifests={"BTCUSDT": result["parts"]},
            required_sources=["BTCUSDT"], start_ms=1000, end_ms=1010,
            records_by_source={"BTCUSDT": [{"time": 1002, "id": 8}]})
        self.assertEqual(verified["status"], "PROOF_MISSING")


if __name__ == "__main__":
    unittest.main()
