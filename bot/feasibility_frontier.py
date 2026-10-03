"""NEXUS Feasibility Frontier v1 (RESEARCH ONLY - never imported by the runtime).

Answers: for a symbol's exchange filters, price, stop distance and gross R:R,
does a candidate pass simultaneously

    MIN_ORDER_FEASIBILITY + NEXUS EV + NEXUS net R:R + FINAL_SIZING + margin cap

at a given equity, without changing any threshold?

No formula is re-implemented by hand: MIN_ORDER / FINAL_SIZING arithmetic is
``sizing_decomposition.decompose`` (the runtime's exact Decimal mirror of
``stop_risk_size``) and EV / net R:R is ``nexus_ai.expected_value`` called with
the same cost split the live calibration passes (taker fee, one-way slippage).
The NEXUS net R:R floor is read from the runtime config, never hard-coded.

decision_effect=NONE execution_effect=NONE: pure functions, no I/O, no
exchange or database access, no runtime registration.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal
import math
import os

from bot.config import cfg
from bot.sizing_decomposition import decompose

MAJORS = ("BTC", "ETH", "SOL")


def nexus_min_rr_net() -> float:
    """Same expression as ``nexus_ai.decide`` (NEXUS_MIN_RR_NET or 0.8*MIN_RR_RATIO)."""
    return float(os.environ.get("NEXUS_MIN_RR_NET", str(round(cfg.MIN_RR_RATIO * 0.80, 2))))


def sizing_slippage_allowance(cost: "CostModel") -> float:
    """Round-trip slippage used by MIN_ORDER / RiskManagerV3 sizing.

    Mirrors ``PlannedRisk.slippage`` / ``min_order_feasibility._stress_slippage``:
    never below NEXUS_EXPECTED_SLIPPAGE_PCT, even when a cheaper cost is modelled.
    """
    configured = float(os.environ.get("NEXUS_EXPECTED_SLIPPAGE_PCT", "0.001"))
    return max(configured, 2.0 * cost.slippage_per_side)


def heuristic_win_probability(confidence: float) -> float:
    from bot.nexus_probability import heuristic_win_probability as hwp
    return hwp(confidence)


@dataclass(frozen=True)
class CostModel:
    """Per-side taker fee and per-side slippage (entry == exit)."""
    taker_fee: float
    slippage_per_side: float

    @property
    def round_trip(self) -> float:
        return 2.0 * self.taker_fee + 2.0 * self.slippage_per_side

    @classmethod
    def static_for(cls, symbol: str, taker_fee: float = 0.0005,
                   base_slippage: float = 0.0005) -> "CostModel":
        """Runtime static fallback: majors 1x, alts 2x ``DEFAULT_SLIPPAGE``."""
        major = any(m in str(symbol).upper() for m in MAJORS)
        return cls(taker_fee, base_slippage if major else 2.0 * base_slippage)


@dataclass(frozen=True)
class Instrument:
    symbol: str
    step: str
    min_qty: str
    min_notional: str
    provenance: str  # OBSERVED | INFERRED | ASSUMED

    def info(self) -> dict:
        return {"quantityUnit": "BASE_ASSET", "qtyStep": self.step, "minQty": self.min_qty,
                "minNotional": self.min_notional, "multiplier": 1.0, "tickSize": "0.0001"}


@dataclass(frozen=True)
class CellResult:
    symbol: str
    equity: float
    gross_rr: float
    stop_pct: float
    min_valid_qty: float
    min_valid_notional: float
    risk_budget: float
    risk_at_min_qty: float
    margin_at_min_qty: float
    max_stop_pct_min_order: float
    min_stop_pct_nexus_rr: float | None
    rr_net: float
    ev_pct: float
    min_order_ok: bool
    nexus_rr_ok: bool
    nexus_ev_ok: bool
    final_sizing_ok: bool
    margin_ok: bool

    @property
    def all_ok(self) -> bool:
        return (self.min_order_ok and self.nexus_rr_ok and self.nexus_ev_ok
                and self.final_sizing_ok and self.margin_ok)

    def as_dict(self) -> dict:
        out = asdict(self)
        out["all_ok"] = self.all_ok
        return out


def min_stop_pct_for_rr(gross_rr: float, cost: float, rr_floor: float) -> float | None:
    """Smallest stop (fraction) with (R*s - c)/(s + c) >= floor; None if unreachable."""
    if gross_rr <= rr_floor:
        return None
    return cost * (1.0 + rr_floor) / (gross_rr - rr_floor)


def min_stop_pct_for_ev(gross_rr: float, cost: float, win_prob: float) -> float | None:
    """Smallest stop (fraction) with p*(R*s - c) - (1-p)*(s + c) > 0."""
    edge = win_prob * gross_rr - (1.0 - win_prob)
    if edge <= 0:
        return None
    return cost / edge


def evaluate(instrument: Instrument, *, price: float, stop_pct: float, gross_rr: float,
             equity: float, risk_pct: float, cost: CostModel, win_prob: float,
             leverage: float | None = None, max_margin_pct: float = 1.0,
             available: float | None = None) -> CellResult:
    """One frontier cell, side-agnostic (LONG geometry; SHORT is symmetric)."""
    from bot import nexus_ai

    if not (math.isfinite(price) and price > 0 and 0 < stop_pct < 1 and gross_rr > 0):
        raise ValueError("invalid frontier geometry")
    lev = float(cfg.LEVERAGE if leverage is None else leverage)
    avail = float(equity if available is None else available)
    entry = Decimal(str(price))
    stop = entry * (Decimal(1) - Decimal(str(stop_pct)))
    target = float(entry) * (1.0 + stop_pct * gross_rr)
    d = decompose(
        info=instrument.info(), equity=equity, available=avail, entry=entry, stop=stop,
        risk_pct=risk_pct, leverage=lev, max_margin_pct=max_margin_pct,
        fee_rate_per_side=cost.taker_fee, slippage_pct=sizing_slippage_allowance(cost),
    )
    if d.get("result") == "BLOCK" and d.get("reason") in {"INVALID_METADATA", "INVALID_INPUT"}:
        raise ValueError(f"invalid instrument/input: {d.get('reason')}")
    ev = nexus_ai.expected_value(win_prob, float(entry), float(stop), target,
                                 taker_fee=cost.taker_fee, slippage=cost.slippage_per_side)
    floor = nexus_min_rr_net()
    q = float(d["min_valid_qty"])
    budget = float(d["risk_budget"])
    sizing_cost = 2.0 * cost.taker_fee + sizing_slippage_allowance(cost)
    max_stop = budget / (q * float(entry)) - sizing_cost
    margin_at_min = float(d["margin_at_min_valid_qty"])
    min_order_ok = d["result"] == "PASS"
    sized_margin = float(d["rounded_qty"]) * float(entry) / lev
    return CellResult(
        symbol=instrument.symbol, equity=equity, gross_rr=gross_rr, stop_pct=stop_pct,
        min_valid_qty=q, min_valid_notional=q * float(entry), risk_budget=budget,
        risk_at_min_qty=float(d["risk_at_min_valid_qty"]), margin_at_min_qty=margin_at_min,
        max_stop_pct_min_order=max(0.0, max_stop),
        min_stop_pct_nexus_rr=min_stop_pct_for_rr(gross_rr, cost.round_trip, floor),
        rr_net=float(ev["rr_net"]), ev_pct=float(ev["ev_pct"]),
        min_order_ok=min_order_ok,
        nexus_rr_ok=float(ev["rr_net"]) >= floor,
        nexus_ev_ok=bool(ev["valid"]),
        final_sizing_ok=min_order_ok and float(d["rounded_qty"]) >= q,
        margin_ok=margin_at_min <= avail * max_margin_pct and sized_margin <= avail * max_margin_pct,
    )


def min_equity_for(instrument: Instrument, *, price: float, stop_pct: float,
                   risk_pct: float, cost: CostModel) -> float:
    """Equity at which the smallest valid order fits the stop-risk budget."""
    d = decompose(info=instrument.info(), equity=1.0, available=1.0, entry=Decimal(str(price)),
                  stop=Decimal(str(price)) * (Decimal(1) - Decimal(str(stop_pct))),
                  risk_pct=risk_pct, leverage=cfg.LEVERAGE, max_margin_pct=1.0,
                  fee_rate_per_side=cost.taker_fee, slippage_pct=sizing_slippage_allowance(cost))
    return float(d["risk_at_min_valid_qty"]) / risk_pct


__all__ = ["CellResult", "CostModel", "Instrument", "evaluate", "min_equity_for",
           "min_stop_pct_for_ev", "min_stop_pct_for_rr", "nexus_min_rr_net",
           "sizing_slippage_allowance"]
