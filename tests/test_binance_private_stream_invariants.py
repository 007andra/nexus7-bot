"""Binance USD-M private/user-data stream invariants (offline, no real orders).

Drives the real ``BinanceClient._private_ws_loop`` / handler over a fake
transport, the real ``OrderRegistry`` state machine, the real REST
normalization (``_normalize_order``) + ``apply_exchange_order_truth``, the
real LIVE preflight private-stream step and the real Binance protection
guard. No network, no real order, fake listenKeys only.
"""
import asyncio
import json
import logging
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot import binance
from bot import pilot
from bot.durable_live_reconciliation import apply_exchange_order_truth
from bot.order_state import OrderRegistry, OrderState
from bot.private_stream_health import PrivateStreamHealth

_STOP = object()
FULL_KEY_1 = "LKone" + "A" * 59
FULL_KEY_2 = "LKtwo" + "B" * 59


def order_event(coid, status, *, oid="9001", symbol="BTCUSDT", filled="0", event_time=None):
    return {
        "e": "ORDER_TRADE_UPDATE", "E": event_time or int(time.time() * 1000),
        "o": {"s": symbol, "c": coid, "S": "BUY", "q": "0.01", "X": status,
              "i": int(oid), "z": filled, "ap": "60000"},
    }


class FakeConn:
    def __init__(self, url):
        self.url = url
        self.queue = asyncio.Queue()
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        self.closed = True
        return False

    async def close(self):
        self.closed = True
        self.queue.put_nowait(_STOP)

    def push(self, frame):
        self.queue.put_nowait(frame if isinstance(frame, str) else json.dumps(frame))

    def drop(self):
        self.queue.put_nowait(ConnectionError("server closed"))

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        while True:
            item = await self.queue.get()
            if item is _STOP:
                return
            if isinstance(item, Exception):
                raise item
            yield item


class Transport:
    def __init__(self):
        self.conns = []

    def connect(self, url, **kw):
        conn = FakeConn(url)
        self.conns.append(conn)
        return conn


async def settle(n=30):
    for _ in range(n):
        await asyncio.sleep(0)


async def wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached")


class PrivateStreamHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = binance.BinanceClient()
        self.registry = OrderRegistry()
        self.client._order_registry = self.registry
        self.keys = iter([FULL_KEY_1, FULL_KEY_2] + [f"LK{n:03d}" + "C" * 59 for n in range(50)])
        self.listen_calls = []
        self.put_fail = False
        self.post_fail = None

        async def listen(method):
            self.listen_calls.append(method)
            if method == "POST":
                if self.post_fail:
                    raise RuntimeError(self.post_fail)
                return {"listenKey": next(self.keys)}
            if self.put_fail:
                raise RuntimeError("Binance listenKey HTTP 400")
            return {}

        self.client._listen_key_request = listen
        self.client.place_order = AsyncMock(side_effect=AssertionError("stream must never place orders"))
        self.transport = Transport()
        self._patch = patch.object(binance.websockets, "connect", self.transport.connect)
        self._patch.start()
        self.task = None

    async def asyncTearDown(self):
        self._patch.stop()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def health(self):
        return self.client.private_stream_health

    async def start(self):
        self.task = asyncio.create_task(self.client._private_ws_loop())
        await wait_for(lambda: self.transport.conns and self.health().state == "CONNECTED")
        return self.transport.conns[-1]

    def submitted(self, coid="bgx7-t-0001", oid="9001"):
        order, _ = self.registry.get_or_create(coid, "BTCUSDT", "Buy", 0.01)
        order.transition(OrderState.SUBMITTING, source="REST")
        order.transition(OrderState.SUBMITTED, order_id=oid, source="REST")
        self.registry.index_order_id(oid, coid)
        return order


