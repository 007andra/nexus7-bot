"""Forensic audit (2026-10-02) — executable reproductions of confirmed defects.

Every test here is offline: no network, no exchange credentials, no orders.
Exchange/database boundaries are replaced by in-memory fakes.

Tests decorated with ``unittest.expectedFailure`` assert the CORRECT behaviour
and currently fail because the defect is present. When a defect is fixed the
test will report "unexpected success", which fails the run and forces the
decorator to be removed, turning the reproduction into a regression guard.

Finding IDs refer to docs/FORENSIC_AUDIT_2026-10-02.md.
"""
import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("PAPER_TRADE", "true")


class _Log:
    def __getattr__(self, _name):
        return lambda *a, **k: None


# ---------------------------------------------------------------------------
# F-001 (P0) — /api/close-all stops the engine and then calls a method that
# does not exist anywhere in the repository.
# ---------------------------------------------------------------------------
class F001EmergencyCloseAllTests(unittest.IsolatedAsyncioTestCase):
    def test_close_all_positions_is_not_defined_on_runtime_engine(self):
        from bot.nexus_runtime_engine import TradingEngine
        import main  # noqa: F401  (main.close_all calls engine.close_all_positions)
        self.assertFalse(
            hasattr(TradingEngine, "close_all_positions"),
            "reproduction precondition: method is missing",
        )

    @unittest.expectedFailure
    async def test_close_all_endpoint_closes_positions_and_keeps_management(self):
        import main
        from bot.kucoin import KuCoinClient
        from bot.nexus_runtime_engine import TradingEngine

        engine = TradingEngine(KuCoinClient())
        engine._running = True
        engine.active = True
        previous = getattr(main.app.state, "engine", None)
        main.app.state.engine = engine
        try:
            # Correct behaviour: returns a result and position management keeps
            # running. Actual: engine.stop() sets _running=False, then
            # AttributeError('close_all_positions').
            result = await main.close_all(SimpleNamespace())
            self.assertIn("closed", result)
            self.assertTrue(engine._running, "position management must continue")
        finally:
            main.app.state.engine = previous

    async def test_close_all_endpoint_actual_behaviour(self):
        import main
        from bot.kucoin import KuCoinClient
        from bot.nexus_runtime_engine import TradingEngine

        engine = TradingEngine(KuCoinClient())
        engine._running = True
        engine.active = True
        previous = getattr(main.app.state, "engine", None)
        main.app.state.engine = engine
        try:
            with self.assertRaises(AttributeError):
                await main.close_all(SimpleNamespace())
            # Side effect already applied before the crash: run loop is told
            # to exit, so trailing / naked-position guard / sync stop.
            self.assertFalse(engine._running)
            self.assertFalse(engine.active)
        finally:
            main.app.state.engine = previous


# ---------------------------------------------------------------------------
# F-002 (P0, FIXED) — LIVE protected entries (native TP/SL) bypassed the canonical
# READY_FOR_NEW_ENTRIES authority (initial reconciliation, protection
# readiness, durable state...). It is only evaluated in the core
# KuCoinClient.place_order, which the native TP/SL wrapper never calls.
# ---------------------------------------------------------------------------
def _not_ready_engine():
    return SimpleNamespace(
        instruments={"BTCUSDT": {}},
        _durable_state_ok=True,
        _financial_state_sane=True,
        _initial_reconciliation_complete=False,   # BGX-READY-002 not proven
        _execution_ownership_valid=True,
        _execution_ownership_expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        connected=True,
        viable_symbols=["BTCUSDT"],
        _market_data_ready=True,
        _protection_system_ready=False,          # BGX-READY-003 not proven
    )


_INSTRUMENT = {
    "minQty": 1, "lotSize": 1, "qtyStep": 1, "tickSize": 0.1,
    "multiplier": 0.001, "minBaseQty": 0.001, "minNotional": 0,
    "kucoinSymbol": "XBTUSDTM",
}


class F002NativeTpslReadinessBypassTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, live_module):
        from bot import kucoin
        from bot import kucoin_native_tpsl

        class Client(kucoin.KuCoinClient):
            pass

        kucoin_native_tpsl.install(Client, live_module, _Log())
        client = Client()
        client._instruments = {"BTCUSDT": dict(_INSTRUMENT)}
        client._engine = _not_ready_engine()
        client._get = AsyncMock(return_value={"marginMode": "CROSS"})
        client._post = AsyncMock(return_value={"orderId": "kc-1"})
        client.get_order_by_client_oid = AsyncMock(return_value={})
        return client

    def _live_module(self):
        from bot import kucoin
        return SimpleNamespace(PAPER_TRADE=False, API_KEY="k", to_kucoin=kucoin.to_kucoin)

    async def test_core_place_order_refuses_when_not_ready(self):
        from bot import kucoin
        client = kucoin.KuCoinClient()
        client._instruments = {"BTCUSDT": dict(_INSTRUMENT)}
        client._engine = _not_ready_engine()
        client._post = AsyncMock(return_value={"orderId": "kc-1"})
        with patch.object(kucoin, "PAPER_TRADE", False), \
                patch.object(kucoin, "API_KEY", "k"), \
                patch("bot.critical_state.critical_state.assert_available_for_new_risk", Mock()), \
                patch("bot.execution_ownership.validate_execution_ownership", AsyncMock()), \
                patch("bot.execution_ownership.publish_valid_execution_ownership", Mock()):
            client._execution_ownership = object()
            with self.assertRaisesRegex(RuntimeError, "READY_FOR_NEW_ENTRIES=false"):
                await client.place_order("BTCUSDT", "Buy", 0.002, sl=99000, tp=104000)
        client._post.assert_not_awaited()

    # FIXED: formerly test_native_tpsl_path_dispatches_when_not_ready_actual
    # (asserted the bypass) + an expectedFailure. Composed-runtime coverage:
    # tests/test_live_entry_readiness_gate.py.
    async def test_native_tpsl_path_must_refuse_when_not_ready(self):
        client = self._client(self._live_module())
        with self.assertRaisesRegex(RuntimeError, "READY_FOR_NEW_ENTRIES=false"):
            await client.place_order("BTCUSDT", "Buy", 0.002, sl=99000, tp=104000,
                                     idem_key="audit-f002", single_submission=True)
        client._post.assert_not_awaited()
        client._get.assert_not_awaited()   # not even the margin-mode read/switch


# ---------------------------------------------------------------------------
# F-010 (P1) — the durable "orders" block reason is shared between
# "unresolved/ambiguous order" and "persistence failed". Any later successful
# persist (e.g. a private-WS transition of another order) clears it while the
# ambiguous order is still non-terminal.
# ---------------------------------------------------------------------------
class F010DurableBlockConflationTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_persist_clears_ambiguity_block(self):
        from bot import durable_execution as durable
        from bot.order_state import OrderRegistry, OrderState

        engine = SimpleNamespace(orders=OrderRegistry(), _durable_state_errors=set(),
                                 _durable_state_ok=True, _durable_order_lock=asyncio.Lock())
        ambiguous, _ = engine.orders.get_or_create("bgx7-ambiguous", "BTCUSDT", "Buy", 2.0)
        ambiguous.transition(OrderState.SUBMITTING, source="LOCAL")
        durable._block(engine, "orders")          # engine.py ambiguous_dispatch path
        self.assertFalse(durable.can_open(engine))

        with patch.object(durable.db, "save_key_value", AsyncMock(return_value=True)):
            await durable.persist_orders(engine, "private_ws_transition")

        self.assertEqual(len(engine.orders.pending_orders()), 1, "still unresolved")
        # Defect: entries are authorised again although the intent is unresolved.
        self.assertTrue(durable.can_open(engine))


# ---------------------------------------------------------------------------
# F-011 (P1) — private WS order events are correlated by clientOid without
# validating symbol/side and re-index any orderId onto the internal order.
# ---------------------------------------------------------------------------
class F011PrivateWsIdentityTests(unittest.IsolatedAsyncioTestCase):
    @unittest.expectedFailure
    async def test_event_for_other_symbol_must_not_mutate_or_reindex(self):
        from bot.kucoin import KuCoinClient
        from bot.order_state import OrderRegistry, OrderState

        client = KuCoinClient()
        registry = OrderRegistry()
        client._order_registry = registry
        order, _ = registry.get_or_create("bgx7-parent", "BTCUSDT", "Buy", 2.0)
        order.transition(OrderState.SUBMITTING, source="LOCAL")
        order.transition(OrderState.SUBMITTED, order_id="parent-1", source="REST")
        registry.index_order_id("parent-1", "bgx7-parent")

        await client._handle_private_order_event({
            "subject": "symbolOrderChange",
            "data": {"orderId": "foreign-9", "clientOid": "bgx7-parent",
                     "symbol": "ETHUSDTM", "side": "sell", "type": "filled",
                     "status": "done", "filledSize": "7", "matchPrice": "1.0"},
        })
        self.assertIsNone(registry.get_by_order_id("foreign-9"))
        self.assertEqual(order.state, OrderState.SUBMITTED)


