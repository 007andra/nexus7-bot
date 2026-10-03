"""#466 P0 — a candidate-scoped idempotency key must never hand a NEW financial
intent the clientOid/ManagedOrder of a finished trade.

The candidate id hashes symbol/direction/geometry/15-minute formation bucket,
so the same setup re-signalled after trade A closed (same closed candle, or a
restart inside the bucket) yields the same id. Without a generation step, trade
B reuses A's clientOid -> registry returns A's terminal ManagedOrder and Binance
may answer -4116 duplicate, which the recovery path resolves to A's old order.
"""
import copy

from bot.order_state import OrderState
from tests.test_binance_cross_stress_dispatch_proof import DispatchProof as _Harness


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


for _name in [n for n in dir(_Harness) if n.startswith("test_")]:
    if _name not in CandidateIdentityGenerationTests.__dict__:
        setattr(CandidateIdentityGenerationTests, _name, None)
del _Harness
