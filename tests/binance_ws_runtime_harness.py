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
   24hrTicker are /market streams, so an unrouted connection opens and then
   stays silent (the production symptom);
5. drives the real ``start_websocket`` / ``_ws_loop`` / handlers and then
   calls the real ``engine.pilot.evaluate``.

It prints one JSON line with the result. No credentials are real.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from urllib.parse import parse_qs, urlparse

MARKET_STREAM_SUFFIXES = ("@ticker", "@kline_")


class FakeBinanceWs:
    """One fake connection honoring Binance's routed-path semantics."""

    connections: list = []

    def __init__(self, url: str, script):
        self.url = url
        parsed = urlparse(url)
        self.path = parsed.path
        self.streams = parse_qs(parsed.query).get("streams", [""])[0].split("/")
        self.script = script
        self.closed = False
        FakeBinanceWs.connections.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    def _routed_for_market(self) -> bool:
        return self.path.startswith("/market/")

    def __aiter__(self):
        return self._frames()

    async def _frames(self):
        market = [s for s in self.streams if s.endswith("@ticker") or "@kline_" in s]
        if not self._routed_for_market() or not market:
            # Unrouted legacy URL: Binance accepts the upgrade but pushes no
            # /market data. The socket simply stays silent.
            while True:
                await asyncio.sleep(3600)
        for frame in self.script(market):
            if frame == "__DISCONNECT__":
                raise ConnectionError("fake server closed connection")
            if frame == "__IDLE__":
                while True:
                    await asyncio.sleep(3600)
            yield frame
            await asyncio.sleep(0)
        while True:
            await asyncio.sleep(3600)


def _kline(stream: str) -> str:
    symbol, _, interval = stream.partition("@kline_")
    return json.dumps({
        "stream": stream,
        "data": {
            "e": "kline", "E": int(time.time() * 1000), "s": symbol.upper(),
            "k": {"t": 1790473500000, "i": interval, "o": "1.0", "h": "1.1",
                  "l": "0.9", "c": "1.05", "v": "100"},
        },
    })


def _ticker(stream: str) -> str:
    symbol = stream.split("@", 1)[0]
    return json.dumps({
        "stream": stream,
        "data": {"e": "24hrTicker", "s": symbol.upper(), "c": "1.05",
                 "b": "1.04", "a": "1.06", "v": "1000", "q": "1050"},
    })


def default_script(market_streams):
    for stream in market_streams:
        yield _ticker(stream) if stream.endswith("@ticker") else _kline(stream)


class _Decision:
    execution_allowed = True


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


async def _scenario(name: str) -> dict:
    _load_repo_sitecustomize()
    import main_hardened  # noqa: F401  (sitecustomize + runtime bootstrap)
    import main
    from bot import binance

    client = main.ExchangeClient()
    engine = main.TradingEngine(client)

    async def no_rest_seed(symbols, intervals):
        return None  # REST seeding must never satisfy public WS freshness

    client._seed_kline_cache = no_rest_seed
    binance.websockets.connect = lambda url, **kw: FakeBinanceWs(url, default_script)

    await client.start_websocket(["SUIUSDT", "NEARUSDT"], intervals=["15"])
    for _ in range(50):
        await asyncio.sleep(0.01)

    reasons = engine.pilot.evaluate(engine, engine.client, "SUIUSDT", _Decision())
    market = [r for r in reasons if r.startswith("11_MARKET_DATA")]
    health = getattr(client, "market_data_health", None)
    out = {
        "scenario": name,
        "bootstrap_installed": bool(getattr(__import__("builtins"), "_nexus_runtime_bootstrap_installed", False)),
        "client_type": f"{type(client).__module__}.{type(client).__qualname__}",
        "engine_client_is_client": engine.client is client,
        "ws_urls": [c.url.split("?")[0] for c in FakeBinanceWs.connections],
        "handler_qualname": binance.BinanceClient._handle_ws_message.__qualname__,
        "market_data_reasons": market,
        "health_is_shared": (
            health is not None
            and getattr(engine.client, "market_data_health", None) is health
        ),
    }
    for task in list(getattr(client, "_ws_tasks", ())):
        task.cancel()
    await asyncio.gather(*list(getattr(client, "_ws_tasks", ())), return_exceptions=True)
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
