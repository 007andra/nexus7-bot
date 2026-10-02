import unittest

from bot.binance_instrument_snapshot import (
    snapshot_exchange_info,
    snapshot_symbol,
)


def _symbol():
    return {
        "symbol": "BTCUSDT",
        "status": "TRADING",
        "contractType": "PERPETUAL",
        "marginAsset": "USDT",
        "onboardDate": 1_600_000_000_000,
        "filters": [
            {
                "filterType": "PRICE_FILTER",
                "minPrice": "0.10",
                "maxPrice": "1000000",
                "tickSize": "0.10",
            },
            {
                "filterType": "LOT_SIZE",
                "minQty": "0.001",
                "maxQty": "1000",
                "stepSize": "0.001",
            },
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
        ],
    }


class BinanceInstrumentSnapshotTests(unittest.TestCase):
    def test_snapshot_extracts_trade_filters(self):
        snap = snapshot_symbol(_symbol(), observed_at_ms=1_700_000_000_000)
        self.assertEqual(snap.symbol, "BTCUSDT")
        self.assertEqual(snap.qty_step, 0.001)
        self.assertEqual(snap.min_notional, 5.0)
        self.assertEqual(len(snap.fingerprint), 64)

    def test_exchange_snapshot_is_sorted_and_unique(self):
        eth = dict(_symbol())
        eth["symbol"] = "ETHUSDT"
        rows = snapshot_exchange_info(
            {"symbols": [eth, _symbol()]},
            observed_at_ms=1_700_000_000_000,
        )
        self.assertEqual([row.symbol for row in rows], ["BTCUSDT", "ETHUSDT"])


if __name__ == "__main__":
    unittest.main()
