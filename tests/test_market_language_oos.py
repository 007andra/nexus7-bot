import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bot.market_language_binance_replay import fetch_binance_klines
from bot.market_language_oos import (
    OOSPoint,
    evaluate_market_language_oos,
    market_language_promotion_decision,
    summarize_oos_points,
)


def _candles(n=260):
    rows = []
    p = 100.0
    for i in range(n):
        c = p * (1.002 if i % 2 == 0 else 0.999)
        rows.append({
            "ts": 1_700_000_000_000 + i * 900_000,
            "o": p,
            "h": max(p, c) * 1.001,
            "l": min(p, c) * 0.999,
            "c": c,
            "v": 1000 + i,
        })
        p = c
    return rows


class _FakeBinance:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    async def get(self, path, params):
        self.calls.append((path, dict(params)))
        return self.pages.pop(0) if self.pages else []


class MarketLanguageOOSTests(unittest.TestCase):
    def test_evaluation_is_prefix_only_and_labels_do_not_overlap(self):
        rows = _candles(120)
        seen_lengths = []

        def fake_forecast(prefix, *, horizon, sample_count):
            seen_lengths.append(len(prefix))
            return SimpleNamespace(
                available=True,
                probability_up=0.8,
                probability_down=0.2,
            )

        with patch("bot.market_language_oos.forecast_market_language", fake_forecast):
            rep = evaluate_market_language_oos(
                rows,
                symbol="BTCUSDT",
                warmup=81,
                horizon=4,
                step=4,
                cost_fraction=0.001,
                bootstrap_samples=50,
            )
        self.assertTrue(rep.points)
        self.assertEqual(seen_lengths, [p.index for p in rep.points])
        self.assertTrue(all(
            b.index - a.index >= 4
            for a, b in zip(rep.points, rep.points[1:])
        ))

    def test_cost_is_subtracted_once_from_round_trip_directional_return(self):
        points = [
            OOSPoint(100, None, 0.8, True, 0.01, True, 1, 0.01, 0.008),
            OOSPoint(104, None, 0.2, True, -0.02, False, -1, 0.02, 0.018),
        ]
        rep = summarize_oos_points(
            "BTCUSDT",
            points,
            horizon=4,
            step=4,
            round_trip_cost_fraction=0.002,
            bootstrap_samples=50,
        )
        self.assertAlmostEqual(rep.mean_gross_directional_return, 0.015)
        self.assertAlmostEqual(rep.mean_net_directional_return, 0.013)
        self.assertEqual(rep.directional_accuracy_when_available, 1.0)

    def test_promotion_fails_closed_without_statistical_net_edge(self):
        points = [
            OOSPoint(i, None, 0.55, True, 0.001, True, 1, 0.001, -0.001)
            for i in range(100)
        ]
        rep = summarize_oos_points(
            "BTCUSDT",
            points,
            horizon=1,
            step=1,
            round_trip_cost_fraction=0.002,
            bootstrap_samples=100,
        )
        ok, blockers = market_language_promotion_decision(
            rep, min_evaluated=50, min_available=50, min_coverage=0.5
        )
        self.assertFalse(ok)
        self.assertIn("NO_POSITIVE_NET_RETURN", blockers)
        self.assertIn("NET_RETURN_NOT_STATISTICALLY_POSITIVE", blockers)

    def test_binance_fetch_is_public_paginated_deduplicated_and_closed_only(self):
        now = 10_000_000
        page1 = [
            [8_000_000, "100", "102", "99", "101", "10", 8_899_999],
            [9_000_000, "101", "103", "100", "102", "11", 10_100_000],
        ]
        page2 = [
            [7_000_000, "99", "101", "98", "100", "9", 7_899_999],
            [8_000_000, "100", "102", "99", "101", "10", 8_899_999],
        ]
        client = _FakeBinance([page1, page2, []])
        rows = asyncio.run(fetch_binance_klines(
            client, "BTCUSDT", limit=3, now_ms=now
        ))
        self.assertEqual([r["ts"] for r in rows], [7_000_000, 8_000_000])
        self.assertTrue(all(call[0] == "/fapi/v1/klines" for call in client.calls))
        self.assertTrue(all("endTime" in call[1] for call in client.calls))


if __name__ == "__main__":
    unittest.main()
