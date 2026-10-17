"""P1-OPEN-1 — stale Binance protective algo orders after a BGX trade closes.

Offline exchange model at the transport boundary (``BinanceClient._request``):
positionRisk, openAlgoOrders, POST order / algoOrder and DELETE algoOrder are
emulated; everything above (place_order, set_position_stops, get_stop_orders
normalization, cancel_algo_order, the stale-protection reconciler, the
pre-entry gate, cleanup_flat_symbol and the readiness flat sweep) is real.

FLAT MUST BE PROVEN. STALE BGX PROTECTION MUST BE ENUMERATED. OWNERSHIP MUST BE
PROVEN. OWNED STALE PROTECTION MUST BE REMOVED. REMOVAL MUST BE READ BACK.
"""
import asyncio
import os
import random
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("EXCHANGE", "binance")

from bot import binance  # noqa: E402
from bot import binance_protection_registry as registry  # noqa: E402
from bot import binance_stale_protection as stale  # noqa: E402

INFO = {"ETHUSDT": {"quantityUnit": "BASE_ASSET", "qtyStep": "0.001", "minQty": "0.001",
                    "minNotional": "0", "tickSize": "0.01", "multiplier": 1.0}}


class _Exchange:
    """Minimal Binance USD-M model: one-way positions + algo service."""

    def __init__(self):
        self.position = 0.0
        self.algos = {}            # algoId -> row (raw Binance shape)
        self.calls = []            # (method, endpoint, params)
        self.next_id = 1000
        self.cancel_mode = "ok"    # ok | lost_ack | http_error
        self.read_fail = False
        self.positions_fail = False

    def _id(self):
        self.next_id += 1
        return self.next_id

    def open_algos(self):
        return [dict(r) for r in self.algos.values() if r["algoStatus"] == "NEW"]

    def add_external(self, client_algo_id, kind="STOP_MARKET", trigger="95"):
        algo_id = self._id()
        self.algos[algo_id] = {"algoId": algo_id, "clientAlgoId": client_algo_id, "symbol": "ETHUSDT",
                               "side": "SELL", "orderType": kind, "triggerPrice": trigger,
                               "closePosition": True, "algoStatus": "NEW"}
        return algo_id

    def trigger(self, kind):
        """The given protective order fires: it is consumed, position goes flat."""
        for row in self.algos.values():
            if row["algoStatus"] == "NEW" and row["orderType"] == kind:
                row["algoStatus"] = "FINISHED"
                self.position = 0.0
                return row
        raise AssertionError("nothing to trigger")

    async def request(self, method, endpoint, params=None, **kwargs):
        params = dict(params or {})
        self.calls.append((method, endpoint, params))
        if method == "GET" and endpoint == "/fapi/v3/positionRisk":
            if self.positions_fail:
                raise RuntimeError("positionRisk timeout")
            return [{"symbol": "ETHUSDT", "positionAmt": str(self.position), "entryPrice": "100",
                     "markPrice": "100", "positionSide": "BOTH"}]
        if method == "GET" and endpoint == "/fapi/v1/openAlgoOrders":
            if self.read_fail:
                raise RuntimeError("openAlgoOrders timeout")
            return self.open_algos()
        if method == "POST" and endpoint == "/fapi/v1/order":
            qty = float(params["quantity"])
            self.position += qty if params["side"] == "BUY" else -qty
            return {"orderId": self._id(), "clientOrderId": params["newClientOrderId"], "status": "FILLED"}
        if method == "POST" and endpoint == "/fapi/v1/algoOrder":
            algo_id = self._id()
            self.algos[algo_id] = {"algoId": algo_id, "clientAlgoId": params["clientAlgoId"],
                                   "symbol": "ETHUSDT", "side": params["side"], "orderType": params["type"],
                                   "triggerPrice": params["triggerPrice"], "closePosition": True,
                                   "algoStatus": "NEW"}
            return {"algoId": algo_id, "clientAlgoId": params["clientAlgoId"]}
        if method == "DELETE" and endpoint == "/fapi/v1/algoOrder":
            row = next((r for r in self.algos.values()
                        if str(r["algoId"]) == str(params.get("algoId"))
                        or r["clientAlgoId"] == params.get("clientAlgoId")), None)
            if self.cancel_mode == "http_error":
                raise binance.BinanceAPIError("DELETE", endpoint, 400, -1000, "unknown error", params)
            if row is None or row["algoStatus"] != "NEW":
                raise binance.BinanceAPIError("DELETE", endpoint, 400, -2011, "Unknown order sent.", params)
            row["algoStatus"] = "CANCELED"
            if self.cancel_mode == "lost_ack":
                raise asyncio.TimeoutError("response lost after acceptance")
            return {"algoId": row["algoId"], "clientAlgoId": row["clientAlgoId"], "code": "200",
                    "msg": "success"}
        raise AssertionError(f"unexpected {method} {endpoint}")

    def deletes(self):
        return [c for c in self.calls if c[0] == "DELETE"]


