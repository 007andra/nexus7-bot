"""Research-only self-chained aggTrade archive integrity tests (no network)."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bot import short_down_bos_event_archive_v1 as archive
from scripts import short_down_bos_public_aggtrade_capture_v1 as collector

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

    def test_prepared_anchor_digest_is_not_a_published_external_proof(self):
        import hashlib

        with tempfile.TemporaryDirectory() as d:
            writer = archive.Archive(
                d, symbol=SYMBOL, session_id=SID,
                generation=1, started_ms=BASE_MS)
            writer.append(trade(0), received_ms=BASE_MS + 2)
            writer.append(trade(1), received_ms=BASE_MS + 12)
            writer.seal()
            expected = hashlib.sha256(
                (Path(d) / (SID + ".manifest.json")).read_bytes()
            ).hexdigest()
            receipt = archive.prepare_anchor_receipt(d, session_id=SID)
            self.assertEqual(receipt["status"],
                             "ANCHOR_PREPARED_NOT_PUBLISHED")
            self.assertEqual(receipt["manifest_sha256"], expected)
            self.assertFalse(receipt["github_anchor_published"])
            self.assertFalse(receipt["independent_source_attestation"])
            self.assertFalse(receipt["durable_remote_copy_proven"])
            self.assertFalse(receipt["live_allowed"])
            self.assertNotIn("candidate_id", receipt)

    def test_incomplete_session_cannot_generate_anchor(self):
        with tempfile.TemporaryDirectory() as d:
            result = archive.prepare_anchor_receipt(d, session_id=SID)
            self.assertEqual(result["status"], "ANCHOR_REFUSED")
            self.assertFalse(result["github_anchor_published"])

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

    def test_strict_disk_ceiling_aborts_without_a_sealed_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            # Apply the actual local-disk safety mechanism on tiny fixtures.
            with patch.object(archive, "MAX_SESSION_BYTES", 400):
                writer = archive.Archive(
                    d, symbol=SYMBOL, session_id=SID,
                    generation=1, started_ms=BASE_MS)
                writer.append(trade(0), received_ms=BASE_MS + 2)
                with self.assertRaisesRegex(
                    ValueError, "LOCAL_DISK_BUDGET_EXCEEDED"
                ):
                    writer.append(trade(1), received_ms=BASE_MS + 12)
                self.assertEqual(
                    archive.verify(d, session_id=SID)["status"],
                    "NOT_PROVEN")
                with self.assertRaises(ValueError):
                    writer.seal()

    def test_byte_accounting_matches_sealed_files(self):
        with tempfile.TemporaryDirectory() as d:
            writer = archive.Archive(
                d, symbol=SYMBOL, session_id=SID,
                generation=1, started_ms=BASE_MS)
            for i in range(3):
                writer.append(trade(i), received_ms=BASE_MS + 10 * i + 2)
            manifest = writer.seal()
            data = (Path(d) / (SID + ".0000.jsonl")).read_bytes()
            self.assertEqual(manifest["archive_data_bytes"], len(data))
            self.assertEqual(
                manifest["local_disk_budget_bytes"], archive.MAX_SESSION_BYTES)
            self.assertEqual(
                archive.verify(d, session_id=SID)["status"],
                "SELF_CHAIN_VERIFIED_UNATTESTED")

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


class PublicCaptureDeadlineTests(unittest.TestCase):
    """No network: the terminal short wait must not invalidate a healthy tape."""

    class _Stub:
        def recv(self):
            return "no actual websocket; wait_for is mocked"

    def test_deadline_timeout_after_recent_event_finishes_cleanly(self):
        with patch.object(collector, "_now", side_effect=[99.9, 100.001]):
            with patch.object(collector.asyncio, "wait_for",
                              side_effect=asyncio.TimeoutError):
                output = asyncio.run(collector._receive_or_expire(
                    self._Stub(), deadline=100.0, last_received_at=99.5))
        self.assertIsNone(output)

    def test_full_silence_timeout_must_still_fail_closed(self):
        with patch.object(collector, "_now", side_effect=[85.0, 100.0]):
            with patch.object(collector.asyncio, "wait_for",
                              side_effect=asyncio.TimeoutError):
                with self.assertRaises(asyncio.TimeoutError):
                    asyncio.run(collector._receive_or_expire(
                        self._Stub(), deadline=105.0, last_received_at=85.0))

    def test_stale_terminal_timeout_cannot_be_mislabeled_complete(self):
        with patch.object(collector, "_now", side_effect=[99.9, 100.001]):
            with patch.object(collector.asyncio, "wait_for",
                              side_effect=asyncio.TimeoutError):
                with self.assertRaises(asyncio.TimeoutError):
                    asyncio.run(collector._receive_or_expire(
                        self._Stub(), deadline=100.0, last_received_at=84.0))

    def test_already_expired_recent_session_finishes_without_recv(self):
        with patch.object(collector, "_now", return_value=101.0):
            with patch.object(collector.asyncio, "wait_for") as waiting:
                output = asyncio.run(collector._receive_or_expire(
                    self._Stub(), deadline=100.0, last_received_at=100.5))
        waiting.assert_not_called()
        self.assertIsNone(output)


if __name__ == "__main__":
    unittest.main()
