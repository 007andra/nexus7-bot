"""F-013 end to end on the composed LIVE-pilot runtime (offline fake exchange).

signal -> F-003 sizing -> POST entry + native SL/TP -> fill with slippage ->
authoritative fill -> native stop reconciliation (make-before-break) ->
Position -> durable exit geometry -> R-based calculations -> restart.
Only pre-trade gates unrelated to geometry (NEXUS, scoring, microstructure,
account reads, leases) are stubbed; the order/sizing/stop chain is real.
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
from bot.exit_geometry import initial_risk_per_unit  # noqa: E402
from bot.nexus_runtime_engine import TradingEngine  # noqa: E402
from bot.nexus_types import NexusDecision  # noqa: E402
from bot.professional_risk import CapitalState  # noqa: E402
from bot.strategy import Signal  # noqa: E402
from tests.postfill_fake import PostfillExchange  # noqa: E402

SOL = {"minQty": 1, "lotSize": 1, "qtyStep": 1, "tickSize": 0.01, "multiplier": 0.1,
       "minNotional": 0, "kucoinSymbol": "SOLUSDTM"}
COST = 0.0022


class ComposedPostfillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = {}

        async def load(key, strict=False):
            return self.store.get(key)

        async def save(key, value, strict=False):
            self.store[key] = value
            return True
        self.stack = ExitStack()
        for p in (patch("bot.live_execution_fence.acquire", AsyncMock(return_value=True)),
                  patch("bot.execution_ownership.validate_execution_ownership", AsyncMock()),
                  patch("bot.execution_ownership.publish_valid_execution_ownership", Mock()),
                  patch("bot.critical_state.critical_state.assert_available_for_new_risk", Mock()),
                  patch("bot.pilot_submission_counter.reserve_submission", AsyncMock(return_value=(True, 1))),
                  patch("bot.durable_execution.persist_orders", AsyncMock(return_value=True)),
                  patch.object(kucoin, "API_KEY", "k"), patch("asyncio.sleep", AsyncMock()),
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
        engine._pilot_account_equity = engine._pilot_available_balance = 100.0
        return {"equity": 100.0, "available": 100.0}

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
        raw.get_balance = AsyncMock(return_value=100.0)
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
            engine.risk.update_capital(CapitalState(100.0, 100.0))
            engine.risk.balance, engine.risk.balance_confirmed = 100.0, True
            return NexusDecision(symbol=sig.symbol, decision=sig.direction, execution_allowed=True,
                                 entry=sig.entry, stop_loss=sig.sl, take_profit=sig.tp,
                                 confidence=80., setup_quality=80.)
        engine._nexus_validate = nexus
        return engine

    async def _open(self, direction, fills, **fake_kw):
        entry, sl, tp = (100.0, 98.0, 104.0) if direction == "LONG" else (100.0, 102.0, 96.0)
        ex = PostfillExchange("SOLUSDTM", 0.1, fills=fills, mark=fake_kw.pop("mark", None) or fills[-1],
                              **fake_kw)
        engine = self._engine(ex)
        sig = Signal("SOLUSDT", direction, entry, sl, tp, .8, "t", 80)
        await engine._open(sig)
        return engine, ex, engine.positions.get("SOLUSDT"), sig

    def _exchange_sl(self, ex, direction):
        levels = ex.sl_levels()
        return max(levels) if direction == "LONG" else min(levels)

    async def _assert_one_geometry(self, direction, fill, expected_sl):
        engine, ex, pos, sig = await self._open(direction, [fill])
        self.assertIsNotNone(pos)
        self.assertEqual((sig.sl, sig.tp), ((98.0, 104.0) if direction == "LONG" else (102.0, 96.0)),
                         "signal never shifted")
        ex_sl = self._exchange_sl(ex, direction)
        self.assertEqual(pos._postfill_state, pg.CONFIRMED)
        self.assertEqual((pos.entry, pos.sl, pos.initial_sl), (fill, ex_sl, ex_sl))
        self.assertEqual(ex_sl, expected_sl)
        self.assertEqual(pos.tp, ex.tp_levels()[0], "local TP = native TP")
        self.assertAlmostEqual(initial_risk_per_unit(pos), abs(fill - ex_sl))         # 1R
        self.assertLessEqual(pos.qty * (abs(fill - ex_sl) + fill * COST), 1.0 + 1e-9)  # F-003
        return engine, ex, pos

    async def test_a_long_adverse(self):
        await self._assert_one_geometry("LONG", 101.0, 99.0)

    async def test_b_long_favorable(self):
        await self._assert_one_geometry("LONG", 99.0, 98.0)

    async def test_c_short_adverse(self):
        await self._assert_one_geometry("SHORT", 99.0, 101.0)

    async def test_d_short_favorable(self):
        await self._assert_one_geometry("SHORT", 101.0, 102.0)

    async def test_e_multiple_fills_vwap(self):
        engine, ex, pos, _ = await self._open("LONG", [100.5, 101.5])
        self.assertAlmostEqual(pos.entry, 101.0)
        self.assertEqual(pos.initial_sl, self._exchange_sl(ex, "LONG"))

    async def test_exact_risk_example_verified_on_exchange(self):
        # equity 100 -> budget 1.00; pre-dispatch 4 contracts, projected 0.888.
        engine, ex, pos, _ = await self._open("LONG", [101.5])
        self.assertEqual(int(ex.posts()[0]["size"]), 4)
        ex_sl = self._exchange_sl(ex, "LONG")
        active_risk = 0.4 * (abs(101.5 - ex_sl) + 101.5 * COST)
        self.assertLessEqual(active_risk, 1.0)
        self.assertEqual(pos.initial_sl, ex_sl)
        old = 0.4 * (101.5 - 98.0 + 101.5 * COST)
        self.assertGreater(old, 1.0, "pre-F013 exchange stop would exceed the budget")

    async def test_n_o_replacement_failure_keeps_exchange_truth(self):
        engine, ex, pos, _ = await self._open("LONG", [101.0], fail_stop_create=True)
        self.assertEqual(ex.sl_levels(), [98.0])
        self.assertEqual((pos.sl, pos.initial_sl), (98.0, 98.0))
        self.assertEqual(pos._postfill_state, pg.OVER_BUDGET)

    async def test_durable_geometry_and_restart_restore_same_initial_sl(self):
        engine, ex, pos = await self._assert_one_geometry("LONG", 101.0, 99.0)
        oid = pos._forensic_lineage["opening_order_id"] if "opening_order_id" in pos._forensic_lineage \
            else pos._forensic_lineage["order_id"]
        record = json.loads(self.store[egd._key(oid)])
        self.assertEqual((record["entry"], record["initial_sl"], record["initial_tp"]), (101.0, 99.0, 104.0))
        restored = SimpleNamespace(symbol="SOLUSDT", direction="LONG", entry=101.0, sl=98.0, tp=0.0,
                                   qty=0.4, qty_original=0.4, initial_sl=None, peak_price=101.0,
                                   trailing_sl=98.0, tp1_hit=False, _forensic_lineage=pos._forensic_lineage)
        engine.positions = {"SOLUSDT": restored}
        self.assertTrue(await egd.restore(engine, "SOLUSDT"))
        self.assertEqual((restored.initial_sl, restored.sl, restored.tp), (99.0, 99.0, 104.0))


if __name__ == "__main__":
    unittest.main()
