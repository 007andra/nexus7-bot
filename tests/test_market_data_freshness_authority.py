"""Invariants of the canonical Binance public market-data freshness authority.

Complements tests/test_pilot_market_data_runtime_bridge.py (real bootstrap in
a child process) with fast in-process checks of every freshness rule.
"""
import asyncio
import json
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from bot import binance
from bot import pilot
from bot.market_data_health import MarketDataHealth

LIMIT = pilot.PILOT_MAX_MARKET_DATA_AGE_S


class Clock:
    def __init__(self, t=10_000.0):
        self.t = t

    def __call__(self):
        return self.t


def kline(symbol="BTCUSDT", interval="15m", close="1.05"):
    return {"stream": f"{symbol.lower()}@kline_{interval}", "data": {
        "e": "kline", "s": symbol,
        "k": {"t": 1790473500000, "i": interval, "o": "1", "h": "1.1",
              "l": "0.9", "c": close, "v": "10"}}}


def ticker(symbol="BTCUSDT", last="1.05"):
    return {"stream": f"{symbol.lower()}@ticker", "data": {
        "e": "24hrTicker", "s": symbol, "c": last, "b": "1", "a": "1.1",
        "v": "1", "q": "1"}}


def new_client(clock=None):
    client = binance.BinanceClient()
    if clock is not None:
        client.market_data_health._monotonic = clock
    return client


def handle(client, message):
    asyncio.run(client._handle_ws_message(message))


def gate(client):
    return pilot.market_data_blockers(client)


class Proxy:
    """Transparent engine-side wrapper (same shape as KuCoinPositionUnitAdapter)."""

    def __init__(self, inner):
        self._client = inner

    def __getattr__(self, name):
        return getattr(self._client, name)


