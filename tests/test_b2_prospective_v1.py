"""B2_PROSPECTIVE_V1: windows, looks and verdict logic on synthetic rows.

Synthetic rows test the decision mechanics only; they are never evidence.
"""
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from bot import b2_prospective_v1 as study
from bot import binance_public_history as hist
from bot import trend_breakout_study_v1 as base


def rows(first, last, value, *, per_month=20, symbols=12, reason="CHANNEL_EXIT", jitter=0.3,
         month_swing=0.0):
    """Mean ``value``; ``month_swing`` alternates whole months up and down."""
    out = []
    for m_i, month in enumerate(hist.months_between(first, last)):
        lo, _ = base.month_bounds([month])
        shift = month_swing if m_i % 2 == 0 else -month_swing
        for i in range(per_month):
            v = value + shift + jitter * ((i % 5) - 2)
            out.append({"symbol": f"S{(i + m_i) % symbols}", "ts": lo + i * 3_600_000,
                        "direction": "LONG", "reason": reason, "cluster": month,
                        "net_r": {"0.0006": v, "0.0005": v, "0.0004": v}})
    return out


class PreregTests(unittest.TestCase):
    def test_window_starts_after_the_freeze_and_after_every_earlier_study(self):
        self.assertGreater(study.PREREG["first_month"], study.PREREG["frozen_on"][:7])
        seen = set(base.PREREG["holdout_months"] + base.PREREG["descriptive_months"])
        self.assertFalse(seen & set(hist.months_between(study.PREREG["first_month"],
                                                        study.PREREG["final_last_month"])))
        self.assertEqual(len(hist.months_between(study.PREREG["first_month"],
                                                 study.PREREG["interim_last_month"])), 12)
        self.assertEqual(len(hist.months_between(study.PREREG["first_month"],
                                                 study.PREREG["final_last_month"])), 24)

    def test_rule_is_the_tested_b2_rule(self):
        b2 = base.PREREG["B2"]
        self.assertEqual(study.PREREG["rule"]["entry_days"], b2["primary"]["entry_days"])
        self.assertEqual(study.PREREG["rule"]["exit_days"], b2["primary"]["exit_days"])
        self.assertEqual(study.PREREG["rule"]["atr_days"], b2["atr_days"])
        self.assertEqual(study.PREREG["rule"]["stop_atr_mult"], b2["stop_atr_mult"])
        self.assertEqual(study.PREREG["symbols"], base.PREREG["symbols"])
        self.assertEqual(len(study.prereg_digest()), 64)


class LookTests(unittest.TestCase):
    def test_no_verdict_before_the_interim_look(self):
        r = study.build_report(rows("2026-11", "2027-09", 0.5), "2027-09")
        self.assertEqual(r["verdict"], "NOT_YET")
        self.assertGreater(r["descriptive"]["trades"], 0)
        self.assertFalse(r["live_allowed"])

    def test_interim_futility_fails_for_good(self):
        r = study.build_report(rows("2026-11", "2028-10", -0.1), "2027-10")
        self.assertEqual((r["look"], r["verdict"]), ("INTERIM", "B2_PROSPECTIVE_FAIL"))

    def test_interim_pass_needs_the_stricter_bound(self):
        strong = study.build_report(rows("2026-11", "2027-10", 0.6, jitter=0.1), "2027-10")
        self.assertEqual(strong["verdict"], "B2_PROSPECTIVE_PASS")
        self.assertEqual(strong["basis"]["lower_quantile"], 0.01)

    def test_weak_positive_continues_then_final_decides(self):
        data = rows("2026-11", "2027-10", 0.05, jitter=0.5, month_swing=1.0)
        r = study.build_report(data, "2027-12")
        self.assertEqual((r["look"], r["verdict"]), ("INTERIM", "CONTINUE"))
        final = study.build_report(
            data + rows("2027-11", "2028-10", 0.05, jitter=0.5, month_swing=1.0), "2028-10")
        self.assertEqual(final["look"], "FINAL")
        self.assertIn(final["verdict"], ("B2_PROSPECTIVE_PASS", "B2_PROSPECTIVE_FAIL"))
        self.assertEqual(final["basis"]["lower_quantile"], 0.05)

    def test_final_below_gate_is_insufficient(self):
        thin = rows("2026-11", "2028-10", 0.05, per_month=5, jitter=0.5)
        r = study.build_report(thin, "2028-10")
        self.assertEqual(r["verdict"], "B2_PROSPECTIVE_INSUFFICIENT_EVIDENCE")

    def test_warmup_and_open_positions_never_count(self):
        data = rows("2026-08", "2026-10", 5.0) + rows("2026-11", "2027-10", 0.6, jitter=0.1)
        data += rows("2027-10", "2027-10", -50.0, reason="DATA_END")
        counted = study.eligible(data, "2026-11", "2027-10")
        self.assertTrue(all(r["reason"] != "DATA_END" for r in counted))
        self.assertTrue(all(r["cluster"] >= "2026-11" for r in counted))
        self.assertEqual(study.build_report(data, "2027-10")["verdict"], "B2_PROSPECTIVE_PASS")


class DataTests(unittest.TestCase):
    def test_published_through_stops_at_the_first_missing_month(self):
        months = ["2026-08", "2026-09", "2026-10", "2026-11"]
        avail = {"BTCUSDT": {"klines_absent": ["2026-11"], "funding_absent": []}}
        self.assertEqual(study.published_through(avail, months), "2026-10")
        avail["BTCUSDT"]["klines_absent"] = ["2026-08"]
        self.assertIsNone(study.published_through(avail, months))

    def test_previous_month(self):
        self.assertEqual(study.previous_month(datetime(2027, 1, 5, tzinfo=timezone.utc)), "2026-12")
        self.assertEqual(study.previous_month(datetime(2026, 11, 2, tzinfo=timezone.utc)), "2026-10")

    def test_only_404_is_absent(self):
        def not_found(url):
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)

        def broken(url):
            raise urllib.error.HTTPError(url, 500, "err", {}, None)

        with tempfile.TemporaryDirectory() as tmp:
            avail = study.download(Path(tmp), ["BTCUSDT"], ["2026-11"], fetch=not_found)
            self.assertEqual(avail["BTCUSDT"]["klines_absent"], ["2026-11"])
            saved = json.loads((Path(tmp) / study.AVAILABILITY).read_text())
            self.assertEqual(saved, avail)
            with self.assertRaises(urllib.error.HTTPError):
                study.download(Path(tmp), ["BTCUSDT"], ["2026-11"], fetch=broken)


if __name__ == "__main__":
    unittest.main()
