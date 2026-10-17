"""Authenticated, read-only capital snapshot reader for NEXUS-7.

This module centralizes the venue account-capital read needed by the new risk
path. It never performs exchange mutation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from bot.professional_risk import CapitalState, capital_state_from_account_overview


@dataclass(frozen=True)
class AccountCapitalSnapshot:
    capital: CapitalState
    raw: Mapping[str, Any]


async def read_account_capital(client, currency: str = "USDT") -> AccountCapitalSnapshot:
    """Read and normalize account capital from the ACTIVE venue, fail-closed.

    The existing `get_balance()` API only returns availableBalance. Professional
    risk management requires the full account overview so equity cannot be
    confused with free collateral.

    Venue rule (same as ``account_balance_semantics.read_account_state``): a
    client exposing ``get_account_state()`` (Binance USD-M: ``/fapi/v3/account``
    normalized to accountEquity/availableMargin/positionMargin/orderMargin/
    unrealisedPNL) is authoritative. Only clients without it use the KuCoin
    ``/api/v1/account-overview`` endpoint. Previously the KuCoin endpoint was
    called unconditionally, so on Binance every NEXUS approval failed here with
    HTTP 404 and was logged as ``decision_source=exception``.
    """
    venue_reader = getattr(client, "get_account_state", None)
    if callable(venue_reader):
        data = await venue_reader()
    else:
        data = await client._get(
            "/api/v1/account-overview",
            {"currency": currency},
            auth=True,
        )
    if not isinstance(data, Mapping):
        raise RuntimeError("account overview unavailable")
    capital = capital_state_from_account_overview(data)
    return AccountCapitalSnapshot(capital=capital, raw=data)
