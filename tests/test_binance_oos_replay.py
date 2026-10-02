import unittest

from bot.binance_oos_replay import (
    adverse_fill,
    fee_return_fraction,
    funding_return_fraction,
    month_range,
)
from bot.binance_research_data import FundingObservation


class BinanceOOSReplayTests(unittest.TestCase):
    def test_month_range_is_inclusive_across_year_boundary(self):
        self.assertEqual(
            month_range("2025-11", "2026-02"),
            ((2025, 11), (2025, 12), (2026, 1), (2026, 2)),
        )

    def test_adverse_fill_is_directionally_conservative(self):
        self.assertGreater(
            adverse_fill(100, "LONG", is_entry=True, slippage_rate=0.001),
            100,
        )
        self.assertLess(
            adverse_fill(100, "LONG", is_entry=False, slippage_rate=0.001),
            100,
        )
        self.assertLess(
            adverse_fill(100, "SHORT", is_entry=True, slippage_rate=0.001),
            100,
        )
        self.assertGreater(
            adverse_fill(100, "SHORT", is_entry=False, slippage_rate=0.001),
            100,
        )

    def test_fee_fraction_counts_entry_and_weighted_exits(self):
        fee = fee_return_fraction(
            100.0,
            [(110.0, 0.5), (120.0, 0.5)],
            0.001,
        )
        self.assertAlmostEqual(fee, 0.001 + 0.00115)

    def test_positive_funding_is_long_cost_and_short_credit(self):
        candles = [
            {"ts": 1000, "c": 100.0},
            {"ts": 2000, "c": 100.0},
            {"ts": 3000, "c": 100.0},
        ]
        timestamps = [1000, 2000, 3000]
        events = [FundingObservation(2000, 8, 0.001)]
        long_value, long_count = funding_return_fraction(
            events,
            direction="LONG",
            entry_ts_ms=1000,
            exit_ts_ms=3000,
            entry_fill=100.0,
            candles=candles,
            timestamps=timestamps,
        )
        short_value, short_count = funding_return_fraction(
            events,
            direction="SHORT",
            entry_ts_ms=1000,
            exit_ts_ms=3000,
            entry_fill=100.0,
            candles=candles,
            timestamps=timestamps,
        )
        self.assertEqual(long_count, 1)
        self.assertEqual(short_count, 1)
        self.assertAlmostEqual(long_value, -0.001)
        self.assertAlmostEqual(short_value, 0.001)


if __name__ == "__main__":
    unittest.main()