class _DB:
    def __init__(self):
        self.kv = {}

    async def load(self, key, *, strict=False):
        return self.kv.get(key)

    async def save(self, key, value, *, strict=False):
        self.kv[key] = value
        return True


class StaleProtectionBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.exchange = _Exchange()
        self.db = _DB()
        self.patches = [
            patch.object(binance, "PAPER_TRADE", False),
            patch.object(binance, "_live_migration_ready", lambda: True),
            patch.object(binance, "_assert_signing_credentials_available", lambda: None),
            patch("bot.database.load_key_value", self.db.load),
            patch("bot.database.save_key_value", self.db.save),
            patch("bot.critical_state.critical_state.assert_available_for_new_risk", Mock(return_value=None)),
            patch("bot.execution_ownership.validate_execution_ownership", AsyncMock(return_value=True)),
            patch("bot.execution_ownership.publish_valid_execution_ownership", Mock(return_value=None)),
            patch("bot.runtime_readiness.assert_ready_for_new_entries", Mock(return_value=None)),
            patch("asyncio.sleep", AsyncMock()),
        ]
        for p in self.patches:
            p.start()
        self.client = self.new_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def new_client(self):
        client = binance.BinanceClient()
        client._instruments = INFO
        client._request = AsyncMock(side_effect=self.exchange.request)
        client._assert_live_account_mode = AsyncMock(return_value=None)
        client._engine = SimpleNamespace(positions={})
        client._execution_ownership = object()
        return client

    async def open_trade(self, client=None, *, sl=95.0, tp=110.0):
        client = client or self.client
        result = await client.place_order("ETHUSDT", "Buy", 0.5, sl=sl, tp=tp)
        self.assertFalse(result["sl_tp_failed"])
        return result

    async def close_by_market(self, client=None):
        """2R / emergency flatten: reduce-only MARKET close, protection untouched."""
        client = client or self.client
        await client.place_order("ETHUSDT", "Sell", 0.5, reduce_only=True)
        self.assertEqual(self.exchange.position, 0.0)

    def active(self, kind=None):
        return [r for r in self.exchange.open_algos() if kind is None or r["orderType"] == kind]

    async def reconcile(self, client=None, *, flat_proven=True):
        return await stale.reconcile_symbol(client or self.client, "ETHUSDT", flat_proven=flat_proven)


