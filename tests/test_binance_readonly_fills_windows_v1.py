"""Offline tests for time-sliced userTrades evidence."""
import asyncio
import unittest

from bot.binance_readonly_fills_windows_v1 import collect_fills_windows


class Client:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    async def _get(self, endpoint, params=None, auth=False):
        self.calls.append((endpoint, dict(params), auth))
        return self.pages.pop(0)


def run(client, **changes):
    args = dict(client=client, symbol="BTCUSDT", start_ms=1000,
                end_ms=1009, window_ms=5, limit=2)
    args.update(changes)
    return asyncio.run(collect_fills_windows(**args))


class WindowTests(unittest.TestCase):
    def test_adjacent_windows_have_no_timestamp_overlap(self):
        client = Client([[{"symbol": "BTCUSDT", "id": 1, "time": 1002}],
                         [{"symbol": "BTCUSDT", "id": 2, "time": 1007}]])
        result = run(client)
        self.assertEqual(result["status"], "FILLS_WINDOWS_SHAPE_VALID")
        self.assertEqual(result["window_count"], 2)
        self.assertEqual(client.calls[0][1]["endTime"], 1004)
        self.assertEqual(client.calls[1][1]["startTime"], 1005)
        self.assertTrue(all(call[2] for call in client.calls))

    def test_saturated_window_fails_closed(self):
        client = Client([[{"symbol": "BTCUSDT", "id": 1, "time": 1001},
                          {"symbol": "BTCUSDT", "id": 2, "time": 1002}]])
        self.assertEqual(run(client)["status"], "PROOF_MISSING")

    def test_window_budget_exhaustion(self):
        client = Client([[{"symbol": "BTCUSDT", "id": 1, "time": 1002}]])
        self.assertEqual(run(client, max_windows=1)["status"], "PROOF_MISSING")

    def test_duplicate_across_windows(self):
        client = Client([[{"symbol": "BTCUSDT", "id": 1, "time": 1002}],
                         [{"symbol": "BTCUSDT", "id": 1, "time": 1007}]])
        self.assertEqual(run(client)["status"], "PROOF_MISSING")


    def test_insufficient_window_budget_no_network(self):
        client = Client([])
        result = run(client, max_windows=1)
        self.assertEqual(result["status"], "PROOF_MISSING")
        self.assertEqual(client.calls, [])

    def test_single_millisecond_tail_is_collected(self):
        client = Client([[{"symbol": "BTCUSDT", "id": 1, "time": 1002}],
                         [{"symbol": "BTCUSDT", "id": 2, "time": 1005}]])
        result = run(client, end_ms=1005)
        self.assertEqual(result["status"], "FILLS_WINDOWS_SHAPE_VALID")
        self.assertEqual(result["window_count"], 2)
        self.assertEqual(client.calls[-1][1]["startTime"], 1005)
        self.assertEqual(client.calls[-1][1]["endTime"], 1005)

    def test_empty_single_millisecond_window(self):
        client = Client([[]])
        result = run(client, start_ms=1000, end_ms=1000)
        self.assertEqual(result["status"], "FILLS_WINDOWS_SHAPE_VALID")
        self.assertEqual(result["records"], [])


if __name__ == "__main__":
    unittest.main()
