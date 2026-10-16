"""TREND_BREAKOUT_V1 simulator: each rule on hand-built price paths.

Synthetic bars test the mechanics only; they are never evidence.
"""
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from bot import trend_breakout_study_v1 as study
from bot import raw_signal_edge_study_v1 as raw
from bot import exit_management_study_v1 as exits

DAY0 = 1_640_995_200_000 // study.DAY_MS  # 2022-01-01
NO_FUNDING = {"events": [], "absent": []}
FEES = [0.0006]


def flat_day(day, o, h, l, c):
    """96 bars: open at o, one bar touches h and l, close at c."""
    bars = []
    for i in range(study.BARS_PER_DAY):
        ts = day * study.DAY_MS + i * study.BAR_MS
        price = o if i < 95 else c
        bars.append({"ts": ts, "o": price, "h": price, "l": price, "c": price, "v": 1.0})
    bars[40].update(h=h)
    bars[60].update(l=l)
    return bars


def path_day(day, prices):
    """Bars whose (o, h, l, c) are given in order; the rest of the day stays at the last close."""
    bars = []
    last = prices[-1][3]
    for i in range(study.BARS_PER_DAY):
        ts = day * study.DAY_MS + i * study.BAR_MS
        o, h, l, c = prices[i] if i < len(prices) else (last, last, last, last)
        bars.append({"ts": ts, "o": o, "h": h, "l": l, "c": c, "v": 1.0})
    return bars


def history(closes, last_range=(105.0, 95.0)):
    """Complete days with the given closes; the last one has the given high/low."""
    bars = []
    for n, c in enumerate(closes):
        h, l = (last_range if n == len(closes) - 1 else (c + 1, c - 1))
        bars += flat_day(DAY0 + n, c, max(h, c), min(l, c), c)
    return bars


def b1(bars, i=None, k=0.5):
    days = study.build_days(bars)
    i = len(days) - 1 if i is None else i
    return study.simulate_b1_day(days, i, k=k, slip=0.0, funding=NO_FUNDING, fees=FEES)


class B1Tests(unittest.TestCase):
    def uptrend(self):
        return history([80.0 + n for n in range(20)] + [100.0])  # close 100 > SMA, range 10

    def test_long_trigger_then_day_close(self):
        bars = self.uptrend() + path_day(DAY0 + 21, [(100, 101, 99.5, 100.5), (100.5, 106, 100.4, 105.5),
                                                     (105.5, 108, 105, 107)])
        r = b1(bars)
        self.assertEqual(r["direction"], "LONG")
        self.assertEqual(r["reason"], "DAY_CLOSE")
        self.assertAlmostEqual(r["entry_fill"], 105.0)
        self.assertAlmostEqual(r["risk_fraction"], 5.0 / 105.0)
        self.assertAlmostEqual(r["gross_r"], (107 - 105) / 5.0)
        self.assertLess(r["net_r"]["0.0006"], r["gross_r"])

    def test_gap_above_trigger_fills_at_open(self):
        bars = self.uptrend() + path_day(DAY0 + 21, [(100, 100.5, 99.8, 100.2), (106, 107, 105.8, 106.5)])
        r = b1(bars)
        self.assertAlmostEqual(r["entry_fill"], 106.0)
        self.assertAlmostEqual(r["risk_fraction"], 5.0 / 106.0)

    def test_stop_in_the_entry_bar_is_taken(self):
        bars = self.uptrend() + path_day(DAY0 + 21, [(100, 105.5, 99.0, 104.0)])
        r = b1(bars)
        self.assertEqual(r["reason"], "STOP")
        self.assertAlmostEqual(r["gross_r"], -1.0)

    def test_later_gap_through_stop_exits_at_open(self):
        bars = self.uptrend() + path_day(DAY0 + 21, [(100, 105.5, 104, 105), (98, 98.5, 97, 98)])
        r = b1(bars)
        self.assertEqual(r["reason"], "STOP_GAP")
        self.assertAlmostEqual(r["gross_r"], (98 - 105) / 5.0)

    def test_no_entry_after_the_cutoff(self):
        quiet = [(100, 100.2, 99.8, 100)] * 80  # bars 0..79 end at 20:00
        bars = self.uptrend() + path_day(DAY0 + 21, quiet + [(100, 106, 100, 105.5)])
        self.assertIsNone(b1(bars))

    def test_short_in_a_downtrend(self):
        bars = history([120.0 - n for n in range(20)] + [100.0]) + path_day(
            DAY0 + 21, [(100, 100, 99, 99.2), (99.2, 99.3, 94.5, 95), (95, 95.2, 93, 93.5)])
        r = b1(bars)
        # k = 0.5 and a 0.5-range stop put the stop exactly at the day's open (100).
        self.assertAlmostEqual(r["stop"], 100.0)
        self.assertEqual(r["direction"], "SHORT")
        self.assertAlmostEqual(r["entry_fill"], 95.0)
        self.assertAlmostEqual(r["gross_r"], (95 - 93.5) / 5.0)

    def test_missing_history_or_incomplete_day_skips(self):
        bars = self.uptrend() + path_day(DAY0 + 21, [(100, 106, 100, 105.5)])
        self.assertIsNone(b1(bars[:-1]))                     # today has 95 bars
        self.assertIsNone(b1(bars[2 * study.BARS_PER_DAY:]))  # only 19 prior days

    def test_k_moves_the_trigger(self):
        bars = self.uptrend() + path_day(DAY0 + 21, [(100, 103.5, 99.9, 103.2)])
        self.assertIsNone(b1(bars, k=0.5))
        self.assertAlmostEqual(b1(bars, k=0.3)["entry_fill"], 103.0)


