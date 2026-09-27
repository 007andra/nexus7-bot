"""Bridge Binance public market-data freshness into the Pilot Guard contract.

The Pilot Guard intentionally fails closed unless the active exchange client
publishes ``_last_ws_update``.  The legacy KuCoin client already exposes that
contract; the Binance USD-M adapter consumed and cached public websocket data
without publishing the timestamp, so valid LIVE candidates were blocked as if
no market data had ever arrived.

This overlay does not create market data and does not weaken freshness limits.
It records wall-clock receipt time only after the canonical Binance websocket
handler successfully processes a recognized public market-data event.
"""
from __future__ import annotations

import time

_INSTALLED = False


def install(BinanceClient, log) -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    original = BinanceClient._handle_ws_message

    async def _handle_ws_message_with_freshness(self, message: dict):
        data = (
            message.get("data", message)
            if isinstance(message, dict)
            else {}
        )
        event = data.get("e") if isinstance(data, dict) else None

        await original(self, message)

        # Only real public websocket events that the Binance adapter recognizes
        # can advance the Pilot Guard freshness clock. REST cache seeding,
        # malformed frames and private user-data events cannot satisfy this gate.
        if event in {"kline", "24hrTicker"}:
            self._last_ws_update = time.time()

    BinanceClient._handle_ws_message = _handle_ws_message_with_freshness
    _INSTALLED = True
    log.info(
        "[BINANCE_MARKET_DATA_FRESHNESS] authority=public_ws "
        "pilot_contract=_last_ws_update freshness_limit_unchanged=true"
    )