class FlatCleanupTests(StaleProtectionBase):
    async def test_A_flat_stale_sl_cancelled_and_read_back(self):
        await self.open_trade()
        self.exchange.trigger("TAKE_PROFIT_MARKET")
        self.assertEqual(len(self.active("STOP_MARKET")), 1)
        self.assertEqual(await self.reconcile(), stale.VERIFIED)
        self.assertEqual(self.active(), [])
        self.assertEqual(len(self.exchange.deletes()), 1)
        self.assertEqual(self.exchange.deletes()[0][1], "/fapi/v1/algoOrder")
        reads = [i for i, c in enumerate(self.exchange.calls) if c[1] == "/fapi/v1/openAlgoOrders"]
        delete_at = self.exchange.calls.index(self.exchange.deletes()[0])
        self.assertTrue(any(i > delete_at for i in reads), "readback after the cancel")

    async def test_B_flat_stale_tp_cancelled(self):
        await self.open_trade()
        self.exchange.trigger("STOP_MARKET")
        self.assertEqual(await self.reconcile(), stale.VERIFIED)
        self.assertEqual(self.active(), [])

    async def test_C_sl_executed_tp_sibling_cancelled_before_next_entry(self):
        await self.open_trade()
        tp_a = self.active("TAKE_PROFIT_MARKET")[0]["clientAlgoId"]
        self.exchange.trigger("STOP_MARKET")
        await self.open_trade(sl=96.0, tp=111.0)          # trade B
        self.assertNotIn(tp_a, [r["clientAlgoId"] for r in self.active()])
        self.assertEqual(sorted(r["orderType"] for r in self.active()), ["STOP_MARKET", "TAKE_PROFIT_MARKET"])

    async def test_D_tp_executed_sl_sibling_cancelled_before_next_entry(self):
        await self.open_trade()
        sl_a = self.active("STOP_MARKET")[0]["clientAlgoId"]
        self.exchange.trigger("TAKE_PROFIT_MARKET")
        await self.open_trade(sl=96.0, tp=111.0)
        self.assertNotIn(sl_a, [r["clientAlgoId"] for r in self.active()])
        self.assertEqual(len(self.active()), 2)

    async def test_E_manual_external_order_is_preserved_and_not_verified(self):
        await self.open_trade()
        self.exchange.trigger("STOP_MARKET")
        manual = self.exchange.add_external("web_8f3a1c", trigger="90")
        self.assertEqual(await self.reconcile(), stale.UNRESOLVED_EXTERNAL_PROTECTION)
        self.assertIn(manual, [r["algoId"] for r in self.active()], "external never cancelled")
        self.assertEqual(len(self.exchange.deletes()), 1, "only the owned stale TP")
        self.assertNotIn(str(manual), [c[2].get("algoId") for c in self.exchange.deletes()])

    async def test_F_unmapped_bgx7_is_not_cancelled_and_not_verified(self):
        self.exchange.add_external("bgx7-0123456789abcdef", trigger="95")
        self.assertEqual(await self.reconcile(), stale.UNRESOLVED_UNMAPPED_BGX_PROTECTION)
        self.assertEqual(self.exchange.deletes(), [])
        self.assertEqual(len(self.active()), 1)
        with self.assertRaisesRegex(RuntimeError, "UNRESOLVED_UNMAPPED_BGX_PROTECTION"):
            await self.client.place_order("ETHUSDT", "Buy", 0.5, sl=95, tp=110)
        self.assertFalse([c for c in self.exchange.calls if c[1] == "/fapi/v1/order"], "entry blocked")


class LineageFilterTests(StaleProtectionBase):
    async def _a_stale_b_open(self, *, same_trigger):
        await self.open_trade(sl=95.0, tp=110.0)
        a_ids = {r["clientAlgoId"] for r in self.active()}
        self.exchange.trigger("STOP_MARKET")                 # A closed; TP A left behind
        # B opens without the pre-entry gate (simulates the race this filter guards).
        with patch.object(stale, "assert_clean_before_entry", AsyncMock()):
            await self.open_trade(sl=95.0 if same_trigger else 96.0, tp=110.0 if same_trigger else 111.0)
        b_ids = {r["clientAlgoId"] for r in self.active()} - a_ids
        self.assertEqual(len(b_ids), 2)
        return a_ids, b_ids

    async def test_G_a_stale_b_active_same_symbol_only_a_cancelled(self):
        a_ids, b_ids = await self._a_stale_b_open(same_trigger=False)
        self.assertEqual(await self.reconcile(flat_proven=False), stale.VERIFIED)
        self.assertEqual({r["clientAlgoId"] for r in self.active()}, b_ids)
        self.assertEqual(len(self.exchange.deletes()), 1)

    async def test_H_same_trigger_for_a_and_b_only_a_cancelled(self):
        a_ids, b_ids = await self._a_stale_b_open(same_trigger=True)
        self.assertEqual(await self.reconcile(flat_proven=False), stale.VERIFIED)
        self.assertEqual({r["clientAlgoId"] for r in self.active()}, b_ids)

    async def test_open_position_with_unknown_current_lineage_cancels_nothing(self):
        await self.open_trade()
        self.client._protection_lineage.clear()
        self.client._protection_opening_order.clear()
        self.assertEqual(await self.reconcile(flat_proven=False), stale.UNRESOLVED_CURRENT_LINEAGE_UNKNOWN)
        self.assertEqual(self.exchange.deletes(), [])


