"""EXIT_MANAGEMENT_V1 simulator: each LIVE exit rule on hand-built price paths.

Synthetic bars test the mechanics only; they are never evidence.
"""
import unittest

from bot import exit_management_study_v1 as study
from bot import raw_signal_edge_study_v1 as base

T0 = 1_727_740_800_000  # 2024-10-01 00:00 UTC


def bars(*ohlc):
    return [{"ts": T0 + i * 900_000, "o": o, "h": h, "l": lo, "c": c, "v": 1.0}
            for i, (o, h, lo, c) in enumerate(ohlc)]


def sim(k15, direction="LONG", entry=100.0, sl=99.0, tp=102.0, fee=0.0, slip=0.0, **kw):
    return study.simulate_live_exit(direction=direction, planned_entry=entry, planned_sl=sl,
                                    tp=tp, decision_idx=0, k15=k15, funding=[],
                                    fee_rate=fee, slippage=slip, **kw)


class LiveExitRuleTests(unittest.TestCase):
    def test_stop_before_target_in_the_same_bar(self):
        r = sim(bars((100, 102.5, 98.5, 100)))
        self.assertEqual(r["reason"], "STOP")
        self.assertAlmostEqual(r["net_r"], -1.0)

    def test_target_takes_partial_first_then_remainder_at_tp(self):
        r = sim(bars((100, 100.5, 99.5, 100.2), (100.2, 102.2, 100.1, 102)))
        self.assertEqual(r["reason"], "TAKE_PROFIT")
        self.assertTrue(r["partial"])
        # 50% at 1R + 0.03% of entry, 50% at 2R
        self.assertAlmostEqual(r["gross_r"], 0.5 * 1.03 + 0.5 * 2.0)

    def test_partial_then_breakeven_stop(self):
        r = sim(bars((100, 101.2, 99.8, 101.0), (101.0, 101.1, 99.9, 100.0)))
        self.assertTrue(r["partial"])
        self.assertIn(r["reason"], ("BREAKEVEN", "TRAIL"))
        self.assertGreater(r["gross_r"], 0.5)

    def test_trailing_locks_three_quarters_of_peak(self):
        # peak +1.6 (0.8 of target), trail = 100 + 0.75 * 1.6 = 101.2
        r = sim(bars((100, 101.6, 99.9, 101.5), (101.5, 101.55, 101.0, 101.1)))
        self.assertEqual(r["reason"], "TRAIL")
        self.assertAlmostEqual(r["gross_r"], 0.5 * 1.03 + 0.5 * 1.2)

    def test_management_only_protects_from_the_next_bar(self):
        # bar 0 reaches the partial and the trailing trigger (trail 100.9) and
        # falls back below entry in the same bar. The new stops only protect
        # from bar 1, which opens through them, so the remainder exits at that
        # open (99.6), worse than the 100.9 a live mark sample would get. This
        # is the deliberately conservative resolution.
        r = sim(bars((100, 101.2, 99.5, 99.6), (99.6, 99.7, 98.9, 99.0)))
        self.assertTrue(r["partial"])
        self.assertEqual(r["reason"], "TRAIL")
        self.assertAlmostEqual(r["gross_r"], 0.5 * 1.03 + 0.5 * -0.4)

    def test_gap_through_stop_fills_at_open(self):
        r = sim(bars((100, 100.4, 99.6, 100.0), (98.0, 98.5, 97.5, 98.0)))
        self.assertEqual(r["reason"], "STOP")
        self.assertAlmostEqual(r["gross_r"], -2.0)

    def test_fill_tightens_the_stop_and_keeps_the_tp(self):
        # Fill at 100.5 above the planned 100: stop re-anchored to 99.5, TP stays 102.
        r = sim(bars((100.5, 100.6, 99.4, 100.0)))
        self.assertEqual(r["reason"], "STOP")
        self.assertAlmostEqual(r["risk_fraction"], 1.0 / 100.5)
        self.assertAlmostEqual(r["gross_r"], -1.0)

    def test_short_mirror_and_costs_subtract(self):
        r = sim(bars((100, 100.2, 97.9, 98.0)), direction="SHORT", sl=101.0, tp=98.0,
                fee=0.0006, slip=0.0005)
        self.assertEqual(r["reason"], "TAKE_PROFIT")
        self.assertLess(r["net_r"], r["gross_r"])

    def test_max_hold_and_data_end(self):
        flat = bars(*[(100, 100.3, 99.7, 100)] * 10)
        self.assertEqual(sim(flat, max_bars=5)["reason"], "MAX_HOLD")
        self.assertEqual(sim(flat)["reason"], "DATA_END")

    def test_invalid_geometry_is_skipped(self):
        self.assertIsNone(sim(bars((100, 101, 99, 100)), sl=100.0))
        self.assertIsNone(sim(bars((102.5, 103, 102, 102.5))))  # fill beyond the TP


