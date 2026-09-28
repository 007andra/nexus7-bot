"""Audit P0-6: 50x liquidation safety at the final Binance pre-dispatch boundary.

Proves with Binance leverage brackets/account state that a LIVE entry is
fail-closed when the configured leverage is not offered for the bracket, when
the protective stop cannot act before the maintenance zone, or when the data
needed to decide is missing. No liquidation price is invented.
"""
import asyncio
import unittest
from types import SimpleNamespace

from bot import binance_cross_portfolio_stress as stress
from bot.config import cfg


def _brackets(symbol, max_lev=50, mmr=0.01):
    return {
        "symbol": symbol,
        "source": "BINANCE_FAPI_LEVERAGE_BRACKET",
        "brackets": [{
            "bracket": 1, "initialLeverage": max_lev, "notionalFloor": 0.0,
            "notionalCap": 5000.0, "maintMarginRatio": mmr, "cum": 0.0,
        }],
    }


class _Client:
    def __init__(self, *, wallet=10.0, max_lev=50, brackets_ok=True, can_trade=True):
        self.wallet, self.max_lev, self.brackets_ok, self.can_trade = wallet, max_lev, brackets_ok, can_trade

    async def get_leverage_brackets(self, symbol):
        if not self.brackets_ok:
            raise RuntimeError("BINANCE_LEVERAGE_BRACKET_UNAVAILABLE")
        return _brackets(str(symbol).upper(), self.max_lev)

    async def get_account_state(self):
        return {"crossWalletBalance": self.wallet, "orderMargin": 0.0,
                "multiAssetsMargin": False, "canTrade": self.can_trade}

    async def get_symbol_config(self, symbol):
        return {"marginType": "CROSSED"}

    async def get_positions(self):
        return []


def _engine(client):
    return SimpleNamespace(paper_trade=False, positions={}, client=client,
                           instruments={"ATOMUSDT": {"qtyStep": 0.01}})


def _sig(stop_pct=0.004):
    return SimpleNamespace(symbol="ATOMUSDT", direction="LONG", entry=4.0, sl=4.0 * (1 - stop_pct))


class Liquidation50xTests(unittest.TestCase):
    def setUp(self):
        self.old = cfg.LEVERAGE
        cfg.LEVERAGE = 50

    def tearDown(self):
        cfg.LEVERAGE = self.old

    def _eval(self, client, qty, sig=None):
        return asyncio.run(stress.evaluate(_engine(client), sig or _sig(), qty))

    def test_risk_sized_50x_entry_passes_with_bracket_evidence(self):
        # ~0.16 USDT loss at stop on a 10 USDT wallet (risk-budget sized qty).
        result = self._eval(_Client(), qty=4.0)
        self.assertTrue(result.allowed, result)
        self.assertLess(result.risk_rate, stress.MAX_STOP_STRESS_RISK_RATE)
        self.assertIn("maxlev_50", result.reason)

    def test_configured_leverage_above_bracket_blocks(self):
        result = self._eval(_Client(max_lev=25), qty=4.0)
        self.assertFalse(result.allowed)
        self.assertIn("configured_leverage_exceeds_bracket", result.reason)

    def test_stop_inside_maintenance_zone_blocks(self):
        # Oversized position (the pre-audit operator-margin size class): the
        # loss at the stop leaves the wallet at/under maintenance -> block.
        result = self._eval(_Client(wallet=1.0), qty=60.0, sig=_sig(stop_pct=0.004))
        self.assertFalse(result.allowed)

    def test_missing_brackets_block_without_inventing_liquidation(self):
        result = self._eval(_Client(brackets_ok=False), qty=4.0)
        self.assertFalse(result.allowed)
        self.assertTrue(result.reason.startswith("bracket_"))

    def test_account_not_tradeable_blocks(self):
        result = self._eval(_Client(can_trade=False), qty=4.0)
        self.assertFalse(result.allowed)
        self.assertIn("account_trading_disabled", result.reason)

    def test_invalid_qty_blocks(self):
        for qty in (0.0, -1.0, float("nan")):
            self.assertFalse(self._eval(_Client(), qty=qty).allowed)


if __name__ == "__main__":
    unittest.main()
