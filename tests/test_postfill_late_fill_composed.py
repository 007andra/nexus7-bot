"""F-013A end to end on the composed LIVE-pilot runtime (offline fake exchange).

F-003 sizes 10 contracts -> entry POST with native SL/TP -> only 5 fill and the
order keeps working (wait_for_fill times out) -> timeout adoption enters the
post-fill pipeline -> CONFIRMED for 0.5 SOL -> 5 more contracts fill adversely
-> pilot ownership guard + periodic reconcile (no WS) -> cumulative VWAP ->
budget recheck -> stop tightened on the exchange + read back -> Position and
durable geometry/lifecycle updated -> 1R / partial / 2R computed on the new
geometry. Only pre-trade gates unrelated to geometry are stubbed.
"""
import json
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.test_live_entry_readiness_gate import _engine as _unused  # noqa: F401  composed LIVE env

from bot import database as db  # noqa: E402
from bot import exit_geometry_durability as egd  # noqa: E402
from bot import kucoin  # noqa: E402
from bot import postfill_geometry as pg  # noqa: E402
from bot import trade_lifecycle  # noqa: E402
from bot.exit_geometry import initial_risk_per_unit, r_multiple  # noqa: E402
from bot.nexus_runtime_engine import TradingEngine  # noqa: E402
from bot.nexus_types import NexusDecision  # noqa: E402
from bot.professional_risk import CapitalState  # noqa: E402
from bot.strategy import Signal  # noqa: E402
from tests.postfill_fake import PostfillExchange  # noqa: E402

SOL = {"minQty": 1, "lotSize": 1, "qtyStep": 1, "tickSize": 0.01, "multiplier": 0.1,
       "minNotional": 0, "kucoinSymbol": "SOLUSDTM"}
COST, EQUITY = 0.0022, 225.0
BUDGET = EQUITY * 0.01


class ComposedLateFillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = {}

        async def load(key, strict=False):
            return self.store.get(key)

        async def save(key, value, strict=False):
            self.store[key] = value
            return True
        original_wait = kucoin.KuCoinClient.wait_for_fill

        async def short_wait(client, order_id, timeout_s=8.0, poll_interval_s=0.5):
            return await original_wait(client, order_id, timeout_s=0.05, poll_interval_s=0)
        self.stack = ExitStack()
        for p in (patch("bot.live_execution_fence.acquire", AsyncMock(return_value=True)),
                  patch("bot.execution_ownership.validate_execution_ownership", AsyncMock()),
                  patch("bot.execution_ownership.publish_valid_execution_ownership", Mock()),
                  patch("bot.critical_state.critical_state.assert_available_for_new_risk", Mock()),
                  patch("bot.pilot_submission_counter.reserve_submission", AsyncMock(return_value=(True, 1))),
                  patch("bot.durable_execution.persist_orders", AsyncMock(return_value=True)),
                  patch.object(kucoin, "API_KEY", "k"), patch("asyncio.sleep", AsyncMock()),
                  patch.object(kucoin.KuCoinClient, "wait_for_fill", short_wait),
                  patch.object(db, "load_key_value", AsyncMock(side_effect=load)),
                  patch.object(db, "save_key_value", AsyncMock(side_effect=save)),
                  patch.object(db, "save_snapshot", AsyncMock()),
                  patch.object(db, "save_trade_open", AsyncMock(return_value=1)),
                  patch("bot.engine.scoring.calculate", AsyncMock(return_value={"aprovado": True, "total": 80})),
                  patch("bot.pilot_live_runtime._refresh_account", side_effect=self._refresh),
                  patch("bot.pilot_risk_cap_hardening.live_microstructure_recheck", side_effect=self._micro)):
            self.stack.enter_context(p)

    def tearDown(self):
        self.stack.close()

    @staticmethod
    async def _refresh(engine, log, for_entry=False):
        engine._pilot_account_equity = engine._pilot_available_balance = EQUITY
        return {"equity": EQUITY, "available": EQUITY}

    @staticmethod
    async def _micro(client, **kw):
        return SimpleNamespace(allowed=True, blockers=(), drift_classification="WITHIN",
                               metrics={"executable_price": kw["signal_entry"], "spread_bps": 1,
                                        "signal_drift_bps": 0, "signed_signal_drift_bps": 0,
                                        "depth_multiple": 10})

    def _engine(self, exchange):
        raw = kucoin.KuCoinClient()
        raw._session, raw._instruments = exchange, {"SOLUSDT": dict(SOL)}
        raw._execution_ownership, raw._prelive_private_ws_probe_ok = object(), True
        raw.get_balance = AsyncMock(return_value=EQUITY)
        engine = TradingEngine(raw)
        engine.instruments = {"SOLUSDT": dict(SOL)}
        for key, value in dict(viable_symbols=["SOLUSDT"], connected=True,
                               _initial_reconciliation_complete=True, _protection_system_ready=True,
                               _execution_ownership_valid=True,
                               _execution_ownership_expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
                               _financial_state_sane=True, _durable_state_ok=True,
                               _market_data_ready=True).items():
            setattr(engine, key, value)
        raw._engine, raw._order_registry = engine, engine.orders
        engine.integrity.assess = AsyncMock()
        engine.integrity.can_open_new = lambda: True
        engine.pilot.can_open_pilot = lambda *a, **k: True
        engine.pilot.reserve_submission = lambda *a, **k: True
        engine.client.get_cached_klines = lambda *a, **k: [{"o": 100, "h": 101, "l": 99, "c": 100, "v": 1e3}] * 30

        async def nexus(sig, *a, **k):
            engine.risk.set_plan(symbol=sig.symbol, entry=sig.entry, stop=sig.sl, risk_pct=0.01)
            engine.risk.update_capital(CapitalState(EQUITY, EQUITY))
            engine.risk.balance, engine.risk.balance_confirmed = EQUITY, True
            return NexusDecision(symbol=sig.symbol, decision=sig.direction, execution_allowed=True,
                                 entry=sig.entry, stop_loss=sig.sl, take_profit=sig.tp,
                                 confidence=80., setup_quality=80.)
        engine._nexus_validate = nexus
        return engine

    async def _open_partial(self, direction):
        entry, sl, tp = (100.0, 98.0, 104.0) if direction == "LONG" else (100.0, 102.0, 96.0)
        ex = PostfillExchange("SOLUSDTM", 0.1, fills=[(5, 100.0)], mark=100.0, keep_active=True)
        engine = self._engine(ex)
        await engine._open(Signal("SOLUSDT", direction, entry, sl, tp, .8, "t", 80))
        return engine, ex, engine.positions.get("SOLUSDT")

    def _exchange_truth(self, ex, direction):
        levels = ex.sl_levels()
        stop = max(levels) if direction == "LONG" else min(levels)
        qty, vwap = abs(ex.position["qty"]) * 0.1, ex.position["entry"]
        return qty, vwap, stop, qty * (abs(vwap - stop) + vwap * COST)

    async def _p(self, direction, late_price, expected_vwap, expected_stop):
        engine, ex, pos = await self._open_partial(direction)
        self.assertEqual(int(ex.posts()[0]["size"]), 10, "F-003 sized 10 contracts")
        self.assertIsNotNone(pos, "timeout adoption kept the BGX position")
        self.assertEqual(pos._postfill_state, pg.CONFIRMED, "adopted position entered the pipeline")
        # The timeout adoption may have added its own (tighter) BGX stop: local
        # geometry is whatever is ACTIVE on the exchange for this trade.
        _, _, adopted_stop, adopted_loss = self._exchange_truth(ex, direction)
        self.assertEqual((pos.qty, pos.entry, pos.sl, pos.initial_sl), (0.5, 100.0, adopted_stop, adopted_stop))
        self.assertLessEqual(adopted_loss, BUDGET)
        self.assertFalse(pos._postfill_version["terminal"])
        oid = ex.entry_order()["id"]

        posts_before = len(ex.posts())
        ex.late_fill(5, late_price)                       # adverse late fill, no WS event
        spy = AsyncMock(side_effect=pg.late_fill_explains)
        with patch.object(pg, "late_fill_explains", spy):
            await engine._reconcile_exchange_positions()  # pilot ownership guard + EXEC-03 hook
        spy.assert_awaited_once()
        self.assertEqual(spy.await_args.args[1:], ("SOLUSDT", 1.0))
        self.assertNotIn("SOLUSDT", getattr(engine, "_external_position_symbols", set()) or set(),
                         "a proven late fill of our order is not EXTERNAL")
        await egd.sync(engine)                            # periodic LIVE pass (idempotent now)

        qty, vwap, stop, loss = self._exchange_truth(ex, direction)
        self.assertIs(engine.positions["SOLUSDT"], pos)
        self.assertEqual(pos._postfill_state, pg.CONFIRMED)
        self.assertEqual((qty, vwap, stop), (1.0, expected_vwap, expected_stop))
        self.assertEqual((pos.qty, pos.entry, pos.sl, pos.initial_sl), (qty, vwap, stop, stop))
        self.assertLessEqual(loss, BUDGET, "INV-LATE-FILL-BUDGET-001 on exchange truth")
        self.assertGreater(len(ex.posts()), posts_before, "stop tightened on the exchange")
        self.assertGreater(1.0 * (abs(vwap - adopted_stop) + vwap * COST), BUDGET,
                           "the pre-recheck stop would have exceeded the budget")
        self.assertAlmostEqual(pos._risk_reserved_usdt, BUDGET, places=6,
                               msg="F-003 reservation unchanged, never released")
        # 1R / partial (1R) / 2R on the NEW geometry
        self.assertAlmostEqual(initial_risk_per_unit(pos), 2.0)
        sign = 1 if direction == "LONG" else -1
        self.assertAlmostEqual(r_multiple(pos, vwap + sign * 2.0), 1.0)
        self.assertAlmostEqual(r_multiple(pos, vwap + sign * 4.0), 2.0)
        # durable state follows the new version
        record = json.loads(self.store[egd._key(oid)])
        self.assertEqual((record["confirmed_entry_qty"], record["initial_sl"], record["entry"]),
                         (1.0, stop, vwap))
        self.assertTrue(record["entry_order_terminal"])
        lifecycle = await trade_lifecycle.load(oid)
        self.assertEqual(lifecycle["opening_qty"], 1.0)
        return engine, ex, pos

    async def test_p_long_late_adverse_fill_end_to_end(self):
        # 5 @100 + 5 @102.5 -> VWAP 101.25; the active stop no longer fits the budget
        await self._p("LONG", 102.5, 101.25, 99.25)

    async def test_p_short_late_adverse_fill_end_to_end(self):
        await self._p("SHORT", 97.5, 98.75, 100.75)

    async def test_manual_increase_is_quarantined_external_not_absorbed(self):
        engine, ex, pos = await self._open_partial("LONG")
        ex.late_fill(5, 100.0)
        await egd.sync(engine)
        self.assertEqual(pos.qty, 1.0)
        ex.manual_add(5, 101.0)                           # not our order
        await engine._reconcile_exchange_positions()
        self.assertIn("SOLUSDT", engine._external_position_symbols)
        self.assertNotIn("SOLUSDT", engine.positions, "quarantined read-only, never absorbed")

    async def test_o_trailing_skips_while_geometry_recheck_holds_the_lock(self):
        engine, ex, pos = await self._open_partial("LONG")
        pos.current_price = 103.5
        engine.client.set_sl = AsyncMock(return_value=True)
        lock = pg.geometry_lock(pos)
        await lock.acquire()
        await engine._apply_trailing_stops()
        engine.client.set_sl.assert_not_called()
        lock.release()


if __name__ == "__main__":
    unittest.main()
