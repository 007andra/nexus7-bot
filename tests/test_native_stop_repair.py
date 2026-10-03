import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from bot.native_stop_repair import set_stops


class NativeStopRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_readback_does_not_resubmit(self):
        c = self.client()
        reads = 0
        accepted = []
        async def post(path, body, **kwargs):
            accepted.append(dict(body, isActive=True))
            raise TimeoutError('ack lost')
        async def read(symbol):
            nonlocal reads
            reads += 1
            return accepted if reads >= 3 else []
        c._post = AsyncMock(side_effect=post)
        c.get_stop_orders = AsyncMock(side_effect=read)
        self.assertTrue(await set_stops(c, 'AVAXUSDT', 7.3, 0, self.module(), Mock()))
        c._post.assert_awaited_once()

    def client(self, side="Buy"):
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[dict(symbol="AVAXUSDT", size=30, side=side, entryPrice=7.4, markPrice=7.4)]),
            _round_price=lambda price, symbol: str(price),
            get_stop_orders=AsyncMock(return_value=[]),
            get_instruments=lambda: {"AVAXUSDT": {"multiplier": "0.1", "lotSize": "1", "minQty": "1"}},
        )
        async def post(path, body, **kwargs):
            self.assertEqual(path, "/api/v1/orders")
            self.assertTrue(body["closeOrder"])
            self.assertTrue(body["reduceOnly"])
            self.assertNotIn("size", body)
            client.get_stop_orders.return_value = [dict(body, id="native", isActive=True)]
            return {"orderId": "native"}
        client._post = AsyncMock(side_effect=post)
        return client

    def module(self):
        return SimpleNamespace(PAPER_TRADE=False, API_KEY="test", to_kucoin=lambda s: s+"M")

    async def test_long_and_short_native_repair_with_readback(self):
        for side, price, trigger, order_side in (("Buy",7.3,"down","sell"),("Sell",7.5,"up","buy")):
            c = self.client(side)
            self.assertTrue(await set_stops(c, "AVAXUSDT", price, 0, self.module(), Mock()))
            body = c._post.call_args.args[1]
            self.assertEqual((body["stop"],body["side"]),(trigger,order_side))
            c._post.reset_mock()
            self.assertTrue(await set_stops(c, "AVAXUSDT", price, 0, self.module(), Mock()))
            c._post.assert_not_called()

    async def test_reduce_only_equivalent_readback_confirms_without_close_order(self):
        c = self.client()
        accepted = []
        async def post(path, body, **kwargs):
            native = dict(body, isActive=True)
            native.pop("closeOrder", None)
            native["reduceOnly"] = True
            native["size"] = 30
            accepted.append(native)
            return {"orderId": "native"}
        c._post = AsyncMock(side_effect=post)
        c.get_stop_orders = AsyncMock(side_effect=lambda symbol: list(accepted))
        self.assertTrue(await set_stops(c, "AVAXUSDT", 7.3, 0, self.module(), Mock()))
        c._post.assert_awaited_once()

    async def test_undersized_reduce_only_readback_fails_closed(self):
        c = self.client()
        accepted = []
        async def post(path, body, **kwargs):
            native = dict(body, isActive=True)
            native.pop("closeOrder", None)
            native["reduceOnly"] = True
            native["size"] = 29
            accepted.append(native)
            return {"orderId": "native"}
        c._post = AsyncMock(side_effect=post)
        c.get_stop_orders = AsyncMock(side_effect=lambda symbol: list(accepted))
        self.assertFalse(await set_stops(c, "AVAXUSDT", 7.3, 0, self.module(), Mock()))

    async def test_readback_without_close_or_reduce_only_fails_closed(self):
        c = self.client()
        accepted = []
        async def post(path, body, **kwargs):
            native = dict(body, isActive=True)
            native.pop("closeOrder", None)
            native["reduceOnly"] = False
            accepted.append(native)
            return {"orderId": "native"}
        c._post = AsyncMock(side_effect=post)
        c.get_stop_orders = AsyncMock(side_effect=lambda symbol: list(accepted))
        self.assertFalse(await set_stops(c, "AVAXUSDT", 7.3, 0, self.module(), Mock()))


    async def test_adjacent_tick_readback_is_equivalent_but_larger_drift_fails(self):
        c = self.client()
        c.get_instruments = lambda: {"AVAXUSDT": {"multiplier": "0.1", "lotSize": "1", "minQty": "1", "tickSize": "0.001"}}
        accepted = []
        async def post(path, body, **kwargs):
            native = dict(body, isActive=True)
            native["stopPrice"] = "7.301"
            native.pop("stopPriceType", None)
            accepted.append(native)
            return {"orderId": "native"}
        c._post = AsyncMock(side_effect=post)
        c.get_stop_orders = AsyncMock(side_effect=lambda symbol: list(accepted))
        self.assertTrue(await set_stops(c, "AVAXUSDT", 7.3, 0, self.module(), Mock()))

        # Drift to the LOOSER side is not equivalent and must be repaired.
        accepted[0]["stopPrice"] = "7.2979"
        c._post.reset_mock()
        c._post = AsyncMock(return_value={"orderId": "replacement"})
        self.assertFalse(await set_stops(c, "AVAXUSDT", 7.3, 0, self.module(), Mock()))
        c._post.assert_awaited_once()

        # Q-01C: drift to the MORE protective side is never loosened back.
        accepted[0]["stopPrice"] = "7.3021"
        c._post = AsyncMock(return_value={"orderId": "replacement"})
        self.assertTrue(await set_stops(c, "AVAXUSDT", 7.3, 0, self.module(), Mock()))
        c._post.assert_not_awaited()

    async def test_acknowledgement_without_readback_fails(self):
        c=self.client()
        c._post=AsyncMock(return_value={"orderId":"ack"})
        self.assertFalse(await set_stops(c,"AVAXUSDT",7.3,0,self.module(),Mock()))

    async def test_unreadable_orders_block_before_post(self):
        c=self.client()
        c.get_stop_orders=AsyncMock(side_effect=TimeoutError())
        self.assertFalse(await set_stops(c,"AVAXUSDT",7.3,0,self.module(),Mock()))
        c._post.assert_not_called()

    async def test_crossed_stop_and_paper_never_submit(self):
        c=self.client()
        self.assertFalse(await set_stops(c,"AVAXUSDT",7.5,0,self.module(),Mock()))
        m=self.module(); m.PAPER_TRADE=True
        self.assertFalse(await set_stops(c,"AVAXUSDT",7.3,0,m,Mock()))
        c._post.assert_not_called()


class NativeStopBreakEvenTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_break_even_stop_is_allowed_when_mark_above_entry(self):
        base = NativeStopRepairTests()
        c = base.client("Buy")
        c.get_positions = AsyncMock(return_value=[dict(symbol="AVAXUSDT", size=30, side="Buy", entryPrice=7.4, markPrice=7.6)])
        self.assertTrue(await set_stops(c, "AVAXUSDT", 7.4, 0, base.module(), Mock()))
        self.assertEqual(c._post.call_args.args[1]["stopPrice"], "7.4")

    async def test_short_break_even_stop_is_allowed_when_mark_below_entry(self):
        base = NativeStopRepairTests()
        c = base.client("Sell")
        c.get_positions = AsyncMock(return_value=[dict(symbol="AVAXUSDT", size=30, side="Sell", entryPrice=7.4, markPrice=7.2)])
        self.assertTrue(await set_stops(c, "AVAXUSDT", 7.4, 0, base.module(), Mock()))
        self.assertEqual(c._post.call_args.args[1]["stopPrice"], "7.4")


class NativeStopTrailingTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_profitable_trailing_stop_is_allowed_below_mark(self):
        base = NativeStopRepairTests()
        c = base.client("Buy")
        c.get_positions = AsyncMock(return_value=[dict(symbol="AVAXUSDT", size=30, side="Buy", entryPrice=7.4, markPrice=7.8)])
        self.assertTrue(await set_stops(c, "AVAXUSDT", 7.6, 0, base.module(), Mock()))

    async def test_short_profitable_trailing_stop_is_allowed_above_mark(self):
        base = NativeStopRepairTests()
        c = base.client("Sell")
        c.get_positions = AsyncMock(return_value=[dict(symbol="AVAXUSDT", size=30, side="Sell", entryPrice=7.4, markPrice=7.0)])
        self.assertTrue(await set_stops(c, "AVAXUSDT", 7.2, 0, base.module(), Mock()))

    async def test_crossed_long_stop_is_rejected(self):
        base = NativeStopRepairTests()
        c = base.client("Buy")
        c.get_positions = AsyncMock(return_value=[dict(symbol="AVAXUSDT", size=30, side="Buy", entryPrice=7.4, markPrice=7.6)])
        self.assertFalse(await set_stops(c, "AVAXUSDT", 7.6, 0, base.module(), Mock()))
        c._post.assert_not_called()
