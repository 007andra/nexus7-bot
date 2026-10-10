"""Offline tests for manifest window continuity and integrity."""
import unittest

from bot.binance_coverage_manifest_verifier_v1 import verify_coverage_manifest

D = "a" * 64


def run(manifests):
    return verify_coverage_manifest(
        manifests=manifests, required_sources=["BTCUSDT", "income"],
        start_ms=1000, end_ms=1009)


def part(start, end, count=0):
    return {"start_ms": start, "end_ms": end, "record_count": count,
            "terminal_page_verified": True, "sha256": D}


class CoverageManifestTests(unittest.TestCase):
    def test_contiguous_sources(self):
        result = run({"BTCUSDT": [part(1000, 1004), part(1005, 1009)],
                      "income": [part(1000, 1009)]})
        self.assertEqual(result["status"], "MANIFEST_SHAPE_VALID")
        self.assertFalse(result["live_allowed"])

    def test_gap_fails(self):
        result = run({"BTCUSDT": [part(1000, 1004), part(1006, 1009)],
                      "income": [part(1000, 1009)]})
        self.assertEqual(result["status"], "PROOF_MISSING")

    def test_missing_source_fails(self):
        self.assertEqual(run({"BTCUSDT": [part(1000, 1009)]})["status"],
                         "PROOF_MISSING")

    def test_missing_terminal_proof_fails(self):
        bad = {**part(1000, 1009), "terminal_page_verified": False}
        self.assertEqual(run({"BTCUSDT": [bad], "income": [part(1000, 1009)]})["status"],
                         "PROOF_MISSING")

    def test_invalid_digest_fails(self):
        bad = {**part(1000, 1009), "sha256": "invalid"}
        self.assertEqual(run({"BTCUSDT": [bad], "income": [part(1000, 1009)]})["status"],
                         "PROOF_MISSING")


if __name__ == "__main__":
    unittest.main()