class CancelSemanticsTests(StaleProtectionBase):
    async def test_I_idempotent_cancel(self):
        await self.open_trade()
        self.exchange.trigger("STOP_MARKET")
        tp = self.active()[0]
        first = await self.client.cancel_algo_order("ETHUSDT", algo_id=str(tp["algoId"]))
        second = await self.client.cancel_algo_order("ETHUSDT", algo_id=str(tp["algoId"]))
        self.assertEqual((first, second), ("ACK_CANCELED", "REJECTED_UNKNOWN_ORDER"))
        self.assertEqual(await self.reconcile(), stale.VERIFIED, "already gone -> nothing to do")
        self.assertEqual(len(self.exchange.deletes()), 2)

    async def test_J_lost_ack_converges_by_readback(self):
        await self.open_trade()
        self.exchange.trigger("STOP_MARKET")
        self.exchange.cancel_mode = "lost_ack"
        self.assertEqual(await self.reconcile(), stale.VERIFIED)
        self.assertEqual(len(self.exchange.deletes()), 1, "never retried blindly")
        self.assertEqual(self.active(), [])

    async def test_K_http_error_is_not_verified_and_blocks_entry_until_retry(self):
        await self.open_trade()
        self.exchange.trigger("STOP_MARKET")
        self.exchange.cancel_mode = "http_error"
        self.assertEqual(await self.reconcile(), stale.CANCEL_UNCONFIRMED)
        orders_before = len([c for c in self.exchange.calls if c[1] == "/fapi/v1/order"])
        with self.assertRaisesRegex(RuntimeError, "CANCEL_UNCONFIRMED"):
            await self.client.place_order("ETHUSDT", "Buy", 0.5, sl=96, tp=111)
        self.assertEqual(len([c for c in self.exchange.calls if c[1] == "/fapi/v1/order"]), orders_before)
        self.exchange.cancel_mode = "ok"                    # later retry
        await self.open_trade(sl=96.0, tp=111.0)
        self.assertEqual(len(self.active()), 2)

    async def test_L_unknown_snapshot_means_zero_cancels(self):
        await self.open_trade()
        self.exchange.trigger("STOP_MARKET")
        self.exchange.read_fail = True
        self.assertEqual(await self.reconcile(), stale.UNRESOLVED_INVENTORY_UNKNOWN)
        self.exchange.read_fail = False
        self.exchange.positions_fail = True
        with self.assertRaises(RuntimeError):
            await self.client.place_order("ETHUSDT", "Buy", 0.5, sl=96, tp=111)
        self.assertEqual(self.exchange.deletes(), [], "no flat proof -> zero cancels")
        self.assertEqual(len(self.active()), 1)

    async def test_cancel_request_is_signed_single_attempt_on_canonical_endpoint(self):
        from tests.test_release_invariants_binance import _Resp, _Session
        client = binance.BinanceClient()
        client._session = _Session([_Resp(503, {})])
        client._ensure_session = AsyncMock()
        client._throttle = AsyncMock()
        with patch.object(binance, "API_KEY", "k"), patch.object(binance, "SIGNING_METHOD", "hmac"), \
                patch.object(binance, "API_SECRET", "s"), \
                patch.object(binance, "assert_exchange_mutation_allowed", lambda *a: None):
            result = await client.cancel_algo_order("ETHUSDT", algo_id="77")
        self.assertEqual(result, "AMBIGUOUS_HTTP_503")
        self.assertEqual(len(client._session.calls), 1, "no unsafe retry")
        method, url, params = client._session.calls[0]
        self.assertEqual((method, url.endswith("/fapi/v1/algoOrder")), ("DELETE", True))
        self.assertEqual(params["algoId"], "77")
        self.assertIn("timestamp", params)
        self.assertIn("recvWindow", params)
        self.assertIn("signature", params)


