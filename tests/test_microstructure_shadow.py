import unittest
from unittest.mock import AsyncMock

from bot.microstructure_shadow import build_snapshot, collect_binance_snapshot
from bot.opportunity_ranker import Opportunity, with_microstructure


class MicrostructureShadowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 1_700_000_005_000
        self.depth = {
            "E": self.now - 100,
            "bids": [["99.99", "20"], ["99.95", "10"], ["99.00", "5"]],
            "asks": [["100.01", "5"], ["100.05", "5"], ["101.00", "5"]],
        }
        self.trades = [
            {"T": self.now - 200, "p": "100.00", "q": "10", "m": False},
            {"T": self.now - 100, "p": "100.00", "q": "2", "m": True},
        ]

    def test_snapshot_computes_directional_book_and_taker_pressure(self):
        snap = build_snapshot("BTCUSDT", self.depth, self.trades, observed_at_ms=self.now)
        self.assertTrue(snap.complete)
        self.assertGreater(snap.book_imbalance, 0)
        self.assertGreater(snap.taker_pressure, 0)
        self.assertGreater(snap.microstructure_alignment, 0)
        self.assertGreater(snap.depth_notional_100bps, 0)
        self.assertEqual(snap.execution_effect, "NONE")
        self.assertFalse(snap.promotion_authority)
        self.assertEqual(len(snap.feature_fingerprint()), 64)
        self.assertEqual(
            snap.feature_fingerprint(),
            build_snapshot(
                "BTCUSDT", self.depth, self.trades,
                observed_at_ms=self.now,
            ).feature_fingerprint(),
        )

    def test_stale_book_fails_closed(self):
        depth = dict(self.depth)
        depth["E"] = self.now - 10_000
        with self.assertRaisesRegex(ValueError, "stale order book"):
            build_snapshot("BTCUSDT", depth, self.trades, observed_at_ms=self.now)

    def test_future_trade_fails_closed(self):
        trades = list(self.trades) + [{"T": self.now + 1, "p": "100", "q": "1", "m": False}]
        with self.assertRaisesRegex(ValueError, "invalid trade timestamp"):
            build_snapshot("BTCUSDT", self.depth, trades, observed_at_ms=self.now)

    def test_no_fresh_trades_fails_closed(self):
        trades = [{"T": self.now - 20_000, "p": "100", "q": "1", "m": False}]
        with self.assertRaisesRegex(ValueError, "no fresh trades"):
            build_snapshot("BTCUSDT", self.depth, trades, observed_at_ms=self.now)

    async def test_collector_uses_public_binance_reads_only(self):
        client = type("Client", (), {})()
        client._get = AsyncMock(side_effect=[self.depth, self.trades])
        snap = await collect_binance_snapshot(client, "BTCUSDT", observed_at_ms=self.now)
        self.assertTrue(snap.complete)
        self.assertEqual(client._get.await_count, 2)
        for call in client._get.await_args_list:
            self.assertFalse(call.kwargs["auth"])
        self.assertEqual(client._get.await_args_list[0].args[0], "/fapi/v1/depth")
        self.assertEqual(client._get.await_args_list[1].args[0], "/fapi/v1/aggTrades")

    def test_snapshot_binds_to_shadow_ranker_without_mutating_base(self):
        snap = build_snapshot("BTCUSDT", self.depth, self.trades, observed_at_ms=self.now)
        base = Opportunity("cid", "BTCUSDT", 1.0, 2.0, 80, 50, 80, 0.001, side="LONG", confidence=70)
        enriched = with_microstructure(base, snap)
        self.assertIsNone(base.microstructure_alignment)
        self.assertEqual(enriched.microstructure_alignment, snap.microstructure_alignment)
        self.assertEqual(enriched.depth_notional_1pct, snap.depth_notional_100bps)

    def test_symbol_mismatch_fails_closed(self):
        snap = build_snapshot("BTCUSDT", self.depth, self.trades, observed_at_ms=self.now)
        item = Opportunity("cid", "ETHUSDT", 1.0, 2.0, 80, 50, 80, 0.001)
        with self.assertRaisesRegex(ValueError, "symbol mismatch"):
            with_microstructure(item, snap)


if __name__ == "__main__":
    unittest.main()
