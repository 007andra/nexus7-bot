"""Q-01B Q — full LIVE-pilot cycle across a crash/restart (offline).

entry -> peak -> partial (real durable_partial_exit) -> trailing -> persist ->
crash -> restart through the FINAL composed ``_load_existing_positions``
(ownership proof, lineage, geometry restore) -> trailing / 2R. The same steps
without a restart must produce the same exchange-side decisions.
"""
import os

os.environ.update({
    "PAPER_TRADE": "false",
    "LIVE_TRADING_CONFIRMED": "I_UNDERSTAND_THE_RISK",
    "REAL_TRADING_PILOT": "true",
    "PILOT_ACCOUNT_CONFIRMED": "true",
    "PILOT_RELEASE_APPROVED": "I_APPROVE_TWO_LIVE_PILOT_ORDERS",
    "VALIDATION_LOCK_RELEASE_APPROVED": "I_APPROVE_CONTROLLED_LIVE_PILOT_EXECUTION",
    "KUCOIN_REST_BASE": "http://127.0.0.1:1",
    "KUCOIN_API_KEY": "", "KUCOIN_API_SECRET": "", "KUCOIN_API_PASSPHRASE": "",
    "NEXUS_TELEGRAM": "false",
})

import unittest  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

import sitecustomize  # noqa: E402,F401
from bot import database as db  # noqa: E402
from bot import engine as core  # noqa: E402
from bot import exit_geometry_durability as g  # noqa: E402
from bot import kucoin  # noqa: E402
from bot.exit_geometry import r_multiple  # noqa: E402
from bot.nexus_runtime_engine import TradingEngine  # noqa: E402
from bot.order_state import OrderState  # noqa: E402
from bot.strategy import Signal  # noqa: E402

INFO = {"multiplier": 0.01, "lotSize": 1, "minQty": 1, "tickSize": 0.01, "minNotional": 0}


class _Exchange:
    """Shared exchange truth surviving the bot process crash."""

    def __init__(self, direction):
        self.side = "Buy" if direction == "LONG" else "Sell"
        self.contracts, self.stop, self.mark = 100, 98.0 if direction == "LONG" else 102.0, 100.0
        self.set_sl_calls, self.reduce_orders = [], []
        # Exchange fill ledger (contracts), the authority used by the restart
        # ownership continuity proof (NOVO-02).
        import time as _time
        self._t = int(_time.time() * 1000) - 60_000
        self.fills = []
        self.add_fill("open-1", self.side.lower(), 100)

    def add_fill(self, order_id, side, contracts):
        self._t += 1000
        self.fills.append({"tradeId": f"t{len(self.fills)}", "orderId": order_id, "symbol": "ETHUSDTM",
                           "side": side, "size": str(contracts), "price": "100", "fee": "0",
                           "feeCurrency": "USDT", "tradeTime": self._t * 1_000_000, "tradeType": "trade"})

    def client(self):
        raw = kucoin.KuCoinClient()
        raw._instruments = {"ETHUSDT": dict(INFO)}
        ex = self

        async def get_positions():
            if ex.contracts <= 0:
                return []
            return [{"symbol": "ETHUSDT", "side": ex.side, "size": float(ex.contracts),
                     "entryPrice": 100.0, "avgPrice": 100.0, "markPrice": ex.mark,
                     "unrealisedPnl": 0.0, "liquidationPrice": 0.0}]

        async def stop_orders(symbol):
            return [{"symbol": "ETHUSDTM", "side": "sell" if ex.side == "Buy" else "buy",
                     "stop": "down" if ex.side == "Buy" else "up", "stopPrice": str(ex.stop),
                     "stopPriceType": "MP", "closeOrder": True, "reduceOnly": True,
                     "isActive": True, "stopTriggered": False, "clientOid": "bgx-stop-1"}]

        async def set_sl(symbol, sl, instruments=None):
            ex.set_sl_calls.append(round(sl, 6))
            ex.stop = sl
            return True

        async def place_order(**kw):
            ex.reduce_orders.append(kw)
            ex.contracts -= round(kw["qty"] * 100)
            oid = f"r-{len(ex.reduce_orders)}"
            ex.add_fill(oid, "sell" if ex.side == "Buy" else "buy", round(kw["qty"] * 100))
            return {"orderId": oid}

        async def private_get(endpoint, params=None, auth=False):
            if endpoint == "/api/v1/fills":
                lo, hi = params["startAt"] * 1_000_000, params["endAt"] * 1_000_000
                items = [f for f in ex.fills if lo <= f["tradeTime"] <= hi]
                return {"currentPage": 1, "totalPage": 1, "totalNum": len(items), "items": items}
            return {}
        raw._get = private_get
        raw.get_positions = get_positions
        raw.get_stop_orders = stop_orders
        raw.set_sl = set_sl
        raw.place_order = place_order
        raw.wait_for_fill = AsyncMock(return_value={"filled": True})
        raw.get_order_by_client_oid = AsyncMock(return_value={})
        raw.get_order_status = AsyncMock(return_value={
            "orderId": "open-1", "clientOid": "bgx7-open-1", "symbol": "ETHUSDTM",
            "side": self.side.lower(), "isActive": False, "cancelExist": False, "filledSize": "100"})
        return raw

    def engine(self):
        engine = TradingEngine(self.client())
        engine.instruments = {"ETHUSDT": dict(INFO)}
        order, _ = engine.orders.get_or_create("bgx7-open-1", "ETHUSDT", self.side, 1.0)
        order.transition(OrderState.SUBMITTING, source="LOCAL")
        order.transition(OrderState.SUBMITTED, source="REST", order_id="open-1")
        order.transition(OrderState.FILLED, source="REST", order_id="open-1", filled_qty=1.0)
        engine.orders.index_order_id("open-1", "bgx7-open-1")
        return engine


