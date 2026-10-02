"""F-014 S/R — malformed position rows on the composed LIVE-pilot runtime.

Real composition (sitecustomize): ``nexus_runtime_engine.TradingEngine`` with
its ``KuCoinPositionUnitAdapter``, the final overlay chain of
``_sync_positions``, protection readiness and the F-002 dispatch gate. Only
the REST transport, DB writes and DB leases are fakes. Offline.
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
os.environ.pop("EXECUTION_CAPABILITY", None)

import unittest  # noqa: E402
from contextlib import ExitStack  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from unittest.mock import AsyncMock, Mock, patch  # noqa: E402

import sitecustomize  # noqa: E402,F401
from bot import engine as core  # noqa: E402
from bot import kucoin  # noqa: E402
from bot.nexus_runtime_engine import TradingEngine  # noqa: E402
from bot.order_state import OrderRegistry, OrderState  # noqa: E402
from bot.protection_readiness import refresh_protection_readiness  # noqa: E402
from bot.runtime_readiness import EntryReadinessRefused  # noqa: E402
from bot.strategy import Signal  # noqa: E402
from tests.test_live_entry_readiness_gate import _INSTRUMENT, _FakeSession  # noqa: E402

ETH_INFO = {"multiplier": 0.01, "lotSize": 1, "minQty": 1, "tickSize": 0.01, "minNotional": 0,
            "kucoinSymbol": "ETHUSDTM"}


class ComposedPositionAuthorityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.stack.enter_context(patch("bot.live_execution_fence.acquire", AsyncMock(return_value=True)))
        self.stack.enter_context(patch("bot.execution_ownership.validate_execution_ownership", AsyncMock()))
        self.stack.enter_context(patch("bot.execution_ownership.publish_valid_execution_ownership", Mock()))
        self.stack.enter_context(patch("bot.critical_state.critical_state.assert_available_for_new_risk", Mock()))
        self.stack.enter_context(patch("bot.pilot_submission_counter.reserve_submission",
                                       AsyncMock(return_value=(True, 1))))
        self.stack.enter_context(patch("bot.durable_execution.persist_orders", AsyncMock(return_value=True)))
        self.stack.enter_context(patch.object(kucoin, "API_KEY", "test-key"))
        self.stack.enter_context(patch("asyncio.sleep", AsyncMock()))
        self.closes = AsyncMock()
        self.stack.enter_context(patch.object(core.db, "save_trade_close", self.closes))
        self.checkpoint = AsyncMock()
        self.stack.enter_context(patch("bot.durable_daily_pnl.checkpoint", self.checkpoint))
        self.stack.enter_context(patch.object(core, "notify", AsyncMock()))

    def tearDown(self):
        self.stack.close()

    async def test_s_r_malformed_row_keeps_position_and_blocks_entry(self):
        self.assertFalse(kucoin.PAPER_TRADE)
        raw = kucoin.KuCoinClient()
        raw._session = _FakeSession()
        raw._instruments = {"BTCUSDT": dict(_INSTRUMENT), "ETHUSDT": dict(ETH_INFO)}
        rows = {"value": [{"symbol": "ETHUSDTM", "currentQty": -30, "avgEntryPrice": "abc",
                           "markPrice": "3000"}]}

        async def fake_get(endpoint, params=None, auth=False):
            if endpoint == "/api/v1/positions":
                return {"code": "200000", "data": rows["value"]}
            return {"code": "200000", "data": {"items": []}}
        raw._get = fake_get
        raw.get_balance = AsyncMock(return_value=100.0)
        engine = TradingEngine(raw)
        self.assertIsNot(engine.client, raw, "production unit adapter in place")
        engine.instruments = {"BTCUSDT": dict(_INSTRUMENT), "ETHUSDT": dict(ETH_INFO)}
        sig = Signal("ETHUSDT", "SHORT", 3100.0, 3162.0, 2945.0, 0.8, "owned", 80)
        engine.positions["ETHUSDT"] = core.Position(sig, 0.3)
        engine._trade_ids["ETHUSDT"] = 11
        stats_before = len(getattr(engine.stats, "trades", []) or [])

        await engine._sync_positions()                 # final composed chain
        self.assertIn("ETHUSDT", engine.positions, "no phantom close")
        self.assertAlmostEqual(engine.positions["ETHUSDT"].qty, 0.3)
        self.closes.assert_not_awaited()
        self.checkpoint.assert_not_awaited()
        self.assertEqual(len(getattr(engine.stats, "trades", []) or []), stats_before)
        self.assertEqual(engine._trade_ids.get("ETHUSDT"), 11)

        # R: readiness cannot derive flat/protected; the F-002 gate refuses.
        engine.connected = True
        self.assertFalse(await refresh_protection_readiness(engine))
        engine._financial_state_sane = True
        engine._initial_reconciliation_complete = True
        engine._execution_ownership_valid = True
        engine._execution_ownership_expires_at = datetime.now(timezone.utc) + timedelta(seconds=30)
        engine._market_data_ready = True
        engine._durable_state_ok = True
        raw._engine = engine
        raw._execution_ownership = object()
        raw._order_registry = OrderRegistry()
        oid = raw.build_client_oid("BTCUSDT", "Buy", 0.002, "f014")
        intent, _ = raw._order_registry.get_or_create(
            oid, "BTCUSDT", "Buy", float(raw._round_qty(0.002, "BTCUSDT")))
        intent.transition(OrderState.SUBMITTING, source="REST")
        with self.assertRaises(EntryReadinessRefused) as ctx:
            await raw.place_order("BTCUSDT", "Buy", 0.002, sl=99000, tp=104000,
                                  idem_key="f014", single_submission=True)
        self.assertIn("protection_system_ready", ctx.exception.blockers)
        self.assertEqual([c for c in raw._session.calls if c[0] == "POST"], [], "zero opening calls")

        # Recovery: a valid read showing the position open keeps managing it.
        rows["value"] = [{"symbol": "ETHUSDTM", "currentQty": -30, "avgEntryPrice": "3100",
                          "markPrice": "3000", "unrealisedPnl": "3"}]
        await engine._sync_positions()
        self.assertIn("ETHUSDT", engine.positions)
        self.closes.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