# ---------------------------------------------------------------------------
# F-012 (P1) — naked-position emergency close passes exchange position size
# (KuCoin CONTRACTS) as place_order qty, which is BASE-asset quantity.
# ---------------------------------------------------------------------------
class F012NakedGuardUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_emergency_close_quantity_is_in_base_asset(self):
        from bot import engine as core
        from bot import prelive_protection_failclosed as guard
        from bot.quantity import base_to_contracts

        class Engine(core.TradingEngine):
            pass

        guard.install(Engine, SimpleNamespace(KuCoinClient=type("K", (), {})), _Log())
        captured = {}

        class Client:
            _engine = None

            async def get_positions(self):
                # 5 contracts of XBTUSDTM (multiplier 0.001) == 0.005 BTC
                return [{"symbol": "BTCUSDT", "side": "Buy", "size": 5.0,
                         "entryPrice": 100000.0}]

            async def set_position_stops(self, *a, **k):
                return False

            async def place_order(self, **kwargs):
                captured.update(kwargs)
                return {}

        engine = Engine.__new__(Engine)
        engine.client = Client()
        engine.paper_trade = False
        engine.instruments = {"BTCUSDT": dict(_INSTRUMENT)}
        engine.positions = {"BTCUSDT": SimpleNamespace(sl=0)}
        engine._unprotected_symbols = set()
        with patch.object(guard, "conditional_stop_confirmed", AsyncMock(return_value=(False, "none"))):
            await engine._guard_naked_positions()

        self.assertTrue(captured.get("reduce_only"))
        sent_contracts = base_to_contracts(captured["qty"], _INSTRUMENT)
        # Actual: qty=5.0 is interpreted as 5 BTC -> 5000 contracts for a
        # 5-contract position (1000x). Correct would be 5 contracts.
        self.assertEqual(sent_contracts, 5000)


# ---------------------------------------------------------------------------
# F-020 (P2) — indicator edge cases.
# ---------------------------------------------------------------------------
class F020IndicatorTests(unittest.TestCase):
    @unittest.expectedFailure
    def test_rsi_of_flat_series_is_neutral(self):
        from bot.indicators import rsi
        self.assertAlmostEqual(float(rsi([100.0] * 30)[-1]), 50.0, places=6)

    @unittest.expectedFailure
    def test_atr_short_series_does_not_raise(self):
        from bot.indicators import atr
        atr([1.0] * 10, [0.5] * 10, [0.8] * 10)

    @unittest.expectedFailure
    def test_adx_between_period_plus_2_and_2_period_does_not_raise(self):
        from bot.indicators import adx
        n = 20
        adx([100 + i for i in range(n)], [98 + i for i in range(n)], [99 + i for i in range(n)])


# ---------------------------------------------------------------------------
# F-021 (P1) — backtest/live candle parity: the core analyzer drops the last
# element unconditionally, but the backtester already passes only closed
# candles, so every backtest decision is made one closed bar late.
# ---------------------------------------------------------------------------
class F021BacktestClosedCandleParityTests(unittest.TestCase):
    def test_core_analyzer_discards_last_closed_candle(self):
        from bot import strategy

        seen = {}
        original = strategy.score_tf

        def spy(closes, *a, **k):
            seen.setdefault("lens", []).append(len(closes))
            seen.setdefault("last", []).append(closes[-1])
            return {"ok": False, "total": 0}

        def candles(n, step_ms):
            return [{"ts": i * step_ms, "o": 100 + i, "h": 101 + i, "l": 99 + i,
                     "c": 100.5 + i, "v": 1000.0} for i in range(n)]

        k15, k1h, k4h = candles(60, 900_000), candles(60, 3_600_000), candles(60, 14_400_000)
        with patch.object(strategy, "score_tf", spy), \
                patch.object(strategy, "detect_regime", lambda *a, **k: "TRENDING_UP"):
            # __wrapped__ = production analyze_mtf without the shadow observers
            # (observe_analyze_mtf re-runs several shadow analyses afterwards).
            strategy.Analyzer.analyze_mtf.__wrapped__(strategy.Analyzer(), "BT", k15, k1h, k4h)
        # All three windows were passed as CLOSED candles; the analyzer used
        # only n-1 of them, i.e. the newest closed close (159.5) is ignored.
        self.assertEqual(seen["lens"], [59, 59, 59])
        self.assertEqual(seen["last"][-1], 100.5 + 58)


# ---------------------------------------------------------------------------
# F-030 (P3) — the offline runner never counts skipped tests.
# ---------------------------------------------------------------------------
class F030RunnerSkipCountTests(unittest.TestCase):
    def test_skip_regex_never_matches_unittest_output(self):
        import re
        from pathlib import Path
        src = Path(__file__).with_name("run_offline.py").read_text()
        self.assertIn(r"r'skipped=(\\d+)'", src)
        self.assertEqual(re.findall(r'skipped=(\\d+)', "OK (skipped=3)"), [])


if __name__ == "__main__":
    unittest.main()
