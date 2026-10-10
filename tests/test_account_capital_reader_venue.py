"""P1-1 (full audit 2026-09-26): capital read must use the active venue."""
import asyncio
import unittest
from unittest.mock import patch

from bot.account_capital_reader import read_account_capital

BINANCE_ACCOUNT = {
    "totalMarginBalance": "9.8410", "totalWalletBalance": "9.8410",
    "availableBalance": "9.5", "totalUnrealizedProfit": "0",
    "totalPositionInitialMargin": "0.2", "totalOpenOrderInitialMargin": "0.1",
    "totalMaintMargin": "0.01", "totalCrossWalletBalance": "9.8410",
    "totalCrossUnPnl": "0", "multiAssetsMargin": False, "canTrade": True,
}


class CapitalReaderVenueTests(unittest.TestCase):
    def test_binance_uses_fapi_account_never_kucoin_endpoint(self):
        from bot import binance
        client = binance.BinanceClient()
        calls = []

        async def fake_request(method, endpoint, params=None, **kw):
            calls.append(endpoint)
            if endpoint == "/fapi/v3/account":
                return dict(BINANCE_ACCOUNT)
            if endpoint == "/fapi/v1/accountConfig":
                return {"canTrade": True, "multiAssetsMargin": False}
            raise RuntimeError(f"Binance GET {endpoint} HTTP 404")

        with patch.object(client, "_request", side_effect=fake_request):
            snap = asyncio.run(read_account_capital(client))
        self.assertEqual(calls, ["/fapi/v3/account", "/fapi/v1/accountConfig"])
        self.assertAlmostEqual(snap.capital.equity, 9.841)
        self.assertAlmostEqual(snap.capital.available_collateral, 9.5)
        self.assertAlmostEqual(snap.capital.position_margin, 0.2)
        self.assertAlmostEqual(snap.capital.order_margin, 0.1)

    def test_kucoin_style_client_keeps_account_overview(self):
        calls = []

        class Client:
            async def _get(self, endpoint, params=None, auth=False):
                calls.append((endpoint, params, auth))
                return {"accountEquity": 100.0, "availableMargin": 80.0,
                        "positionMargin": 15.0, "orderMargin": 5.0}

        snap = asyncio.run(read_account_capital(Client()))
        self.assertEqual(calls, [("/api/v1/account-overview", {"currency": "USDT"}, True)])
        self.assertEqual(snap.capital.equity, 100.0)

    def test_venue_read_failure_still_fails_closed(self):
        class Client:
            async def get_account_state(self):
                raise RuntimeError("Binance account state unavailable")

        with self.assertRaises(RuntimeError):
            asyncio.run(read_account_capital(Client()))

    def test_invalid_venue_payload_fails_closed(self):
        class Client:
            async def get_account_state(self):
                return {"accountEquity": float("nan"), "availableMargin": 1.0}

        with self.assertRaises(ValueError):
            asyncio.run(read_account_capital(Client()))


if __name__ == "__main__":
    unittest.main()
