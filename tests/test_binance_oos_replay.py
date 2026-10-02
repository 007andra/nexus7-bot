import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.binance_historical_context import MetricsObservation, MetricsTimeline
from bot.binance_oos_replay import (
    adverse_fill,
    dates_for_months,
    derivatives_context_at,
    fee_return_fraction,
    funding_return_fraction,
    load_verified_metrics,
    month_range,
)
from bot.binance_research_data import FundingObservation


class BinanceOOSReplayTests(unittest.TestCase):
    def test_month_range_is_inclusive_across_year_boundary(self):
        self.assertEqual(
            month_range("2025-11", "2026-02"),
            ((2025, 11), (2025, 12), (2026, 1), (2026, 2)),
        )

    def test_dates_for_months_expands_calendar_days(self):
        days = dates_for_months(((2026, 2),))
        self.assertEqual(days[0], "2026-02-01")
        self.assertEqual(days[-1], "2026-02-28")
        self.assertEqual(len(days), 28)

    def test_derivative_context_uses_latest_available_metric_only(self):
        rows = [
            MetricsObservation(
                label_ts_ms=1000,
                effective_ts_ms=1000,
                symbol="BTCUSDT",
                sum_open_interest=100.0,
                sum_open_interest_value=10000.0,
                top_account_ls_ratio=1.0,
                top_position_ls_ratio=1.2,
                global_account_ls_ratio=1.0,
                taker_ls_volume_ratio=1.0,
                source_date="2026-01-01",
                convention="END_LABEL",
            ),
            MetricsObservation(
                label_ts_ms=2000,
                effective_ts_ms=2000,
                symbol="BTCUSDT",
                sum_open_interest=101.0,
                sum_open_interest_value=10100.0,
                top_account_ls_ratio=1.0,
                top_position_ls_ratio=1.3,
                global_account_ls_ratio=1.0,
                taker_ls_volume_ratio=1.0,
                source_date="2026-01-01",
                convention="END_LABEL",
            ),
        ]
        context, complete = derivatives_context_at(
            MetricsTimeline(rows), 2500, max_age_ms=1000
        )
        self.assertTrue(complete)
        self.assertAlmostEqual(context["oi_delta"], 0.01)
        self.assertEqual(context["ls_ratio"], 1.3)

    def test_metrics_loader_rejects_unbounded_concurrency(self):
        with self.assertRaises(ValueError):
            asyncio.run(
                load_verified_metrics(
                    "BTCUSDT",
                    ("2026-01-01",),
                    concurrency=33,
                )
            )

    def test_metrics_loader_merges_by_requested_date_not_completion_order(self):
        dates = ("2026-01-01", "2026-01-02", "2026-01-03")
        timestamps = {
            "2026-01-01": 1000,
            "2026-01-02": 2000,
            "2026-01-03": 3000,
        }

        async def fake_download(url, *, cache_path=None, timeout_s=30.0):
            # Force reverse-ish completion order to prove gather timing cannot
            # alter deterministic merge order.
            if "2026-01-01" in url:
                await asyncio.sleep(0.02)
            elif "2026-01-02" in url:
                await asyncio.sleep(0.01)
            return SimpleNamespace(
                payload=url.encode("utf-8"),
                sha256=("a" if "01-01" in url else "b" if "01-02" in url else "c") * 64,
            )

        def fake_parse(payload, *, source_date):
            ts = timestamps[source_date]
            return (
                MetricsObservation(
                    label_ts_ms=ts,
                    effective_ts_ms=ts,
                    symbol="BTCUSDT",
                    sum_open_interest=100.0 + ts,
                    sum_open_interest_value=10000.0,
                    top_account_ls_ratio=1.0,
                    top_position_ls_ratio=1.2,
                    global_account_ls_ratio=1.0,
                    taker_ls_volume_ratio=1.0,
                    source_date=source_date,
                    convention="END_LABEL",
                ),
            )

        with patch(
            "bot.binance_oos_replay.download_archive_verified",
            side_effect=fake_download,
        ), patch(
            "bot.binance_oos_replay.parse_metrics_archive",
            side_effect=fake_parse,
        ):
            rows, artifacts = asyncio.run(
                load_verified_metrics(
                    "BTCUSDT",
                    dates,
                    concurrency=3,
                )
            )

        self.assertEqual(
            [row.effective_ts_ms for row in rows],
            [1000, 2000, 3000],
        )
        self.assertEqual(
            [artifact.first_ts for artifact in artifacts],
            [1000, 2000, 3000],
        )

    async def test_metrics_loader_rejects_invalid_concurrency(self):
        with self.assertRaises(ValueError):
            await load_verified_metrics(
                "BTCUSDT", (), concurrency=0
            )
        with self.assertRaises(ValueError):
            await load_verified_metrics(
                "BTCUSDT", (), concurrency=33
            )

    async def test_metrics_loader_merge_order_is_deterministic(self):
        dates = ("2026-01-01", "2026-01-02", "2026-01-03")

        async def fake_download(url, cache_path=None):
            del cache_path
            if "2026-01-01" in url:
                await asyncio.sleep(0.03)
            elif "2026-01-02" in url:
                await asyncio.sleep(0.01)
            return SimpleNamespace(
                payload=url.encode("utf-8"),
                sha256=("a" * 64),
            )

        def fake_parse(payload, *, source_date):
            del payload
            day = int(source_date[-2:])
            ts = 1_700_000_000_000 + day * 300_000
            return (
                MetricsObservation(
                    label_ts_ms=ts,
                    effective_ts_ms=ts,
                    symbol="BTCUSDT",
                    sum_open_interest=100.0 + day,
                    sum_open_interest_value=10000.0 + day,
                    top_account_ls_ratio=1.0,
                    top_position_ls_ratio=1.1,
                    global_account_ls_ratio=1.0,
                    taker_ls_volume_ratio=1.0,
                    source_date=source_date,
                    convention="END_LABEL",
                ),
            )

        with (
            patch(
                "bot.binance_oos_replay.download_archive_verified",
                side_effect=fake_download,
            ),
            patch(
                "bot.binance_oos_replay.parse_metrics_archive",
                side_effect=fake_parse,
            ),
        ):
            rows, artifacts = await load_verified_metrics(
                "BTCUSDT", dates, concurrency=3
            )

        self.assertEqual(
            [row.source_date for row in rows],
            list(dates),
        )
        self.assertEqual(
            [artifact.source.split("-metrics-")[-1].removesuffix(".zip") for artifact in artifacts],
            list(dates),
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
