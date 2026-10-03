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

    async def ping(self):
        # Protocol ping/pong works on every path, routed or not: it proves
        # transport only, never that /private events can be delivered.
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(None)
        return fut

    async def close(self):
        self.queue.put_nowait(_DISCONNECT)

    def _delivers(self, frame) -> bool:
        try:
            event = json.loads(frame)
        except (TypeError, ValueError):
            return self.path.startswith(("/market/", "/private/"))
        data = event.get("data", event) if isinstance(event, dict) else {}
        kind = data.get("e") if isinstance(data, dict) else None
        if kind in {"kline", "24hrTicker"}:
            return self.path.startswith("/market/")
        # user-data events (ORDER_TRADE_UPDATE, ACCOUNT_UPDATE, ALGO_UPDATE,
        # TRADE_LITE, listenKeyExpired, ...) are /private traffic.
        return self.path.startswith("/private/")

    async def _frames(self):
        while True:
            frame = await self.queue.get()
            if frame is _DISCONNECT:
                raise ConnectionError("fake server closed connection")
            if not self._delivers(frame):
                continue  # routed-path rule: never pushed on this connection
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


def order_update_frame(client_oid: str, status: str, *, order_id: str = "9001",
                       symbol: str = "SUIUSDT", filled: str = "0", avg: str = "0") -> str:
    return json.dumps({
        "e": "ORDER_TRADE_UPDATE", "E": int(time.time() * 1000), "T": int(time.time() * 1000),
        "o": {"s": symbol, "c": client_oid, "S": "BUY", "o": "MARKET", "q": "10",
              "X": status, "x": "TRADE" if "FILLED" in status else status,
              "i": int(order_id), "z": filled, "ap": avg, "l": filled},
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


def _non_shadow_connections() -> list:
    """Connections other than the observation-only BBO cost shadow (/public)."""
    return [c for c in FakeBinanceWs.connections
            if not c.url.split("?")[0].endswith("/public/stream")]


async def _wait_connections(count: int) -> None:
    for _ in range(400):
        if len(_non_shadow_connections()) >= count:
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

    steps: dict = {}
    if name.startswith("private"):
        return await _private_scenario(name, client, engine, binance, steps)

    await client.start_websocket(["SUIUSDT", "NEARUSDT"], intervals=["15"])
    await _wait_connections(1)

    if name == "fresh_public_market_data":
        steps["before_any_frame"] = gate()
        _non_shadow_connections()[-1].push(kline_frame())
        _non_shadow_connections()[-1].push(ticker_frame("NEARUSDT"))
        await _settle()
        steps["after_frames"] = gate()
    elif name == "reconnect":
        clock = _Clock()
        health = getattr(client, "market_data_health", None)
        if health is not None:
            health._monotonic = clock
        conn = _non_shadow_connections()[-1]
        conn.push(kline_frame())
        await _settle()
        steps["connected_fresh"] = gate()
        conn.push(_DISCONNECT)
        await _wait_connections(2)  # real _ws_loop reconnect (1s backoff)
        clock.t += 121.0
        steps["reconnected_no_frame_stale"] = gate()
        _non_shadow_connections()[-1].push(ticker_frame())
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
        # Trading market-data connections. The BBO cost shadow opens a separate,
        # observation-only /public @bookTicker connection, reported on its own.
        "ws_urls": [c.url.split("?")[0] for c in FakeBinanceWs.connections
                    if not c.url.split("?")[0].endswith("/public/stream")],
        "shadow_ws_urls": [c.url for c in FakeBinanceWs.connections
                           if c.url.split("?")[0].endswith("/public/stream")],
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


async def _private_scenario(name, client, engine, binance, steps) -> dict:
    import builtins

    from bot.order_state import OrderState
    from bot.prelive_readonly_probe import _private_ws_probe

    keys = iter(f"lk{n:04d}" + "x" * 56 for n in range(1, 100))

    async def fake_listen_key(method):
        # POST issues/returns the active key; PUT keeps it alive. Never real.
        return {"listenKey": next(keys)} if method == "POST" else {}

    client._listen_key_request = fake_listen_key
    steps["probe_result"] = await _private_ws_probe(client, "ETHUSDT")
    probe_url = FakeBinanceWs.connections[-1].url
    FakeBinanceWs.connections.clear()

    task = engine.client.start_private_websocket(engine.orders, ["SUIUSDT"])
    steps["private_task_started"] = task is not None
    await _wait_connections(1)
    order, _ = engine.orders.get_or_create("bgx7-harness-0001", "SUIUSDT", "Buy", 10.0)
    order.transition(OrderState.SUBMITTING, source="REST")
    order.transition(OrderState.SUBMITTED, order_id="9001", source="REST")
    engine.orders.index_order_id("9001", order.client_oid)
    conn = FakeBinanceWs.connections[-1]
    conn.push(order_update_frame(order.client_oid, "PARTIALLY_FILLED", filled="4", avg="1.05"))
    conn.push(order_update_frame(order.client_oid, "FILLED", filled="10", avg="1.05"))
    await _settle()
    steps["order_state_after_ws_fill"] = order.state.value

    health = getattr(client, "private_stream_health", None)
    if name == "private_reconnect" and health is not None:
        steps["check_before_drop"] = list(health.check())
        conn.push(_DISCONNECT)
        for _ in range(400):
            if len(FakeBinanceWs.connections) >= 2 and health.state == "CONNECTED":
                break
            await asyncio.sleep(0.01)
        await _settle()
        steps["epoch_after_reconnect"] = health.connection_epoch
        steps["check_after_reconnect"] = list(health.check())
        steps["order_state_after_reconnect"] = order.state.value
    snap = health.snapshot() if health is not None else None
    out = {
        "scenario": name,
        "bootstrap_installed": bool(getattr(builtins, "_nexus_runtime_bootstrap_installed", False)),
        "client_type": f"{type(client).__module__}.{type(client).__qualname__}",
        "engine_client_is_client": engine.client is client,
        "private_urls": [c.url.rsplit("/", 1)[0] if "/ws/" in c.url else c.url.split("?")[0]
                         for c in FakeBinanceWs.connections],
        "probe_url_prefix": probe_url.rsplit("/", 1)[0] if "/ws/" in probe_url else probe_url.split("?")[0],
        "private_handler_qualname": binance.BinanceClient._handle_private_order_event.__qualname__,
        "private_health": None if snap is None else {
            k: snap[k] for k in ("state", "events_total", "last_event", "route")
        },
        "private_health_is_shared": (
            health is not None
            and getattr(engine.client, "private_stream_health", None) is health
        ),
        "steps": steps,
    }
    tasks = [t for t in (getattr(client, "_private_ws_task", None),) if t]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    out["private_task_cancelled_cleanly"] = all(t.done() for t in tasks)
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
