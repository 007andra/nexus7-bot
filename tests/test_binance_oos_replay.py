import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.binance_historical_context import MetricsObservation, MetricsTimeline
from bot.binance_oos_replay import (
    adverse_fill,
    dates_for_months,
    derivatives_context_at,
    fee_return_fraction,
    funding_return_fraction,
    load_verified_agg_trades,
    load_verified_book_depth,
    load_verified_metrics,
    month_range,
)
from bot.binance_research_data import AggTradeObservation, FundingObservation



def _filtered_fake(full_parse):
    """Fake of parse_agg_trades_archive_filtered built on a full-parse fake:
    keeps rows by the loader's predicate, diagnostics over ALL rows."""
    from bot.binance_research_data import agg_trade_gap_diagnostics

    def fake(payload, *, keep):
        rows = tuple(full_parse(payload))
        diag = dict(agg_trade_gap_diagnostics(rows))
        retained = tuple(r for r in rows if keep(int(r.timestamp)))
        diag.update(raw_rows=len(rows), retained_rows=len(retained),
                    first_ts=rows[0].timestamp if rows else None,
                    last_ts=rows[-1].timestamp if rows else None)
        return retained, diag
    return fake

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
        timeline = MetricsTimeline(rows)
        first_context, first_complete = derivatives_context_at(
            timeline,
            2500,
            previous_candidate_oi=None,
            max_age_ms=1000,
        )
        self.assertTrue(first_complete)
        self.assertIsNone(first_context["oi_delta"])
        self.assertEqual(
            first_context["oi_delta_reference"],
            "PREVIOUS_NEXUS_CANDIDATE",
        )

        context, complete = derivatives_context_at(
            timeline,
            2500,
            previous_candidate_oi=100.0,
            max_age_ms=1000,
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

        async def fake_download(url, *, cache_path=None, timeout_s=30.0, retries=0):
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

        def fake_parse(payload, *, source_date, expected_symbol=None):
            # The loader now consumes per-archive provenance as well.
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
            "bot.binance_oos_replay.parse_metrics_archive_with_provenance",
            side_effect=lambda payload, **kw: (
                fake_parse(payload, **kw),
                {"symbol": "BTCUSDT", "source_date": kw["source_date"], "convention": "END_LABEL",
                 "raw_rows": 1, "rows_after_dedup": 1, "exact_duplicates_dropped": 0,
                 "conflicting_duplicates": 0},
            ),
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

    def test_book_depth_loader_treats_404_as_observational_gap(self):
        async def fake_download(url, *, cache_path=None, timeout_s=30.0, retries=0):
            raise RuntimeError("Binance research archive HTTP 404")

        with patch(
            "bot.binance_oos_replay.download_archive_verified",
            side_effect=fake_download,
        ):
            rows, artifacts, missing = asyncio.run(
                load_verified_book_depth(
                    "BTCUSDT",
                    ("2026-08-31",),
                )
            )

        self.assertEqual(rows, [])
        self.assertEqual(artifacts, ())
        self.assertEqual(missing, ("2026-08-31",))

    def test_book_depth_loader_rejects_bad_concurrency(self):
        with self.assertRaises(ValueError):
            asyncio.run(
                load_verified_book_depth(
                    "BTCUSDT",
                    ("2026-08-31",),
                    concurrency=0,
                )
            )

    def test_agg_trades_loader_treats_404_as_observational_gap(self):
        async def fake_download(url, *, cache_path=None, timeout_s=30.0, retries=0):
            raise RuntimeError("Binance research archive HTTP 404")

        with patch(
            "bot.binance_oos_replay.download_archive_verified",
            side_effect=fake_download,
        ):
            rows, artifacts, missing, gaps = asyncio.run(
                load_verified_agg_trades(
                    "BTCUSDT",
                    ("2026-08-31",),
                )
            )

        self.assertEqual(rows, [])
        self.assertEqual(artifacts, ())
        self.assertEqual(missing, ("2026-08-31",))
        self.assertEqual(gaps, {})

    def test_agg_trades_loader_is_deterministic_under_concurrency(self):
        dates = ("2026-08-30", "2026-08-31")

        async def fake_download(url, *, cache_path=None, timeout_s=30.0, retries=0):
            if "08-30" in url:
                await asyncio.sleep(0.02)
                payload = b"day30"
                sha = "a" * 64
            else:
                payload = b"day31"
                sha = "b" * 64
            return SimpleNamespace(payload=payload, sha256=sha)

        def fake_parse(payload):
            if payload == b"day30":
                return (
                    AggTradeObservation(
                        10, 100.0, 1.0, 100, 100,
                        1_700_000_000_000, False,
                    ),
                )
            return (
                AggTradeObservation(
                    11, 101.0, 2.0, 101, 101,
                    1_700_086_400_000, True,
                ),
            )

        with patch(
            "bot.binance_oos_replay.download_archive_verified",
            side_effect=fake_download,
        ), patch(
            "bot.binance_oos_replay.parse_agg_trades_archive_filtered",
            side_effect=_filtered_fake(fake_parse),
        ):
            rows, artifacts, missing, gaps = asyncio.run(
                load_verified_agg_trades(
                    "BTCUSDT",
                    dates,
                    concurrency=2,
                )
            )

        self.assertEqual(
            [row.aggregate_trade_id for row in rows],
            [10, 11],
        )
        self.assertEqual(len(artifacts), 2)
        self.assertEqual(missing, ())
        self.assertEqual(sorted(gaps), list(dates))

    def test_agg_trades_loader_rejects_bad_concurrency(self):
        with self.assertRaises(ValueError):
            asyncio.run(
                load_verified_agg_trades(
                    "BTCUSDT",
                    ("2026-08-31",),
                    concurrency=9,
                )
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


class AggTradesWindowEquivalenceTests(unittest.TestCase):
    """Window filtering keeps exactly the rows AggTradeTimeline.pressure reads."""

    def test_filtered_pressure_equals_full_pressure_including_day_boundary(self):
        import random
        from bot.binance_historical_context import AggTradeTimeline
        rng = random.Random(4661)
        day0 = 1_767_225_600_000                      # 2026-01-01T00:00Z
        trades_by_day = {}
        for d, day in enumerate(("2026-01-01", "2026-01-02")):
            base = day0 + d * 86_400_000
            ts = sorted(base + rng.randint(0, 86_399_999) for _ in range(4000))
            ts += [base + 86_400_000 - 1_000, base + 86_400_000 - 120_000]   # tail near midnight
            trades_by_day[day] = tuple(sorted(
                (AggTradeObservation(d * 100_000 + i, 100.0 + rng.random(), 1.0 + rng.random(),
                                     i, i, t, rng.random() < 0.5)
                 for i, t in enumerate(sorted(ts))),
                key=lambda r: r.timestamp))
        decisions = [day0 + 86_400_000 + 60_000,                        # 00:01 day 2 -> window crosses midnight
                     day0 + 3_600_000 * 5, day0 + 86_400_000 + 3_600_000 * 13]

        async def fake_download(url, *, cache_path=None, timeout_s=30.0, retries=0):
            self.assertIsNone(cache_path, "aggTrades archives are not cached on disk")
            self.assertGreaterEqual(timeout_s, 600)
            self.assertGreaterEqual(retries, 1)
            day = "2026-01-01" if "2026-01-01" in url else "2026-01-02"
            return SimpleNamespace(payload=day.encode(), sha256="c" * 64)

        with patch("bot.binance_oos_replay.download_archive_verified", side_effect=fake_download), \
                patch("bot.binance_oos_replay.parse_agg_trades_archive_filtered",
                      side_effect=_filtered_fake(lambda payload: trades_by_day[payload.decode()])):
            filtered, _, _, gaps = asyncio.run(load_verified_agg_trades(
                "BTCUSDT", ("2026-01-01", "2026-01-02"), decision_timestamps=decisions))
            full, _, _, _ = asyncio.run(load_verified_agg_trades(
                "BTCUSDT", ("2026-01-01", "2026-01-02")))
        self.assertLess(len(filtered), len(full) // 10, "memory bounded")
        a, b = AggTradeTimeline(filtered), AggTradeTimeline(full)
        for ts in decisions:
            self.assertEqual(a.pressure(ts), b.pressure(ts))
        self.assertEqual(gaps["2026-01-02"]["rows_in_archive"], len(trades_by_day["2026-01-02"]))


class DownloadRetryTests(unittest.TestCase):
    def test_transient_timeout_is_retried_and_checksum_still_verified(self):
        from bot import binance_research_data as data
        calls = []

        async def flaky(url, timeout_s):
            calls.append(url)
            if len([c for c in calls if not c.endswith(".CHECKSUM")]) == 1 and not url.endswith(".CHECKSUM"):
                raise asyncio.TimeoutError()
            return b"payload" if not url.endswith(".CHECKSUM") else (
                data.archive_sha256(b"payload") + "  BTCUSDT-aggTrades-2026-01-01.zip").encode()
        url = data.daily_agg_trades_url("BTCUSDT", "2026-01-01")
        with patch.object(data, "_http_get_bytes", side_effect=flaky), \
                patch.object(asyncio, "sleep", AsyncMock()):
            archive = asyncio.run(data.download_archive_verified(url, retries=2))
        self.assertEqual(archive.sha256, data.archive_sha256(b"payload"))

    def test_http_error_and_checksum_mismatch_are_not_retried(self):
        from bot import binance_research_data as data
        url = data.daily_agg_trades_url("BTCUSDT", "2026-01-01")
        calls = []

        async def not_found(u, timeout_s):
            calls.append(u)
            raise RuntimeError("Binance research archive HTTP 404")
        with patch.object(data, "_http_get_bytes", side_effect=not_found):
            with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
                asyncio.run(data.download_archive_verified(url, retries=3))
        self.assertLessEqual(len(calls), 2, "one attempt per URL")

        async def bad(u, timeout_s):
            return b"payload" if not u.endswith(".CHECKSUM") else ("0" * 64 + "  x.zip").encode()
        with patch.object(data, "_http_get_bytes", side_effect=bad):
            with self.assertRaises(ValueError):
                asyncio.run(data.download_archive_verified(url, retries=3))
