import hashlib
import json
import unittest
from decimal import Decimal
from bot.binance_usdm_oos_depth_proof import assess_depth

RAW = json.dumps({"bids": [["100", "1"], ["99", "2"]],
                  "asks": [["101", "1"], ["102", "2"]]}).encode()
SHA = hashlib.sha256(RAW).hexdigest()
T = 1791504000000


def assess(**kwargs):
    return assess_depth(side="SHORT", quantity=kwargs.pop("quantity", "2"),
                        raw=kwargs.pop("raw", RAW),
                        sha256=kwargs.pop("sha256", SHA),
                        snapshot_ms=kwargs.pop("snapshot_ms", T + 100),
                        decision_ms=T, **kwargs)


class TestDepthProof(unittest.TestCase):
    def test_short_walks_bid_levels(self):
        r = assess()
        self.assertEqual(r["status"], "DISPLAYED_DEPTH_BOUND")
        self.assertEqual(Decimal(r["weighted_price"]), Decimal("99.5"))
        self.assertFalse(r["hypothetical_fill_proven"])

    def test_insufficient_depth_fails_closed(self):
        self.assertIn("INSUFFICIENT_DISPLAYED_DEPTH", assess(quantity="4")["blockers"])

    def test_modified_source_fails_closed(self):
        self.assertIn("RAW_SHA256_MISMATCH", assess(raw=RAW + b" ")["blockers"])

    def test_predecision_snapshot_fails_closed(self):
        self.assertIn("SNAPSHOT_OUTSIDE_DECISION_WINDOW",
                      assess(snapshot_ms=T - 1)["blockers"])

    def test_invalid_book_order_fails_closed(self):
        raw = json.dumps({"bids": [["99", "1"], ["100", "2"]],
                          "asks": [["101", "3"]]}).encode()
        r = assess(raw=raw, sha256=hashlib.sha256(raw).hexdigest())
        self.assertIn("UNSORTED_OR_DUPLICATE_DEPTH", r["blockers"])


if __name__ == "__main__":
    unittest.main()
