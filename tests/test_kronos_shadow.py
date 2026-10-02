import math
import unittest

from bot.kronos_shadow import build_candle_payload, summarize_forecast_paths


def _bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


class KronosShadowTests(unittest.TestCase):
    def test_long_forecast_features_are_side_aligned(self):
        paths = [
            [_bar(100, 103, 99, 102), _bar(102, 106, 101, 105)],
            [_bar(100, 101, 97, 98), _bar(98, 99, 95, 96)],
            [_bar(100, 104, 99, 103), _bar(103, 108, 102, 107)],
            [_bar(100, 102, 99, 101), _bar(101, 103, 100, 102)],
        ]
        f = summarize_forecast_paths(
            symbol="BTCUSDT", side="LONG", entry=100, sl=95, tp=106, paths=paths
        )
        self.assertEqual(f.sample_count, 4)
        self.assertEqual(f.direction_probability_pct, 75.0)
        self.assertEqual(f.tp_before_sl_pct, 25.0)
        self.assertEqual(f.sl_before_tp_pct, 25.0)
        self.assertGreater(f.median_return_pct, 0)
        self.assertGreater(f.dispersion_pct, 0)

    def test_short_forecast_features_flip_return_sign(self):
        paths = [
            [_bar(100, 101, 96, 97), _bar(97, 98, 92, 93)],
            [_bar(100, 104, 99, 103), _bar(103, 106, 102, 105)],
        ]
        f = summarize_forecast_paths(
            symbol="ETHUSDT", side="SHORT", entry=100, sl=106, tp=93, paths=paths
        )
        self.assertEqual(f.direction_probability_pct, 50.0)
        self.assertEqual(f.tp_before_sl_pct, 50.0)
        self.assertEqual(f.sl_before_tp_pct, 50.0)
        self.assertTrue(math.isfinite(f.mean_return_pct))

    def test_same_bar_tp_and_sl_is_marked_ambiguous_not_assumed(self):
        paths = [[_bar(100, 107, 94, 101)]]
        f = summarize_forecast_paths(
            symbol="SOLUSDT", side="LONG", entry=100, sl=95, tp=106, paths=paths
        )
        self.assertEqual(f.ambiguous_barrier_pct, 100.0)
        self.assertEqual(f.tp_before_sl_pct, 0.0)
        self.assertEqual(f.sl_before_tp_pct, 0.0)

    def test_invalid_paths_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "no valid forecast paths"):
            summarize_forecast_paths(
                symbol="BTCUSDT",
                side="LONG",
                entry=100,
                sl=95,
                tp=105,
                paths=[[{"open": 100, "high": 90, "low": 95, "close": 99}]],
            )

    def test_candle_payload_accepts_exchange_aliases_and_truncates_context(self):
        candles = [
            {
                "ts": 1_700_000_000_000 + i,
                "o": 100 + i,
                "h": 102 + i,
                "l": 99 + i,
                "c": 101 + i,
                "v": 10 + i,
            }
            for i in range(10)
        ]
        payload = build_candle_payload(candles, max_context=4)
        self.assertEqual(len(payload), 4)
        self.assertEqual(payload[0]["open"], 106)
        self.assertEqual(payload[-1]["close"], 110)


if __name__ == "__main__":
    unittest.main()