class B2Tests(unittest.TestCase):
    def run_b2(self, bars, entry=20, exit_=10):
        days = study.build_days(bars)
        return study.simulate_b2("X", days, entry_days=entry, exit_days=exit_, slip=0.0,
                                 funding=NO_FUNDING, fees=FEES)

    def base(self, n=25):
        return history([100.0] * n, last_range=(101.0, 99.0))

    def test_breakout_enters_next_open_and_exits_on_opposite_channel(self):
        bars = []
        for n in range(25):                              # wide days: ATR 12, stop 24 away
            bars += flat_day(DAY0 + n, 100, 106, 94, 100)
        d = DAY0 + 25
        bars += flat_day(d, 100, 107.5, 100, 107)        # close 107 > 20-day high 106
        bars += flat_day(d + 1, 108, 110, 106, 109)      # entry at 108
        bars += flat_day(d + 2, 109, 109, 100, 101)
        bars += flat_day(d + 3, 101, 101, 92.9, 93)      # close < 10-day low 94 -> exit next open
        bars += flat_day(d + 4, 93.2, 94, 93, 93.5)
        rows = self.run_b2(bars)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["direction"], r["reason"]), ("LONG", "CHANNEL_EXIT"))
        self.assertAlmostEqual(r["entry_fill"], 108.0)
        self.assertAlmostEqual(r["exit_fill"], 93.2)
        self.assertGreater(r["risk_fraction"], 0)
        self.assertEqual(r["cluster"], "2022-01")

    def test_initial_stop_is_two_atr_from_the_fill(self):
        bars = self.base()
        d = DAY0 + 25
        bars += flat_day(d, 100, 105, 100, 105)
        days = study.build_days(bars)
        atr = study._atr(days, len(days) - 1, 20)
        bars += flat_day(d + 1, 106, 106, 106 - 2 * atr - 0.5, 103)
        rows = self.run_b2(bars)
        self.assertEqual(rows[0]["reason"], "STOP")
        self.assertAlmostEqual(rows[0]["gross_r"], -1.0)
        self.assertAlmostEqual(rows[0]["risk_fraction"], 2 * atr / 106.0)

    def test_open_position_closes_at_data_end_and_on_gaps(self):
        bars = self.base()
        d = DAY0 + 25
        bars += flat_day(d, 100, 105, 100, 105) + flat_day(d + 1, 106, 107, 105.5, 106)
        self.assertEqual(self.run_b2(bars)[0]["reason"], "DATA_END")
        gap = bars + flat_day(d + 3, 106, 107, 105.5, 106)   # day d+2 missing
        self.assertEqual(self.run_b2(gap)[0]["reason"], "DATA_GAP")

    def test_no_signal_without_breakout(self):
        self.assertEqual(self.run_b2(self.base(40)), [])


