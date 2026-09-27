"""One-shot PRE-LIVE read-only reconciliation probe.

Never submits/cancels orders, changes leverage/stops, or alters Railway state.
It authenticates read-only REST, checks real positions/open orders, and proves
private WebSocket authentication using the active exchange's native user-data
stream handshake. No trading mutation is performed.
"""
import asyncio
import json
import sys
import time

from bot.kucoin import KuCoinClient, REST_BASE as KUCOIN_REST_BASE, to_kucoin


def _active_orders(data):
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("items", "dataList", "orders", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


async def _kucoin_private_ws_probe(client, symbol: str) -> bool:
    """Authenticate KuCoin bullet-private and receive subscription ack."""
    import websockets

    await client._ensure_session()
    endpoint = "/api/v1/bullet-private"
    headers = client._auth_headers("POST", endpoint, "")
    async with client._session.post(
        KUCOIN_REST_BASE + endpoint, headers=headers
    ) as response:
        payload = await response.json(content_type=None)
    if not isinstance(payload, dict) or payload.get("code") != "200000":
        return False
    data = payload.get("data") or {}
    token = data.get("token") or ""
    servers = data.get("instanceServers") or []
    if not token or not servers:
        return False
    ws_endpoint = servers[0].get("endpoint")
    if not ws_endpoint:
        return False

    ws_url = (
        f"{ws_endpoint}?token={token}"
        f"&connectId=bgx7-prelive-{int(time.time())}"
    )
    async with websockets.connect(
        ws_url, ping_interval=None, close_timeout=5
    ) as ws:
        try:
            await asyncio.wait_for(ws.recv(), timeout=5)
        except asyncio.TimeoutError:
            return False
        request_id = str(int(time.time() * 1_000_000))
        await ws.send(json.dumps({
            "id": request_id,
            "type": "subscribe",
            "topic": f"/contractMarket/tradeOrders:{to_kucoin(symbol)}",
            "privateChannel": True,
            "response": True,
        }))
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(
                    ws.recv(), timeout=max(0.2, deadline - time.time())
                )
            except asyncio.TimeoutError:
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("type") == "ack" and str(msg.get("id")) == request_id:
                return True
            if msg.get("type") == "error":
                return False
    return False


async def _binance_private_ws_probe(client, symbol: str) -> bool:
    """Read-only transport probe of the Binance USD-M user-data stream.

    Binance user-data streams need no subscription message: authorization is
    a listenKey confirmed by REST (POST /fapi/v1/listenKey). This probe proves
    only that such a key is accepted on the routed ``/private`` path and that
    the socket answers ping/pong. It does NOT prove event delivery (ping/pong
    also succeeds on the legacy unrouted ``/ws/<listenKey>`` path, which no
    longer pushes user-data events). Live event capability is owned by
    ``BinanceClient.private_stream_health``.
    """
    import websockets
    from bot.binance import WS_PRIVATE_BASE

    data = await client._listen_key_request("POST")
    listen_key = str((data or {}).get("listenKey", "") or "")
    if not listen_key:
        return False

    async with websockets.connect(
        f"{WS_PRIVATE_BASE}/ws/{listen_key}",
        ping_interval=None,
        close_timeout=5,
        max_queue=16,
    ) as ws:
        pong_waiter = await ws.ping()
        await asyncio.wait_for(pong_waiter, timeout=5.0)
        return True


async def _private_ws_probe(client, symbol: str) -> bool:
    """Dispatch private WS probe to the exchange-native protocol."""
    if callable(getattr(client, "_listen_key_request", None)):
        return await _binance_private_ws_probe(client, symbol)
    return await _kucoin_private_ws_probe(client, symbol)


async def run_probe() -> int:
    client = KuCoinClient()
    try:
        if not await client.sync_time():
            print("[PRELIVE_READONLY_PROBE] result=FAIL reason=time_sync")
            return 2
        balance = await client.get_balance()
        instruments = await client.load_instruments()
        positions = await client.get_positions()
        orders_raw = await client._get("/api/v1/orders", {"status": "active"}, auth=True)
        orders = _active_orders(orders_raw)
        probe_symbol = "ETHUSDT" if "ETHUSDT" in instruments else next(iter(instruments), "")
        ws_ok = bool(probe_symbol) and await _private_ws_probe(client, probe_symbol)

        unprotected = [p for p in positions if abs(float(p.get("size", 0) or 0)) > 0 and float(p.get("stopLoss", 0) or 0) <= 0]
        blocked = bool(positions or orders or unprotected or not ws_ok)
        result = "BLOCKED" if blocked else "PASS"
        print(
            f"[PRELIVE_READONLY_PROBE] result={result} balance={balance:.4f} "
            f"positions={len(positions)} active_orders={len(orders)} "
            f"unprotected_positions={len(unprotected)} private_ws={'PASS' if ws_ok else 'FAIL'}"
        )
        return 3 if blocked else 0
    except Exception as exc:
        print(f"[PRELIVE_READONLY_PROBE] result=FAIL reason={type(exc).__name__}")
        return 2
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(run_probe()))
