"""Stop/target OHLC offline replay tests; NO Binance connection."""
import unittest

from research.oos_rca_v1.binance_path_replay import evaluate


def candle(ts, o=100, h=101, l=99, c=100):
    return {"ts": ts, "o": o, "h": h, "l": l, "c": c}


def candidate(side="LONG", bars=None, *, cost=True, capture=1):
    entry, stop, target = (100, 95, 110) if side == "LONG" else (100, 105, 90)
    obj = {
        "candidate_id": "A", "symbol": "BTCUSDT", "side": side,
        "captured_epoch": capture, "entry": entry, "stop": stop,
        "target": target,
        "bars": bars if bars is not None else [
            candle(t) for t in range(900, 15300, 900)
        ],
    }
    if cost:
        obj["cost_snapshot"] = {
            "candidate_id": "A", "symbol": "BTCUSDT",
            "exchange": "BINANCE", "observed_at": 1,
            "taker_fee": 0.0005, "entry_slippage": 0.0002,
            "exit_slippage": 0.0003,
        }
    return obj


class TestOfflineBinancePathReplay(unittest.TestCase):
    def test_gross_horizon_close_flat_cost_modeled(self):
        r = evaluate(candidate(), as_of_epoch=18000)
        self.assertEqual([x["exit_reason"] for x in r], ["HORIZON_CLOSE", "HORIZON_CLOSE"])
        self.assertAlmostEqual(r[0]["return_gross_reference"], 0)
        self.assertLess(r[0]["modeled_net_ex_funding"], 0)
        self.assertFalse(r[0]["is_executed_pnl"])

    def test_long_stop_first_ambiguous_bar(self):
        bars = [candle(900, h=112, l=94)] + [
            candle(t) for t in range(1800, 15300, 900)
        ]
        r = evaluate(candidate(bars=bars), as_of_epoch=18000)
        self.assertEqual(r[0]["exit_reason"], "AMBIGUOUS_STOP_FIRST")
        self.assertEqual(r[1]["exit_reason"], "AMBIGUOUS_STOP_FIRST")
        self.assertAlmostEqual(r[0]["return_gross_reference"], -0.05)
        self.assertTrue(r[0]["both_levels_touched"])

    def test_long_stop_gap_worse_than_stop(self):
        bars = [candle(900, o=92, h=95, l=90, c=94)] + [
            candle(t) for t in range(1800, 15300, 900)
        ]
        r = evaluate(candidate(bars=bars), as_of_epoch=18000)
        self.assertEqual(r[0]["exit_reason"], "STOP_GAP")
        self.assertAlmostEqual(r[0]["exit_reference"], 92)
        self.assertLess(r[0]["modeled_net_ex_funding"], -0.08)

    def test_short_stop_first_ambiguous_bar(self):
        bars = [candle(900, h=106, l=89)] + [
            candle(t) for t in range(1800, 15300, 900)
        ]
        r = evaluate(candidate("SHORT", bars=bars), as_of_epoch=18000)
        self.assertEqual(r[0]["exit_reason"], "AMBIGUOUS_STOP_FIRST")
        self.assertAlmostEqual(r[0]["return_gross_reference"], -0.05)

    def test_short_gap_beyond_stop_is_adverse(self):
        bars = [candle(900, o=109, h=110, l=106, c=108)] + [
            candle(t) for t in range(1800, 15300, 900)
        ]
        r = evaluate(candidate("SHORT", bars=bars), as_of_epoch=18000)
        self.assertEqual(r[0]["exit_reason"], "STOP_GAP")
        self.assertEqual(r[0]["exit_reference"], 109)
        self.assertLess(r[0]["modeled_net_ex_funding"], -0.09)

    def test_take_profit_fills_at_target_without_gap_improvement(self):
        bars = [candle(900, o=115, h=116, l=111, c=112)] + [
            candle(t) for t in range(1800, 15300, 900)
        ]
        r = evaluate(candidate(bars=bars), as_of_epoch=18000)
        self.assertEqual(r[0]["exit_reason"], "TARGET")
        self.assertEqual(r[0]["exit_reference"], 110)
        self.assertLess(r[0]["modeled_net_ex_funding"], 0.1)

    def test_60m_matured_240m_not_matured(self):
        r = evaluate(candidate(), as_of_epoch=4500)
        self.assertEqual(r[0]["status"], "MODELED_NET_EX_FUNDING")
        self.assertEqual(r[1]["status"], "NOT_MATURED")
        self.assertIsNone(r[1]["return_gross_reference"])

    def test_missing_intermediate_bar_fail_closed(self):
        bars = [candle(t) for t in range(900, 15300, 900) if t != 2700]
        r = evaluate(candidate(bars=bars), as_of_epoch=18000)
        self.assertEqual(r[0]["status"], "UNKNOWN_CANDLE_GAP")
        self.assertEqual(r[1]["status"], "UNKNOWN_CANDLE_GAP")

    def test_duplicate_bar_timestamp_fail_closed(self):
        c = candidate()
        c["bars"].append(candle(900))
        with self.assertRaisesRegex(ValueError, "DUPLICATE_CANDLE_TIMESTAMP"):
            evaluate(c, as_of_epoch=18000)

    def test_stale_cost_does_not_claim_net(self):
        c = candidate(capture=200)
        r = evaluate(c, as_of_epoch=18000)
        self.assertEqual(r[0]["cost_state"], "COST_NOT_POINT_IN_TIME")
        self.assertIsNone(r[0]["modeled_net_ex_funding"])
        self.assertIsNotNone(r[0]["return_gross_reference"])

    def test_future_cost_snapshot_fail_closed(self):
        c = candidate()
        c["cost_snapshot"]["observed_at"] = 2
        r = evaluate(c, as_of_epoch=18000)
        self.assertIsNone(r[0]["modeled_net_ex_funding"])

    def test_cost_identity_mismatch(self):
        c = candidate()
        c["cost_snapshot"]["candidate_id"] = "OTHER"
        r = evaluate(c, as_of_epoch=18000)
        self.assertIsNone(r[0]["modeled_net_ex_funding"])

    def test_missing_cost_is_not_zero(self):
        r = evaluate(candidate(cost=False), as_of_epoch=18000)
        self.assertEqual(r[0]["status"], "MODELED_GROSS_ONLY")
        self.assertIsNone(r[0]["modeled_net_ex_funding"])
        self.assertEqual(r[0]["funding_status"], "NOT_MODELED")

    def test_bad_price_levels_rejected(self):
        c = candidate()
        c["stop"] = 105
        with self.assertRaisesRegex(ValueError, "INVALID_LONG_LEVELS"):
            evaluate(c, as_of_epoch=18000)

    def test_candle_outside_ohlc_rejected(self):
        c = candidate()
        c["bars"][0]["c"] = 120
        with self.assertRaisesRegex(ValueError, "CANDLE_OHLC_INCONSISTENT"):
            evaluate(c, as_of_epoch=18000)

    def test_candle_ms_and_seconds_same(self):
        c = candidate()
        r1 = evaluate(c, as_of_epoch=18000)
        c["bars"] = [{**x, "ts": x["ts"] * 1000} for x in c["bars"]]
        # Epoch 900_000 is ambiguous if all timestamps shorter than 1e11:
        # enforce proper 13-digit epoch timestamps via real-world epoch.
        c["bars"] = [{**x, "ts": 1791465300000 + (i * 900000)} for i, x in enumerate(c["bars"])]
        c["captured_epoch"] = 1791465200
        c["cost_snapshot"]["observed_at"] = 1791465199
        r2 = evaluate(c, as_of_epoch=1791482000)
        self.assertAlmostEqual(r1[0]["modeled_net_ex_funding"], r2[0]["modeled_net_ex_funding"])


    def test_cohort_decision_and_regime_preserved(self):
        c = candidate()
        c["cohort_decision"] = "COUNTERFACTUAL_REJECTED"
        c["regime"] = "TRENDING_UP"
        c["setup"] = "MOMENTUM"
        rows = evaluate(c, as_of_epoch=18000)
        self.assertEqual(rows[0]["cohort_decision"], "COUNTERFACTUAL_REJECTED")
        self.assertEqual(rows[0]["regime"], "TRENDING_UP")
        self.assertEqual(rows[0]["setup"], "MOMENTUM")
        self.assertEqual(rows[0]["captured_epoch"], c["captured_epoch"])


if __name__ == "__main__":
    unittest.main()