class CostTests(unittest.TestCase):
    def test_real_and_assumed_funding(self):
        lo, hi = study.month_bounds(["2022-01"])
        entry, exit_ = lo + 1, lo + study.DAY_MS  # crosses 08:00, 16:00 and next 00:00
        real = {"events": [{"timepoint": lo + 8 * 3_600_000, "fundingRate": 0.001}], "absent": []}
        r = study._net("LONG", 100.0, 100.0, 0.01, entry, exit_, real, FEES)
        self.assertAlmostEqual(r["funding_r"], -0.1)
        self.assertFalse(r["funding_assumed"])
        missing = {"events": [], "absent": [(lo, hi)]}
        r = study._net("SHORT", 100.0, 100.0, 0.01, entry, exit_, missing, FEES)
        self.assertEqual(r["funding_events"], 3)
        self.assertAlmostEqual(r["funding_r"], -3 * 0.0001 / 0.01)
        self.assertTrue(r["funding_assumed"])


class ReportTests(unittest.TestCase):
    def rows(self, hypothesis, variant, value, clusters):
        lo, _ = study.month_bounds(study.PREREG["holdout_months"])
        out = []
        for i in range(200):
            v = value + (i % 3) * 0.01
            out.append({"hypothesis": hypothesis, "variant": variant, "symbol": f"S{i % 12}",
                        "direction": "LONG", "ts": lo + i * 3_600_000, "exit_ts": lo + i * 3_600_000 + 1,
                        "reason": "DAY_CLOSE", "risk_fraction": 0.02, "gross_r": v + 0.1,
                        "funding_r": 0.0, "funding_assumed": False, "cluster": str(i % clusters),
                        "net_r": {"0.0006": v, "0.0005": v, "0.0004": v}})
        return out

    def test_verdicts_are_independent(self):
        rows = self.rows("B1", study.PRIMARY["B1"], 0.3, 60) + self.rows("B2", study.PRIMARY["B2"], -0.2, 20)
        report = study.build_report(rows)
        self.assertEqual(report["hypotheses"]["B1"]["verdict"], "B1_PASS")
        self.assertEqual(report["hypotheses"]["B2"]["verdict"], "B2_FAIL")
        self.assertFalse(report["live_allowed"])

    def test_sensitivity_variant_never_decides(self):
        rows = self.rows("B1", "B1_k0.3", 0.5, 60)
        report = study.build_report(rows)
        self.assertEqual(report["hypotheses"]["B1"]["verdict"], "B1_INSUFFICIENT_EVIDENCE")

    def test_descriptive_window_never_decides(self):
        lo, _ = study.month_bounds(study.PREREG["descriptive_months"])
        rows = self.rows("B1", study.PRIMARY["B1"], 0.3, 60)
        for r in rows:
            r["ts"] += lo
        self.assertEqual(study.build_report(rows)["hypotheses"]["B1"]["verdict"], "B1_INSUFFICIENT_EVIDENCE")


class PreregTests(unittest.TestCase):
    def test_holdout_was_never_read_by_earlier_studies(self):
        earlier = set(raw.PREREG["warmup_months"] + raw.PREREG["discovery_months"] + raw.PREREG["holdout_months"]
                      + exits.PREREG["warmup_months"] + exits.PREREG["holdout_months"]
                      + exits.PREREG["descriptive_months"])
        mine = set(study.PREREG["warmup_months"] + study.PREREG["holdout_months"])
        self.assertFalse(earlier & mine)
        self.assertEqual(len(study.PREREG["holdout_months"]), 32)
        self.assertEqual(len(study.prereg_digest()), 64)


class DownloadTests(unittest.TestCase):
    def test_only_404_is_recorded_as_absent(self):
        def not_found(url):
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)

        def server_error(url):
            raise urllib.error.HTTPError(url, 500, "err", {}, None)

        with tempfile.TemporaryDirectory() as tmp:
            avail = study.download(Path(tmp), ["XUSDT"], fetch=not_found)
            self.assertEqual(len(avail["XUSDT"]["klines_absent"]), len(study._months_all()))
            saved = json.loads((Path(tmp) / study.AVAILABILITY).read_text())
            self.assertEqual(saved, avail)
            with self.assertRaises(urllib.error.HTTPError):
                study.download(Path(tmp), ["YUSDT"], fetch=server_error)


if __name__ == "__main__":
    unittest.main()