class ProtocolAndHealthTests(PrivateStreamHarness):
    async def test_01_02_03_authority_route_and_listen_key(self):
        self.assertIsInstance(self.health(), PrivateStreamHealth)
        conn = await self.start()
        self.assertEqual(conn.url, f"wss://fstream.binance.com/private/ws/{FULL_KEY_1}")
        self.assertEqual(self.listen_calls, ["POST"])
        snap = self.health().snapshot()
        self.assertEqual((snap["state"], snap["route"], snap["connection_epoch"]), ("CONNECTED", "/private", 1))
        self.assertGreater(snap["listen_key_ttl_s"], 3000)
        # Connected is not trusted until REST reconciliation for this epoch.
        self.assertEqual(self.health().check(), (False, "reconcile_required"))
        self.assertTrue(self.health().mark_reconciled(1))
        self.assertEqual(self.health().check(), (True, "event_capable"))

    async def test_04_auth_error_blocks(self):
        self.post_fail = "Binance listenKey HTTP 401"
        self.task = asyncio.create_task(self.client._private_ws_loop())
        await wait_for(lambda: self.health().snapshot()["auth_failed"])
        self.assertEqual(self.health().check(), (False, "auth_failed"))
        self.assertEqual(pilot.private_stream_blockers(self.client),
                         ["14_WS: stream privado não apto (reason=auth_failed)"])
        self.assertEqual(self.transport.conns, [])

    async def test_05_06_07_malformed_unknown_and_account_update(self):
        conn = await self.start()
        order = self.submitted()
        conn.push("{not json")
        conn.push("[1,2]")
        conn.push({"e": "SOMETHING_NEW", "E": 1})
        await settle()
        self.assertEqual(self.health().snapshot()["events_total"], 0)
        self.assertEqual(order.state, OrderState.SUBMITTED)
        self.assertEqual(self.health().state, "CONNECTED")  # stream kept alive
        conn.push({"e": "ACCOUNT_UPDATE", "E": 5, "a": {"B": [], "P": []}})
        await settle()
        snap = self.health().snapshot()
        self.assertEqual((snap["events_total"], snap["last_event"]), (1, "ACCOUNT_UPDATE"))
        self.assertEqual(snap["last_event_exchange_ms"], 5)
        self.assertEqual(order.state, OrderState.SUBMITTED)  # REST owns positions

    async def test_08_to_12_order_statuses(self):
        conn = await self.start()
        cases = [
            ("bgx7-new", ["NEW"], OrderState.SUBMITTED, True),
            ("bgx7-part", ["PARTIALLY_FILLED"], OrderState.PARTIALLY_FILLED, False),
            ("bgx7-fill", ["PARTIALLY_FILLED", "FILLED"], OrderState.FILLED, False),
            ("bgx7-cxl", ["CANCELED"], OrderState.CANCELLED, False),
            ("bgx7-rej", ["REJECTED"], OrderState.REJECTED, False),
            ("bgx7-exp", ["EXPIRED"], OrderState.CANCELLED, False),
        ]
        for n, (coid, statuses, expected, from_submitting) in enumerate(cases):
            oid = str(7000 + n)
            order, _ = self.registry.get_or_create(coid, "BTCUSDT", "Buy", 0.01)
            order.transition(OrderState.SUBMITTING, source="REST")
            if not from_submitting:
                order.transition(OrderState.SUBMITTED, order_id=oid, source="REST")
            for status in statuses:
                conn.push(order_event(coid, status, oid=oid))
            await settle()
            self.assertEqual(order.state, expected, coid)
            self.assertEqual(order.last_source, "WS", coid)

    async def test_13_14_duplicates_and_late_events_are_monotonic(self):
        conn = await self.start()
        order = self.submitted()
        conn.push(order_event(order.client_oid, "FILLED", filled="0.01"))
        await settle()
        history = len(order.history)
        for status in ("FILLED", "FILLED", "PARTIALLY_FILLED", "CANCELED", "NEW"):
            conn.push(order_event(order.client_oid, status))
        await settle()
        self.assertEqual(order.state, OrderState.FILLED)
        self.assertEqual(len(order.history), history)  # no duplicated fill/transition
        self.client.place_order.assert_not_awaited()

    async def test_15_16_17_18_disconnect_reconnect_requires_reconciliation(self):
        conn = await self.start()
        self.health().mark_reconciled(1)
        order = self.submitted()
        before = (order.state, len(order.history))
        conn.drop()
        await wait_for(lambda: self.health().state != "CONNECTED")
        self.assertIn(self.health().check()[1], {"disconnected", "connecting"})
        await wait_for(lambda: len(self.transport.conns) == 2 and self.health().state == "CONNECTED", timeout=5)
        snap = self.health().snapshot()
        self.assertEqual(snap["connection_epoch"], 2)
        self.assertTrue(self.transport.conns[1].url.endswith(FULL_KEY_2))
        self.assertEqual(self.health().check(), (False, "reconcile_required"))
        self.assertFalse(self.health().mark_reconciled(1))  # stale epoch cannot clear
        self.assertEqual((order.state, len(order.history)), before)  # nothing invented

    async def test_disconnect_in_every_phase_then_reconnect_with_duplicates(self):
        phases = {
            # phase: (events before drop, REST truth after reconnect, expected)
            "during_new": (["NEW"], ("NEW", "0"), OrderState.SUBMITTED),
            "between_ack_and_fill": ([], ("FILLED", "0.01"), OrderState.FILLED),
            "during_partial": (["PARTIALLY_FILLED"], ("FILLED", "0.01"), OrderState.FILLED),
            "after_filled_before_protection": (["FILLED"], ("FILLED", "0.01"), OrderState.FILLED),
        }
        conn = await self.start()
        engine = SimpleNamespace(orders=self.registry)
        for n, (phase, (before, (rest_status, rest_qty), expected)) in enumerate(phases.items()):
            coid, oid = f"bgx7-ph-{n}", str(9500 + n)
            order = self.submitted(coid, oid)
            for status in before:
                conn.push(order_event(coid, status, oid=oid))
            await settle()
            state_at_drop = order.state
            epoch = self.health().connection_epoch
            conn.drop()
            await wait_for(lambda e=epoch: self.health().connection_epoch == e + 1
                           and self.health().state == "CONNECTED", timeout=5)
            conn = self.transport.conns[-1]
            self.assertEqual(order.state, state_at_drop, phase)  # nothing invented
            self.assertEqual(self.health().check(), (False, "reconcile_required"), phase)
            # replayed/duplicated events after reconnect are monotonic
            for status in before + before:
                conn.push(order_event(coid, status, oid=oid))
            await settle()
            apply_exchange_order_truth(engine, order, self.client._normalize_order({
                "orderId": int(oid), "clientOrderId": coid, "symbol": "BTCUSDT",
                "status": rest_status, "executedQty": rest_qty}, "BTCUSDT"))
            self.assertEqual(order.state, expected, phase)
            self.assertTrue(self.health().mark_reconciled(self.health().connection_epoch))
            self.assertEqual(self.health().check(), (True, "event_capable"), phase)
        self.client.place_order.assert_not_awaited()

    async def test_19_keepalive_failure_forces_new_key_and_reconnect(self):
        self.put_fail = True
        with patch.object(binance, "LISTEN_KEY_KEEPALIVE_S", 0.01):
            await self.start()
            await wait_for(lambda: self.health().snapshot()["keepalive_failures"] >= 1)
            await wait_for(lambda: len(self.transport.conns) >= 2, timeout=5)
        self.assertIn("PUT", self.listen_calls)
        self.assertGreaterEqual(self.listen_calls.count("POST"), 2)
        self.assertTrue(self.health().snapshot()["reconcile_required"])

    async def test_20_listen_key_expired_rotates_key(self):
        conn = await self.start()
        self.health().mark_reconciled(1)
        conn.push({"e": "listenKeyExpired", "E": 1, "listenKey": FULL_KEY_1})
        await wait_for(lambda: len(self.transport.conns) == 2 and self.health().state == "CONNECTED", timeout=5)
        self.assertTrue(self.transport.conns[1].url.endswith(FULL_KEY_2))
        self.assertEqual(self.health().check(), (False, "reconcile_required"))

    async def test_listen_key_validity_is_monotonic_and_enforced(self):
        clock = [1000.0]
        health = PrivateStreamHealth(monotonic=lambda: clock[0])
        health.mark_listen_key_confirmed()
        epoch = health.mark_connected("/private")
        health.mark_reconciled(epoch)
        self.assertTrue(health.check()[0])
        clock[0] += 60 * 60.0 - 5 * 60.0 + 1  # inside the safety margin
        self.assertEqual(health.check(), (False, "listen_key_unconfirmed"))
        health.mark_listen_key_confirmed()  # keepalive PUT succeeded
        self.assertTrue(health.check()[0])

    async def test_unrouted_route_is_never_healthy(self):
        health = PrivateStreamHealth()
        health.mark_listen_key_confirmed()
        epoch = health.mark_connected("/ws")
        health.mark_reconciled(epoch)
        self.assertEqual(health.check(), (False, "unrouted"))

    async def test_handler_error_requires_reconciliation(self):
        conn = await self.start()
        self.health().mark_reconciled(1)
        self.submitted()
        with patch.object(self.registry, "get_or_create", side_effect=RuntimeError("boom")):
            conn.push(order_event("bgx7-t-0001", "FILLED"))
            await settle()
        # registry error is contained by the handler (logged, skipped) ...
        with patch.object(self.client, "_handle_private_order_event", AsyncMock(side_effect=RuntimeError("boom"))):
            conn.push(order_event("bgx7-t-0001", "FILLED"))
            await settle()
        # ... and an unhandled handler error demands REST reconciliation.
        self.assertEqual(self.health().check(), (False, "reconcile_required"))
        self.assertEqual(self.health().state, "CONNECTED")

    async def test_27_28_shutdown_cancels_tasks_without_orphans(self):
        before = set(asyncio.all_tasks())
        with patch.object(binance, "PAPER_TRADE", False):
            task = self.client.start_private_websocket(self.registry, ["BTCUSDT"])
        await wait_for(lambda: self.health().state == "CONNECTED")
        await self.client.close()
        await settle()
        self.assertTrue(task.done())
        leftover = [t for t in asyncio.all_tasks() - before if not t.done()]
        self.assertEqual(leftover, [])
        self.assertEqual(self.health().state, "DISCONNECTED")

    async def test_30_no_secret_in_logs(self):
        with self.assertLogs("kakazito-trade", level="DEBUG") as logs:
            conn = await self.start()
            conn.push("{bad")
            conn.drop()
            await wait_for(lambda: len(self.transport.conns) == 2 and self.health().state == "CONNECTED", timeout=5)
        text = "\n".join(logs.output)
        self.assertIn("[PRIVATE_STREAM] event=connected route=/private", text)
        self.assertNotIn(FULL_KEY_1, text)
        self.assertNotIn(FULL_KEY_2, text)
        self.assertIn("LKon…AAAA", text)


