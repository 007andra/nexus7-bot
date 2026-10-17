import unittest

from bot.binance import BinanceClient


class _Health:
    def __init__(self):
        self.events = []

    def record_event(self, event, event_time=None):
        self.events.append((event, event_time))


class _RegistryMustNotBeTouched:
    def __init__(self):
        self.calls = 0

    def get_or_create(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("Algo actual order must not enter ManagedOrder registry")

    def index_order_id(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("Algo actual order must not be indexed as ManagedOrder")


class BinanceAlgoActualOrderWsTests(unittest.IsolatedAsyncioTestCase):
    async def test_triggered_algo_actual_order_is_observability_only(self):
        client = BinanceClient.__new__(BinanceClient)
        client.private_stream_health = _Health()
        client._order_id_symbol = {}
        client._client_oid_symbol = {}
        client._algo_actual_order_client = {"24873239201": "bgx7-stop-atom"}
        client._algo_order_cache = {
            "bgx7-stop-atom": {
                "clientAlgoId": "bgx7-stop-atom",
                "algoStatus": "TRIGGERED",
                "actualOrderId": "24873239201",
            }
        }
        registry = _RegistryMustNotBeTouched()
        client._order_registry = registry

        await client._handle_private_order_event({
            "e": "ORDER_TRADE_UPDATE",
            "E": 1790860552000,
            "o": {
                "c": "bgx7-generated-stop-order",
                "i": 24873239201,
                "s": "ATOMUSDT",
                "X": "FILLED",
                "S": "BUY",
                "q": "570.55",
                "z": "570.55",
                "ap": "1.709",
            },
        })

        self.assertEqual(registry.calls, 0)
        cached = client._algo_order_cache["bgx7-stop-atom"]
        self.assertEqual(cached["actualOrderStatus"], "FILLED")
        self.assertEqual(cached["actualExecutedQty"], "570.55")
        self.assertEqual(cached["actualAvgPrice"], "1.709")
        self.assertEqual(client._order_id_symbol["24873239201"], "ATOMUSDT")
        self.assertTrue(client.private_stream_health.events)


if __name__ == "__main__":
    unittest.main()
