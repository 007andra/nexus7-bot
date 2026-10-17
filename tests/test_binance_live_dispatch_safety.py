"""Dispatch safety invariants on the production venue (Binance LIVE path).

Ports the Binance-relevant parts of test_durable_dispatch_gate.py and
test_quantity_boundary.py (KuCoin core engine only) and adds protection
failure handling. Every case asserts on what actually reached the fake
exchange, not only on client calls.
"""
from tests.binance_engine_fixture import (  # noqa: I001
    BinanceEngineFixture, approval,
)

import json
import unittest
from unittest.mock import AsyncMock, patch

from bot import database as db
from bot import durable_execution as durable


class BinanceLiveDispatchSafetyTests(BinanceEngineFixture):
    def opening_posts(self):
        return [c for c in self.mutations("/fapi/v1/order")
                if c[2].get("reduceOnly") != "true"]

    async def test_durable_intent_failure_blocks_before_exchange(self):
        with patch("bot.database.save_key_value",
                   AsyncMock(side_effect=db.PersistenceError("offline failure"))):
            await self.attempt(approval())
        self.assertEqual(self.opening_posts(), [])
        self.assertEqual(self.fake.state["positions"], [])
        self.assertFalse(durable.can_open(self.engine))

    async def test_dispatch_attempt_is_durable_before_the_exchange_post(self):
        # Without this mark a pre-POST gate on a later pass could record the
        # order as proven-not-dispatched while it exists on the exchange.
        saved, at_post = [], []
        real_save = db.save_key_value

        async def recording_save(key, value, *args, **kwargs):
            result = await real_save(key, value, *args, **kwargs)
            if key == durable._ORDER_KEY:
                saved.append(value)
            return result
        self.replace("bot.database.save_key_value", recording_save)
        fake_cls = type(self.fake)
        real_post = fake_cls._post_fapi_v1_order

        def post(fake, q):
            if q.get("reduceOnly") != "true":
                at_post.append((q.get("newClientOrderId"), list(saved)))
            return real_post(fake, q)
        self.replace("tests.binance_fake_exchange.FakeBinance._post_fapi_v1_order", post)

        await self.attempt(approval())
        self.assertEqual(len(at_post), 1)
        oid, durable_before_post = at_post[0]
        self.assertTrue(oid.startswith("bgx7-"))

        def attempted(payload):
            for order in json.loads(payload)["orders"]:
                if order.get("client_oid") == oid:
                    return any(isinstance(e, (list, tuple)) and len(e) > 3
                               and isinstance(e[3], dict)
                               and e[3].get("dispatch_attempted") is True
                               for e in order.get("history", []))
            return False
        self.assertTrue(any(attempted(p) for p in durable_before_post),
                        "dispatch_attempted not persisted before the POST")

    async def test_exact_exchange_minimum_reaches_exchange(self):
        # Risk sizing floors to the step; make the minimum equal one step.
        self.engine.instruments["TESTUSDT"].update(minQty=0.5, lotSize=0.5,
                                                   minBaseQty=0.5, qtyStep=0.5)
        await self.attempt(approval())
        posts = self.opening_posts()
        self.assertEqual(len(posts), 1)
        self.assertEqual(float(posts[0][2]["quantity"]), 0.5)

    async def test_rounding_never_increases_sized_exposure(self):
        await self.attempt(approval())
        posted = float(self.opening_posts()[0][2]["quantity"])
        sized = self.engine.risk._last_sizing["TESTUSDT"]["sized_qty"]
        self.assertLessEqual(posted, sized + 1e-12)

    async def test_missing_credentials_block_before_exchange(self):
        self.replace("bot.binance.API_KEY", "")
        await self.attempt(approval())
        self.assertEqual(self.opening_posts(), [])

    async def test_protection_rejection_flattens_and_blocks_new_entries(self):
        # Venue accepts the entry but rejects every conditional order.
        self.replace("tests.binance_fake_exchange.FakeBinance._post_fapi_v1_algoOrder",
                     lambda _self, q: ({"code": -4130, "msg": "rejected"}, 400))
        await self.attempt(approval())
        reduce_posts = [c for c in self.mutations("/fapi/v1/order")
                        if c[2].get("reduceOnly") == "true"]
        self.assertEqual(len(self.opening_posts()), 1)
        self.assertEqual(len(reduce_posts), 1, "unprotected position must be closed")
        self.assertEqual(self.fake.state["positions"], [])

    async def test_wrong_margin_mode_blocks_before_exchange(self):
        self.fake.state["margin_type"] = "ISOLATED"
        await self.attempt(approval())
        self.assertEqual(self.opening_posts(), [])

    async def test_hedge_mode_account_blocks_before_exchange(self):
        self.fake.state["dual_side"] = True
        await self.attempt(approval())
        self.assertEqual(self.opening_posts(), [])


if __name__ == "__main__":
    unittest.main()