class LifecycleIntegrationTests(StaleProtectionBase):
    def engine(self, client):
        return SimpleNamespace(client=client, positions={}, orders=None)

    async def test_M_emergency_flatten_then_readiness_sweep_cleans(self):
        from bot.protection_readiness import _reconcile_global_flat_bgx_protection
        await self.open_trade()
        await self.close_by_market()
        self.assertEqual(len(self.active()), 2, "flatten leaves SL and TP behind")
        engine = self.engine(self.client)
        self.assertTrue(await _reconcile_global_flat_bgx_protection(engine, ["ETHUSDT"]))
        self.assertEqual(self.active(), [])

    async def test_N_two_r_exit_then_next_entry_inherits_nothing(self):
        await self.open_trade()
        a_ids = {r["clientAlgoId"] for r in self.active()}
        await self.close_by_market()
        await self.open_trade(sl=96.0, tp=111.0)
        self.assertFalse(a_ids & {r["clientAlgoId"] for r in self.active()})
        self.assertEqual(len(self.active()), 2)

    async def test_O_restart_during_cleanup_converges_from_durable_map(self):
        await self.open_trade()
        self.exchange.trigger("STOP_MARKET")
        self.exchange.cancel_mode = "http_error"
        self.assertEqual(await self.reconcile(), stale.CANCEL_UNCONFIRMED)
        restarted = self.new_client()                      # process restart: memory gone
        self.assertEqual(restarted._algo_registry, {})
        self.exchange.cancel_mode = "ok"
        engine = self.engine(restarted)
        from bot.conditional_stop_lifecycle import cleanup_flat_symbol
        self.assertTrue(await cleanup_flat_symbol(engine, "ETHUSDT", exchange_position_qty=0.0,
                                                  active_entry_confirmed_absent=True))
        self.assertEqual(self.active(), [])

    async def test_P_repeated_cleanup_has_no_side_effects(self):
        await self.open_trade()
        self.exchange.trigger("STOP_MARKET")
        for _ in range(3):
            self.assertEqual(await self.reconcile(), stale.VERIFIED)
        self.assertEqual(len(self.exchange.deletes()), 1)
        self.assertEqual([c for c in self.exchange.calls if c[0] == "POST" and c[1] != "/fapi/v1/order"
                          and c[1] != "/fapi/v1/algoOrder"], [])

    async def test_durable_map_is_bounded_after_verified_flat(self):
        import json
        await self.open_trade()
        self.exchange.trigger("STOP_MARKET")
        self.assertEqual(len(self.client._algo_registry), 2)
        self.assertEqual(await self.reconcile(), stale.VERIFIED)
        self.assertEqual(self.client._algo_registry, {})
        self.assertEqual(json.loads(self.db.kv[registry._key()]), {})

    async def test_cleanup_flat_symbol_never_runs_with_position_open(self):
        from bot.conditional_stop_lifecycle import cleanup_flat_symbol
        await self.open_trade()
        engine = self.engine(self.client)
        self.assertFalse(await cleanup_flat_symbol(engine, "ETHUSDT", exchange_position_qty=0.5,
                                                   active_entry_confirmed_absent=True))
        self.assertEqual(self.exchange.deletes(), [])


