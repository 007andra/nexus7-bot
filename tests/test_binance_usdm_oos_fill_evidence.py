import unittest
from bot.binance_usdm_oos_fill_evidence import validate_fill_evidence

CID = "HARD_GATE_SHADOW:SOLUSDT:SHORT:BOS_BREAK:1"
START = 1791504000000
END = START + 3600000


def record(timestamp, price="100"):
    return {"candidate_id": CID, "venue": "BINANCE_USDM",
            "evidence_kind": "TRADES_REPLAY", "source_sha256": "a" * 64,
            "price": price, "quantity": "1", "timestamp_ms": timestamp,
            "independently_verified": True}


class TestFillEvidence(unittest.TestCase):
    def test_valid_independent_evidence_contract(self):
        result = validate_fill_evidence(candidate_id=CID, decision_epoch_ms=START,
                                        horizon_end_ms=END,
                                        entry=record(START), exit=record(END))
        self.assertEqual(result["status"], "FILL_EVIDENCE_VERIFIED")
        self.assertFalse(result["live_allowed"])

    def test_ohlc_only_rejected(self):
        e = record(START)
        e["evidence_kind"] = "OHLC_TOUCH"
        result = validate_fill_evidence(candidate_id=CID, decision_epoch_ms=START,
                                        horizon_end_ms=END, entry=e, exit=record(END))
        self.assertEqual(result["status"], "NET_PROOF_MISSING")
        self.assertIn("ENTRY_INDEPENDENT_TICK_DATA_MISSING", result["missing"])

    def test_predecision_rejected(self):
        result = validate_fill_evidence(candidate_id=CID, decision_epoch_ms=START,
                                        horizon_end_ms=END,
                                        entry=record(START - 1), exit=record(END))
        self.assertEqual(result["status"], "NET_PROOF_MISSING")

    def test_missing_verification_rejected(self):
        e = record(START)
        e["independently_verified"] = False
        result = validate_fill_evidence(candidate_id=CID, decision_epoch_ms=START,
                                        horizon_end_ms=END, entry=e, exit=record(END))
        self.assertIn("ENTRY_INDEPENDENT_VERIFICATION_MISSING", result["missing"])


if __name__ == "__main__":
    unittest.main()
