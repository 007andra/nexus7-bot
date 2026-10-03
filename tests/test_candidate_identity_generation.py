"""#466 P0 — a candidate-scoped idempotency key must never hand a NEW financial
intent the clientOid/ManagedOrder of a finished trade.

The candidate id hashes symbol/direction/geometry/15-minute formation bucket,
so the same setup re-signalled after trade A closed (same closed candle, or a
restart inside the bucket) yields the same id. Without a generation step, trade
B reuses A's clientOid -> registry returns A's terminal ManagedOrder and Binance
may answer -4116 duplicate, which the recovery path resolves to A's old order.
"""
import copy
from unittest.mock import AsyncMock

# The harness sets the synthetic LIVE environment before the bot is imported.
from tests.test_binance_cross_stress_dispatch_proof import DispatchProof as _Harness

from bot.binance import BinanceAPIError  # noqa: E402
from bot.order_state import OrderRegistry, OrderState  # noqa: E402


class CandidateIdentityGenerationTests(_Harness):
    def _signal(self):
        sig = copy.copy(self.signal)
        sig._bgx_formation_bucket = 1_900_000            # same 15-minute bucket
        for attr in ("_bgx_setup_id", "_bgx_candidate_payload"):
            sig.__dict__.pop(attr, None)
        return sig

    def oids(self):
        return [p.get("newClientOrderId") for _, e, p in self.requests if e == "/fapi/v1/order"]

    def _reset_after_close(self):
        self.engine.positions.pop("ETHUSDT", None)
        self.engine._durable_state_errors, self.engine._durable_state_ok = set(), True
        self.evaluations.clear()

    async def test_same_candidate_after_terminal_trade_gets_new_identity(self):
        await self.engine._open(self._signal())
        first = self.oids()[0]
        self.engine.orders.get(first).state = OrderState.FILLED   # trade A finished
        self._reset_after_close()
        await self.engine._open(self._signal())                    # same setup, trade B
        oids = self.oids()
        self.assertEqual(len(oids), 2, self.events)
        self.assertNotEqual(oids[0], oids[1])
        self.assertIsNot(self.engine.orders.get(oids[0]), self.engine.orders.get(oids[1]))
        self.assertEqual(self.engine.orders.get(oids[1]).candidate_id,
                         self.engine.orders.get(oids[0]).candidate_id, "lineage kept")

    async def test_unresolved_previous_intent_keeps_duplicate_guard(self):
        await self.engine._open(self._signal())
        first = self.oids()[0]
        self.engine.orders.get(first).state = OrderState.SUBMITTED  # still unresolved
        self._reset_after_close()
        await self.engine._open(self._signal())
        self.assertTrue(all(o == first for o in self.oids()), "same unresolved intent, same key")

    async def test_same_intent_retries_reuse_one_client_oid(self):
        sig = self._signal()
        await self.engine._open(sig)
        await self.engine._open(sig)            # first intent still unresolved (ACK, no fill)
        oids = self.oids()
        self.assertGreaterEqual(len(oids), 1)
        self.assertEqual(len(set(oids)), 1, "same unresolved intent -> same clientOid")

    def _restart(self):
        snapshot = self.engine.orders.snapshot()
        fresh = OrderRegistry()
        fresh.restore(snapshot)                 # durable registry after process restart
        self.engine.orders = fresh
        self._reset_after_close()

    async def test_restart_unfinished_intent_keeps_identity(self):
        await self.engine._open(self._signal())
        first = self.oids()[0]
        self.engine.orders.get(first).state = OrderState.SUBMITTED
        self._restart()
        await self.engine._open(self._signal())
        self.assertTrue(all(o == first for o in self.oids()), "restart: same unfinished intent, same id")

    async def test_restart_after_completed_trade_gets_new_identity(self):
        await self.engine._open(self._signal())
        first = self.oids()[0]
        self.engine.orders.get(first).state = OrderState.FILLED
        self._restart()
        await self.engine._open(self._signal())
        self.assertNotEqual(self.oids()[-1], first)

    async def test_completed_trade_is_never_recovered_by_a_new_one(self):
        await self.engine._open(self._signal())
        first = self.oids()[0]
        old = self.engine.orders.get(first)
        old.state = OrderState.FILLED
        self._reset_after_close()
        lookups = []

        async def by_oid(oid):
            lookups.append(oid)
            return ({"orderId": "A-old", "clientOid": first, "status": "FILLED", "isActive": False}
                    if oid == first else {})
        self.client.get_order_by_client_oid = AsyncMock(side_effect=by_oid)
        base_request = self.request

        async def duplicate(method, endpoint, params=None, **kwargs):
            if method == "POST" and endpoint == "/fapi/v1/order":
                self.requests.append((method, endpoint, params))
                raise BinanceAPIError(method, endpoint, 400, -4116,
                                      "ClientOrderId is duplicated.", params)
            return await base_request(method, endpoint, params, **kwargs)
        self.client._request = AsyncMock(side_effect=duplicate)
        await self.engine._open(self._signal())
        new = self.oids()[-1]
        self.assertNotEqual(new, first)
        self.assertIn(new, lookups, "the -4116 is reconciled by trade B's own id")
        # Any lookup of A's id belongs to A's own record (ambiguous-entry
        # recovery reconciles each order by its own clientOid); A's exchange
        # identity is never attached to B.
        new_order = self.engine.orders.get(new)
        self.assertIsNot(new_order, old)
        self.assertNotEqual(new_order.order_id, "A-old")
        self.assertNotEqual(new_order.state, OrderState.FILLED)
        self.assertEqual(old.client_oid, first)
        self.assertNotIn("ETHUSDT", self.engine.positions, "no position adopted for trade B")


for _name in [n for n in dir(_Harness) if n.startswith("test_")]:
    if _name not in CandidateIdentityGenerationTests.__dict__:
        setattr(CandidateIdentityGenerationTests, _name, None)
del _Harness
