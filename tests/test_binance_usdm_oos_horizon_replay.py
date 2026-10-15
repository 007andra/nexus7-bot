import unittest
from bot.binance_usdm_oos_horizon_replay import replay_horizon


class TestBinanceHorizonReplay(unittest.TestCase):
    def test_partial_minute_fails_closed_without_fetch(self):
        def forbidden(_):
            raise AssertionError("must not fetch")
        result = replay_horizon(symbol="SOLUSDT", side="SHORT",
                                decision_epoch_ms=1791504001000,
                                horizon_minutes=60, entry="100",
                                stop="101", target="98", transport=forbidden)
        self.assertEqual(result["status"], "NET_PROOF_MISSING")
        self.assertEqual(result["reason"], "PARTIAL_MINUTE_DECISION_REQUIRES_FINER_DATA")

    def test_invalid_horizon_fails_closed(self):
        result = replay_horizon(symbol="SOLUSDT", side="SHORT",
                                decision_epoch_ms=1791504000000,
                                horizon_minutes=30, entry="100",
                                stop="101", target="98",
                                transport=lambda _: b"[]")
        self.assertEqual(result["reason"], "UNSUPPORTED_HORIZON")

    def test_empty_history_fails_closed(self):
        result = replay_horizon(symbol="SOLUSDT", side="SHORT",
                                decision_epoch_ms=1791504000000,
                                horizon_minutes=60, entry="100",
                                stop="101", target="98",
                                transport=lambda _: b"[]")
        self.assertEqual(result["status"], "NET_PROOF_MISSING")
        self.assertEqual(result["reason"], "BINANCE_CANDLE_EVIDENCE_ERROR")


if __name__ == "__main__":
    unittest.main()
