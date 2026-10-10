import json
import unittest
from bot.binance_usdm_oos_evidence_importer import (
    EvidenceError, fetch_klines, fetch_funding_events,
)


def bar(ts, high="101", low="99"):
    return [ts, "100", high, low, "100", "1", ts + 59999, "100", 1, "1", "100", "0"]


class TestBinanceUsdmOosImporter(unittest.TestCase):
    def test_contiguous_candles_with_hash_provenance(self):
        def transport(url):
            self.assertIn("fapi.binance.com/fapi/v1/klines", url)
            return json.dumps([bar(1791504000000), bar(1791504060000)]).encode()
        r = fetch_klines(symbol="SOLUSDT", interval="1m",
                         start_utc="2026-10-09T00:00:00Z",
                         end_utc="2026-10-09T00:02:00Z", transport=transport)
        self.assertEqual(len(r["candles"]), 2)
        self.assertEqual(len(r["request_provenance"][0]["sha256"]), 64)
        self.assertFalse(r["live_allowed"])

    def test_missing_candle_rejected(self):
        with self.assertRaisesRegex(EvidenceError, "MISSING_KLINES"):
            fetch_klines(symbol="SOLUSDT", interval="1m",
                         start_utc="2026-10-09T00:00:00Z",
                         end_utc="2026-10-09T00:02:00Z",
                         transport=lambda url: json.dumps([bar(1791504000000)]).encode())

    def test_mismatched_timestamp_rejected(self):
        with self.assertRaisesRegex(EvidenceError, "KLINE_TIME_GAP_OR_MISMATCH"):
            fetch_klines(symbol="SOLUSDT", interval="1m",
                         start_utc="2026-10-09T00:00:00Z",
                         end_utc="2026-10-09T00:01:00Z",
                         transport=lambda url: json.dumps([bar(1791504060000)]).encode())

    def test_unaligned_window_rejected(self):
        with self.assertRaisesRegex(EvidenceError, "INVALID_OR_UNALIGNED_WINDOW"):
            fetch_klines(symbol="SOLUSDT", interval="1m",
                         start_utc="2026-10-09T00:00:01Z",
                         end_utc="2026-10-09T00:02:00Z",
                         transport=lambda url: b"[]")

    def test_invalid_ohlc_rejected(self):
        with self.assertRaisesRegex(EvidenceError, "INVALID_OHLC"):
            fetch_klines(symbol="SOLUSDT", interval="1m",
                         start_utc="2026-10-09T00:00:00Z",
                         end_utc="2026-10-09T00:01:00Z",
                         transport=lambda url: json.dumps([bar(1791504000000, high="98")]).encode())

    def test_funding_events_and_provenance(self):
        def transport(url):
            self.assertIn("/fapi/v1/fundingRate", url)
            return json.dumps([{"fundingTime": 1791504000000,
                                "fundingRate": "0.0001", "markPrice": "100"}]).encode()
        r = fetch_funding_events(symbol="SOLUSDT",
                                 start_utc="2026-10-09T00:00:00Z",
                                 end_utc="2026-10-09T01:00:00Z",
                                 transport=transport)
        self.assertEqual(len(r["events"]), 1)
        self.assertEqual(len(r["request_provenance"][0]["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
