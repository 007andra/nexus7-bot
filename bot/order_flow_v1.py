"""Aggressor-side order-flow diagnostics for Binance-style trades.

For Binance aggTrade/trade semantics, buyer_is_maker=True means the aggressive
side was SELL; False means aggressive BUY. Analytics only.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable


@dataclass(frozen=True)
class AggressorTrade:
    timestamp: float
    price: float
    qty: float
    buyer_is_maker: bool

    def validate(self) -> "AggressorTrade":
        if not all(
            math.isfinite(float(value))
            for value in (self.timestamp, self.price, self.qty)
        ):
            raise ValueError("non-finite trade")
        if self.price <= 0 or self.qty <= 0:
            raise ValueError("price/qty must be positive")
        return self

    @property
    def aggressive_side(self) -> str:
        return "SELL" if self.buyer_is_maker else "BUY"


def order_flow_snapshot(
    trades: Iterable[AggressorTrade],
) -> dict[str, object]:
    vals = sorted(
        (trade.validate() for trade in trades),
        key=lambda trade: trade.timestamp,
    )
    if not vals:
        raise ValueError("trades required")

    buy_volume = sum(
        trade.qty for trade in vals if trade.aggressive_side == "BUY"
    )
    sell_volume = sum(
        trade.qty for trade in vals if trade.aggressive_side == "SELL"
    )
    total = buy_volume + sell_volume
    cvd = buy_volume - sell_volume
    imbalance = cvd / total if total > 0 else 0.0
    first = vals[0].price
    last = vals[-1].price
    price_move_bps = (last / first - 1.0) * 10_000.0

    dominant_side = (
        "BUY"
        if cvd > 0
        else "SELL"
        if cvd < 0
        else "BALANCED"
    )
    # Absorption proxy: strong one-sided aggression with limited price progress.
    absorption_proxy = (
        abs(imbalance) >= 0.35 and abs(price_move_bps) <= 5.0
    )
    return {
        "trade_count": len(vals),
        "aggressive_buy_volume": buy_volume,
        "aggressive_sell_volume": sell_volume,
        "cvd": cvd,
        "volume_imbalance": imbalance,
        "price_move_bps": price_move_bps,
        "dominant_aggressor": dominant_side,
        "absorption_proxy": absorption_proxy,
        "proxy_only": True,
        "promotion_authority": False,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }


def cvd_series(
    trades: Iterable[AggressorTrade],
) -> tuple[tuple[float, float], ...]:
    vals = sorted(
        (trade.validate() for trade in trades),
        key=lambda trade: trade.timestamp,
    )
    cumulative = 0.0
    output = []
    for trade in vals:
        cumulative += (
            trade.qty if trade.aggressive_side == "BUY" else -trade.qty
        )
        output.append((trade.timestamp, cumulative))
    return tuple(output)