class ComposedLivePilotTests(StaleProtectionBase):
    async def test_Q_composed_attack_sl_closes_a_tp_a_cancelled_before_b_dispatch(self):
        a = await self.open_trade(sl=95.0, tp=110.0)              # trade A LONG, SL/TP algo
        tp_a = self.active("TAKE_PROFIT_MARKET")[0]
        self.exchange.trigger("STOP_MARKET")                       # SL A closes the position
        self.assertEqual([r["algoId"] for r in self.active()], [tp_a["algoId"]], "TP A still armed")
        marker = len(self.exchange.calls)
        b = await self.open_trade(sl=95.0, tp=110.0)              # B: same levels on purpose
        tail = self.exchange.calls[marker:]
        seq = [(m, e) for m, e, _ in tail]
        cancel_at = seq.index(("DELETE", "/fapi/v1/algoOrder"))
        entry_at = seq.index(("POST", "/fapi/v1/order"))
        readback = [i for i, s in enumerate(seq) if s == ("GET", "/fapi/v1/openAlgoOrders")]
        self.assertLess(seq.index(("GET", "/fapi/v3/positionRisk")), cancel_at, "flat proven first")
        self.assertTrue(any(cancel_at < i < entry_at for i in readback), "readback before dispatch")
        self.assertEqual(tail[cancel_at][2], {"algoId": str(tp_a["algoId"])})
        self.assertEqual(self.exchange.algos[tp_a["algoId"]]["algoStatus"], "CANCELED")
        self.assertNotIn(tp_a["algoId"], [r["algoId"] for r in self.active()])
        for row in self.active():                                 # B inherits nothing
            record = registry.lookup(self.client, row["clientAlgoId"])
            self.assertEqual(record["opening_order_id"], str(b["orderId"]))
            self.assertNotEqual(record["opening_order_id"], str(a["orderId"]))
        self.assertEqual(len(self.active()), 2)
        self.assertEqual(len(self.exchange.deletes()), 1)


class StrongLineagePropertyTests(StaleProtectionBase):
    async def test_property_only_closed_lineage_orders_are_cancelled(self):
        rng = random.Random(4711)
        for case in range(150):
            self.exchange = _Exchange()
            client = self.new_client()
            closed_x, current = f"9{case}01", f"9{case}02"
            client._protection_opening_order["ETHUSDT"] = current
            client._protection_lineage["ETHUSDT"] = f"bgx7-entry-{current}"
            expected = set()
            for i in range(rng.randint(1, 8)):
                kind = rng.choice(("closed", "current", "unmapped", "external", "other_symbol"))
                trigger = rng.choice(("95", "110", "96.5"))      # shared triggers on purpose
                cid = (f"ext-{case}-{i}" if kind == "external" else f"bgx7-{case:03d}{i:02d}")
                algo_id = self.exchange.add_external(cid, kind=rng.choice(("STOP_MARKET", "TAKE_PROFIT_MARKET")),
                                                     trigger=trigger)
                if kind in ("closed", "current", "other_symbol"):
                    client._algo_registry[cid] = {
                        "symbol": "BTCUSDT" if kind == "other_symbol" else "ETHUSDT", "kind": "SL",
                        "opening_order_id": closed_x if kind != "current" else current,
                        "opening_client_oid": "bgx7-entry-" + (closed_x if kind != "current" else current)}
                if kind == "closed":
                    expected.add(str(algo_id))
            before = {cid: dict(rec) for cid, rec in client._algo_registry.items()}
            await stale.reconcile_symbol(client, "ETHUSDT", flat_proven=False)
            cancelled = {c[2]["algoId"] for c in self.exchange.deletes()}
            self.assertEqual(cancelled, expected, case)
            for algo_id in cancelled:                              # stored_opening_order_id == X
                cid = next(r["clientAlgoId"] for r in self.exchange.algos.values()
                           if str(r["algoId"]) == algo_id)
                self.assertEqual((before[cid]["symbol"], before[cid]["opening_order_id"]),
                                 ("ETHUSDT", closed_x), case)

if __name__ == "__main__":
    unittest.main()
