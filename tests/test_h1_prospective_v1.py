"""H1_PROSPECTIVE_V1: windows, looks and verdict logic on synthetic rows.

Synthetic rows test the decision mechanics only; they are never evidence.
"""
import tempfile
import unittest
import urllib.error
from pathlib import Path

from bot import binance_public_history as hist
from bot import h1_prospective_v1 as study
from bot import trend_breakout_study_v1 as base
from bot import trend_long_study_v1 as long_study


def rows(first, last, value, *, per_month=10, symbols=12, jitter=0.3, month_swing=0.0):
    out = []
    for m_i, month in enumerate(hist.months_between(first, last)):
        lo, _ = base.month_bounds([month])
        shift = month_swing if m_i % 2 == 0 else -month_swing
        for i in range(per_month):
            out.append({"hypothesis": "H1", "symbol": f"S{(i + m_i) % symbols}", "ts": lo + i * 3_600_000,
                        "reason": "CHANNEL_EXIT", "cluster": month,
                        "net_r": value + shift + jitter * ((i % 5) - 2)})
    return out


class PreregTests(unittest.TestCase):
    def test_window_starts_after_the_freeze_and_every_earlier_window(self):
        self.assertGreater(study.PREREG["first_month"], study.PREREG["frozen_on"][:7])
        seen = set(long_study.PREREG["confirmation"]["months"] + long_study.PREREG["decisive"]["months"])
        evaluated = set(hist.months_between(study.PREREG["first_month"], study.PREREG["final_last_month"]))
        self.assertFalse(seen & evaluated)
        self.assertEqual(len(hist.months_between(study.PREREG["first_month"],
                                                 study.PREREG["interim_last_month"])), 12)

    def test_rule_is_the_tested_h1_rule(self):
        rule = {k: v for k, v in study.PREREG["rule"].items() if k != "source"}
        self.assertEqual(rule, long_study.PREREG["H1"])
        self.assertEqual(study.PREREG["fee"], long_study.PREREG["fee"])
        self.assertEqual(study.PREREG["symbols"], long_study.PREREG["symbols"])
        self.assertEqual(len(study.prereg_digest()), 64)

    def test_warmup_is_long_enough_for_the_200_day_filter(self):
        lo, hi = base.month_bounds(study.PREREG["warmup_months"])
        self.assertGreaterEqual((hi - lo) // long_study.DAY_MS, 200 + 21)


class LookTests(unittest.TestCase):
    def test_no_verdict_before_the_interim_look(self):
        r = study.build_report(rows("2026-11", "2027-09", 0.5), "2027-09")
        self.assertEqual(r["verdict"], "NOT_YET")
        self.assertGreater(r["descriptive"]["trades"], 0)
        self.assertFalse(r["live_allowed"])

    def test_interim_futility_fails_for_good(self):
        r = study.build_report(rows("2026-11", "2028-10", -0.1), "2027-10")
        self.assertEqual((r["look"], r["verdict"]), ("INTERIM", "H1_PROSPECTIVE_FAIL"))

    def test_no_trades_at_interim_continues(self):
        r = study.build_report([], "2027-12")
        self.assertEqual((r["look"], r["verdict"]), ("INTERIM", "CONTINUE"))

    def test_interim_pass_needs_the_stricter_bound(self):
        strong = study.build_report(rows("2026-11", "2027-10", 0.6, jitter=0.1), "2027-10")
        self.assertEqual(strong["verdict"], "H1_PROSPECTIVE_PASS")
        self.assertEqual(strong["basis"]["lower_quantile"], 0.01)

    def test_weak_positive_continues_then_final_decides(self):
        data = rows("2026-11", "2027-10", 0.05, jitter=0.5, month_swing=1.0)
        self.assertEqual(study.build_report(data, "2027-12")["verdict"], "CONTINUE")
        final = study.build_report(data + rows("2027-11", "2028-10", 0.05, jitter=0.5, month_swing=1.0),
                                   "2028-10")
        self.assertEqual(final["look"], "FINAL")
        self.assertEqual(final["basis"]["lower_quantile"], 0.05)

    def test_final_below_gate_is_insufficient(self):
        thin = rows("2026-11", "2028-10", 0.5, per_month=3, jitter=0.1)
        self.assertEqual(study.build_report(thin, "2028-10")["verdict"],
                         "H1_PROSPECTIVE_INSUFFICIENT_EVIDENCE")

    def test_warmup_never_counts(self):
        data = rows("2026-01", "2026-10", -5.0) + rows("2026-11", "2027-10", 0.6, jitter=0.1)
        self.assertTrue(all(r["cluster"] >= "2026-11" for r in study.eligible(data, "2026-11", "2027-10")))
        self.assertEqual(study.build_report(data, "2027-10")["verdict"], "H1_PROSPECTIVE_PASS")

    def test_missing_funding_is_reported(self):
        r = study.build_report([], "2026-10", {"BTCUSDT": [], "XRPUSDT": ["2026-11"]})
        self.assertEqual(r["funding_absent"], {"XRPUSDT": ["2026-11"]})


class DataTests(unittest.TestCase):
    def test_published_through_stops_at_the_first_missing_month(self):
        avail = {"BTCUSDT": {"klines_absent": ["2026-11"], "funding_absent": []}}
        self.assertEqual(study.published_through(avail, ["2026-09", "2026-10", "2026-11"]), "2026-10")

    def test_only_404_is_absent_and_daily_archives_are_requested(self):
        seen = []

        def not_found(url):
            seen.append(url)
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)

        def broken(url):
            raise urllib.error.HTTPError(url, 500, "err", {}, None)

        with tempfile.TemporaryDirectory() as tmp:
            avail = study.download(Path(tmp), ["BTCUSDT"], ["2026-11"], fetch=not_found)
            self.assertEqual(avail["BTCUSDT"]["klines_absent"], ["2026-11"])
            self.assertTrue(any("/1d/BTCUSDT-1d-2026-11" in u for u in seen))
            with self.assertRaises(urllib.error.HTTPError):
                study.download(Path(tmp), ["BTCUSDT"], ["2026-11"], fetch=broken)


if __name__ == "__main__":
    unittest.main()
