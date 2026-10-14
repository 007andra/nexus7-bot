"""PRE-LIVE read-only private-stream probe helpers.

Never submits/cancels orders, changes leverage/stops, or alters Railway state.
Proves private WebSocket authentication with the Binance user-data stream
handshake and normalizes active-order listings. No trading mutation is
performed.
"""
import asyncio


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
    # No listen-key capability means no provable private stream: fail closed.
    return False
