import unittest
from unittest.mock import patch
from bot.binance_usdm_oos_candidate_bridge import evaluate_candidate


class TestCandidateBridge(unittest.TestCase):
    def test_unverified_fills_block_both_horizons(self):
        with patch("bot.binance_usdm_oos_candidate_bridge.replay_horizon",
                   return_value={"status": "PATH_OBSERVED_NET_UNPROVEN",
                                 "path": {"exit_price": "101", "exit": "STOP"}}):
            result = evaluate_candidate(
                symbol="SOLUSDT", candidate_id="immutable-id", side="SHORT",
                decision_epoch_ms=1791504000000, entry="100", stop="101",
                target="98", quantity="1", capital_usdt="20",
                costs={}, proofs={}, transport=lambda _: b"[]")
        self.assertFalse(result["live_allowed"])
        self.assertEqual(set(result["horizons"]), {60, 240})
        for horizon in (60, 240):
            self.assertEqual(result["horizons"][horizon]["status"], "NET_PROOF_MISSING")
            self.assertIn("ENTRY_FILL_UNVERIFIED", result["horizons"][horizon]["missing"])

    def test_missing_path_blocks_net(self):
        with patch("bot.binance_usdm_oos_candidate_bridge.replay_horizon",
                   return_value={"status": "NET_PROOF_MISSING",
                                 "reason": "PARTIAL_MINUTE_DECISION_REQUIRES_FINER_DATA"}):
            result = evaluate_candidate(
                symbol="SOLUSDT", candidate_id="immutable-id", side="SHORT",
                decision_epoch_ms=1791504001000, entry="100", stop="101",
                target="98", quantity="1", capital_usdt="20",
                costs={}, proofs={}, transport=lambda _: b"[]")
        self.assertEqual(result["horizons"][60]["status"], "NET_PROOF_MISSING")
        self.assertNotIn("net", result["horizons"][60])


if __name__ == "__main__":
    unittest.main()
