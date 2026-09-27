"""Isolated LIVE-shaped Binance runtime harness for market-data freshness tests.

Run as ``python -m tests.binance_ws_runtime_harness <scenario>`` in a child
process (the bootstrap patches classes process-wide). It:

1. installs the offline network guard (no real exchange traffic, no orders);
2. imports ``main_hardened`` -> sitecustomize -> ``bot.runtime_bootstrap``
   (the exact production install order);
3. composes client/engine exactly like ``main.lifespan``
   (``ExchangeClient()`` -> ``TradingEngine(client)``);
4. replaces only the network transport (``websockets.connect`` in
   ``bot.binance``) with a fake that implements Binance's documented USD-M
   base-URL split: connections without a routed path (``/public``,
   ``/market``, ``/private``) receive only /public streams; kline and
   24hrTicker are /market streams, so on an unrouted connection they are
   dropped and the socket stays silent (the production symptom);
5. drives the real ``start_websocket`` / ``_ws_loop`` / handlers and the real
   ``engine.pilot.evaluate``.

It prints one ``HARNESS_RESULT`` JSON line. No credentials are real.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from urllib.parse import parse_qs, urlparse

_DISCONNECT = object()


class FakeBinanceWs:
    """One fake connection honoring Binance's routed-path semantics."""

    connections: list = []

    def __init__(self, url: str):
        self.url = url
        parsed = urlparse(url)
        self.path = parsed.path
        self.streams = parse_qs(parsed.query).get("streams", [""])[0].split("/")
        self.queue: asyncio.Queue = asyncio.Queue()
        FakeBinanceWs.connections.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def push(self, frame) -> None:
        self.queue.put_nowait(frame)

    def __aiter__(self):
        return self._frames()

    async def _frames(self):
        routed_market = self.path.startswith("/market/")
        while True:
            frame = await self.queue.get()
            if frame is _DISCONNECT:
                raise ConnectionError("fake server closed connection")
            if not routed_market:
                continue  # /market data is never pushed on unrouted paths
            yield frame


def kline_frame(symbol: str = "SUIUSDT", interval: str = "15m") -> str:
    lower = symbol.lower()
    return json.dumps({
        "stream": f"{lower}@kline_{interval}",
        "data": {
            "e": "kline", "E": int(time.time() * 1000), "s": symbol,
            "k": {"t": 1790473500000, "i": interval, "o": "1.0", "h": "1.1",
                  "l": "0.9", "c": "1.05", "v": "100"},
        },
    })


def ticker_frame(symbol: str = "SUIUSDT") -> str:
    return json.dumps({
        "stream": f"{symbol.lower()}@ticker",
        "data": {"e": "24hrTicker", "s": symbol, "c": "1.05",
                 "b": "1.04", "a": "1.06", "v": "1000", "q": "1050"},
    })


class _Decision:
    execution_allowed = True


class _Clock:
    def __init__(self):
        self.t = 1_000.0

    def __call__(self):
        return self.t


def _load_repo_sitecustomize() -> None:
    """Make ``import sitecustomize`` resolve to the repository module.

    Some interpreters (Debian/Ubuntu) ship a system ``sitecustomize`` that the
    site module imports first and caches. Production (Railway) resolves the
    repository file; mirror that so the real runtime bootstrap is installed.
    """
    import importlib.util
    from pathlib import Path

    repo_file = Path(__file__).resolve().parents[1] / "sitecustomize.py"
    current = sys.modules.get("sitecustomize")
    if current is not None and Path(getattr(current, "__file__", "") or "").resolve() == repo_file:
        return
    spec = importlib.util.spec_from_file_location("sitecustomize", repo_file)
    module = importlib.util.module_from_spec(spec)
    sys.modules["sitecustomize"] = module
    spec.loader.exec_module(module)


async def _settle(n: int = 20) -> None:
    for _ in range(n):
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)


async def _wait_connections(count: int) -> None:
    for _ in range(400):
        if len(FakeBinanceWs.connections) >= count:
            await _settle()
            return
        await asyncio.sleep(0.01)
    raise RuntimeError(f"expected {count} websocket connections")


async def _scenario(name: str) -> dict:
    _load_repo_sitecustomize()
    import builtins

    import main_hardened  # noqa: F401  (sitecustomize + runtime bootstrap)
    import main
    from bot import binance

    client = main.ExchangeClient()
    engine = main.TradingEngine(client)

    async def no_rest_seed(symbols, intervals):
        return None  # REST seeding must never satisfy public WS freshness

    client._seed_kline_cache = no_rest_seed
    binance.websockets.connect = lambda url, **kw: FakeBinanceWs(url)

    def gate():
        reasons = engine.pilot.evaluate(engine, engine.client, "SUIUSDT", _Decision())
        return [r for r in reasons if r.startswith("11_MARKET_DATA")]

    await client.start_websocket(["SUIUSDT", "NEARUSDT"], intervals=["15"])
    await _wait_connections(1)
    steps: dict = {}

    if name == "fresh_public_market_data":
        steps["before_any_frame"] = gate()
        FakeBinanceWs.connections[-1].push(kline_frame())
        FakeBinanceWs.connections[-1].push(ticker_frame("NEARUSDT"))
        await _settle()
        steps["after_frames"] = gate()
    elif name == "reconnect":
        clock = _Clock()
        health = getattr(client, "market_data_health", None)
        if health is not None:
            health._monotonic = clock
        conn = FakeBinanceWs.connections[-1]
        conn.push(kline_frame())
        await _settle()
        steps["connected_fresh"] = gate()
        conn.push(_DISCONNECT)
        await _wait_connections(2)  # real _ws_loop reconnect (1s backoff)
        clock.t += 121.0
        steps["reconnected_no_frame_stale"] = gate()
        FakeBinanceWs.connections[-1].push(ticker_frame())
        await _settle()
        steps["reconnected_first_frame"] = gate()
    else:
        raise SystemExit(f"unknown scenario {name}")

    health = getattr(client, "market_data_health", None)
    out = {
        "scenario": name,
        "bootstrap_installed": bool(getattr(builtins, "_nexus_runtime_bootstrap_installed", False)),
        "client_type": f"{type(client).__module__}.{type(client).__qualname__}",
        "engine_client_is_client": engine.client is client,
        "ws_urls": [c.url.split("?")[0] for c in FakeBinanceWs.connections],
        "handler_qualname": binance.BinanceClient._handle_ws_message.__qualname__,
        "market_data_reasons": gate(),
        "steps": steps,
        "health_is_shared": (
            health is not None
            and getattr(engine.client, "market_data_health", None) is health
        ),
        "health_instance": getattr(health, "instance_id", None),
    }
    tasks = list(getattr(client, "_ws_tasks", ()))
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return out


def main_entry() -> int:
    from tests.run_offline import install_network_guard

    install_network_guard()
    name = sys.argv[1] if len(sys.argv) > 1 else "fresh_public_market_data"
    result = asyncio.run(_scenario(name))
    print("HARNESS_RESULT " + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    os.environ.setdefault("EXCHANGE", "binance")
    sys.exit(main_entry())
