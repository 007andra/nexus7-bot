"""Regression tests for external/manual-position performance-HWM quarantine."""
import json
import logging
import unittest
from types import SimpleNamespace

import aiosqlite

from bot import database as db
from bot import drawdown_persistence as ddp
from bot import external_position_performance as epp
from bot import hwm_namespace
from bot import pilot_live_runtime as plr
from bot.professional_risk_adapter import ProfessionalRiskAdapter
from bot.risk import RiskManager


LOG = logging.getLogger("test_external_position_performance_hwm")


def new_risk(equity: float):
    legacy = RiskManager()
    legacy.init(float(equity))
    return ProfessionalRiskAdapter(legacy)


class FakeBinance:
    def __init__(self, *, wallet: float, unrealized: float = 0.0):
        self.wallet = float(wallet)
        self.unrealized = float(unrealized)
        self.position_margin = 0.0
        self.positions = []
        self.rows = []
        self.trades = []
        self.orders = []
        self.now_ms = epp._INCIDENT_END_MS
        self.fail_positions = False

    def _listen_key_request(self):
        return None

    def _now_ms(self):
        return self.now_ms

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
        if self.fail_positions:
            raise RuntimeError("position endpoint unavailable")
        return [dict(row) for row in self.positions]

    async def _get(self, endpoint, params=None, auth=False):
        start = int(params["startTime"])
        end = int(params["endTime"])
        if endpoint == "/fapi/v1/income":
            source = self.rows
        elif endpoint == "/fapi/v1/userTrades":
            source = self.trades
        elif endpoint == "/fapi/v1/allOrders":
            source = self.orders
        else:
            raise AssertionError(endpoint)
        return [
            dict(row)
            for row in source
            if start <= int(row["time"]) <= end
        ]


def incident_rows(net_adjustment: float = 0.0):
    base = [
        ("COMMISSION", -0.01346650, epp._INCIDENT_START_MS + 10_000, 8101),
        ("COMMISSION", -0.01346650, epp._INCIDENT_START_MS + 20_000, 8102),
        ("COMMISSION", -0.01346650, epp._INCIDENT_START_MS + 30_000, 8103),
        ("COMMISSION", -0.01346650, epp._INCIDENT_START_MS + 40_000, 8104),
        ("COMMISSION", -0.01346648, epp._INCIDENT_START_MS + 50_000, 8105),
        ("REALIZED_PNL", 0.45130999, epp._INCIDENT_END_MS - 30_000, 8106),
        ("COMMISSION", -0.14653506, epp._INCIDENT_END_MS - 20_000, 8107),
        ("FUNDING_FEE", -0.02571883 + net_adjustment, epp._INCIDENT_END_MS - 10_000, 8108),
    ]
    return [
        {
            "symbol": "ATOMUSDT",
            "incomeType": kind,
            "income": f"{amount:.8f}",
            "asset": "USDT",
            "info": "external manual position",
            "time": ts,
            "tranId": tran,
            "tradeId": str(tran),
        }
        for kind, amount, ts, tran in base
    ]


def incident_trade_evidence(*, bgx_identity: bool = False):
    opening_commissions = [
        -0.01346650,
        -0.01346650,
        -0.01346650,
        -0.01346650,
        -0.01346648,
    ]
    trades = []
    orders = []
    for idx, commission in enumerate(opening_commissions, start=1):
        order_id = 9100 + idx
        ts = epp._INCIDENT_START_MS + idx * 10_000
        trades.append({
            "symbol": "ATOMUSDT",
            "id": 9200 + idx,
            "orderId": order_id,
            "side": "BUY",
            "price": "1.80",
            "qty": "1",
            "quoteQty": "1.80",
            "realizedPnl": "0.00000000",
            "commission": f"{commission:.8f}",
            "commissionAsset": "USDT",
            "time": ts,
            "buyer": True,
            "maker": False,
            "positionSide": "BOTH",
        })
        orders.append({
            "symbol": "ATOMUSDT",
            "orderId": order_id,
            "clientOrderId": (
                "bgx7-forbidden" if bgx_identity and idx == 1
                else f"manual-open-{idx}"
            ),
            "status": "FILLED",
            "side": "BUY",
            "positionSide": "BOTH",
            "type": "MARKET",
            "origType": "MARKET",
            "reduceOnly": False,
            "closePosition": False,
            "executedQty": "1",
            "avgPrice": "1.80",
            "time": ts,
            "updateTime": ts,
        })

    close_order_id = 9199
    close_ts = epp._INCIDENT_END_MS - 20_000
    trades.append({
        "symbol": "ATOMUSDT",
        "id": 9299,
        "orderId": close_order_id,
        "side": "SELL",
        "price": "1.85",
        "qty": "5",
        "quoteQty": "9.25",
        "realizedPnl": "0.45130999",
        "commission": "-0.14653506",
        "commissionAsset": "USDT",
        "time": close_ts,
        "buyer": False,
        "maker": False,
        "positionSide": "BOTH",
    })
    orders.append({
        "symbol": "ATOMUSDT",
        "orderId": close_order_id,
        "clientOrderId": "manual-close",
        "status": "FILLED",
        "side": "SELL",
        "positionSide": "BOTH",
        "type": "MARKET",
        "origType": "MARKET",
        "reduceOnly": True,
        "closePosition": False,
        "executedQty": "5",
        "avgPrice": "1.85",
        "time": close_ts,
        "updateTime": close_ts,
    })
    return trades, orders


class ExternalPerformanceHwmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._orig = (db._conn, db._is_pg)
        db._conn = await aiosqlite.connect(":memory:")
        db._is_pg = False
        await db._create_tables()

    async def asyncTearDown(self):
        await db._conn.close()
        db._conn, db._is_pg = self._orig

    async def set_peak(self, value: float):
        await db.save_key_value(ddp.DURABLE_EQUITY_PEAK_KEY, str(value), strict=True)

    async def peak(self):
        raw = await db.load_key_value(ddp.DURABLE_EQUITY_PEAK_KEY, strict=True)
        return None if raw is None else float(raw)

    async def quarantine(self):
        raw = await db.load_key_value(epp.state_key(), strict=True)
        return None if raw is None else json.loads(raw)

    def engine(self, client, *, prior_equity=None, prior_ms=None):
        engine = SimpleNamespace(
            client=client,
            risk=new_risk(prior_equity or client.wallet),
            positions={},
        )
        if prior_equity is not None:
            engine._pilot_prev_account_equity = float(prior_equity)
        if prior_ms is not None:
            engine._pilot_prev_account_observed_ms = int(prior_ms)
        return engine

    def test_owned_symbol_requires_compatible_side_and_quantity(self):
        client = FakeBinance(wallet=6.0)
        engine = SimpleNamespace(
            client=client,
            positions={
                "ATOMUSDT": SimpleNamespace(qty=10.0, direction="LONG")
            },
        )

        compatible = [{
            "symbol": "ATOMUSDT",
            "size": 8.0,
            "sizeUnit": "BASE_ASSET",
            "side": "Buy",
        }]
        self.assertEqual(epp._unowned_symbols(engine, compatible), set())

        increased = [{
            "symbol": "ATOMUSDT",
            "size": 12.0,
            "sizeUnit": "BASE_ASSET",
            "side": "Buy",
        }]
        reversed_side = [{
            "symbol": "ATOMUSDT",
            "size": 8.0,
            "sizeUnit": "BASE_ASSET",
            "side": "Sell",
        }]
        self.assertEqual(epp._unowned_symbols(engine, increased), {"ATOMUSDT"})
        self.assertEqual(
            epp._unowned_symbols(engine, reversed_side), {"ATOMUSDT"}
        )

    async def test_active_external_position_cannot_create_new_performance_high(self):
        await self.set_peak(6.4680)
        client = FakeBinance(wallet=6.4680, unrealized=0.8882)
        client.position_margin = 1.0
        client.positions = [{
            "symbol": "ATOMUSDT",
            "size": 10.0,
            "sizeUnit": "BASE_ASSET",
            "side": "Buy",
        }]
        engine = self.engine(
            client,
            prior_equity=5.8827,
            prior_ms=epp._INCIDENT_START_MS - 1_000,
        )

        await plr._refresh_account(engine, LOG)

        self.assertAlmostEqual(await self.peak(), 6.4680, places=6)
        legacy = engine.risk._legacy
        self.assertAlmostEqual(legacy.peak_balance, 6.4680, places=6)
        self.assertTrue(engine._external_performance_quarantine)
        state = await self.quarantine()
        self.assertEqual(state["status"], "ACTIVE")
        self.assertEqual(state["symbols"], ["ATOMUSDT"])
        self.assertAlmostEqual(state["pre_event_equity"], 5.8827, places=6)
        self.assertAlmostEqual(state["pre_event_peak"], 6.4680, places=6)
        self.assertFalse(plr._entry_drawdown_allows(engine, LOG))

    async def test_flat_after_unresolved_external_episode_remains_blocked(self):
        await self.set_peak(6.4680)
        client = FakeBinance(wallet=6.0944)
        engine = self.engine(client, prior_equity=5.8827, prior_ms=epp._INCIDENT_START_MS)
        await db.save_key_value(
            epp.state_key(),
            json.dumps({
                "version": 1,
                "status": "ACTIVE",
                "reason": "external_position_detected",
                "symbols": ["ATOMUSDT"],
                "started_at_ms": epp._INCIDENT_START_MS,
                "pre_event_equity": 5.8827,
                "pre_event_peak": 6.4680,
                "execution_effect": "BLOCK_NEW_ENTRIES",
            }, sort_keys=True, separators=(",", ":")),
            strict=True,
        )

        await plr._refresh_account(engine, LOG)

        self.assertAlmostEqual(await self.peak(), 6.4680, places=6)
        self.assertTrue(engine._external_performance_quarantine)
        self.assertFalse(plr._entry_drawdown_allows(engine, LOG))

    async def test_known_atom_incident_rebases_to_pre_episode_drawdown(self):
        await self.set_peak(epp._INCIDENT_BAD_HWM)
        client = FakeBinance(wallet=epp._INCIDENT_POST_EQUITY)
        client.rows = incident_rows()
        client.trades, client.orders = incident_trade_evidence()
        engine = self.engine(client, prior_equity=epp._INCIDENT_POST_EQUITY)

        await plr._refresh_account(engine, LOG)

        pre_equity = epp._INCIDENT_POST_EQUITY - epp._INCIDENT_NET_EXTERNAL
        expected_peak = (
            epp._INCIDENT_PRE_HWM
            * epp._INCIDENT_POST_EQUITY
            / pre_equity
        )
        expected_dd = 1.0 - pre_equity / epp._INCIDENT_PRE_HWM

        self.assertAlmostEqual(await self.peak(), expected_peak, places=9)
        self.assertAlmostEqual(engine.risk._legacy.drawdown, expected_dd, places=9)
        self.assertAlmostEqual(engine.risk._legacy.drawdown * 100.0, 9.04953, places=4)
        self.assertFalse(engine._external_performance_quarantine)
        self.assertTrue(plr._entry_drawdown_allows(engine, LOG))

        raw = await db.load_key_value(hwm_namespace.provenance_key(), strict=True)
        provenance = json.loads(raw)
        self.assertEqual(
            provenance["reason"], "external_position_performance_rebase"
        )

    async def test_incident_repair_refuses_non_manual_order_identity(self):
        await self.set_peak(epp._INCIDENT_BAD_HWM)
        client = FakeBinance(wallet=epp._INCIDENT_POST_EQUITY)
        client.rows = incident_rows()
        client.trades, client.orders = incident_trade_evidence(
            bgx_identity=True
        )
        engine = self.engine(client, prior_equity=epp._INCIDENT_POST_EQUITY)

        with self.assertRaisesRegex(
            db.PersistenceError,
            "external incident ownership is not provably manual",
        ):
            await plr._refresh_account(engine, LOG)

        self.assertAlmostEqual(
            await self.peak(), epp._INCIDENT_BAD_HWM, places=6
        )

    async def test_incident_repair_refuses_mismatched_exchange_ledger(self):
        await self.set_peak(epp._INCIDENT_BAD_HWM)
        client = FakeBinance(wallet=epp._INCIDENT_POST_EQUITY)
        client.rows = incident_rows(net_adjustment=-0.01)
        client.trades, client.orders = incident_trade_evidence()
        engine = self.engine(client, prior_equity=epp._INCIDENT_POST_EQUITY)

        with self.assertRaisesRegex(
            db.PersistenceError, "external incident income signature mismatch"
        ):
            await plr._refresh_account(engine, LOG)

        self.assertAlmostEqual(await self.peak(), epp._INCIDENT_BAD_HWM, places=6)

    async def test_position_read_failure_never_creates_new_high_and_blocks(self):
        await self.set_peak(6.4680)
        client = FakeBinance(wallet=7.3562)
        client.fail_positions = True
        engine = self.engine(client, prior_equity=5.8827, prior_ms=epp._INCIDENT_START_MS)

        await plr._refresh_account(engine, LOG)

        self.assertAlmostEqual(await self.peak(), 6.4680, places=6)
        self.assertTrue(engine._external_performance_quarantine)
        self.assertFalse(plr._entry_drawdown_allows(engine, LOG))


if __name__ == "__main__":
    unittest.main()
