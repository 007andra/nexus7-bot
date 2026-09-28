"""Forensic proof: external/manual Binance position PnL contaminates NEXUS HWM.

This is an audit-only reproduction of the current runtime semantics. It does not
change drawdown policy. The real LIVE refresh path uses Binance account equity
(totalMarginBalance) as the durable HWM input, while external-position ownership
is enforced by a separate guard. This test proves that external unrealized/realized
PnL can therefore raise and later draw down the NEXUS performance HWM even when no
BGX-owned trade exists.
"""
import logging
import unittest

import aiosqlite

from bot import database as db
from bot import drawdown_persistence as ddp
from bot import pilot_live_runtime as plr
from bot.professional_risk_adapter import ProfessionalRiskAdapter
from bot.risk import RiskManager


LOG = logging.getLogger("test_external_position_drawdown_contamination")
T0 = 1_790_100_000_000


class ExternalPositionBinance:
    """Minimal Binance-shaped read surface.

    wallet + unrealized models totalMarginBalance, exactly the field normalized
    into account equity by bot.binance.BinanceClient.get_account_state().
    """

    def __init__(self, wallet=6.4680):
        self.wallet = float(wallet)
        self.unrealized = 0.0
        self.position_margin = 0.0
        self.now = T0
        self.rows = []
        self.position_reads = 0
        self._tran = 9000

    def _listen_key_request(self):
        return None

    def _now_ms(self):
        return self.now

    async def _get(self, endpoint, params=None, auth=False):
        assert endpoint == "/fapi/v1/income", endpoint
        start, end = int(params["startTime"]), int(params["endTime"])
        return [dict(r) for r in self.rows if start <= int(r["time"]) <= end]

    async def get_account_state(self):
        equity = self.wallet + self.unrealized
        return {
            "equity": equity,
            "available": max(0.0, equity - self.position_margin),
            "available_source": "availableBalance",
            "walletBalance": self.wallet,
            "unrealisedPNL": self.unrealized,
            "positionMargin": self.position_margin,
            "orderMargin": 0.0,
            "multiAssetsMargin": False,
            "canTrade": True,
        }

    async def get_positions(self):
        # The drawdown refresh path must not need this to reproduce the issue.
        self.position_reads += 1
        raise AssertionError("drawdown refresh unexpectedly queried position ownership")

    def advance(self, ms=120_000):
        self.now += ms

    def realized_external_pnl(self, amount):
        self.now += 1_000
        self._tran += 1
        self.wallet += float(amount)
        self.rows.append(
            {
                "symbol": "ATOMUSDT",
                "incomeType": "REALIZED_PNL",
                "income": f"{float(amount):.8f}",
                "asset": "USDT",
                "info": "manual external position",
                "time": self.now,
                "tranId": self._tran,
                "tradeId": str(self._tran),
            }
        )
        self.now += 1_000


def risk(equity):
    legacy = RiskManager()
    legacy.init(float(equity))
    return ProfessionalRiskAdapter(legacy)


class ExternalPositionDrawdownContaminationProof(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._orig = (db._conn, db._is_pg)
        db._conn = await aiosqlite.connect(":memory:")
        db._is_pg = False
        await db._create_tables()

    async def asyncTearDown(self):
        await db._conn.close()
        db._conn, db._is_pg = self._orig

    def engine(self, client):
        from types import SimpleNamespace

        return SimpleNamespace(client=client, risk=risk(client.wallet))

    async def refresh(self, engine):
        engine._pilot_last_capital_flow_check = 0.0
        engine.client.advance()
        return await plr._refresh_account(engine, LOG)

    async def peak(self):
        raw = await db.load_key_value(ddp.DURABLE_EQUITY_PEAK_KEY)
        return None if raw is None else float(raw)

    @staticmethod
    def drawdown(engine):
        legacy = getattr(engine.risk, "_legacy", engine.risk)
        return float(legacy.drawdown)

    async def test_external_unrealized_profit_raises_durable_hwm(self):
        client = ExternalPositionBinance(wallet=6.4680)
        engine = self.engine(client)

        await self.refresh(engine)
        self.assertAlmostEqual(await self.peak(), 6.4680, places=6)

        # Manual/external ATOM position temporarily contributes +0.8882 UPNL.
        # Account equity becomes exactly the production HWM observed on 2026-09-27.
        client.position_margin = 1.0
        client.unrealized = 0.8882
        await self.refresh(engine)

        self.assertAlmostEqual(client.wallet + client.unrealized, 7.3562, places=6)
        self.assertAlmostEqual(await self.peak(), 7.3562, places=6)
        self.assertAlmostEqual(self.drawdown(engine), 0.0, places=9)

        # No ownership/external-position read is involved in HWM creation.
        self.assertEqual(client.position_reads, 0)
        self.assertEqual(client.rows, [])

    async def test_same_external_position_can_reproduce_current_17pct_drawdown(self):
        client = ExternalPositionBinance(wallet=6.4680)
        engine = self.engine(client)
        await self.refresh(engine)

        client.position_margin = 1.0
        client.unrealized = 0.8882
        await self.refresh(engine)
        self.assertAlmostEqual(await self.peak(), 7.3562, places=6)

        # The external position later moves against the account. This equity is
        # the production value observed after the position episode.
        client.unrealized = -0.3736
        await self.refresh(engine)

        self.assertAlmostEqual(client.wallet + client.unrealized, 6.0944, places=6)
        expected = 1.0 - 6.0944 / 7.3562
        self.assertAlmostEqual(self.drawdown(engine), expected, places=9)
        self.assertAlmostEqual(self.drawdown(engine) * 100.0, 17.15, places=2)

        # The normal 10% hard gate now blocks, despite this entire HWM path
        # requiring no BGX-owned order/trade evidence.
        self.assertFalse(plr._entry_drawdown_allows(engine, LOG))
        self.assertEqual(client.position_reads, 0)

    async def test_external_realized_pnl_keeps_residual_hwm_after_position_disappears(self):
        client = ExternalPositionBinance(wallet=6.4680)
        engine = self.engine(client)
        await self.refresh(engine)

        client.position_margin = 1.0
        client.unrealized = 0.8882
        await self.refresh(engine)
        self.assertAlmostEqual(await self.peak(), 7.3562, places=6)

        # Manual position closes at -0.3736 relative to wallet. REALIZED_PNL is
        # deliberately performance under the current cash-flow ledger, and there
        # is no BGX ownership filter on this drawdown path.
        client.unrealized = 0.0
        client.position_margin = 0.0
        client.realized_external_pnl(-0.3736)
        await self.refresh(engine)

        self.assertAlmostEqual(client.wallet, 6.0944, places=6)
        self.assertAlmostEqual(await self.peak(), 7.3562, places=6)
        self.assertAlmostEqual(self.drawdown(engine) * 100.0, 17.15, places=2)
        self.assertFalse(plr._entry_drawdown_allows(engine, LOG))
        self.assertEqual(client.position_reads, 0)


if __name__ == "__main__":
    unittest.main()
