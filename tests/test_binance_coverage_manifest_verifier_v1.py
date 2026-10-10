"""Offline tests for manifest window continuity and integrity."""
import unittest
import hashlib
import json

from bot.binance_coverage_manifest_verifier_v1 import verify_coverage_manifest

RECORDS = {"BTCUSDT": [{"time": 1002, "id": 1}], "income": []}


def digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def run(manifests, records_by_source=None):
    return verify_coverage_manifest(
        manifests=manifests, required_sources=["BTCUSDT", "income"],
        start_ms=1000, end_ms=1009,
        records_by_source=RECORDS if records_by_source is None else records_by_source)


def part(start, end, count=0, rows=None):
    return {"start_ms": start, "end_ms": end, "record_count": count,
            "terminal_page_verified": True, "sha256": digest(rows or [])}


class CoverageManifestTests(unittest.TestCase):
    def test_contiguous_sources(self):
        result = run({"BTCUSDT": [part(1000, 1004, 1, RECORDS["BTCUSDT"]), part(1005, 1009)],
                      "income": [part(1000, 1009)]})
        self.assertEqual(result["status"], "MANIFEST_SHAPE_VALID")
        self.assertFalse(result["live_allowed"])

    def test_gap_fails(self):
        result = run({"BTCUSDT": [part(1000, 1004), part(1006, 1009)],
                      "income": [part(1000, 1009)]})
        self.assertEqual(result["status"], "PROOF_MISSING")

    def test_missing_source_fails(self):
        self.assertEqual(run({"BTCUSDT": [part(1000, 1009, 1, RECORDS["BTCUSDT"])]})["status"],
                         "PROOF_MISSING")

    def test_missing_terminal_proof_fails(self):
        bad = {**part(1000, 1009, 1, RECORDS["BTCUSDT"]), "terminal_page_verified": False}
        self.assertEqual(run({"BTCUSDT": [bad], "income": [part(1000, 1009)]})["status"],
                         "PROOF_MISSING")

    def test_invalid_digest_fails(self):
        bad = {**part(1000, 1009, 1, RECORDS["BTCUSDT"]), "sha256": "invalid"}
        self.assertEqual(run({"BTCUSDT": [bad], "income": [part(1000, 1009)]})["status"],
                         "PROOF_MISSING")


    def test_mutated_raw_record_fails(self):
        manifests = {"BTCUSDT": [part(1000, 1009, 1, RECORDS["BTCUSDT"])],
                     "income": [part(1000, 1009)]}
        changed = {"BTCUSDT": [{"time": 1002, "id": 999}], "income": []}
        self.assertEqual(run(manifests, records_by_source=changed)["status"],
                         "PROOF_MISSING")

    def test_missing_raw_records_fails(self):
        manifests = {"BTCUSDT": [part(1000, 1009, 1, RECORDS["BTCUSDT"])],
                     "income": [part(1000, 1009)]}
        self.assertEqual(run(manifests, records_by_source={"BTCUSDT": [], "income": []})["status"],
                         "PROOF_MISSING")


if __name__ == "__main__":
    unittest.main()
