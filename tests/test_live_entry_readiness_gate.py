"""INV-LIVE-READINESS-001 on the composed production LIVE runtime (F-002).

No new LIVE entry may reach any exchange dispatch while the canonical
``assert_ready_for_new_entries`` refuses. This suite composes the real runtime
(``sitecustomize`` -> runtime_bootstrap -> runtime_overlays) with the full
controlled-pilot release configuration, so ``KuCoinClient.place_order`` and
``KuCoinClient._post`` are the FINAL production wrappers. The only network
object is a recording fake HTTP session; REST base points to 127.0.0.1:1, no
credentials exist and the offline runner blocks non-loopback sockets.
Database/ownership leases (not exchange I/O) are mocked.
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
    "KUCOIN_API_KEY": "",
    "KUCOIN_API_SECRET": "",
    "KUCOIN_API_PASSPHRASE": "",
    "NEXUS_TELEGRAM": "false",
})
os.environ.pop("EXECUTION_CAPABILITY", None)

import builtins  # noqa: E402
import json  # noqa: E402
import unittest  # noqa: E402
from contextlib import ExitStack  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock, Mock, patch  # noqa: E402

import sitecustomize  # noqa: E402,F401  (production overlay composition)
from bot import kucoin  # noqa: E402
from bot.order_state import OrderRegistry, OrderState  # noqa: E402
from bot.runtime_readiness import EntryReadinessRefused  # noqa: E402

_INSTRUMENT = {
    "minQty": 1, "lotSize": 1, "qtyStep": 1, "tickSize": 0.1,
    "multiplier": 0.001, "minBaseQty": 0.001, "minNotional": 0,
    "kucoinSymbol": "XBTUSDTM",
}


class _Response:
    def __init__(self, payload):
        self.status = 200
        self.headers = {}
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Records every HTTP call. Any call here == an exchange side effect."""
    closed = False

    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        body = json.loads(kwargs.get("data") or "{}")
        self.calls.append(("POST", url, body))
        return _Response({"code": "200000", "data": {"orderId": "kc-fake-1"}})

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, None))
        if "getMarginMode" in url:
            return _Response({"code": "200000", "data": {"marginMode": "CROSS"}})
        return _Response({"code": "200000", "data": {}})

    def delete(self, url, **kwargs):
        self.calls.append(("DELETE", url, None))
        return _Response({"code": "200000", "data": {}})

    def posts(self, *endpoints):
        return [c for c in self.calls if c[0] == "POST" and any(c[1].endswith(e) for e in endpoints)]


def _engine(**overrides):
    state = dict(
        instruments={"BTCUSDT": dict(_INSTRUMENT)}, _durable_state_ok=True,
        _financial_state_sane=True, _initial_reconciliation_complete=True,
        _execution_ownership_valid=True,
        _execution_ownership_expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        connected=True, viable_symbols=["BTCUSDT"], _market_data_ready=True,
        _protection_system_ready=True, positions={}, pilot=None,
    )
    state.update(overrides)
    return SimpleNamespace(**state)


class LiveEntryReadinessGateComposedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        # Database-backed leases/budgets only; never exchange I/O.
        self.stack.enter_context(patch("bot.live_execution_fence.acquire", AsyncMock(return_value=True)))
        self.stack.enter_context(patch("bot.execution_ownership.validate_execution_ownership", AsyncMock()))
        self.stack.enter_context(patch("bot.execution_ownership.publish_valid_execution_ownership", Mock()))
        self.stack.enter_context(patch("bot.critical_state.critical_state.assert_available_for_new_risk", Mock()))
        self.stack.enter_context(patch("bot.pilot_submission_counter.reserve_submission",
                                       AsyncMock(return_value=(True, 1))))
        self.stack.enter_context(patch("bot.durable_execution.persist_orders", AsyncMock(return_value=True)))
        self.stack.enter_context(patch.object(kucoin, "API_KEY", "test-key"))
        self.stack.enter_context(patch("asyncio.sleep", AsyncMock()))

    def tearDown(self):
        self.stack.close()

    def _client(self, engine):
        client = kucoin.KuCoinClient()
        client._session = _FakeSession()
        client._instruments = {"BTCUSDT": dict(_INSTRUMENT)}
        client._engine = engine
        client._execution_ownership = object()
        client._order_registry = OrderRegistry()
        return client

    def _intent(self, client, idem, side="Buy", qty=0.002):
        oid = client.build_client_oid("BTCUSDT", side, qty, idem)
        order, _ = client._order_registry.get_or_create(oid, "BTCUSDT", side, float(client._round_qty(qty, "BTCUSDT")))
        order.transition(OrderState.SUBMITTING, source="REST")
        return order

    # -- composition sanity -------------------------------------------------
    def test_composition_is_production_live_pilot(self):
        self.assertEqual(builtins._nexus_sitecustomize_status, "ok")
        self.assertEqual(getattr(builtins, "_nexus_runtime_contract_status", None), "ok")
        self.assertFalse(kucoin.PAPER_TRADE)
        self.assertEqual(kucoin.KuCoinClient.place_order.__module__, "bot.live_execution_fence")
        self.assertEqual(kucoin.KuCoinClient._fenced_entry_post.__module__, "bot.pilot_submission_counter")
        self.assertEqual(kucoin.KuCoinClient._entry_safe_post.__module__, "bot.kucoin")
        self.assertTrue(getattr(kucoin.KuCoinClient, "_native_tpsl_entry_installed", False))

    # -- TEST A / D: ready -> native TP/SL reaches the fake exchange ------------
    async def test_a_ready_native_tpsl_entry_reaches_dispatch(self):
        client = self._client(_engine())
        self._intent(client, "gate-a")
        out = await client.place_order("BTCUSDT", "Buy", 0.002, sl=99000, tp=104000,
                                       idem_key="gate-a", single_submission=True)
        self.assertEqual(out.get("orderId"), "kc-fake-1")
        self.assertEqual(len(client._session.posts("/api/v1/st-orders")), 1)

    # -- TEST B / C / D / F: not ready -> zero exchange calls -------------------
    async def _assert_native_refused(self, engine, idem):
        client = self._client(engine)
        order = self._intent(client, idem)
        with self.assertRaisesRegex(EntryReadinessRefused, "READY_FOR_NEW_ENTRIES=false"):
            await client.place_order("BTCUSDT", "Buy", 0.002, sl=99000, tp=104000,
                                     idem_key=idem, single_submission=True)
        self.assertEqual(client._session.calls, [], "no exchange call of any kind")
        self.assertEqual(order.state, OrderState.FAILED)
        self.assertIsNone(order.order_id, "no external orderId")
        self.assertTrue(any(
            isinstance(h[3], dict) and h[3].get("predispatch_abort_reason") == "PRE_DISPATCH_READINESS_DENIED"
            for h in order.history))

    async def test_b_initial_reconciliation_incomplete_blocks_entry(self):
        await self._assert_native_refused(_engine(_initial_reconciliation_complete=False), "gate-b")

    async def test_c_protection_readiness_invalid_blocks_entry(self):
        await self._assert_native_refused(_engine(_protection_system_ready=False), "gate-c")

    async def test_missing_engine_blocks_entry(self):
        client = self._client(None)
        with self.assertRaises(EntryReadinessRefused):
            await client.place_order("BTCUSDT", "Buy", 0.002, sl=99000, tp=104000,
                                     idem_key="gate-none", single_submission=True)
        self.assertEqual(client._session.calls, [])

    # -- TEST E: original (non-TPSL) path, same behaviour ------------------------
    async def test_e_original_path_ready_and_not_ready(self):
        ready = self._client(_engine())
        await ready.place_order("BTCUSDT", "Buy", 0.002, idem_key="gate-e1")
        self.assertEqual(len(ready._session.posts("/api/v1/orders")), 1)

        blocked = self._client(_engine(_protection_system_ready=False))
        with self.assertRaisesRegex(RuntimeError, "READY_FOR_NEW_ENTRIES=false"):
            await blocked.place_order("BTCUSDT", "Buy", 0.002, idem_key="gate-e2")
        self.assertEqual(blocked._session.posts("/api/v1/orders", "/api/v1/st-orders"), [])

    # -- transport backstop: any helper calling the final _post directly ---------
    async def test_transport_boundary_blocks_direct_new_risk_post(self):
        for endpoint in ("/api/v1/orders", "/api/v1/st-orders"):
            client = self._client(_engine(_initial_reconciliation_complete=False))
            order = self._intent(client, "gate-t-" + endpoint[-6:])
            body = {"clientOid": order.client_oid, "symbol": "XBTUSDTM", "side": "buy",
                    "type": "market", "size": "2", "leverage": "10"}
            with self.assertRaises(EntryReadinessRefused):
                await client._post(endpoint, body)
            self.assertEqual(client._session.calls, [], endpoint)
            self.assertEqual(order.state, OrderState.FAILED, "proven not dispatched")

    # -- TEST G: reduce-only management is not blocked by the entry gate ---------
    async def test_g_reduce_only_exit_allowed_when_not_ready(self):
        client = self._client(_engine(_initial_reconciliation_complete=False,
                                      _protection_system_ready=False))
        await client.place_order("BTCUSDT", "Sell", 0.002, reduce_only=True, idem_key="gate-g")
        posted = client._session.posts("/api/v1/orders")
        self.assertEqual(len(posted), 1)
        self.assertTrue(posted[0][2].get("reduceOnly"))

    async def test_g_protective_stop_order_allowed_when_not_ready(self):
        client = self._client(_engine(_initial_reconciliation_complete=False))
        body = {"clientOid": "bgx7-stop", "symbol": "XBTUSDTM", "side": "sell", "type": "market",
                "stop": "down", "stopPrice": "99000", "stopPriceType": "MP",
                "closeOrder": True, "reduceOnly": True}
        await client._post("/api/v1/orders", body, single_attempt=True)
        self.assertEqual(len(client._session.posts("/api/v1/orders")), 1)

    # -- TEST H: readiness evaluation error fails closed ---------------------------
    async def test_h_readiness_exception_fails_closed(self):
        client = self._client(_engine())
        self._intent(client, "gate-h")
        with patch("bot.runtime_readiness.runtime_readiness", side_effect=ValueError("boom")):
            with self.assertRaisesRegex(EntryReadinessRefused, "readiness evaluation failed"):
                await client.place_order("BTCUSDT", "Buy", 0.002, sl=99000, tp=104000,
                                         idem_key="gate-h", single_submission=True)
            with self.assertRaises(EntryReadinessRefused):
                await client._post("/api/v1/st-orders", {"clientOid": "bgx7-h", "symbol": "XBTUSDTM",
                                                         "side": "buy", "size": 2})
        self.assertEqual(client._session.calls, [])


if __name__ == "__main__":
    unittest.main()