class ComposedRestartGeometryTests(unittest.IsolatedAsyncioTestCase):
    async def _cycle(self, direction, restart):
        s = 1 if direction == "LONG" else -1
        store = {}

        async def load(key, strict=False):
            return store.get(key)

        async def save(key, value, strict=False):
            store[key] = value
            return True
        ex = _Exchange(direction)
        with patch.object(db, "load_key_value", AsyncMock(side_effect=load)), \
                patch.object(db, "save_key_value", AsyncMock(side_effect=save)):
            engine = ex.engine()
            pos = core.Position(Signal("ETHUSDT", direction, 100.0, 100 - 2 * s, 100 + 4 * s, .8, "t", 80), 1.0)
            pos._forensic_lineage = {"order_id": "open-1", "client_oid": "bgx7-open-1", "version": 2}
            engine.positions["ETHUSDT"] = pos
            from bot import trade_lifecycle
            await trade_lifecycle.open_trade(pos, pos.qty)   # the post_trade_forensics hook
            await g.persist(pos, "entry_confirmed")

            async def step(price, *, exits=True):
                ex.mark = price
                p = engine.positions["ETHUSDT"]
                p.update_pnl(price)
                with patch.object(engine, "_sync_positions", AsyncMock()):
                    await engine._manage_partial_tp()
                    await engine._apply_trailing_stops()
                    if exits:
                        await engine._check_rr_double()

            await step(100 + 1.5 * s)
            await step(100 + 2.1 * s)                        # partial at +1R (+0.03 %)
            await step(100 + 3.0 * s)                        # trailing, new peak persisted
            snapshot = engine.positions["ETHUSDT"]
            pre = (snapshot.initial_sl, snapshot.tp, snapshot.tp1_hit, snapshot.peak_price)
            if restart:
                ex.mark = 100 + 2.6 * s                      # price retraced during the downtime
                engine = ex.engine()                         # crash: new process, same DB/exchange
                await engine._load_existing_positions()
                self.assertIn("ETHUSDT", engine.positions, "BGX-reduced position re-adopted")
                restored = engine.positions["ETHUSDT"]
                self.assertEqual((restored.initial_sl, restored.tp, restored.tp1_hit, restored.peak_price),
                                 pre)
                self.assertEqual((restored.qty, restored.sl), (0.5, ex.stop))
            await step(100 + 2.6 * s)
            await step(100 + 3.5 * s)
            r_after = r_multiple(engine.positions["ETHUSDT"], 100 + 3.5 * s)
            await step(100 + 4.0 * s)                        # 2R on initial risk
        return ex, r_after

    async def test_q_restart_parity_long_and_short(self):
        self.assertFalse(kucoin.PAPER_TRADE)
        for direction in ("LONG", "SHORT"):
            base, r_base = await self._cycle(direction, restart=False)
            again, r_again = await self._cycle(direction, restart=True)
            self.assertEqual(r_again, r_base)
            self.assertEqual(again.set_sl_calls, base.set_sl_calls, direction)
            s = 1 if direction == "LONG" else -1
            self.assertEqual(again.set_sl_calls,
                             [round(100 + s * d, 6) for d in (0.0, 1.575, 2.25, 2.625, 3.0)])
            self.assertEqual([round(o["qty"], 6) for o in again.reduce_orders],
                             [round(o["qty"], 6) for o in base.reduce_orders])
            self.assertEqual(len(again.reduce_orders), 2, "one partial + one 2R exit, no duplicate")
            self.assertTrue(all(o["reduce_only"] for o in again.reduce_orders))


if __name__ == "__main__":
    unittest.main()
