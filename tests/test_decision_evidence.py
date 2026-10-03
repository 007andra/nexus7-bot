import unittest

from bot.decision_evidence import (
    CandleEvidence,
    DecisionEvidenceBundle,
    normalize_candles,
)


class DecisionEvidenceTests(unittest.TestCase):
    def _args(self, candles, decision_ts):
        return dict(
            candidate_id="BTC-1",
            symbol="BTCUSDT",
            side="LONG",
            decision_ts=decision_ts,
            code_sha="abc",
            feature_schema_version="v1",
            feature_fingerprint="deadbeef",
            candles=candles,
            signal={"score": 70},
            decision={"approved": True},
            cost_snapshot={"cost": 0.001},
        )

    def test_bundle_hash_is_deterministic_across_seconds_and_ms(self):
        candles = normalize_candles("15m", [
            {
                "ts": 1_700_000_000,
                "o": 100, "h": 110, "l": 90, "c": 105, "v": 5,
            },
            {
                "ts": 1_700_000_900_000,
                "o": 105, "h": 112, "l": 100, "c": 108, "v": 6,
            },
        ])
        a = DecisionEvidenceBundle(
            **self._args(candles, 1_700_001_800)
        )
        b = DecisionEvidenceBundle(
            **self._args(candles, 1_700_001_800_000)
        )
        self.assertEqual(a.decision_ts, 1_700_001_800_000)
        self.assertEqual(a.bundle_hash, b.bundle_hash)

    def test_microseconds_normalize_to_same_hash(self):
        ms = CandleEvidence(
            "15m", 1_700_000_000_000, 100, 110, 90, 105, 5
        )
        us = CandleEvidence(
            "15m", 1_700_000_000_000_000, 100, 110, 90, 105, 5
        )
        self.assertEqual(ms.ts, us.ts)

    def test_future_candle_fails_closed(self):
        candles = normalize_candles("15m", [
            {
                "ts": 1_700_004_000_000,
                "o": 100, "h": 110, "l": 90, "c": 105, "v": 5,
            },
        ])
        with self.assertRaises(ValueError):
            DecisionEvidenceBundle(
                **self._args(candles, 1_700_003_000_000)
            )


if __name__ == "__main__":
    unittest.main()
