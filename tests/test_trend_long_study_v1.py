"""TREND_LONG_V1 mechanics on hand-built daily bars; never evidence."""
import unittest
from datetime import datetime, timezone

from bot import binance_public_history as hist
from bot import exit_management_study_v1 as exits
from bot import raw_signal_edge_study_v1 as raw
from bot import trend_breakout_study_v1 as tb
from bot import trend_long_study_v1 as study

DAY = study.DAY_MS
D0 = datetime(2018, 1, 1, tzinfo=timezone.utc).timestamp() * 1000 // DAY  # a Monday
NO_FUNDING = ("ASSUMED", 0.0)


def day(i, o, h, l, c):
    return {"ts": int((D0 + i) * DAY), "o": o, "h": h, "l": l, "c": c, "v": 1.0, "day": int(D0 + i)}


def flat(n, price=100.0, width=1.0, start=0):
    return [day(start + i, price, price + width, price - width, price) for i in range(n)]


class PreregTests(unittest.TestCase):
    def test_decisive_window_was_never_read(self):
        earlier = set(raw.PREREG["warmup_months"] + raw.PREREG["discovery_months"] + raw.PREREG["holdout_months"]
                      + exits.PREREG["warmup_months"] + exits.PREREG["holdout_months"] + exits.PREREG["descriptive_months"]
                      + tb.PREREG["warmup_months"] + tb.PREREG["holdout_months"] + tb.PREREG["descriptive_months"])
        decisive = set(study.PREREG["decisive"]["warmup_months"] + study.PREREG["decisive"]["months"])
        self.assertFalse(earlier & decisive)
        self.assertEqual(study.PREREG["decisive"]["source"], "SPOT")
        self.assertEqual(len(study.prereg_digest()), 64)

    def test_spot_archives_use_the_spot_endpoint_and_a_separate_cache_path(self):
        archive = hist.spot_kline_archive("BTCUSDT", "1d", "2018-01")
        self.assertTrue(archive.startswith("spot/"))
        self.assertEqual(hist._url_for(archive),
                         "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-2018-01.zip")
        fut = hist.kline_archive("BTCUSDT", "1d", "2022-01")
        self.assertTrue(hist._url_for(fut).startswith("https://data.binance.vision/data/futures/um/monthly/"))


class FilterTests(unittest.TestCase):
    def test_btc_filter_needs_200_days_and_compares_to_the_average(self):
        days = flat(199, 100.0) + [day(199, 100, 120, 100, 120)]
        up = study.btc_filter(days)
        self.assertEqual(list(up), [days[-1]["day"]])
        self.assertTrue(up[days[-1]["day"]])
        down = flat(199, 100.0) + [day(199, 100, 100, 80, 80)]
        self.assertFalse(study.btc_filter(down)[down[-1]["day"]])


class H1Tests(unittest.TestCase):
    def run_h1(self, days, up=True):
        filt = {d["day"]: up for d in days}
        return study.simulate_h1("X", days, filt, 0.0, NO_FUNDING)

    def wide_base(self, n=25):
        return flat(n, 100.0, 6.0)  # ATR 12 -> stop 24 away

    def test_breakout_long_then_channel_exit(self):
        days = self.wide_base()
        days += [day(25, 100, 107.5, 100, 107), day(26, 108, 110, 106, 109),
                 day(27, 109, 109, 100, 101), day(28, 101, 101, 92.9, 93), day(29, 93.2, 94, 93, 93.5)]
        rows = self.run_h1(days)
        self.assertEqual([r["reason"] for r in rows], ["CHANNEL_EXIT"])
        distance = 2.0 * study._atr(days, 25, 20)  # the breakout day is part of the ATR
        self.assertAlmostEqual(rows[0]["gross_r"], (93.2 - 108) / distance)

    def test_filter_blocks_entries_and_shorts_never_happen(self):
        days = self.wide_base()
        days += [day(25, 100, 107.5, 100, 107), day(26, 108, 110, 106, 109), day(27, 109, 109, 100, 101),
                 day(28, 101, 101, 92.9, 93), day(29, 93.2, 94, 93, 93.5)]
        self.assertEqual(self.run_h1(days, up=False), [])
        crash = self.wide_base() + [day(25, 100, 100, 80, 81), day(26, 81, 82, 79, 80)]
        self.assertEqual(self.run_h1(crash), [])  # breakdowns are not traded

    def test_stop_at_two_atr(self):
        days = self.wide_base() + [day(25, 100, 107.5, 100, 107), day(26, 108, 108, 83, 90)]
        rows = self.run_h1(days)
        self.assertEqual(rows[0]["reason"], "STOP")
        self.assertAlmostEqual(rows[0]["gross_r"], -1.0)

    def test_open_position_at_data_end_is_not_counted(self):
        days = self.wide_base() + [day(25, 100, 107.5, 100, 107), day(26, 108, 110, 106, 109)]
        self.assertEqual(self.run_h1(days), [])


