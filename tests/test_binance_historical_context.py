import io
import unittest
import zipfile

from bot.binance_historical_context import (
    BookDepthTimeline,
    MetricsTimeline,
    parse_book_depth_archive,
    parse_metrics_archive,
    shadow_microstructure_signal,
)


def _zip_csv(name, text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, text)
    return buf.getvalue()


def _metrics_row(ts, oi="100", oi_value="10000", top_pos="1.2"):
    return (
        f"{ts},BTCUSDT,{oi},{oi_value},1.1,{top_pos},1.0,1.05\n"
    )


class BinanceHistoricalContextTests(unittest.TestCase):
    def test_pre_change_metrics_are_end_labeled(self):
        payload = _zip_csv(
            "m.csv",
            "create_time,symbol,sum_open_interest,sum_open_interest_value,"
            "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
            "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
            + _metrics_row("2026-06-24 00:05:00"),
        )
        rows = parse_metrics_archive(payload, source_date="2026-06-24")
        self.assertEqual(rows[0].label_ts_ms, rows[0].effective_ts_ms)
        self.assertEqual(rows[0].convention, "END_LABEL")

    def test_post_change_metrics_shift_five_minutes_to_prevent_lookahead(self):
        payload = _zip_csv(
            "m.csv",
            "create_time,symbol,sum_open_interest,sum_open_interest_value,"
            "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
            "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
            + _metrics_row("2026-06-25 00:00:00")
            + _metrics_row("2026-06-25 00:05:00", oi="101"),
        )
        rows = parse_metrics_archive(payload, source_date="2026-06-25")
        self.assertEqual(
            rows[0].effective_ts_ms - rows[0].label_ts_ms,
            5 * 60 * 1000,
        )
        timeline = MetricsTimeline(rows)
        before, _ = timeline.asof(rows[0].label_ts_ms)
        self.assertIsNone(before)
        current, previous = timeline.asof(rows[1].effective_ts_ms)
        self.assertEqual(current.sum_open_interest, 101.0)
        self.assertEqual(previous.sum_open_interest, 100.0)
        inputs = current.as_nexus_inputs(previous)
        self.assertAlmostEqual(inputs["oi_delta"], 0.01)

    def test_book_depth_float_percentages_parse_and_group(self):
        payload = _zip_csv(
            "b.csv",
            "timestamp,percentage,depth,notional\n"
            "2026-08-31 12:00:00,-1.00,10,1000\n"
            "2026-08-31 12:00:00,1.00,8,800\n"
            "2026-08-31 12:00:00,-2.00,20,2000\n"
            "2026-08-31 12:00:00,2.00,15,1500\n"
            "2026-08-31 12:00:00,-5.00,50,5000\n"
            "2026-08-31 12:00:00,5.00,40,4000\n",
        )
        rows = parse_book_depth_archive(payload, source_date="2026-08-31")
        self.assertEqual(len(rows), 1)
        summary = rows[0].summary()
        self.assertEqual(summary["bid_notional_1pct"], 1000.0)
        self.assertGreater(summary["imbalance_1pct"], 0)
        shadow = shadow_microstructure_signal(rows[0])
        self.assertTrue(shadow["available"])
        self.assertEqual(shadow["execution_effect"], "NONE")

    def test_known_september_book_depth_issue_is_quarantined(self):
        payload = _zip_csv(
            "b.csv",
            "timestamp,percentage,depth,notional\n"
            "2026-09-03 12:00:00,-1,10,1000\n"
            "2026-09-03 12:00:00,1,8,800\n",
        )
        rows = parse_book_depth_archive(payload, source_date="2026-09-03")
        self.assertEqual(rows[0].quality, "KNOWN_UPSTREAM_ISSUE_QUARANTINE")
        self.assertFalse(shadow_microstructure_signal(rows[0])["available"])
        timeline = BookDepthTimeline(rows)
        self.assertIsNotNone(timeline.asof(rows[0].timestamp_ms))


if __name__ == "__main__":
    unittest.main()
