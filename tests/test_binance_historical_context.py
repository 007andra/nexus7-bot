import io
import unittest
import zipfile

from bot.binance_research_data import AggTradeObservation

from bot.binance_historical_context import (
    AggTradeTimeline,
    BookDepthTimeline,
    MetricsObservation,
    MetricsTimeline,
    parse_book_depth_archive,
    parse_metrics_archive,
    shadow_microstructure_context,
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


_HEADER = (
    "create_time,symbol,sum_open_interest,sum_open_interest_value,"
    "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
    "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
)


class MetricsArchiveOrderingTests(unittest.TestCase):
    def test_out_of_order_rows_are_sorted_by_effective_time(self):
        payload = _zip_csv("m.csv", _HEADER + _metrics_row("2026-03-01 00:10:00", oi="102")
                           + _metrics_row("2026-03-01 00:05:00", oi="101"))
        rows = parse_metrics_archive(payload, source_date="2026-03-01")
        self.assertEqual([r.sum_open_interest for r in rows], [101.0, 102.0])

    def test_exact_duplicate_row_is_dropped_with_provenance(self):
        from bot.binance_historical_context import METRICS_ARCHIVE_PROVENANCE
        row = _metrics_row("2026-03-02 00:05:00")
        rows = parse_metrics_archive(_zip_csv("m.csv", _HEADER + row + row), source_date="2026-03-02")
        self.assertEqual(len(rows), 1)
        self.assertEqual(METRICS_ARCHIVE_PROVENANCE["2026-03-02"]["exact_duplicates_dropped"], 1)

    def test_conflicting_duplicate_fails_closed_with_location(self):
        payload = _zip_csv("m.csv", _HEADER + _metrics_row("2026-03-03 00:05:00", oi="100")
                           + _metrics_row("2026-03-03 00:05:00", oi="999"))
        with self.assertRaisesRegex(ValueError, "conflicting metrics rows.*2026-03-03"):
            parse_metrics_archive(payload, source_date="2026-03-03")


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

    def test_shadow_context_combines_depth_taker_flow_and_oi(self):
        payload = _zip_csv(
            "b.csv",
            "timestamp,percentage,depth,notional\n"
            "2026-08-31 12:00:00,-1,10,1500\n"
            "2026-08-31 12:00:00,1,8,500\n"
            "2026-08-31 12:00:00,-2,20,2500\n"
            "2026-08-31 12:00:00,2,15,1000\n"
            "2026-08-31 12:00:00,-5,50,5000\n"
            "2026-08-31 12:00:00,5,40,2500\n",
        )
        snapshot = parse_book_depth_archive(
            payload, source_date="2026-08-31"
        )[0]
        metrics_payload = _zip_csv(
            "m.csv",
            "create_time,symbol,sum_open_interest,sum_open_interest_value,"
            "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
            "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
            + _metrics_row("2026-08-31 11:55:00", oi="100")
            + _metrics_row("2026-08-31 12:00:00", oi="102"),
        )
        metrics = parse_metrics_archive(
            metrics_payload, source_date="2026-08-31"
        )
        result = shadow_microstructure_context(
            snapshot,
            metrics[-1],
            metrics[-2],
            decision_ts_ms=snapshot.timestamp_ms + 5 * 60 * 1000,
            side="LONG",
            oi_delta_override=0.05,
        )
        self.assertTrue(result["available"])
        self.assertGreater(result["depth_imbalance"], 0)
        self.assertAlmostEqual(result["oi_delta"], 0.05)
        self.assertGreater(result["directional_alignment"], 0)
        self.assertEqual(result["score_effect"], "NONE")
        self.assertFalse(result["promotion_authority"])

    def test_stale_depth_is_not_used_as_available_microstructure(self):
        payload = _zip_csv(
            "b.csv",
            "timestamp,percentage,depth,notional\n"
            "2026-08-31 12:00:00,-1,10,1000\n"
            "2026-08-31 12:00:00,1,8,800\n",
        )
        snapshot = parse_book_depth_archive(
            payload, source_date="2026-08-31"
        )[0]
        result = shadow_microstructure_context(
            snapshot,
            None,
            None,
            decision_ts_ms=snapshot.timestamp_ms + 16 * 60 * 1000,
            side="LONG",
        )
        self.assertFalse(result["available"])
        self.assertFalse(result["depth_available"])

    def test_agg_trade_timeline_is_causal_and_directional(self):
        rows = [
            AggTradeObservation(
                1, 100.0, 3.0, 10, 10,
                1_700_000_000_000, False,
            ),
            AggTradeObservation(
                2, 100.0, 1.0, 11, 11,
                1_700_000_001_000, True,
            ),
            AggTradeObservation(
                3, 100.0, 100.0, 12, 12,
                1_700_000_400_000, False,
            ),
        ]
        timeline = AggTradeTimeline(rows)
        pressure = timeline.pressure(
            1_700_000_002_000,
            window_ms=5 * 60 * 1000,
        )
        self.assertTrue(pressure["available"])
        self.assertGreater(pressure["taker_pressure"], 0)
        self.assertEqual(pressure["rows"], 2)
        self.assertFalse(pressure["future_rows_used"])
        self.assertEqual(pressure["execution_effect"], "NONE")

    def test_agg_trade_pressure_override_replaces_metrics_proxy_only_in_shadow(self):
        metrics = MetricsObservation(
            label_ts_ms=1_700_000_000_000,
            effective_ts_ms=1_700_000_000_000,
            symbol="BTCUSDT",
            sum_open_interest=100.0,
            sum_open_interest_value=10000.0,
            top_account_ls_ratio=1.0,
            top_position_ls_ratio=1.0,
            global_account_ls_ratio=1.0,
            taker_ls_volume_ratio=0.5,
            source_date="2026-08-31",
            convention="END_LABEL",
        )
        result = shadow_microstructure_context(
            None,
            metrics,
            None,
            decision_ts_ms=1_700_000_001_000,
            side="LONG",
            agg_trade_pressure_override=0.75,
        )
        self.assertEqual(result["taker_pressure_source"], "AGG_TRADES")
        self.assertAlmostEqual(result["taker_pressure"], 0.75)
        self.assertEqual(result["score_effect"], "NONE")
        self.assertEqual(result["execution_effect"], "NONE")
        self.assertFalse(result["promotion_authority"])

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
        context = shadow_microstructure_context(
            rows[0],
            None,
            None,
            decision_ts_ms=rows[0].timestamp_ms,
            side="LONG",
        )
        self.assertEqual(
            context["quality"],
            "KNOWN_UPSTREAM_ISSUE_QUARANTINE",
        )
        self.assertEqual(context["execution_effect"], "NONE")
        timeline = BookDepthTimeline(rows)
        self.assertIsNotNone(timeline.asof(rows[0].timestamp_ms))


if __name__ == "__main__":
    unittest.main()