class H2Tests(unittest.TestCase):
    def series(self, slope, n=50, start_price=100.0):
        out, p = [], start_price
        for i in range(n):
            o = p
            c = p * (1 + slope)
            out.append(day(i, o, max(o, c) * 1.01, min(o, c) * 0.99, c))
            p = c
        return out

    def test_picks_top_three_positive_and_exits_next_monday(self):
        data = {"A": self.series(0.01), "B": self.series(0.008), "C": self.series(0.006),
                "D": self.series(0.004), "E": self.series(-0.01)}
        slips = {s: 0.0 for s in data}
        funds = {s: NO_FUNDING for s in data}
        rows = study.simulate_h2(data, slips, funds)
        self.assertTrue(rows)
        weeks = {}
        for r in rows:
            weeks.setdefault(r["ts"], set()).add(r["symbol"])
        for picked in weeks.values():
            self.assertEqual(picked, {"A", "B", "C"})
        self.assertTrue(all(r["reason"] == "WEEKLY_EXIT" for r in rows))
        self.assertTrue(all(datetime.fromtimestamp(r["ts"] / 1000, tz=timezone.utc).weekday() == 0 for r in rows))

    def test_no_trade_when_nothing_is_rising(self):
        data = {s: self.series(-0.005) for s in "ABCD"}
        rows = study.simulate_h2(data, {s: 0.0 for s in data}, {s: NO_FUNDING for s in data})
        self.assertEqual(rows, [])


class CostAndVerdictTests(unittest.TestCase):
    def test_assumed_funding_is_charged_against_longs(self):
        free = study.trade_row("H1", "X", 0, 100.0, 3 * DAY, 100.0, 10.0, "STOP", 0.0, ("ASSUMED", 0.0))
        paid = study.trade_row("H1", "X", 0, 100.0, 3 * DAY, 100.0, 10.0, "STOP", 0.0, ("ASSUMED", 0.0002))
        self.assertAlmostEqual(free["net_r"] - paid["net_r"], 9 * 0.0002 / 0.1)

    def rows(self, value, window_months, n_per_month=12, symbols=10, swing=0.0):
        out = []
        for m_i, month in enumerate(window_months):
            lo, _ = tb.month_bounds([month])
            for i in range(n_per_month):
                v = value + (swing if m_i % 2 else -swing) + 0.2 * ((i % 3) - 1)
                out.append({"hypothesis": "H1", "symbol": f"S{i % symbols}", "ts": lo + i * DAY,
                            "reason": "STOP", "net_r": v, "gross_r": v, "cluster": month})
        return out

    def test_pass_needs_the_confirmation_window_too(self):
        dec = self.rows(0.5, study.PREREG["decisive"]["months"])
        good = study.build_report({"decisive": dec, "confirmation": self.rows(0.2, study.PREREG["confirmation"]["months"][:6])})
        self.assertEqual(good["hypotheses"]["H1"]["verdict"], "H1_PASS")
        bad = study.build_report({"decisive": dec, "confirmation": self.rows(-0.1, study.PREREG["confirmation"]["months"][:6])})
        self.assertEqual(bad["hypotheses"]["H1"]["verdict"], "H1_FAIL")
        self.assertEqual(good["hypotheses"]["H2"]["verdict"], "H2_INSUFFICIENT_EVIDENCE")
        self.assertFalse(good["live_allowed"])

    def test_noisy_months_fail_the_bonferroni_bound(self):
        dec = self.rows(0.05, study.PREREG["decisive"]["months"], swing=1.5)
        r = study.build_report({"decisive": dec, "confirmation": self.rows(0.2, study.PREREG["confirmation"]["months"][:6])})
        self.assertEqual(r["hypotheses"]["H1"]["verdict"], "H1_FAIL")


if __name__ == "__main__":
    unittest.main()
