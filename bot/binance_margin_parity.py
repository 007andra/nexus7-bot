"""Binance USD-M MARKET-order collateral parity diagnostics.

This module is intentionally pure: it never authorizes, blocks, sizes, submits,
retries, or cancels an order. It exists to explain differences between the
bot's local affordability estimate and an exchange-side -2019 rejection.
"""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class MarginParity:
    available: float
    qty: float
    reference_price: float
    leverage: float
    taker_fee_rate: float
    market_take_bound: float | None
    current_required: float
    current_headroom: float
    bound_required: float | None
    bound_headroom: float | None
    exhaustion_price: float
    exhaustion_move_fraction: float


def _positive(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return number


def collateral_required(
    *,
    qty: float,
    price: float,
    leverage: float,
    taker_fee_rate: float,
    execution_price_factor: float = 1.0,
) -> float:
    """Local collateral projection: initial margin plus opening taker fee."""
    qty = _positive("qty", qty)
    price = _positive("price", price)
    leverage = _positive("leverage", leverage)
    fee = float(taker_fee_rate)
    factor = _positive("execution_price_factor", execution_price_factor)
    if not math.isfinite(fee) or fee < 0:
        raise ValueError("taker_fee_rate must be nonnegative and finite")
    return qty * price * factor * (1.0 / leverage + fee)


def audit_market_order_margin(
    *,
    available: float,
    qty: float,
    reference_price: float,
    leverage: float,
    taker_fee_rate: float,
    market_take_bound: float | None,
) -> MarginParity:
    """Quantify headroom without asserting an undocumented Binance margin formula.

    market_take_bound is used only as a diagnostic scenario because Binance
    documents it as the maximum MARKET-order price difference from mark price.
    """
    available = _positive("available", available)
    qty = _positive("qty", qty)
    reference_price = _positive("reference_price", reference_price)
    leverage = _positive("leverage", leverage)
    fee = float(taker_fee_rate)
    if not math.isfinite(fee) or fee < 0:
        raise ValueError("taker_fee_rate must be nonnegative and finite")

    current_required = collateral_required(
        qty=qty,
        price=reference_price,
        leverage=leverage,
        taker_fee_rate=fee,
    )
    exhaustion_price = available / (qty * (1.0 / leverage + fee))
    exhaustion_move_fraction = exhaustion_price / reference_price - 1.0

    bound_required = None
    bound_headroom = None
    if market_take_bound is not None:
        bound = float(market_take_bound)
        if not math.isfinite(bound) or bound < 0:
            raise ValueError("market_take_bound must be nonnegative and finite")
        bound_required = collateral_required(
            qty=qty,
            price=reference_price,
            leverage=leverage,
            taker_fee_rate=fee,
            execution_price_factor=1.0 + bound,
        )
        bound_headroom = available - bound_required

    return MarginParity(
        available=available,
        qty=qty,
        reference_price=reference_price,
        leverage=leverage,
        taker_fee_rate=fee,
        market_take_bound=market_take_bound,
        current_required=current_required,
        current_headroom=available - current_required,
        bound_required=bound_required,
        bound_headroom=bound_headroom,
        exhaustion_price=exhaustion_price,
        exhaustion_move_fraction=exhaustion_move_fraction,
    )
