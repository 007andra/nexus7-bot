"""F-010 L/N — causal blocks on the composed LIVE-pilot runtime (offline).

Real composition (sitecustomize), final ``KuCoinClient.place_order`` chain with
the F-002 dispatch gate, real ``persist_orders`` and the composed
``reconcile_pending``. Only the database write, DB leases and HTTP session are
fakes; the session records every exchange call.
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

import asyncio  # noqa: E402
import unittest  # noqa: E402
from contextlib import ExitStack  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock, Mock, patch  # noqa: E402

import sitecustomize  # noqa: E402,F401
from bot import durable_execution as durable  # noqa: E402
from bot import durable_live_reconciliation as reconciler  # noqa: E402
from bot import kucoin  # noqa: E402
from bot.order_state import OrderRegistry, OrderState  # noqa: E402
from bot.runtime_readiness import EntryReadinessRefused  # noqa: E402
from tests.test_live_entry_readiness_gate import _INSTRUMENT, _FakeSession  # noqa: E402


class ComposedCausalBlockTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.stack.enter_context(patch("bot.live_execution_fence.acquire", AsyncMock(return_value=True)))
        self.stack.enter_context(patch("bot.execution_ownership.validate_execution_ownership", AsyncMock()))
        self.stack.enter_context(patch("bot.execution_ownership.publish_valid_execution_ownership", Mock()))
        self.stack.enter_context(patch("bot.critical_state.critical_state.assert_available_for_new_risk", Mock()))
        self.stack.enter_context(patch("bot.pilot_submission_counter.reserve_submission",
                                       AsyncMock(return_value=(True, 1))))
        self.saves = []

        async def save(key, value, strict=False):
            self.saves.append(key)
            return True
        self.stack.enter_context(patch.object(durable.db, "save_key_value", AsyncMock(side_effect=save)))
        self.stack.enter_context(patch.object(kucoin, "API_KEY", "test-key"))
        self.stack.enter_context(patch("asyncio.sleep", AsyncMock()))

    def tearDown(self):
        self.stack.close()

    def _runtime(self):
        engine = SimpleNamespace(
            instruments={"BTCUSDT": dict(_INSTRUMENT)}, _durable_state_ok=True,
            _durable_state_errors=set(), _durable_order_lock=asyncio.Lock(),
            _financial_state_sane=True, _initial_reconciliation_complete=True,
            _execution_ownership_valid=True,
            _execution_ownership_expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
            connected=True, viable_symbols=["BTCUSDT"], _market_data_ready=True,
            _protection_system_ready=True, positions={}, pilot=None, paper_trade=False,
            orders=OrderRegistry())
        client = kucoin.KuCoinClient()
        client._session = _FakeSession()
        client._instruments = {"BTCUSDT": dict(_INSTRUMENT)}
        client._engine = engine
        client._execution_ownership = object()
        client._order_registry = engine.orders

        async def callback(_order):
            await durable.persist_orders(engine, "private_ws_transition")
        engine.orders.persist_callback = callback
        return engine, client

    def _intent(self, client, idem):
        oid = client.build_client_oid("BTCUSDT", "Buy", 0.002, idem)
        order, _ = client._order_registry.get_or_create(
            oid, "BTCUSDT", "Buy", float(client._round_qty(0.002, "BTCUSDT")))
        order.transition(OrderState.SUBMITTING, source="REST")
        return order

    async def test_l_n_ambiguity_blocks_dispatch_after_unrelated_persist_until_resolved(self):
        self.assertFalse(kucoin.PAPER_TRADE)
        self.assertEqual(kucoin.KuCoinClient.place_order.__module__, "bot.live_execution_fence")
        engine, client = self._runtime()

        ambiguous, _ = engine.orders.get_or_create("bgx7-ambiguous", "ETHUSDT", "Buy", 1.0)
        ambiguous.transition(OrderState.SUBMITTING, source="REST")
        durable._block(engine, durable.ORDERS_UNRESOLVED, source="ambiguous_dispatch")

        # Unrelated successful persistence through the real registry callback.
        other, _ = engine.orders.get_or_create("bgx7-other", "SOLUSDT", "Sell", 1.0)
        other.transition(OrderState.SUBMITTING, source="REST")
        other.transition(OrderState.SUBMITTED, order_id="kc-other", source="WS")
        await engine.orders.persist_callback(other)
        self.assertIn("order_registry_state_v1", self.saves, "persist really succeeded")
        self.assertEqual(durable.active_reasons(engine), (durable.ORDERS_UNRESOLVED,))

        self._intent(client, "f010-blocked")
        with self.assertRaises(EntryReadinessRefused) as ctx:
            await client.place_order("BTCUSDT", "Buy", 0.002, sl=99000, tp=104000,
                                     idem_key="f010-blocked", single_submission=True)
        self.assertIn("durable:" + durable.ORDERS_UNRESOLVED, ctx.exception.blockers)
        self.assertEqual(client._session.calls, [], "zero exchange calls under ambiguity")

        # Real resolution: composed reconcile proves the ambiguous intent terminal.
        engine.client = SimpleNamespace(get_order_by_client_oid=AsyncMock(return_value={
            "clientOid": "bgx7-ambiguous", "symbol": "ETHUSDTM", "orderId": "kc-amb",
            "isActive": False, "cancelExist": True, "status": "done", "filledSize": "0"}))
        engine._durable_live_reconcile_last = 0.0
        other.transition(OrderState.CANCELLED, order_id="kc-other", source="WS")
        with patch.object(reconciler, "_owner_valid", AsyncMock(return_value=True)):
            self.assertTrue(await reconciler.reconcile_pending(engine, min_interval_s=0))
        self.assertEqual(ambiguous.state, OrderState.CANCELLED)
        self.assertEqual(durable.active_reasons(engine), ())

        self._intent(client, "f010-ready")
        out = await client.place_order("BTCUSDT", "Buy", 0.002, sl=99000, tp=104000,
                                       idem_key="f010-ready", single_submission=True)
        self.assertEqual(out.get("orderId"), "kc-fake-1")
        self.assertEqual(len(client._session.posts("/api/v1/st-orders")), 1)


if __name__ == "__main__":
    unittest.main()
