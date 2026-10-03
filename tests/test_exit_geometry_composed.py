"""Q-01 N — exit geometry on the composed LIVE-pilot runtime (offline).

Real composition (sitecustomize): final ``Position.calc_trailing_sl`` overlay,
final ``_manage_partial_tp`` -> ``durable_partial_exit``,
``_apply_trailing_stops`` and ``_check_rr_double`` -> ``confirmed_rr_exit``.
Exchange calls are recorded mocks; DB is an in-memory dict.
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
from bot.exit_geometry import r_multiple, stop_on_valid_side  # noqa: E402
from bot.nexus_runtime_engine import TradingEngine  # noqa: E402
from bot.strategy import Signal  # noqa: E402

INFO = {"multiplier": 0.001, "lotSize": 1, "minQty": 1, "tickSize": 0.1, "minNotional": 0}


class ComposedExitGeometryTests(unittest.IsolatedAsyncioTestCase):
    async def test_n_partial_trailing_and_2r_on_composed_runtime(self):
        self.assertFalse(kucoin.PAPER_TRADE)
        self.assertEqual(core.Position.calc_trailing_sl.__module__, "bot.trailing_safety_hardening")
        store = {}

        async def load(key, strict=False):
            return store.get(key)

        async def save(key, value, strict=False):
            store[key] = value
            return True
        raw = kucoin.KuCoinClient()
        raw._instruments = {"BTCUSDT": dict(INFO)}
        raw.place_order = AsyncMock(return_value={"orderId": "x-1"})
        raw.wait_for_fill = AsyncMock(return_value={"filled": True})
        raw.get_order_by_client_oid = AsyncMock(return_value={})
        raw.set_sl = AsyncMock(return_value=True)
        engine = TradingEngine(raw)
        engine.instruments = {"BTCUSDT": dict(INFO)}
        pos = core.Position(Signal("BTCUSDT", "LONG", 100.0, 98.0, 104.0, 0.8, "t", 80), 1.0)
        pos._forensic_lineage = {"order_id": "opening-1"}
        engine.positions["BTCUSDT"] = pos
        engine.client.get_positions = AsyncMock(
            return_value=[{"symbol": "BTCUSDT", "size": 0.5, "sizeUnit": "BASE_ASSET"}])
        with patch("bot.confirmed_rr_exit.db.load_key_value", AsyncMock(side_effect=load)), \
                patch("bot.confirmed_rr_exit.db.save_key_value", AsyncMock(side_effect=save)), \
                patch("bot.durable_partial_exit.db.load_key_value", AsyncMock(side_effect=load)), \
                patch("bot.durable_partial_exit.db.save_key_value", AsyncMock(side_effect=save)), \
                patch.object(engine, "_sync_positions", AsyncMock()):
            pos.update_pnl(102.1)
            await engine._manage_partial_tp()              # partial at +1R (initial risk)
            self.assertEqual((pos.qty, pos.tp1_hit, pos.sl), (0.5, True, 100.0))
            self.assertAlmostEqual(r_multiple(pos, 102.1), 1.05)

            seen = []
            for price in (102.1, 103.0, 103.5):
                pos.update_pnl(price)
                raw.set_sl.reset_mock()
                await engine._apply_trailing_stops()
                for call in raw.set_sl.await_args_list:
                    seen.append((price, call.args[1]))
            self.assertTrue(seen, "trailing alive after the partial")
            self.assertEqual([(p, round(s, 6)) for p, s in seen],
                             [(102.1, 101.575), (103.0, 102.25), (103.5, 102.625)])
            for price, stop in seen:
                self.assertTrue(stop_on_valid_side("LONG", stop, price), (price, stop))

            raw.place_order.reset_mock()
            engine.client.get_positions = AsyncMock(return_value=[])
            pos.update_pnl(104.0 - 1e-9)
            await engine._check_rr_double()
            raw.place_order.assert_not_awaited()
            pos.update_pnl(104.0)
            await engine._check_rr_double()                # 2R on initial risk after BE
        raw.place_order.assert_awaited_once()
        self.assertTrue(raw.place_order.await_args.kwargs["reduce_only"])


if __name__ == "__main__":
    unittest.main()
