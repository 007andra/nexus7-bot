"""F-010B N/K/G — failed restore on the composed LIVE-pilot runtime (offline).

Real composition (sitecustomize): final ``restore_engine_state``, final private
WS handler (F-011), real ``persist_orders`` and the F-002 dispatch gate. The
durable store is an in-memory dict and the HTTP session records every call.
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

import time  # noqa: E402
import unittest  # noqa: E402
from contextlib import ExitStack  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock, Mock, patch  # noqa: E402

import sitecustomize  # noqa: E402,F401
from bot import durable_execution as durable  # noqa: E402
from bot import kucoin  # noqa: E402
from bot.order_state import OrderRegistry, OrderState  # noqa: E402
from bot.runtime_readiness import EntryReadinessRefused  # noqa: E402
from tests.test_durable_restore_evidence import KEY, _seed, _Store  # noqa: E402
from tests.test_live_entry_readiness_gate import _INSTRUMENT, _FakeSession  # noqa: E402


class ComposedRestoreEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.stack.enter_context(patch("bot.live_execution_fence.acquire", AsyncMock(return_value=True)))
        self.stack.enter_context(patch("bot.execution_ownership.validate_execution_ownership", AsyncMock()))
        self.stack.enter_context(patch("bot.execution_ownership.publish_valid_execution_ownership", Mock()))
        self.stack.enter_context(patch("bot.critical_state.critical_state.assert_available_for_new_risk", Mock()))
        self.stack.enter_context(patch("bot.pilot_submission_counter.reserve_submission",
                                       AsyncMock(return_value=(True, 1))))
        self.stack.enter_context(patch.object(kucoin, "API_KEY", "test-key"))
        self.stack.enter_context(patch("asyncio.sleep", AsyncMock()))

    def tearDown(self):
        self.stack.close()

    async def test_n_failed_restore_preserves_snapshot_through_ws_and_blocks_entry(self):
        self.assertFalse(kucoin.PAPER_TRADE)
        self.assertEqual(kucoin.KuCoinClient.place_order.__module__, "bot.live_execution_fence")
        store = _Store()
        original = await _seed(store, ["bgx7-A", "bgx7-B"])
        store.read_error = OSError("storage read timeout")
        for p in store.patches():
            self.stack.enter_context(p)

        engine = SimpleNamespace(
            instruments={"BTCUSDT": dict(_INSTRUMENT)}, _financial_state_sane=True,
            _initial_reconciliation_complete=True, _execution_ownership_valid=True,
            _execution_ownership_expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
            connected=True, viable_symbols=["BTCUSDT"], _market_data_ready=True,
            _protection_system_ready=True, positions={}, pilot=None, paper_trade=False,
            orders=OrderRegistry())
        await durable.restore_engine_state(engine)           # composed (final) restore
        self.assertEqual(engine._durable_order_restore_state, "RESTORE_FAILED")

        client = kucoin.KuCoinClient()
        client._session = _FakeSession()
        client._instruments = {"BTCUSDT": dict(_INSTRUMENT)}
        client._engine = engine
        client._execution_ownership = object()
        client._order_registry = engine.orders

        # G: a legitimate private-WS event for an order created after the failure.
        late, _ = engine.orders.get_or_create("bgx7-late", "BTCUSDT", "Sell", 0.002)
        late.transition(OrderState.SUBMITTING, source="LOCAL")
        late.transition(OrderState.SUBMITTED, order_id="kc-late", source="REST")
        engine.orders.index_order_id("kc-late", late.client_oid)
        await client._handle_private_order_event({"subject": "symbolOrderChange", "data": {
            "orderId": "kc-late", "clientOid": "bgx7-late", "symbol": "XBTUSDTM",
            "side": "sell", "type": "filled", "status": "done", "filledSize": "2",
            "size": "2", "ts": int(time.time() * 1e9)}})
        self.assertEqual(late.state, OrderState.FILLED, "in-memory update allowed")
        self.assertEqual(store.data[KEY], original, "WS cannot overwrite the snapshot")

        # K: F-002 gate refuses the new entry; zero exchange calls.
        oid = client.build_client_oid("BTCUSDT", "Buy", 0.002, "f010b")
        intent, _ = engine.orders.get_or_create(
            oid, "BTCUSDT", "Buy", float(client._round_qty(0.002, "BTCUSDT")))
        intent.transition(OrderState.SUBMITTING, source="REST")
        with self.assertRaises(EntryReadinessRefused) as ctx:
            await client.place_order("BTCUSDT", "Buy", 0.002, sl=99000, tp=104000,
                                     idem_key="f010b", single_submission=True)
        self.assertIn("durable:" + durable.ORDERS_RESTORE, ctx.exception.blockers)
        self.assertEqual(client._session.calls, [])
        self.assertEqual(store.data[KEY], original, "pre-dispatch abort persisted nothing")
        self.assertIn(durable.ORDERS_RESTORE, durable.active_reasons(engine))

        # Crash + restart with a healthy read recovers the original evidence.
        store.read_error = None
        boot2 = SimpleNamespace(orders=OrderRegistry(), paper_trade=False)
        await durable.restore_engine_state(boot2)
        self.assertEqual(sorted(o.client_oid for o in boot2.orders.pending_orders()),
                         ["bgx7-A", "bgx7-B"])
        self.assertEqual(durable.active_reasons(boot2), ())


if __name__ == "__main__":
    unittest.main()
