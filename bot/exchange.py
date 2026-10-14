"""Exchange selection boundary for NEXUS-7.

Binance USD-M Futures is the only supported venue. EXCHANGE may be unset or
name Binance; any other value (including the removed KuCoin adapter) fails
closed at import instead of silently selecting a different venue.

This module exposes only normalized runtime identities; it does not grant LIVE
permission or change any pilot/release gate.
"""
from __future__ import annotations

import os

_raw = os.environ.get("EXCHANGE", "binance").strip().lower().replace("-", "_")

if _raw not in {"binance", "binance_usdm", "binance_futures", "usdm"}:
    raise RuntimeError(
        f"Unsupported EXCHANGE={_raw!r}; only binance is supported"
    )

from bot.binance import (  # noqa: E402
    BinanceClient as ExchangeClient,
    PAPER_TRADE,
    TRADING_MODE_REASON,
    TAKER_FEE,
    MAKER_FEE,
)

EXCHANGE_NAME = "binance"
EXCHANGE_PRODUCT = "USD-M Futures"


def _announce_live_venue() -> None:
    """Trading-mode banner, announced exactly once at import."""
    from bot.logger import log

    if PAPER_TRADE:
        # Formerly printed by the KuCoin module on import; kept so a missing
        # or partial LIVE acknowledgement stays loud in the startup log.
        log.critical("=" * 62)
        log.critical("🚫 NENHUMA ORDEM SERÁ ENVIADA À EXCHANGE")
        log.critical("   %s", TRADING_MODE_REASON)
        log.critical("=" * 62)
        return
    log.critical("=" * 62)
    log.critical(
        "🔴 OPERAÇÃO REAL ATIVA — ordens serão enviadas à %s %s",
        EXCHANGE_NAME.capitalize(), EXCHANGE_PRODUCT,
    )
    log.critical("=" * 62)


_announce_live_venue()


def is_binance() -> bool:
    return EXCHANGE_NAME == "binance"

