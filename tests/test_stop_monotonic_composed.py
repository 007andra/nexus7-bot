"""Q-01C Q — monotonic stops on the composed LIVE-pilot runtime (offline).

Real composition (sitecustomize): final ``_manage_partial_tp`` ->
``durable_partial_exit`` break-even and final ``_apply_trailing_stops``.
Exchange stop calls are recorded mocks; DB is an in-memory dict.
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
from bot import engine as core  # noqa: E402
from bot import kucoin  # noqa: E402
from bot.nexus_runtime_engine import TradingEngine  # noqa: E402
from bot.strategy import Signal  # noqa: E402

INFO = {"multiplier": 0.001, "lotSize": 1, "minQty": 1, "tickSize": 0.1, "minNotional": 0}


class ComposedMonotonicStopTests(unittest.IsolatedAsyncioTestCase):
    async def _runtime(self, direction, stop):
        raw = kucoin.KuCoinClient()
        raw._instruments = {"BTCUSDT": dict(INFO)}
        raw.place_order = AsyncMock(return_value={"orderId": "p-1"})
        raw.wait_for_fill = AsyncMock(return_value={"filled": True})
        raw.get_order_by_client_oid = AsyncMock(return_value={})
        raw.set_sl = AsyncMock(return_value=True)
        engine = TradingEngine(raw)
        engine.instruments = {"BTCUSDT": dict(INFO)}
        entry, isl, tp = (100.0, 98.0, 104.0) if direction == "LONG" else (100.0, 102.0, 96.0)
        pos = core.Position(Signal("BTCUSDT", direction, entry, isl, tp, 0.8, "t", 80), 1.0)
        pos._forensic_lineage = {"order_id": "opening-1"}
        pos.sl = pos.trailing_sl = stop
        engine.positions["BTCUSDT"] = pos
        engine.client.get_positions = AsyncMock(
            return_value=[{"symbol": "BTCUSDT", "size": 0.5, "sizeUnit": "BASE_ASSET"}])
        return raw, engine, pos

    async def test_q_partial_be_never_loosens_and_trailing_only_tightens(self):
        self.assertFalse(kucoin.PAPER_TRADE)
        store = {}

        async def load(key, strict=False):
            return store.get(key)

        async def save(key, value, strict=False):
            store[key] = value
            return True
        with patch("bot.durable_partial_exit.db.load_key_value", AsyncMock(side_effect=load)), \
                patch("bot.durable_partial_exit.db.save_key_value", AsyncMock(side_effect=save)):
            for direction, stop, price in (("LONG", 101.5, 102.1), ("SHORT", 98.5, 97.9)):
                store.clear()
                raw, engine, pos = await self._runtime(direction, stop)
                pos.update_pnl(price)
                await engine._manage_partial_tp()
                self.assertEqual((pos.qty, pos.tp1_hit, pos.sl), (0.5, True, stop), direction)
                raw.set_sl.assert_not_awaited()

                s = 1 if direction == "LONG" else -1
                effective = []
                for px in (100 + 2.4 * s, 100 + 2.2 * s, 100 + 2.8 * s, 100 + 2.6 * s):
                    pos.update_pnl(px)
                    await engine._apply_trailing_stops()
                    effective.append(pos.sl)
                pairs = list(zip([stop] + effective, effective))
                self.assertTrue(all((b >= a) if s > 0 else (b <= a) for a, b in pairs), effective)
                sent = [c.args[1] for c in raw.set_sl.await_args_list]
                self.assertTrue(sent, "trailing really moved the stop")
                self.assertEqual(sent, sorted(sent, reverse=(s < 0)), "exchange only sees tightening")
                self.assertEqual(len(sent), len(set(sent)), "no churn")


if __name__ == "__main__":
    unittest.main()
