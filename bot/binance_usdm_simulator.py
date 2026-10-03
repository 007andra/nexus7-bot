"""Binance USD-M research simulator primitives.

Models exchange filters, depth/VWAP market fills, fees, funding, conservative
maintenance brackets, cross-margin liquidation geometry, STOP_MARKET gaps and
STOP_FIRST same-bar ambiguity. It has no exchange client and no LIVE authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
import math
from typing import Iterable, Sequence


@dataclass(frozen=True)
class SymbolRules:
    tick_size: float
    qty_step: float
    min_qty: float
    min_notional: float

    def validate(self) -> "SymbolRules":
        values = (
            self.tick_size,
            self.qty_step,
            self.min_qty,
            self.min_notional,
        )
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("non-finite symbol rules")
        if self.tick_size <= 0 or self.qty_step <= 0 or self.min_qty <= 0:
            raise ValueError("tick/step/min_qty must be positive")
        if self.min_notional < 0:
            raise ValueError("min_notional cannot be negative")
        return self


@dataclass(frozen=True)
class FillResult:
    requested_qty: float
    filled_qty: float
    average_price: float | None
    best_price: float | None
    impact_bps: float | None
    partial: bool


@dataclass(frozen=True)
class SimBar:
    timestamp: float
    open: float
    high: float
    low: float
    close: float

    def validate(self) -> "SimBar":
        vals = (self.timestamp, self.open, self.high, self.low, self.close)
        if not all(math.isfinite(float(v)) for v in vals):
            raise ValueError("non-finite bar")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("bar prices must be positive")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("invalid bar high")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("invalid bar low")
        return self


@dataclass(frozen=True)
class FundingEvent:
    timestamp: float
    rate: float
    mark_price: float

    def validate(self) -> "FundingEvent":
        if not all(
            math.isfinite(float(v))
            for v in (self.timestamp, self.rate, self.mark_price)
        ):
            raise ValueError("non-finite funding event")
        if self.mark_price <= 0:
            raise ValueError("mark_price must be positive")
        return self


@dataclass(frozen=True)
class TradeSimulation:
    side: str
    qty: float
    entry_fill: float
    exit_fill: float
    exit_reason: str
    gross_pnl: float
    fees: float
    funding_pnl: float
    net_pnl: float
    risk_usdt: float
    net_r: float | None
    hold_seconds: float
    liquidation_price: float | None
    decision_effect: str = "NONE"
    execution_effect: str = "NONE"


def _d(value: float) -> Decimal:
    return Decimal(str(float(value)))


def floor_to_step(value: float, step: float) -> float:
    if value < 0 or step <= 0:
        raise ValueError("invalid step normalization")
    units = (_d(value) / _d(step)).to_integral_value(rounding=ROUND_DOWN)
    return float(units * _d(step))


def normalize_market_qty(qty: float, price: float, rules: SymbolRules) -> float:
    rules.validate()
    if not math.isfinite(float(qty)) or not math.isfinite(float(price)):
        raise ValueError("non-finite order")
    if qty <= 0 or price <= 0:
        raise ValueError("qty and price must be positive")
    normalized = floor_to_step(qty, rules.qty_step)
    if normalized < rules.min_qty:
        raise ValueError("MIN_QTY")
    if normalized * price < rules.min_notional:
        raise ValueError("MIN_NOTIONAL")
    return normalized


def normalize_price_down(price: float, rules: SymbolRules) -> float:
    rules.validate()
    if price <= 0 or not math.isfinite(float(price)):
        raise ValueError("invalid price")
    return floor_to_step(price, rules.tick_size)


def _book_levels(orderbook: dict, order_side: str) -> list[tuple[float, float]]:
    key = "a" if order_side.upper() == "BUY" else "b"
    raw = orderbook.get(key, []) if isinstance(orderbook, dict) else []
    levels: list[tuple[float, float]] = []
    for row in raw:
        try:
            price, qty = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price > 0 and qty > 0 and math.isfinite(price) and math.isfinite(qty):
            levels.append((price, qty))
    levels.sort(key=lambda x: x[0], reverse=order_side.upper() == "SELL")
    return levels


def vwap_market_fill(orderbook: dict, order_side: str, qty: float) -> FillResult:
    side = order_side.upper()
    if side not in {"BUY", "SELL"} or qty <= 0:
        raise ValueError("invalid market fill request")
    levels = _book_levels(orderbook, side)
    if not levels:
        return FillResult(qty, 0.0, None, None, None, True)

    remaining = float(qty)
    filled = 0.0
    notional = 0.0
    best = levels[0][0]
    for price, available in levels:
        take = min(remaining, available)
        filled += take
        notional += take * price
        remaining -= take
        if remaining <= 1e-15:
            break

    average = notional / filled if filled > 0 else None
    if average is None:
        impact = None
    elif side == "BUY":
        impact = (average / best - 1.0) * 10_000.0
    else:
        impact = (1.0 - average / best) * 10_000.0
    return FillResult(
        requested_qty=float(qty),
        filled_qty=filled,
        average_price=average,
        best_price=best,
        impact_bps=impact,
        partial=filled + 1e-12 < float(qty),
    )


def adverse_fill(price: float, order_side: str, slippage_rate: float) -> float:
    if price <= 0 or slippage_rate < 0:
        raise ValueError("invalid adverse fill")
    side = order_side.upper()
    if side == "BUY":
        return price * (1.0 + slippage_rate)
    if side == "SELL":
        return price * (1.0 - slippage_rate)
    raise ValueError("order_side must be BUY or SELL")


def mark_to_market_pnl(
    entry: float, mark: float, qty: float, position_side: str
) -> float:
    direction = 1.0 if position_side.upper() == "LONG" else -1.0
    return direction * (float(mark) - float(entry)) * float(qty)


def funding_cashflow(
    position_side: str, qty: float, mark_price: float, rate: float
) -> float:
    notional = abs(float(qty) * float(mark_price))
    if position_side.upper() == "LONG":
        return -notional * float(rate)
    if position_side.upper() == "SHORT":
        return notional * float(rate)
    raise ValueError("position_side must be LONG or SHORT")


def select_bracket(payload: dict, notional: float) -> dict:
    if notional <= 0 or not isinstance(payload, dict):
        raise ValueError("invalid bracket request")
    brackets = payload.get("brackets")
    if not isinstance(brackets, list) or not brackets:
        raise ValueError("missing brackets")
    for index, bracket in enumerate(brackets):
        if not isinstance(bracket, dict):
            continue
        floor = float(bracket.get("notionalFloor", -1))
        cap = float(bracket.get("notionalCap", -1))
        is_last = index == len(brackets) - 1
        if floor <= notional < cap or (is_last and floor <= notional <= cap):
            return bracket
    raise ValueError("notional outside brackets")


def conservative_maintenance(payload: dict, notional: float) -> float:
    bracket = select_bracket(payload, notional)
    mmr = float(bracket["maintMarginRatio"])
    if not 0 < mmr < 1:
        raise ValueError("invalid maintenance ratio")
    # Match the LIVE Binance CROSS stress guard: do not infer a cum deduction.
    return float(notional) * mmr


def initial_margin(notional: float, leverage: float) -> float:
    if notional <= 0 or leverage <= 0:
        raise ValueError("invalid initial margin")
    return float(notional) / float(leverage)


def cross_liquidation_price(
    *,
    wallet_balance: float,
    entry: float,
    qty: float,
    position_side: str,
    bracket_payload: dict,
    taker_fee_rate: float,
) -> float | None:
    """Solve the conservative single-position CROSS liquidation boundary.

    Equity includes unrealized PnL after the opening taker fee. Maintenance is
    the same conservative notional*MMR model used by the LIVE cross-stress gate.
    Closing taker fee is included in the unsafe boundary.
    """
    if wallet_balance <= 0 or entry <= 0 or qty <= 0 or taker_fee_rate < 0:
        raise ValueError("invalid liquidation inputs")
    side = position_side.upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("invalid position side")

    wallet_after_open = (
        float(wallet_balance)
        - float(entry) * float(qty) * float(taker_fee_rate)
    )

    def unsafe(mark: float) -> bool:
        notional = max(1e-12, float(mark) * float(qty))
        equity = wallet_after_open + mark_to_market_pnl(
            entry, mark, qty, side
        )
        maintenance = conservative_maintenance(bracket_payload, notional)
        close_fee = notional * float(taker_fee_rate)
        return equity <= maintenance + close_fee

    if unsafe(entry):
        return float(entry)

    if side == "LONG":
        low, high = max(entry * 1e-9, 1e-12), float(entry)
        if not unsafe(low):
            return None
        for _ in range(80):
            mid = (low + high) / 2.0
            if unsafe(mid):
                low = mid
            else:
                high = mid
        return (low + high) / 2.0

    low, high = float(entry), float(entry) * 2.0
    for _ in range(40):
        if unsafe(high):
            break
        high *= 2.0
    else:
        return None
    for _ in range(80):
        mid = (low + high) / 2.0
        if unsafe(mid):
            high = mid
        else:
            low = mid
    return (low + high) / 2.0


def _stop_triggered(side: str, stop: float, bar: SimBar) -> bool:
    return bar.low <= stop if side == "LONG" else bar.high >= stop


def _target_triggered(side: str, target: float, bar: SimBar) -> bool:
    return bar.high >= target if side == "LONG" else bar.low <= target


def _gap_reference(
    side: str, level: float, bar: SimBar, *, stop: bool
) -> float:
    if side == "LONG":
        if stop and bar.open <= level:
            return bar.open
        if not stop and bar.open >= level:
            return bar.open
    else:
        if stop and bar.open >= level:
            return bar.open
        if not stop and bar.open <= level:
            return bar.open
    return level


def simulate_trade_path(
    *,
    side: str,
    qty: float,
    entry_reference: float,
    stop_loss: float,
    take_profit: float,
    bars: Sequence[SimBar],
    taker_fee_rate: float,
    entry_slippage_rate: float,
    exit_slippage_rate: float,
    funding_events: Iterable[FundingEvent] = (),
    wallet_balance: float | None = None,
    bracket_payload: dict | None = None,
) -> TradeSimulation:
    """Simulate one full-position market entry with STOP_FIRST ambiguity.

    STOP_MARKET and TAKE_PROFIT_MARKET exits use adverse slippage. If a bar opens
    through the liquidation boundary, liquidation wins before the protective
    stop; otherwise a reachable protective stop is assumed to execute before an
    intrabar liquidation boundary.
    """
    position_side = side.upper()
    if position_side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    if qty <= 0 or entry_reference <= 0 or not bars:
        raise ValueError("positive qty/entry and bars required")
    if stop_loss <= 0 or take_profit <= 0:
        raise ValueError("positive stop/take-profit required")
    if taker_fee_rate < 0 or entry_slippage_rate < 0 or exit_slippage_rate < 0:
        raise ValueError("cost rates cannot be negative")

    ordered_bars = [bar.validate() for bar in bars]
    ordered_bars.sort(key=lambda bar: bar.timestamp)
    entry_order_side = "BUY" if position_side == "LONG" else "SELL"
    exit_order_side = "SELL" if position_side == "LONG" else "BUY"
    entry_fill = adverse_fill(
        entry_reference, entry_order_side, entry_slippage_rate
    )
    entry_ts = ordered_bars[0].timestamp

    liquidation = None
    if wallet_balance is not None and bracket_payload is not None:
        liquidation = cross_liquidation_price(
            wallet_balance=wallet_balance,
            entry=entry_fill,
            qty=qty,
            position_side=position_side,
            bracket_payload=bracket_payload,
            taker_fee_rate=taker_fee_rate,
        )

    reason = "OPEN_AT_HORIZON"
    exit_reference = ordered_bars[-1].close
    exit_ts = ordered_bars[-1].timestamp

    for bar in ordered_bars:
        gap_liquidated = (
            liquidation is not None
            and (
                (position_side == "LONG" and bar.open <= liquidation)
                or (position_side == "SHORT" and bar.open >= liquidation)
            )
        )
        if gap_liquidated:
            reason = "LIQUIDATION_GAP"
            exit_reference = bar.open
            exit_ts = bar.timestamp
            break

        stop_hit = _stop_triggered(position_side, stop_loss, bar)
        target_hit = _target_triggered(position_side, take_profit, bar)
        if stop_hit:
            reason = "STOP_MARKET"
            exit_reference = _gap_reference(
                position_side, stop_loss, bar, stop=True
            )
            exit_ts = bar.timestamp
            break
        if target_hit:
            reason = "TAKE_PROFIT_MARKET"
            exit_reference = _gap_reference(
                position_side, take_profit, bar, stop=False
            )
            exit_ts = bar.timestamp
            break

        intrabar_liquidated = (
            liquidation is not None
            and (
                (position_side == "LONG" and bar.low <= liquidation)
                or (position_side == "SHORT" and bar.high >= liquidation)
            )
        )
        if intrabar_liquidated:
            reason = "LIQUIDATION_INTRABAR"
            exit_reference = liquidation
            exit_ts = bar.timestamp
            break

    exit_fill = adverse_fill(
        exit_reference, exit_order_side, exit_slippage_rate
    )
    gross_pnl = mark_to_market_pnl(
        entry_fill, exit_fill, qty, position_side
    )
    fees = (
        entry_fill * qty * taker_fee_rate
        + exit_fill * qty * taker_fee_rate
    )

    funding_pnl = 0.0
    for event in funding_events:
        item = event.validate()
        if entry_ts < item.timestamp <= exit_ts:
            funding_pnl += funding_cashflow(
                position_side,
                qty,
                item.mark_price,
                item.rate,
            )

    net_pnl = gross_pnl - fees + funding_pnl
    risk_usdt = abs(entry_fill - stop_loss) * qty
    net_r = net_pnl / risk_usdt if risk_usdt > 0 else None
    return TradeSimulation(
        side=position_side,
        qty=float(qty),
        entry_fill=entry_fill,
        exit_fill=exit_fill,
        exit_reason=reason,
        gross_pnl=gross_pnl,
        fees=fees,
        funding_pnl=funding_pnl,
        net_pnl=net_pnl,
        risk_usdt=risk_usdt,
        net_r=net_r,
        hold_seconds=max(0.0, exit_ts - entry_ts),
        liquidation_price=liquidation,
    )