class ReconciliationAndSafetyTests(unittest.IsolatedAsyncioTestCase):
    def rest(self, client, coid, status, executed, symbol="BTCUSDT", oid="9001"):
        return client._normalize_order({
            "orderId": int(oid), "clientOrderId": coid, "symbol": symbol,
            "status": status, "executedQty": executed, "cumQuote": "600",
        }, symbol)

    async def test_21_24_rest_detects_fill_when_ws_is_lost(self):
        client = binance.BinanceClient()
        engine = SimpleNamespace(orders=OrderRegistry())
        order, _ = engine.orders.get_or_create("bgx7-rest", "BTCUSDT", "Buy", 0.01)
        order.transition(OrderState.SUBMITTING, source="REST")
        order.transition(OrderState.SUBMITTED, order_id="9001", source="REST")
        # No WS event ever arrived. REST is the authority.
        changed, terminal = apply_exchange_order_truth(
            engine, order, self.rest(client, "bgx7-rest", "FILLED", "0.01"), source="LIVE_REST")
        self.assertTrue(changed and terminal)
        self.assertEqual(order.state, OrderState.FILLED)
        # Late WS FILLED after REST FILLED is idempotent.
        client._order_registry = engine.orders
        await client._handle_private_order_event(order_event("bgx7-rest", "FILLED"))
        await client._handle_private_order_event(order_event("bgx7-rest", "PARTIALLY_FILLED"))
        self.assertEqual(order.state, OrderState.FILLED)

    async def test_22_23_timeout_after_submit_reconciles_by_client_oid_without_resend(self):
        client = binance.BinanceClient()
        client._client_oid_symbol["bgx7-amb"] = "BTCUSDT"
        get = AsyncMock(return_value={"orderId": 9002, "clientOrderId": "bgx7-amb", "symbol": "BTCUSDT",
                                      "status": "NEW", "executedQty": "0"})
        with patch.object(client, "_get", get):
            found = await client._recover_ambiguous_order(
                "/fapi/v1/order", {"newClientOrderId": "bgx7-amb", "symbol": "BTCUSDT"})
        self.assertEqual(found["clientOid"], "bgx7-amb")
        self.assertEqual(get.await_args.args[1]["origClientOrderId"], "bgx7-amb")
        # WS being down never turns "not seen" into "does not exist".
        client.private_stream_health.mark_disconnected("test")
        self.assertIn("14_WS", pilot.private_stream_blockers(client)[0])

    async def test_29_concurrent_rest_and_ws_converge(self):
        client = binance.BinanceClient()
        engine = SimpleNamespace(orders=OrderRegistry())
        client._order_registry = engine.orders
        for n in range(20):
            coid = f"bgx7-cc-{n:03d}"
            oid = str(8000 + n)
            order, _ = engine.orders.get_or_create(coid, "BTCUSDT", "Buy", 0.01)
            order.transition(OrderState.SUBMITTING, source="REST")

            async def rest_ack(order=order, oid=oid):
                await asyncio.sleep(0)
                order.transition(OrderState.SUBMITTED, order_id=oid, source="REST")

            async def rest_fill(order=order, coid=coid, oid=oid):
                await asyncio.sleep(0)
                apply_exchange_order_truth(engine, order, self.rest(client, coid, "FILLED", "0.01", oid=oid))

            await asyncio.gather(
                client._handle_private_order_event(order_event(coid, "FILLED", oid=oid)),
                rest_ack(), rest_fill(),
                client._handle_private_order_event(order_event(coid, "PARTIALLY_FILLED", oid=oid)),
                return_exceptions=False,
            )
            self.assertEqual(order.state, OrderState.FILLED, coid)
            self.assertEqual(order.order_id, oid)

    async def test_algo_actual_order_is_correlated_without_managed_order_mutation(self):
        client = binance.BinanceClient()
        registry = Mock()
        registry.get_or_create.side_effect = AssertionError(
            "algo actual order must never enter OrderRegistry"
        )
        client._order_registry = registry

        await client._handle_private_order_event({
            "e": "ALGO_UPDATE",
            "E": 100,
            "o": {
                "caid": "bgx7-sl-atom",
                "aid": 55,
                "ai": 991122,
                "s": "ATOMUSDT",
                "S": "SELL",
                "X": "TRIGGERED",
                "o": "STOP_MARKET",
            },
        })
        self.assertEqual(
            client._algo_actual_order_client["991122"],
            "bgx7-sl-atom",
        )

        await client._handle_private_order_event({
            "e": "ORDER_TRADE_UPDATE",
            "E": 101,
            "o": {
                "s": "ATOMUSDT",
                "c": "bgx7-sl-atom",
                "S": "SELL",
                "q": "100",
                "X": "FILLED",
                "i": 991122,
                "z": "100",
                "ap": "3.00",
            },
        })
        registry.get_or_create.assert_not_called()
        cached = client._algo_order_cache["bgx7-sl-atom"]
        self.assertEqual(cached["actualOrderStatus"], "FILLED")
        self.assertEqual(cached["actualExecutedQty"], "100")

    async def test_algo_triggering_then_actual_order_then_link_never_mutates_registry(self):
        client = binance.BinanceClient()
        registry = OrderRegistry()
        client._order_registry = registry

        await client._handle_private_order_event({
            "e": "ALGO_UPDATE",
            "E": 100,
            "o": {
                "caid": "bgx7-sl-race-a",
                "aid": 77,
                "ai": "",
                "s": "UNIUSDT",
                "S": "BUY",
                "X": "TRIGGERING",
                "o": "STOP_MARKET",
            },
        })

        await client._handle_private_order_event({
            "e": "ORDER_TRADE_UPDATE",
            "E": 101,
            "o": {
                "s": "UNIUSDT",
                "c": "bgx7-child-race-a",
                "S": "BUY",
                "q": "1",
                "X": "FILLED",
                "i": 991201,
                "z": "1",
                "ap": "7.964",
            },
        })
        self.assertIsNone(registry.get("bgx7-child-race-a"))
        self.assertIn("991201", client._algo_unlinked_actual_events)

        await client._handle_private_order_event({
            "e": "ALGO_UPDATE",
            "E": 102,
            "o": {
                "caid": "bgx7-sl-race-a",
                "aid": 77,
                "ai": 991201,
                "s": "UNIUSDT",
                "S": "BUY",
                "X": "TRIGGERED",
                "o": "STOP_MARKET",
            },
        })
        self.assertNotIn("991201", client._algo_unlinked_actual_events)
        self.assertEqual(
            client._algo_actual_order_client["991201"],
            "bgx7-sl-race-a",
        )
        cached = client._algo_order_cache["bgx7-sl-race-a"]
        self.assertEqual(cached["actualOrderStatus"], "FILLED")
        self.assertEqual(cached["actualExecutedQty"], "1")
        self.assertEqual(cached["actualAvgPrice"], "7.964")
        self.assertEqual(registry.all_orders(), [])

    async def test_actual_order_before_algo_update_is_quarantined_then_correlated(self):
        client = binance.BinanceClient()
        registry = OrderRegistry()
        client._order_registry = registry
        entry, _ = registry.get_or_create(
            "bgx7-entry-race-b", "UNIUSDT", "Sell", 1.0
        )

        await client._handle_private_order_event({
            "e": "ORDER_TRADE_UPDATE",
            "E": 200,
            "o": {
                "s": "UNIUSDT",
                "c": "bgx7-entry-race-b",
                "S": "BUY",
                "q": "1",
                "X": "NEW",
                "i": 991202,
                "z": "0",
                "ap": "0",
            },
        })
        await client._handle_private_order_event({
            "e": "ORDER_TRADE_UPDATE",
            "E": 201,
            "o": {
                "s": "UNIUSDT",
                "c": "bgx7-entry-race-b",
                "S": "BUY",
                "q": "1",
                "X": "FILLED",
                "i": 991202,
                "z": "1",
                "ap": "7.964",
            },
        })

        self.assertEqual(entry.side, "Sell")
        self.assertEqual(entry.state, OrderState.CREATED)
        self.assertIsNone(entry.order_id)
        self.assertIsNone(registry.get_by_order_id("991202"))
        self.assertEqual(
            client._algo_unlinked_actual_events["991202"]["o"]["X"],
            "FILLED",
        )

        await client._handle_private_order_event({
            "e": "ALGO_UPDATE",
            "E": 202,
            "o": {
                "caid": "bgx7-sl-race-b",
                "aid": 78,
                "ai": 991202,
                "s": "UNIUSDT",
                "S": "BUY",
                "X": "TRIGGERED",
                "o": "STOP_MARKET",
            },
        })
        self.assertNotIn("991202", client._algo_unlinked_actual_events)
        self.assertEqual(
            client._algo_actual_order_client["991202"],
            "bgx7-sl-race-b",
        )
        cached = client._algo_order_cache["bgx7-sl-race-b"]
        self.assertEqual(cached["actualOrderStatus"], "FILLED")
        self.assertEqual(cached["actualExecutedQty"], "1")
        self.assertEqual(entry.side, "Sell")
        self.assertEqual(entry.state, OrderState.CREATED)
        self.assertIsNone(entry.order_id)
        self.assertIsNone(registry.get_by_order_id("991202"))

    async def test_preflight_clears_reconcile_only_after_rest_reconciliation(self):
        from bot import pilot_live_runtime as plr

        client = binance.BinanceClient()
        health = client.private_stream_health
        health.mark_listen_key_confirmed()
        epoch = health.mark_connected("/private")
        engine = SimpleNamespace(client=client)
        log = logging.getLogger("test_private_preflight")

        client._prelive_account_exposure_verified = True
        with patch("bot.durable_live_reconciliation.reconcile_pending", AsyncMock(return_value=False)):
            self.assertEqual(await plr._private_stream_ready(engine, log), (False, "reconcile_required"))
        client._prelive_account_exposure_verified = False
        with patch("bot.durable_live_reconciliation.reconcile_pending", AsyncMock(return_value=True)):
            self.assertEqual(await plr._private_stream_ready(engine, log), (False, "reconcile_required"))
        client._prelive_account_exposure_verified = True
        with patch("bot.durable_live_reconciliation.reconcile_pending", AsyncMock(side_effect=RuntimeError("x"))):
            self.assertEqual(await plr._private_stream_ready(engine, log), (False, "reconcile_required"))
        with patch("bot.durable_live_reconciliation.reconcile_pending", AsyncMock(return_value=True)) as rp:
            self.assertEqual(await plr._private_stream_ready(engine, log), (True, "event_capable"))
            rp.assert_awaited_once_with(engine, min_interval_s=0.0)
        self.assertEqual(health.snapshot()["reconciled_epoch"], epoch)
        # A later reconnect invalidates the reconciliation again.
        health.mark_disconnected("drop")
        self.assertEqual(await plr._private_stream_ready(engine, log), (False, "disconnected"))

    async def test_legacy_clients_keep_historical_gate_14(self):
        self.assertEqual(pilot.private_stream_blockers(SimpleNamespace()), [])


