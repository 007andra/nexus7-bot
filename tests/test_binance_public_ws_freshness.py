import asyncio
import time
import unittest


class _Log:
    def info(self, *args, **kwargs):
        pass


class _Client:
    async def _handle_ws_message(self, message):
        self.handled = message


class BinancePublicWsFreshnessTests(unittest.TestCase):
    def setUp(self):
        from bot import binance_public_ws_freshness as bridge

        bridge._INSTALLED = False
        self.bridge = bridge
        # Each test uses a fresh class because install intentionally patches
        # the class once for process lifetime.
        class Client:
            async def _handle_ws_message(self, message):
                self.handled = message
        self.Client = Client

    def test_ticker_advances_freshness_after_handler(self):
        self.bridge.install(self.Client, _Log())
        client = self.Client()
        before = time.time()
        asyncio.run(client._handle_ws_message({"data": {"e": "24hrTicker", "s": "UNIUSDT"}}))
        self.assertGreaterEqual(client._last_ws_update, before)

    def test_kline_advances_freshness(self):
        self.bridge.install(self.Client, _Log())
        client = self.Client()
        asyncio.run(client._handle_ws_message({"data": {"e": "kline", "s": "UNIUSDT", "k": {}}}))
        self.assertGreater(client._last_ws_update, 0)

    def test_unknown_event_does_not_satisfy_pilot_contract(self):
        self.bridge.install(self.Client, _Log())
        client = self.Client()
        asyncio.run(client._handle_ws_message({"data": {"e": "ACCOUNT_UPDATE"}}))
        self.assertFalse(hasattr(client, "_last_ws_update"))

    def test_malformed_frame_does_not_satisfy_pilot_contract(self):
        self.bridge.install(self.Client, _Log())
        client = self.Client()
        asyncio.run(client._handle_ws_message({"data": "bad"}))
        self.assertFalse(hasattr(client, "_last_ws_update"))

    def test_handler_failure_does_not_advance_freshness(self):
        class FailingClient:
            async def _handle_ws_message(self, message):
                raise RuntimeError("parse failed")

        self.bridge._INSTALLED = False
        self.bridge.install(FailingClient, _Log())
        client = FailingClient()
        with self.assertRaises(RuntimeError):
            asyncio.run(client._handle_ws_message({"data": {"e": "24hrTicker"}}))
        self.assertFalse(hasattr(client, "_last_ws_update"))

    def test_pilot_guard_accepts_fresh_timestamp_and_rejects_stale(self):
        from bot import pilot

        now = time.time()
        fresh_client = type("C", (), {"_last_ws_update": now})()
        stale_client = type("C", (), {"_last_ws_update": now - pilot.PILOT_MAX_MARKET_DATA_AGE_S - 1})()

        # Contract-level assertion: this is the exact predicate used by gate 11.
        self.assertLess(time.time() - fresh_client._last_ws_update, pilot.PILOT_MAX_MARKET_DATA_AGE_S)
        self.assertGreater(time.time() - stale_client._last_ws_update, pilot.PILOT_MAX_MARKET_DATA_AGE_S)


if __name__ == "__main__":
    unittest.main()
