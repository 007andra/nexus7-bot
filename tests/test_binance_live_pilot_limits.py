"""Pilot submission limits on the production venue with production values.

Port of tests/test_pilot_counter.py + test_pilot_minimum.py, which ran on the
KuCoin core engine with the code default of 2 submissions. Production runs
PILOT_MAX_NEW_ORDER_SUBMISSIONS=1 and PILOT_MAX_CONCURRENT_POSITIONS=1 (see
the [CONTROLLED_PILOT_RELEASE] startup log); this suite pins exactly those.
"""
import os

os.environ.update(PILOT_MAX_NEW_ORDER_SUBMISSIONS="1",
                  PILOT_MAX_CONCURRENT_POSITIONS="1")

from tests.binance_engine_fixture import (  # noqa: E402
    BinanceEngineFixture, Signal, approval,
)

import asyncio  # noqa: E402
import unittest  # noqa: E402

from bot import pilot  # noqa: E402
from bot.order_state import OrderState  # noqa: E402


class BinanceLivePilotLimitTests(BinanceEngineFixture):
    def count(self):
        return self.engine.pilot.state.new_order_submissions_this_session

    def entries(self):
        return [c for c in self.client.place_order.await_args_list
                if not c.kwargs.get("reduce_only")]

    async def second_symbol(self):
        sig = Signal("OTHERUSDT", "LONG", 100., 99., 103., 80., "offline", 90)
        self.decide.return_value = approval("OTHERUSDT")
        await self.engine._open(sig)

    def test_production_limits_are_pinned(self):
        self.assertEqual(pilot.MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION, 1)
        self.assertEqual(pilot.PILOT_MAX_CONCURRENT_POSITIONS, 1)

    async def test_first_submission_consumes_the_only_slot(self):
        await self.engine._open(self.sig)
        self.assertEqual(len(self.entries()), 1)
        self.assertEqual(self.count(), 1)

    async def test_second_submission_in_session_is_blocked(self):
        await self.engine._open(self.sig)
        await self.second_symbol()
        self.assertEqual(len(self.entries()), 1)
        self.assertEqual(self.count(), 1)
        self.assertTrue(any("PILOT_SESSION" in b or "PILOT_CONCURRENT" in b
                            for b in self.blockers()), self.blockers())

    async def test_lost_response_is_recovered_never_resubmitted(self):
        # Exchange executes the order but the HTTP response is lost (503).
        self.fake.state["lose_next_order_response"] = True
        await self.engine._open(self.sig)
        await self.second_symbol()
        opening_posts = [c for c in self.mutations("/fapi/v1/order")
                         if c[2].get("reduceOnly") != "true"]
        self.assertEqual(len(opening_posts), 1)
        self.assertEqual(self.count(), 1)

    async def test_unconfirmed_fill_blocks_followup(self):
        self.fake.state["order_fill"] = "NEW"   # accepted, never fills
        self.replace("bot.binance.BinanceClient.wait_for_fill.__defaults__",
                     (0.3, 0.05))
        await self.engine._open(self.sig)
        self.fake.state["order_fill"] = "FILLED"
        await self.second_symbol()
        opening_posts = [c for c in self.mutations("/fapi/v1/order")
                         if c[2].get("reduceOnly") != "true"]
        self.assertEqual(len(opening_posts), 1)
        self.assertEqual(self.count(), 1)

    async def test_filled_entry_installs_exchange_protection(self):
        await self.engine._open(self.sig)
        algos = list(self.fake.state["algos"].values())
        self.assertEqual(sorted(a["orderType"] for a in algos),
                         ["STOP_MARKET", "TAKE_PROFIT_MARKET"])
        self.assertTrue(all(a["closePosition"] and a["side"] == "SELL" for a in algos))
        self.assertEqual(len(self.fake.state["positions"]), 1)

    async def test_pending_order_on_other_symbol_blocks_entry(self):
        pending, _ = self.engine.orders.get_or_create("pending", "OTHERUSDT", "Buy", 1.)
        pending.transition(OrderState.SUBMITTING)
        await self.engine._open(self.sig)
        self.client.place_order.assert_not_awaited()
        self.assertTrue(any(b.startswith("13_AMBIGUOUS") for b in self.blockers()),
                        self.blockers())

    async def test_concurrent_opens_submit_at_most_one(self):
        other = Signal("OTHERUSDT", "LONG", 100., 99., 103., 80., "offline", 90)

        def decide(*args, **kwargs):
            return approval(kwargs.get("symbol") or args[0])
        self.decide.side_effect = decide
        await asyncio.wait_for(asyncio.gather(
            self.engine._open(self.sig), self.engine._open(other)), 30)
        # Both candidates may reach the client; the durable budget at the HTTP
        # boundary must let exactly one opening order reach the exchange.
        opening_posts = [c for c in self.mutations("/fapi/v1/order")
                         if c[2].get("reduceOnly") != "true"]
        self.assertEqual(len(opening_posts), 1)
        self.assertEqual(len(self.fake.state["positions"]), 1)
        self.assertEqual(self.count(), 1)

    async def test_minimum_quantity_respects_exchange_step(self):
        await self.engine._open(self.sig)
        qty = float(self.entries()[0].kwargs["qty"])
        step = self.engine.instruments["TESTUSDT"]["qtyStep"]
        self.assertGreater(qty, 0)
        self.assertAlmostEqual(qty / step, round(qty / step), places=6)

    async def test_zero_available_collateral_blocks(self):
        self.fake.state["available"] = 0.0
        self.engine._pilot_available_balance = 0.0
        await self.engine._open(self.sig)
        self.client.place_order.assert_not_awaited()

    async def test_exchange_minimum_above_risk_budget_blocks(self):
        # 1 whole unit at ~100 risks ~1.3 USDT > 1% of 100 USDT equity.
        self.engine.instruments["TESTUSDT"].update(qtyStep=1.0, minQty=1.0,
                                                   lotSize=1.0, minBaseQty=1.0)
        await self.engine._open(self.sig)
        self.client.place_order.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
