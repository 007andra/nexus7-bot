import hashlib
import json
import unittest
from bot.binance_usdm_oos_source_proof import verify_source_bytes, verify_trade_record

RAW = json.dumps([{"id": 7, "time": 1791504000000, "price": "100",
                   "qty": "1"}]).encode()
SHA = hashlib.sha256(RAW).hexdigest()
CID = "HARD_GATE_SHADOW:SOLUSDT:SHORT:BOS_BREAK:7"
RECORD = {"candidate_id": CID, "venue": "BINANCE_USDM",
          "timestamp_ms": 1791504000000, "trade_id": 7,
          "price": "100", "quantity": "1"}


class TestSourceProof(unittest.TestCase):
    def test_exact_source_hash(self):
        self.assertTrue(verify_source_bytes(raw=RAW, expected_sha256=SHA)["verified"])

    def test_mismatched_bytes(self):
        self.assertFalse(verify_source_bytes(raw=RAW + b" ", expected_sha256=SHA)["verified"])

    def test_trade_bound_to_bytes_not_boolean(self):
        result = verify_trade_record(record=RECORD, raw=RAW, expected_sha256=SHA,
                                     candidate_id=CID, decision_epoch_ms=1791504000000,
                                     horizon_end_ms=1791504060000)
        self.assertTrue(result["historical_trade_observed"])
        self.assertFalse(result["hypothetical_fill_proven"])

    def test_fabricated_trade_rejected(self):
        record = dict(RECORD, trade_id=8)
        result = verify_trade_record(record=record, raw=RAW, expected_sha256=SHA,
                                     candidate_id=CID, decision_epoch_ms=1791504000000,
                                     horizon_end_ms=1791504060000)
        self.assertIn("TRADE_NOT_IN_AUTHENTIC_SOURCE", result["blockers"])


if __name__ == "__main__":
    unittest.main()