class EndToEndSimulatedTests(unittest.IsolatedAsyncioTestCase):
    """candidate -> submit(fake) -> ACK -> PARTIAL -> FILLED -> position ->
    SL/TP (fake) -> protection confirmed; then the same with WS lost after ACK."""

    def protection_engine(self, positions_rows, stops_rows):
        from bot import binance_protection_failclosed as protection

        class Engine:
            async def _open(self, sig, *a, **k):
                return "open"

            async def _guard_naked_positions(self):
                return None

        protection.install(Engine, Mock())
        engine = Engine()
        engine.paper_trade = False
        engine._durable_state_enforced = False
        engine._validation_safety_lock_active = False
        engine._external_position_symbols = set()
        engine._unprotected_symbols = set()
        engine.instruments = {"BTCUSDT": {"multiplier": 1.0, "minQty": 0.001, "qtyStep": 0.001, "tickSize": 0.1}}
        engine.positions = {"BTCUSDT": SimpleNamespace(qty=0.01, direction="LONG", sl=59000, tp=62000)}
        engine.pilot = SimpleNamespace(enabled=True)
        engine.client = SimpleNamespace(
            get_positions=AsyncMock(return_value=positions_rows[0]),
            get_stop_orders=AsyncMock(side_effect=stops_rows),
            set_position_stops=AsyncMock(return_value=True),
            place_order=AsyncMock(return_value={"orderId": "777"}),
            wait_for_fill=AsyncMock(return_value={"filled": True}),
        )
        return engine

    POSITION = {"symbol": "BTCUSDT", "side": "Buy", "size": 0.01, "sizeUnit": "BASE_ASSET",
                "entryPrice": 60000, "markPrice": 60100, "stopLoss": 0}
    STOP = {"symbol": "BTCUSDT", "side": "sell", "status": "NEW", "stopPrice": 59000,
            "closeOrder": True, "reduceOnly": False, "size": 0, "sizeUnit": "BASE_ASSET"}

    async def run_flow(self, *, ws_lost_after_ack):
        client = binance.BinanceClient()
        registry = OrderRegistry()
        client._order_registry = registry
        engine = SimpleNamespace(orders=registry)
        coid = "bgx7-e2e-0001"
        order, _ = registry.get_or_create(coid, "BTCUSDT", "Buy", 0.01)  # candidate sized (simulated)
        order.transition(OrderState.SUBMITTING, source="REST")           # submit (fake)
        order.transition(OrderState.SUBMITTED, order_id="9100", source="REST")  # ACK
        registry.index_order_id("9100", coid)
        self.assertNotEqual(order.state, OrderState.FILLED)              # ACK != fill
        if ws_lost_after_ack:
            client.private_stream_health.mark_disconnected("lost_after_ack")
            self.assertIn("14_WS", pilot.private_stream_blockers(client)[0])
            self.assertEqual(order.state, OrderState.SUBMITTED)           # never assumed
            apply_exchange_order_truth(engine, order, client._normalize_order({
                "orderId": 9100, "clientOrderId": coid, "symbol": "BTCUSDT",
                "status": "FILLED", "executedQty": "0.01"}, "BTCUSDT"))
        else:
            await client._handle_private_order_event(order_event(coid, "PARTIALLY_FILLED", oid="9100", filled="0.004"))
            self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)
            await client._handle_private_order_event(order_event(coid, "FILLED", oid="9100", filled="0.01"))
        self.assertEqual(order.state, OrderState.FILLED)

        # Position reconciliation + SL/TP creation (fake) + read-back.
        prot = self.protection_engine([[self.POSITION]], [[], [self.STOP]])
        await prot._guard_naked_positions()
        prot.client.set_position_stops.assert_awaited_once_with("BTCUSDT", sl=59000.0, tp=62000.0)
        self.assertNotIn("BTCUSDT", prot._unprotected_symbols)          # protection confirmed
        # A second guard pass with protection present never duplicates SL/TP.
        prot.client.get_stop_orders.side_effect = [[self.STOP]]
        await prot._guard_naked_positions()
        prot.client.set_position_stops.assert_awaited_once()
        prot.client.place_order.assert_not_awaited()

    async def test_e2e_with_private_events(self):
        await self.run_flow(ws_lost_after_ack=False)

    async def test_e2e_ws_lost_after_ack_rest_confirms_fill(self):
        await self.run_flow(ws_lost_after_ack=True)

    async def test_25_unprotected_position_stays_fail_closed(self):
        prot = self.protection_engine([[self.POSITION], [self.POSITION], [self.POSITION]], [[], [], []])
        prot.client.set_position_stops.return_value = False
        prot.client.wait_for_fill.return_value = {"filled": False}
        await prot._guard_naked_positions()
        self.assertIn("BTCUSDT", prot._unprotected_symbols)
        reasons = pilot.PilotGuard().evaluate(
            SimpleNamespace(_unprotected_symbols=prot._unprotected_symbols, positions={}, risk=None),
            binance.BinanceClient(), "ETHUSDT", SimpleNamespace(execution_allowed=True))
        self.assertTrue(any(r.startswith("7_8_UNPROTECTED") for r in reasons), reasons)
        self.assertTrue(any(r.startswith("14_WS") for r in reasons), reasons)

    async def test_26_duplicate_algo_update_never_creates_protection_orders(self):
        client = binance.BinanceClient()
        client.place_order = AsyncMock()
        client.set_position_stops = AsyncMock()
        event = {"e": "ALGO_UPDATE", "E": 1, "o": {"caid": "bgx7-sl-1", "aid": 55, "s": "BTCUSDT",
                                                   "X": "NEW", "o": "STOP_MARKET"}}
        for _ in range(3):
            await client._handle_private_order_event(event)
        self.assertEqual(len([k for k in client._algo_order_cache if k == "bgx7-sl-1"]), 1)
        client.place_order.assert_not_awaited()
        client.set_position_stops.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
