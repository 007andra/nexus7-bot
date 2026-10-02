import unittest

from bot.decision_evidence import (
    DecisionEvidenceBundle,
    normalize_candles,
)


class DecisionEvidenceTests(unittest.TestCase):
    def test_bundle_hash_is_deterministic(self):
        candles = normalize_candles("15m", [
            {"ts": 1000, "o": 100, "h": 110, "l": 90, "c": 105, "v": 5},
            {"ts": 2000, "o": 105, "h": 112, "l": 100, "c": 108, "v": 6},
        ])
        args = dict(
            candidate_id="BTC-1",
            symbol="BTCUSDT",
            side="LONG",
            decision_ts=3000,
            code_sha="abc",
            feature_schema_version="v1",
            feature_fingerprint="deadbeef",
            candles=candles,
            signal={"score": 70},
            decision={"approved": True},
            cost_snapshot={"cost": 0.001},
        )
        a = DecisionEvidenceBundle(**args)
        b = DecisionEvidenceBundle(**args)
        self.assertEqual(a.bundle_hash, b.bundle_hash)

    def test_future_candle_fails_closed(self):
        candles = normalize_candles("15m", [
            {"ts": 4000, "o": 100, "h": 110, "l": 90, "c": 105, "v": 5},
        ])
        with self.assertRaises(ValueError):
            DecisionEvidenceBundle(
                candidate_id="BTC-1", symbol="BTCUSDT", side="LONG",
                decision_ts=3000, code_sha="abc",
                feature_schema_version="v1", feature_fingerprint="x",
                candles=candles, signal={}, decision={}, cost_snapshot={},
            )


if __name__ == "__main__":
    unittest.main()