class FreshnessContractTests(unittest.TestCase):
    def test_03_first_valid_kline_passes(self):
        client = new_client()
        handle(client, kline())
        self.assertEqual(gate(client), [])
        self.assertEqual(client.market_data_health.snapshot()["last_event"], "kline")

    def test_04_valid_ticker_passes(self):
        client = new_client()
        handle(client, ticker())
        self.assertEqual(gate(client), [])

    def test_05_no_data_blocks(self):
        client = new_client()
        self.assertEqual(
            gate(client),
            ["11_MARKET_DATA: nenhum dado de mercado recebido (reason=no_market_data)"],
        )
        self.assertEqual(client._last_ws_update, 0.0)

    def test_06_stale_over_limit_blocks(self):
        clock = Clock()
        client = new_client(clock)
        handle(client, kline())
        clock.t += LIMIT + 1
        reasons = gate(client)
        self.assertEqual(len(reasons), 1)
        self.assertIn("reason=stale_market_data", reasons[0])

    def test_07_exact_boundary(self):
        # Same ``>`` boundary as the historical gate: age == limit is PASS,
        # anything above is BLOCK. The limit itself is unchanged (120 s).
        self.assertEqual(LIMIT, 120.0)
        clock = Clock()
        client = new_client(clock)
        handle(client, ticker())
        clock.t += LIMIT
        self.assertEqual(gate(client), [])
        clock.t += 0.001
        self.assertIn("reason=stale_market_data", gate(client)[0])

    def test_08_invalid_json_frame_through_ws_loop_does_not_advance(self):
        client = new_client()
        frames = ["{not json", json.dumps([1, 2, 3]), json.dumps({"data": "bad"})]
        run_ws_loop(client, frames)
        self.assertEqual(client.market_data_health.snapshot()["events_total"], 0)
        self.assertIn("no_market_data", gate(client)[0])

    def test_09_unknown_events_do_not_advance(self):
        client = new_client()
        for event in ("markPriceUpdate", "ACCOUNT_UPDATE", "ORDER_TRADE_UPDATE",
                      "bookTicker", "depthUpdate", None):
            handle(client, {"data": {"e": event, "s": "BTCUSDT"}})
        handle(client, {"data": {"e": "kline", "s": "BTCUSDT", "k": {"i": "7m"}}})  # unknown interval
        handle(client, {"data": {"e": "24hrTicker", "s": ""}})  # no symbol
        self.assertEqual(client.market_data_health.snapshot()["events_total"], 0)
        self.assertIn("no_market_data", gate(client)[0])

    def test_10_kline_handler_error_does_not_advance(self):
        client = new_client()
        with self.assertRaises(ValueError):
            handle(client, kline(close="not-a-number"))
        self.assertIn("no_market_data", gate(client)[0])

    def test_11_ticker_handler_error_does_not_advance(self):
        client = new_client()
        with self.assertRaises(ValueError):
            handle(client, ticker(last="nan-ish"))
        self.assertIn("no_market_data", gate(client)[0])

    def test_12_private_ws_does_not_release_gate(self):
        client = new_client()
        asyncio.run(client._handle_private_order_event({"e": "listenKeyExpired"}))
        asyncio.run(client._handle_private_order_event({"e": "ACCOUNT_UPDATE", "a": {}}))
        self.assertIn("no_market_data", gate(client)[0])

    def test_13_rest_does_not_release_gate(self):
        client = new_client()
        rows = [[1790473500000, "1", "1.1", "0.9", "1.05", "10"]]
        with patch.object(client, "_get", AsyncMock(return_value=rows)):
            asyncio.run(client.get_klines("BTCUSDT", "15"))
            asyncio.run(client._seed_kline_cache(["BTCUSDT"], ["15"]))
        async def rest_ticker(endpoint, params=None, auth=False):
            if endpoint == "/fapi/v1/ticker/bookTicker":
                return {"symbol": "BTCUSDT", "bidPrice": "0.9999", "askPrice": "1.0001"}
            return {"lastPrice": "1"}
        with patch.object(client, "_get", AsyncMock(side_effect=rest_ticker)):
            asyncio.run(client.get_ticker("BTCUSDT"))
        self.assertTrue(client.get_cached_ticker("BTCUSDT"))
        self.assertIn("no_market_data", gate(client)[0])

    def test_14_15_reconnect_semantics(self):
        clock = Clock()
        client = new_client(clock)
        health = client.market_data_health
        health.mark_connected()
        handle(client, kline())
        self.assertEqual(gate(client), [])
        health.mark_disconnected()
        clock.t += LIMIT + 5
        self.assertIn("stale_market_data", gate(client)[0])
        health.mark_connected()  # reconnect alone never refreshes
        self.assertIn("stale_market_data", gate(client)[0])
        self.assertEqual(health.snapshot()["events_this_connection"], 0)
        handle(client, ticker())
        self.assertEqual(gate(client), [])

    def test_16_two_accidental_clients_never_share_freshness(self):
        a, b = new_client(), new_client()
        self.assertIsNot(a.market_data_health, b.market_data_health)
        self.assertNotEqual(a.market_data_health.instance_id, b.market_data_health.instance_id)
        handle(a, kline())
        self.assertEqual(gate(a), [])
        # WS writing A can never make a guard that reads B pass.
        self.assertIn("no_market_data", gate(b)[0])

    def test_17_wrapper_reads_the_same_authority(self):
        client = new_client()
        proxy = Proxy(client)
        self.assertIs(proxy.market_data_health, client.market_data_health)
        self.assertIn("no_market_data", gate(proxy)[0])
        handle(client, ticker())
        self.assertEqual(gate(proxy), [])
        self.assertEqual(proxy._last_ws_update, client._last_ws_update)

    def test_18_concurrent_writers_do_not_corrupt_state(self):
        health = MarketDataHealth()
        errors = []

        def worker(n):
            try:
                for i in range(500):
                    health.record_public_event("kline" if i % 2 else "24hrTicker", f"S{n}")
                    health.check(LIMIT)
                    health.snapshot()
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(health.snapshot()["events_total"], 8 * 500)
        self.assertTrue(health.check(LIMIT)[0])

        client = new_client()

        async def burst():
            await asyncio.gather(*(client._handle_ws_message(
                kline() if i % 2 else ticker(symbol=f"X{i}USDT")) for i in range(200)))

        asyncio.run(burst())
        self.assertEqual(client.market_data_health.snapshot()["events_total"], 200)

    def test_19_monotonic_clock_ignores_wall_clock_jumps(self):
        clock = Clock()
        client = new_client(clock)
        handle(client, kline())
        real = time.time()
        # Wall clock jumps an hour forward and backward: freshness unchanged.
        for jump in (3600.0, -3600.0):
            with patch("time.time", return_value=real + jump):
                self.assertEqual(gate(client), [])
        clock.t += LIMIT + 1
        with patch("time.time", return_value=real - 3600.0):
            self.assertIn("stale_market_data", gate(client)[0])
        # A monotonic regression is refused and never produces PASS.
        health = MarketDataHealth(monotonic=clock)
        health.record_public_event("kline", "BTCUSDT")
        clock.t -= 50
        self.assertFalse(health.record_public_event("kline", "BTCUSDT"))
        ok, reason, _ = health.check(LIMIT)
        self.assertFalse(ok)
        self.assertEqual(reason, "clock_regression")

    def test_20_incident_candidate_not_blocked_by_gate_11_when_fresh(self):
        client = new_client()
        handle(client, ticker("SUIUSDT"))
        handle(client, kline("NEARUSDT"))

        class Decision:
            execution_allowed = True

        class Engine:
            client = None
            positions = {}
            risk = None

        engine = Engine()
        engine.client = client
        stale_client = new_client()
        blocked = pilot.PilotGuard().evaluate(engine, stale_client, "SUIUSDT", Decision())
        # Gate 11 is really evaluated (no evaluation error short-circuit) ...
        self.assertFalse([r for r in blocked if r.startswith("PILOT_EVAL_ERROR")], blocked)
        self.assertIn("11_MARKET_DATA: nenhum dado de mercado recebido (reason=no_market_data)", blocked)
        reasons = pilot.PilotGuard().evaluate(engine, client, "SUIUSDT", Decision())
        self.assertFalse([r for r in reasons if r.startswith("PILOT_EVAL_ERROR")], reasons)
        # ... and fresh public data clears exactly gate 11, nothing else.
        self.assertFalse([r for r in reasons if r.startswith("11_MARKET_DATA")], reasons)
        self.assertEqual(
            sorted(set(blocked) - set(reasons)),
            ["11_MARKET_DATA: nenhum dado de mercado recebido (reason=no_market_data)"],
        )

    def test_legacy_last_ws_update_is_read_only_view(self):
        client = new_client()
        with self.assertRaises(AttributeError):
            client._last_ws_update = time.time()
        self.assertIn("no_market_data", gate(client)[0])

    def test_legacy_clients_keep_historical_contract(self):
        class Legacy:
            _last_ws_update = 0

        self.assertEqual(gate(Legacy()), ["11_MARKET_DATA: nenhum dado de mercado recebido"])
        fresh = Legacy()
        fresh._last_ws_update = time.time()
        self.assertEqual(gate(fresh), [])
        stale = Legacy()
        stale._last_ws_update = time.time() - LIMIT - 5
        self.assertIn("11_MARKET_DATA: dado com", gate(stale)[0])

    def test_runtime_contract_detects_ws_handler_overlay(self):
        from bot.runtime_contract_guard import ContractItem, verify

        native = binance.BinanceClient._handle_ws_message
        ok, _ = verify((ContractItem("binance._handle_ws_message", native, "binance.py"),))
        self.assertTrue(ok)

        async def overlay(self, message):  # a PR #421-style late wrapper
            return await native(self, message)

        ok, errors = verify((ContractItem("binance._handle_ws_message", overlay, "binance.py"),))
        self.assertFalse(ok)
        self.assertIn("binance._handle_ws_message", errors[0])
        self.assertTrue(binance.WS_MARKET_BASE.endswith("/market"))

    def test_public_ws_url_is_routed_to_market(self):
        client = new_client()
        urls = []

        class Silent:
            def __init__(self, url):
                urls.append(url)

            async def __aenter__(self):
                raise asyncio.CancelledError  # stop after recording the URL

            async def __aexit__(self, *a):
                return False

        with patch.object(binance.websockets, "connect", lambda url, **kw: Silent(url)):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(client._ws_loop(["SUIUSDT"], ["15", "60", "240"]))
        self.assertTrue(urls[0].startswith("wss://fstream.binance.com/market/stream?streams="), urls)
        self.assertIn("suiusdt@ticker", urls[0])
        self.assertIn("suiusdt@kline_15m", urls[0])

    def test_silent_stream_reconnects_and_never_advances_freshness(self):
        client = new_client()
        connections = []

        class SilentConn:
            def __init__(self, url):
                connections.append(url)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def __aiter__(self):
                return self._gen()

            async def _gen(self):
                while True:
                    await asyncio.sleep(3600)
                    yield "never"

        async def run():
            task = asyncio.create_task(client._ws_loop(["SUIUSDT"], ["15"]))
            for _ in range(300):
                await asyncio.sleep(0.01)
                if len(connections) >= 2:
                    break
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        with patch.object(binance, "PUBLIC_WS_SILENCE_RECONNECT_S", 0.05), \
             patch.object(binance.websockets, "connect", lambda url, **kw: SilentConn(url)), \
             self.assertLogs("kakazito-trade", level="WARNING") as logs:
            asyncio.run(run())
        self.assertGreaterEqual(len(connections), 2)
        self.assertTrue(any("event=silent_stream" in line for line in logs.output))
        self.assertIn("no_market_data", gate(client)[0])


def run_ws_loop(client, frames):
    """Feed raw frames through the real _ws_loop once, then stop."""

    class Conn:
        def __init__(self, url):
            self.url = url

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for frame in frames:
                yield frame
            raise asyncio.CancelledError

    with patch.object(binance.websockets, "connect", lambda url, **kw: Conn(url)):
        try:
            asyncio.run(client._ws_loop(["BTCUSDT"], ["15"]))
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    unittest.main()