class PreregTests(unittest.TestCase):
    def test_holdout_is_untouched_by_raw_signal_edge_v1(self):
        seen = set(base.PREREG["discovery_months"] + base.PREREG["holdout_months"])
        self.assertFalse(seen & set(study.PREREG["holdout_months"]))
        self.assertEqual(set(study.PREREG["descriptive_months"]), seen)
        self.assertEqual(len(study.prereg_digest()), 64)
        self.assertEqual(study.PREREG["windows"], base.PREREG["windows"])

    def test_verdict_label(self):
        lo, _ = base._month_bounds(study.PREREG["holdout_months"])
        row = {"rr": 2.0, "pullback_pass": True, "funnel_pass": True, "direction": "LONG",
               "entry_type": "PULLBACK", "regime": "TRENDING_UP", "gross_r": 1.0,
               "reason": "TAKE_PROFIT", "partial": True, "bars_held": 4}
        def rows(path_r, cons_r):
            out = []
            for i in range(200):
                ts = lo + i * 86_400_000
                fees = lambda v: {"0.0006": v, "0.0004": v, "0.0005": v}
                out.append(dict(row, symbol=f"S{i % 12}", ts=ts, exit_ts=ts + 1,
                                net_r=fees(path_r + (i % 3) * 0.01),
                                conservative={"exit_ts": ts + 1, "reason": "STOP", "gross_r": 0.0,
                                              "net_r": fees(cons_r + (i % 3) * 0.01)}))
            return out
        report = study.build_report(rows(-0.2, -0.3))
        self.assertEqual(report["verdict"], "EXIT_MANAGEMENT_FAIL")
        self.assertFalse(report["live_allowed"])
        self.assertEqual(study.build_report(rows(0.3, 0.2))["verdict"], "EXIT_MANAGEMENT_PASS")
        self.assertEqual(study.build_report(rows(0.3, -0.2))["verdict"],
                         "EXIT_MANAGEMENT_INCONCLUSIVE_INTRABAR")


class OhlcPathTests(unittest.TestCase):
    def path(self, k15, **kw):
        return sim(k15, mode="OHLC_PATH", **kw)

    def test_intrabar_trailing_exits_at_the_trail(self):
        # down bar walks open -> high -> low: partial at 101.03, trail 100.9, then hit.
        r = self.path(bars((100, 101.2, 99.5, 99.6)))
        self.assertEqual(r["reason"], "TRAIL")
        self.assertAlmostEqual(r["gross_r"], 0.5 * 1.03 + 0.5 * 0.9)

    def test_up_bar_hits_the_low_first(self):
        r = self.path(bars((100, 102.5, 98.5, 100)))
        self.assertEqual(r["reason"], "STOP")
        self.assertAlmostEqual(r["gross_r"], -1.0)

    def test_down_bar_reaches_the_high_first(self):
        r = self.path(bars((100, 102.5, 98.5, 99.9)))
        self.assertEqual(r["reason"], "TAKE_PROFIT")
        self.assertAlmostEqual(r["gross_r"], 0.5 * 1.03 + 0.5 * 2.0)

    def test_short_path_is_mirrored(self):
        r = self.path(bars((100, 100.5, 98.8, 100.4)), direction="SHORT", sl=101.0, tp=98.0)
        # up bar: open -> low 98.8 (partial at 98.97, trail 100 - 0.75*1.2 = 99.1) -> high 100.5
        self.assertEqual(r["reason"], "TRAIL")
        self.assertAlmostEqual(r["gross_r"], 0.5 * 1.03 + 0.5 * 0.9)

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            study.simulate_live_exit(mode="X")


if __name__ == "__main__":
    unittest.main()
