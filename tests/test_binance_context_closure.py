"""Cross-midnight causal context closure and streaming aggTrades parsing.

INV-CONTEXT-DATE-CLOSURE-001, INV-AGG-WINDOW-001, INV-STREAM-PARITY-001,
INV-NO-FUTURE-AGG-001.
"""
import asyncio
import io
import random
import unittest
import zipfile
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import binance_research_data as data
from bot.binance_historical_context import (
    AggTradeTimeline, BookDepthBand, BookDepthSnapshot, BookDepthTimeline,
    shadow_microstructure_context,
)
from bot.binance_oos_replay import (
    AGG_TRADES_WINDOW_MS, BOOK_DEPTH_MAX_AGE_MS, load_verified_agg_trades,
    plan_context_archives, required_archive_dates,
)
from bot.binance_research_data import (
    AggTradeObservation, agg_trade_gap_diagnostics, parse_agg_trades_archive,
    parse_agg_trades_archive_filtered,
)

W = AGG_TRADES_WINDOW_MS


def ms(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _zip(rows, header=True):
    text = ("agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n"
            if header else "")
    text += "".join(",".join(str(v) for v in r) + "\n" for r in rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("BTCUSDT-aggTrades.csv", text)
    return buf.getvalue()


def _rows(timestamps, start_id=1, gap_every=0):
    out, agg, trade = [], start_id, start_id * 10
    for i, ts in enumerate(timestamps):
        if gap_every and i and i % gap_every == 0:
            agg += 2
            trade += 3
        out.append((agg, 100.0 + i % 7, 0.5 + i % 3, trade, trade + 1, ts, "true" if i % 2 else "false"))
        agg += 1
        trade += 2
    return out


def oracle_retained(trade_ts, decisions):
    return any(d - W < trade_ts <= d for d in decisions)


class RequiredDatesTests(unittest.TestCase):
    def test_A_candidate_0002_requires_previous_day_aggtrades(self):
        self.assertEqual(required_archive_dates([ms("2026-02-02T00:02:00")], W),
                         ("2026-02-01", "2026-02-02"))
        plan = plan_context_archives([ms("2026-02-02T00:02:00")], [(2026, 2)], include_agg_trades=True)
        self.assertEqual(plan["agg_trade_dates"], ("2026-02-01", "2026-02-02"))

    def test_window_not_crossing_midnight_needs_only_its_day(self):
        self.assertEqual(required_archive_dates([ms("2026-02-02T00:05:00")], W), ("2026-02-02",))
        self.assertEqual(required_archive_dates([ms("2026-02-02T13:00:00")], W), ("2026-02-02",))

    def test_exclusive_lower_bound_decides_dates(self):
        # (00:05 - 5m, 00:05] = (00:00, 00:05] -> 00:00:00.000 excluded -> only day 2
        self.assertEqual(required_archive_dates([ms("2026-02-02T00:05:00")], W), ("2026-02-02",))
        # (23:59:59.999, 00:04:59.999]: earliest admissible trade is 00:00:00.000
        self.assertEqual(required_archive_dates([ms("2026-02-02T00:04:59.999")], W), ("2026-02-02",))
        # (23:59:59.998, 00:04:59.998]: 23:59:59.999 is admissible -> previous day needed
        self.assertEqual(required_archive_dates([ms("2026-02-02T00:04:59.998")], W),
                         ("2026-02-01", "2026-02-02"))

    def test_D_bookdepth_early_day_requires_previous_day(self):
        d = ms("2026-02-02T00:05:00")
        self.assertEqual(required_archive_dates([d], BOOK_DEPTH_MAX_AGE_MS, lower_inclusive=True),
                         ("2026-02-01", "2026-02-02"))
        plan = plan_context_archives([d], [(2026, 2)], include_agg_trades=False)
        self.assertEqual(plan["book_depth_dates"], ("2026-02-01", "2026-02-02"))

    def test_F_first_evaluation_day_loads_context_only(self):
        plan = plan_context_archives([ms("2026-01-01T00:03:00")], [(2026, 1)], include_agg_trades=True)
        self.assertIn("2025-12-31", plan["agg_trade_dates"])
        self.assertEqual(plan["context_lookback_dates"], ("2025-12-31",))
        self.assertNotIn("2025-12-31", plan["evaluation_dates"])
        self.assertEqual(min(plan["evaluation_dates"]), "2026-01-01")

    def test_property_dates_cover_every_window_near_midnight_month_and_year_end(self):
        rng = random.Random(466)
        anchors = [ms("2026-02-01T00:00:00"), ms("2026-03-01T00:00:00"), ms("2027-01-01T00:00:00"),
                   ms("2026-06-30T00:00:00")]
        for _ in range(3000):
            d = rng.choice(anchors) + rng.randint(-20 * 60_000, 20 * 60_000)
            for lookback, inclusive in ((W, False), (BOOK_DEPTH_MAX_AGE_MS, True)):
                dates = set(required_archive_dates([d], lookback, lower_inclusive=inclusive))
                lo = d - lookback + (0 if inclusive else 1)
                for t in (lo, d, (lo + d) // 2):
                    day = datetime.fromtimestamp(t / 1000, tz=timezone.utc).date().isoformat()
                    self.assertIn(day, dates)
                self.assertLessEqual(len(dates), 2)


class WindowSemanticsTests(unittest.TestCase):
    D = ms("2026-02-02T00:02:00")
    POINTS = [("2026-02-01T23:57:00.000", False), ("2026-02-01T23:57:00.001", True),
              ("2026-02-01T23:59:59.999", True), ("2026-02-02T00:00:00.000", True),
              ("2026-02-02T00:02:00.000", True), ("2026-02-02T00:02:00.001", False)]

    def _load(self, days):
        payloads = {}
        for day in days:
            pts = [ms(t) for t, _ in self.POINTS if t.startswith(day)]
            payloads[day] = _zip(_rows(pts, start_id=1 if day.endswith("01") else 1000))

        async def fake_download(url, **kw):
            for day, payload in payloads.items():
                if day in url:
                    return SimpleNamespace(payload=payload, sha256="d" * 64)
            raise RuntimeError("Binance research archive HTTP 404")
        with patch("bot.binance_oos_replay.download_archive_verified", side_effect=fake_download):
            return asyncio.run(load_verified_agg_trades(
                "BTCUSDT", required_archive_dates([self.D], W), decision_timestamps=[self.D]))

    def test_A_B_C_P_cross_midnight_window_exact_bounds(self):
        rows, _, missing, gaps = self._load(["2026-02-01", "2026-02-02"])
        kept = sorted(r.timestamp for r in rows)
        expected = sorted(ms(t) for t, inside in self.POINTS if inside)
        self.assertEqual(kept, expected)                     # A, B (lower excl.), C (upper incl.), P (no future)
        self.assertEqual(missing, ())
        self.assertEqual(AggTradeTimeline(rows).pressure(self.D)["rows"], 4)

    def test_G_missing_previous_day_archive_is_missing_context(self):
        rows, _, missing, _ = self._load(["2026-02-02"])
        self.assertEqual(missing, ("2026-02-01",))
        self.assertTrue(set(required_archive_dates([self.D], W)) & set(missing),
                        "the replay marks this decision CONTEXT_ARCHIVE_MISSING, never a value")

    def test_E_bookdepth_snapshot_older_than_15m_is_refused(self):
        d = ms("2026-02-02T00:05:00")
        for age_min, ok in ((10, True), (15, True), (16, False)):
            ts = d - age_min * 60_000
            bands = tuple(BookDepthBand(ts, p, 10, 1000.0 * (1 + abs(p)), "x") for p in (-5, -2, -1, 1, 2, 5))
            snap = BookDepthTimeline([BookDepthSnapshot(ts, "x", bands, "OK")]).asof(d)
            ctx = shadow_microstructure_context(snap, None, None, decision_ts_ms=d, side="LONG")
            self.assertEqual(bool(ctx.get("depth_available")), ok, age_min)


class StreamingParserTests(unittest.TestCase):
    def _dataset(self, n=3000, seed=7):
        rng = random.Random(seed)
        base = ms("2026-02-01T00:00:00")
        ts = sorted(base + rng.randint(0, 86_399_999) for _ in range(n))
        return _rows(ts, gap_every=97)

    def test_I_J_stream_equals_full_parse_plus_filter_and_full_diagnostics(self):
        rows = self._dataset()
        payload = _zip(rows)
        rng = random.Random(3)
        decisions = sorted(rows[rng.randrange(len(rows))][5] + rng.randint(-1000, 300_000) for _ in range(25))
        keep = lambda t: oracle_retained(t, decisions)  # noqa: E731
        full = parse_agg_trades_archive(payload)
        stream, diag = parse_agg_trades_archive_filtered(payload, keep=keep)
        self.assertEqual(stream, tuple(r for r in full if keep(r.timestamp)))     # I
        oracle_diag = agg_trade_gap_diagnostics(full)
        self.assertEqual({k: diag[k] for k in oracle_diag}, oracle_diag)          # J
        self.assertGreater(diag["aggregate_id_gap_events"], 0)
        self.assertEqual((diag["raw_rows"], diag["first_ts"], diag["last_ts"]),
                         (len(full), full[0].timestamp, full[-1].timestamp))

    def test_K_no_full_materialization_objects_only_for_retained_rows(self):
        rows = self._dataset(n=5000)
        payload = _zip(rows)
        decisions = [rows[2500][5]]
        built = []
        real = data.AggTradeObservation

        def counting(*a, **k):
            built.append(1)
            return real(*a, **k)
        with patch.object(data, "_csv_rows_from_zip",
                          side_effect=AssertionError("full CSV materialized")), \
                patch.object(data, "AggTradeObservation", side_effect=counting):
            kept, diag = parse_agg_trades_archive_filtered(
                payload, keep=lambda t: oracle_retained(t, decisions))
        self.assertEqual(len(built), len(kept))
        self.assertLess(diag["retained_rows"] * 50, diag["raw_rows"])

    def test_H_overlapping_windows_retain_each_trade_once(self):
        base = ms("2026-02-01T12:00:00")
        rows = _rows([base + i * 1000 for i in range(600)])
        decisions = [base + 300_000, base + 360_000, base + 420_000]     # heavy overlap
        kept, _ = parse_agg_trades_archive_filtered(
            _zip(rows), keep=lambda t: oracle_retained(t, decisions))
        ids = [r.aggregate_trade_id for r in kept]
        self.assertEqual(len(ids), len(set(ids)))
        tl = AggTradeTimeline(kept)
        full_tl = AggTradeTimeline(parse_agg_trades_archive(_zip(rows)))
        for d in decisions:
            self.assertEqual(tl.pressure(d), full_tl.pressure(d))

    def test_O_malformed_csv_is_fatal(self):
        base = ms("2026-02-01T12:00:00")
        good = _rows([base, base + 1])
        for bad in ([(*good[0][:6], "maybe")], [good[0][:5]], [(good[0][0], "nan", *good[0][2:])],
                    [good[1], good[0]]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_agg_trades_archive_filtered(_zip(bad), keep=lambda t: True)
        with self.assertRaises(ValueError):
            parse_agg_trades_archive_filtered(b"not a zip", keep=lambda t: True)

    def test_property_retained_iff_inside_some_window(self):
        rng = random.Random(4662)
        for anchor in ("2026-02-01T00:00:00", "2026-03-01T00:00:00", "2027-01-01T00:00:00"):
            a = ms(anchor)
            ts = sorted(a + rng.randint(-900_000, 900_000) for _ in range(2000))
            rows = _rows(ts)
            decisions = sorted(a + rng.randint(-600_000, 600_000) for _ in range(8))
            decisions += [rows[5][5], rows[5][5] + W]          # exact boundaries
            kept, _ = parse_agg_trades_archive_filtered(
                _zip(rows), keep=lambda t: any(d - W < t <= d for d in decisions))
            kept_ids = {r.aggregate_trade_id for r in kept}
            for r in rows:
                self.assertEqual(r[0] in kept_ids, oracle_retained(r[5], decisions))

    def test_loader_predicate_matches_oracle(self):
        # The loader's bisect predicate (not just the test lambda) is exact.
        base = ms("2026-02-01T23:50:00")
        ts = [base + i * 997 for i in range(1500)]
        decisions = [ms("2026-02-02T00:02:00"), ts[100] + W, ts[200]]
        payload = _zip(_rows(ts))

        async def fake_download(url, **kw):
            return SimpleNamespace(payload=payload, sha256="e" * 64)
        with patch("bot.binance_oos_replay.download_archive_verified", side_effect=fake_download):
            rows, _, _, gaps = asyncio.run(load_verified_agg_trades(
                "BTCUSDT", ("2026-02-01",), decision_timestamps=decisions))
        self.assertEqual(sorted(r.timestamp for r in rows),
                         [t for t in ts if oracle_retained(t, decisions)])
        prov = gaps["2026-02-01"]
        self.assertEqual((prov["cache"], prov["raw_rows"], prov["symbol"]), (False, 1500, "BTCUSDT"))
        self.assertEqual(prov["retained_rows"], len(rows))


class DownloadSemanticsTests(unittest.TestCase):
    URL = data.daily_agg_trades_url("BTCUSDT", "2026-01-01")

    def test_L_checksum_mismatch_fatal_and_not_retried(self):
        calls = []

        async def bad(u, timeout_s):
            calls.append(u)
            return b"payload" if not u.endswith(".CHECKSUM") else ("0" * 64 + "  x.zip").encode()
        with patch.object(data, "_http_get_bytes", side_effect=bad):
            with self.assertRaises(ValueError):
                asyncio.run(data.download_archive_verified(self.URL, retries=3))
        self.assertEqual(len(calls), 2)

    def test_M_http_404_not_retried(self):
        calls = []

        async def nf(u, timeout_s):
            calls.append(u)
            raise RuntimeError("Binance research archive HTTP 404")
        with patch.object(data, "_http_get_bytes", side_effect=nf):
            with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
                asyncio.run(data.download_archive_verified(self.URL, retries=3))
        self.assertLessEqual(len(calls), 2)

    def test_N_timeout_retried_then_verified(self):
        calls = []

        async def flaky(u, timeout_s):
            calls.append(u)
            if not u.endswith(".CHECKSUM") and calls.count(u) == 1:
                raise asyncio.TimeoutError()
            return b"payload" if not u.endswith(".CHECKSUM") else (
                data.archive_sha256(b"payload") + "  BTCUSDT-aggTrades-2026-01-01.zip").encode()
        with patch.object(data, "_http_get_bytes", side_effect=flaky), \
                patch.object(asyncio, "sleep", AsyncMock()):
            archive = asyncio.run(data.download_archive_verified(self.URL, retries=2))
        self.assertEqual(archive.sha256, data.archive_sha256(b"payload"))


class CrossArchiveTests(unittest.TestCase):
    def test_duplicate_aggregate_id_across_archives_fails_closed(self):
        d = ms("2026-02-02T00:01:00")
        rows_a = _rows([d - 30_000], start_id=5)
        rows_b = _rows([d - 10_000], start_id=5)            # same agg id in next day's archive
        payloads = {"2026-02-01": _zip(rows_a), "2026-02-02": _zip(rows_b)}

        async def fake_download(url, **kw):
            day = "2026-02-01" if "2026-02-01" in url else "2026-02-02"
            return SimpleNamespace(payload=payloads[day], sha256="f" * 64)
        with patch("bot.binance_oos_replay.download_archive_verified", side_effect=fake_download):
            with self.assertRaisesRegex(ValueError, "duplicate aggregate trade id"):
                asyncio.run(load_verified_agg_trades(
                    "BTCUSDT", ("2026-02-01", "2026-02-02"), decision_timestamps=[d]))


if __name__ == "__main__":
    unittest.main()
