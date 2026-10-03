"""Exchange selection boundary for NEXUS-7.

Keeps the engine decoupled from venue-specific modules.  KuCoin remains the
safe default so existing deployments do not change merely because this module
is imported.  Binance USD-M Futures is selected explicitly with EXCHANGE=binance.

This module exposes only normalized runtime identities; it does not grant LIVE
permission or change any pilot/release gate.
"""
from __future__ import annotations

import os

_raw = os.environ.get("EXCHANGE", "kucoin").strip().lower().replace("-", "_")

if _raw in {"binance", "binance_usdm", "binance_futures", "usdm"}:
    from bot.binance import (
        BinanceClient as ExchangeClient,
        PAPER_TRADE,
        TRADING_MODE_REASON,
        TAKER_FEE,
        MAKER_FEE,
    )
    EXCHANGE_NAME = "binance"
    EXCHANGE_PRODUCT = "USD-M Futures"
elif _raw in {"kucoin", "kucoin_futures"}:
    from bot.kucoin import (
        KuCoinClient as ExchangeClient,
        PAPER_TRADE,
        TRADING_MODE_REASON,
        TAKER_FEE,
        MAKER_FEE,
    )
    EXCHANGE_NAME = "kucoin"
    EXCHANGE_PRODUCT = "Futures"
else:
    raise RuntimeError(
        f"Unsupported EXCHANGE={_raw!r}; expected kucoin or binance"
    )


def _announce_live_venue() -> None:
    """Venue-correct LIVE banner for Binance (bot.kucoin prints its own only
    when KuCoin is the active venue, so each venue is announced exactly once)."""
    if PAPER_TRADE or EXCHANGE_NAME != "binance":
        return
    from bot.logger import log

    log.critical("=" * 62)
    log.critical(
        "🔴 OPERAÇÃO REAL ATIVA — ordens serão enviadas à %s %s",
        EXCHANGE_NAME.capitalize(), EXCHANGE_PRODUCT,
    )
    log.critical("=" * 62)


_announce_live_venue()


def is_binance() -> bool:
    return EXCHANGE_NAME == "binance"


def is_kucoin() -> bool:
    return EXCHANGE_NAME == "kucoin"
