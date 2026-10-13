"""Research-only self-chained aggTrade archive integrity tests (no network)."""
from pathlib import Path
import tempfile
import unittest

from bot import short_down_bos_event_archive_v1 as archive

SYMBOL = "BTCUSDT"
SID = "research-20261012-0001"
BASE_MS = 1_800_000_000_000


def trade(i, symbol=SYMBOL):
    return {
        "e": "aggTrade", "E": BASE_MS + 10 * i + 2,
        "s": symbol, "a": 5000 + i, "p": "100.01",
        "q": "1.5", "f": 9000 + i, "l": 9000 + i,
        "T": BASE_MS + 10 * i, "m": False,
        "st": 1,
    }


class ArchiveTests(unittest.TestCase):
    def test_integrity_and_authority_flags(self):
        with tempfile.TemporaryDirectory() as d:
            writer = archive.Archive(d, symbol=SYMBOL, session_id=SID,
                                     generation=1, started_ms=BASE_MS)
            writer.append(trade(0), received_ms=BASE_MS + 2)
            writer.append(trade(1), received_ms=BASE_MS + 12)
            manifest = writer.seal()
            self.assertEqual(manifest["event_count"], 2)
            self.assertFalse(manifest["independent_source_attestation"])
            self.assertFalse(manifest["real_exchange_fills_proven"])
            self.assertFalse(manifest["candidate_approval_proven"])
            result = archive.verify(d, session_id=SID)
            self.assertEqual(result["status"], "SELF_CHAIN_VERIFIED_UNATTESTED")
            self.assertEqual(result["event_count"], 2)
            self.assertFalse(result["live_allowed"])
            self.assertFalse(result["promotion_allowed"])
            self.assertEqual(result["execution_effect"], "NONE")
            with self.assertRaises(FileExistsError):
                archive.Archive(d, symbol=SYMBOL, session_id=SID,
                                generation=1, started_ms=BASE_MS)

    def test_modified_event_is_detected_after_seal(self):
        with tempfile.TemporaryDirectory() as d:
            writer = archive.Archive(d, symbol=SYMBOL, session_id=SID,
                                     generation=1, started_ms=BASE_MS)
            for i in range(4):
                writer.append(trade(i), received_ms=BASE_MS + 10 * i + 2)
            writer.seal()
            file = Path(d) / (SID + ".0000.jsonl")
            data = file.read_bytes()
            self.assertIn(b"100.01", data)
            file.write_bytes(data.replace(b"100.01", b"999.01", 1))
            outcome = archive.verify(d, session_id=SID)
            self.assertEqual(outcome["status"], "NOT_PROVEN")

    def test_truncation_is_detected(self):
        with tempfile.TemporaryDirectory() as d:
            writer = archive.Archive(d, symbol=SYMBOL, session_id=SID,
                                     generation=1, started_ms=BASE_MS)
            for i in range(3):
                writer.append(trade(i), received_ms=BASE_MS + 10 * i + 2)
            writer.seal()
            path = Path(d) / (SID + ".0000.jsonl")
            rows = path.read_bytes().splitlines(keepends=True)
            path.write_bytes(b"".join(rows[:-1]))
            self.assertEqual(archive.verify(d, session_id=SID)["status"],
                             "NOT_PROVEN")

    def test_restart_or_missing_end_manifest_is_not_proven(self):
        with tempfile.TemporaryDirectory() as d:
            writer = archive.Archive(d, symbol=SYMBOL, session_id=SID,
                                     generation=1, started_ms=BASE_MS)
            writer.append(trade(0), received_ms=BASE_MS + 2)
            writer.abort()
            self.assertEqual(archive.verify(d, session_id=SID)["status"],
                             "NOT_PROVEN")
            with self.assertRaises(ValueError):
                writer.seal()

    def test_failed_trade_id_time_symbol_or_qty_ends_session(self):
        changes = [
            {"a": 5005}, {"a": 5000},
            {"T": BASE_MS - 1}, {"s": "ETHUSDT"},
            {"p": "NaN"}, {"q": -1}, {"m": 1},
            {"f": 100, "l": 10}, {"e": "bookTicker"},
        ]
        for change in changes:
            with self.subTest(change=change):
                with tempfile.TemporaryDirectory() as d:
                    writer = archive.Archive(
                        d, symbol=SYMBOL, session_id=SID,
                        generation=1, started_ms=BASE_MS)
                    writer.append(trade(0), received_ms=BASE_MS + 2)
                    bad = {**trade(1), **change}
                    with self.assertRaises(ValueError):
                        writer.append(bad, received_ms=BASE_MS + 12)
                    with self.assertRaises(ValueError):
                        writer.append(trade(2), received_ms=BASE_MS + 22)
                    self.assertEqual(
                        archive.verify(d, session_id=SID)["status"], "NOT_PROVEN")

    def test_rolling_segments_preserves_order_and_hash_chain(self):
        with tempfile.TemporaryDirectory() as d:
            writer = archive.Archive(d, symbol=SYMBOL, session_id=SID,
                                     generation=1, started_ms=BASE_MS)
            for i in range(archive.PER_SEGMENT + 2):
                writer.append(trade(i), received_ms=BASE_MS + 10 * i + 2)
            manifest = writer.seal()
            self.assertEqual(len(manifest["segments"]), 2)
            self.assertEqual(manifest["segments"][0]["count"], archive.PER_SEGMENT)
            self.assertEqual(manifest["segments"][1]["count"], 2)
            self.assertEqual(archive.verify(d, session_id=SID)["event_count"],
                             archive.PER_SEGMENT + 2)

    def test_missing_archive_directory_symlinks_and_unsafe_names(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                archive.Archive(Path(d) / "not-created", symbol=SYMBOL,
                                session_id=SID, generation=1, started_ms=BASE_MS)
            for bad in ("../traversal", "Bad Symbol", "x", ""):
                with self.subTest(bad=bad):
                    self.assertEqual(
                        archive.verify(d, session_id=bad)["status"], "NOT_PROVEN")
            with self.assertRaises(ValueError):
                archive.Archive(d, symbol="BTCUSDT", session_id="../traversal",
                                generation=1, started_ms=BASE_MS)

    def test_invalid_recorder_cannot_implicitly_attest_source(self):
        self.assertFalse(archive.AUTHORITY["independent_source_attestation"])
        self.assertFalse(archive.AUTHORITY["durable_remote_copy_proven"])
        self.assertFalse(archive.AUTHORITY["real_exchange_fills_proven"])
        self.assertFalse(archive.AUTHORITY["candidate_approval_proven"])


if __name__ == "__main__":
    unittest.main()
