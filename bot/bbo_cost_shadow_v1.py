"""BBO cost shadow v1: STATIC_COST vs LIVE_BBO_COST (pure functions, SHADOW ONLY).

Never feeds execution_cost, NEXUS, EV, R:R, sizing or any LIVE path.
shadow_only=true decision_effect=NONE execution_effect=NONE

Cost components are kept separate (all per round trip, bps of price):

* fee        - taker fee, both sides (same rate in static and live);
* spread     - observed: half-spread paid on entry + half-spread on exit;
* impact     - UNPROVEN: top-of-book quantity alone cannot measure depth walk,
               so two live variants are reported and never blended:
               LIVE_BBO_SPREAD_ONLY        = fee + observed spread;
               LIVE_BBO_PLUS_STATIC_IMPACT = fee + observed spread + the
                 runtime's own impact floor (execution_cost.ticker_slippage:
                 1 bp/side majors, 2 bp/side alts) - an assumption, labelled;
* slippage floor - NEXUS_EXPECTED_SLIPPAGE_PCT, used ONLY by MIN_ORDER /
               RiskManagerV3 sizing; reported, never altered;
* static     - the production snapshot (taker + entry slippage + exit slippage).

The NEXUS comparison reuses the exact algebra of ``nexus_ai.expected_value``
(cost = 2*fee + entry_slip + exit_slip, gain/loss net of cost) and the runtime
net R:R floor; parity is pinned by tests. ``shadow_allowed`` is a
counterfactual of the EV/net-R:R gate only (``decision_scope=EV_RR_GATE``):
later NEXUS stages are not re-run and the champion decision is untouched.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import math
import os

MAJORS = ("BTC", "ETH", "SOL")
IMPACT_MODEL = "UNPROVEN"


def impact_floor_per_side(symbol: str) -> float:
    """Same assumption as execution_cost.ticker_slippage (labelled hypothesis)."""
    return 0.00010 if any(m in str(symbol).upper() for m in MAJORS) else 0.00020


def sizing_slippage_floor() -> float:
    return float(os.environ.get("NEXUS_EXPECTED_SLIPPAGE_PCT", "0.001"))


def _finite_pos(value, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    out = float(value)
    if not math.isfinite(out) or out <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return out


@dataclass(frozen=True)
class BookMetrics:
    mid: float
    spread_abs: float
    spread_bps: float
    half_spread_bps: float


def book_metrics(bid, ask) -> BookMetrics:
    b, a = _finite_pos(bid, "bid"), _finite_pos(ask, "ask")
    if a < b:
        raise ValueError("crossed book")
    mid = (a + b) / 2.0
    spread_abs = a - b
    spread_bps = spread_abs / mid * 10_000.0
    return BookMetrics(mid, spread_abs, spread_bps, spread_bps / 2.0)


@dataclass(frozen=True)
class CostBreakdown:
    static_fee_bps: float          # round trip
    static_slippage_bps: float     # entry + exit
    static_total_cost_bps: float
    live_fee_bps: float            # round trip (same taker rate)
    live_spread_bps: float | None  # full quoted spread
    live_half_spread_bps: float | None
    estimated_impact_bps: float | None  # None == NA (UNPROVEN)
    impact_model: str
    live_spread_only_cost_bps: float | None
    live_plus_static_impact_cost_bps: float | None
    sizing_slippage_floor_bps: float


def cost_breakdown(*, symbol: str, taker_fee: float, entry_slippage: float,
                   exit_slippage: float, book: BookMetrics | None) -> CostBreakdown:
    fee = float(taker_fee)
    if not (math.isfinite(fee) and 0 <= fee < 0.05):
        raise ValueError("invalid taker fee")
    s_in, s_out = float(entry_slippage), float(exit_slippage)
    if not all(math.isfinite(x) and 0 <= x < 0.05 for x in (s_in, s_out)):
        raise ValueError("invalid static slippage")
    static_fee = 2 * fee * 1e4
    static_slip = (s_in + s_out) * 1e4
    if book is None:
        live_spread = live_half = spread_only = plus_impact = None
    else:
        live_spread, live_half = book.spread_bps, book.half_spread_bps
        spread_only = static_fee + 2 * live_half
        plus_impact = spread_only + 2 * impact_floor_per_side(symbol) * 1e4
    return CostBreakdown(
        static_fee_bps=static_fee, static_slippage_bps=static_slip,
        static_total_cost_bps=static_fee + static_slip,
        live_fee_bps=static_fee, live_spread_bps=live_spread, live_half_spread_bps=live_half,
        estimated_impact_bps=None, impact_model=IMPACT_MODEL,
        live_spread_only_cost_bps=spread_only, live_plus_static_impact_cost_bps=plus_impact,
        sizing_slippage_floor_bps=sizing_slippage_floor() * 1e4,
    )


def top_book_coverage(side: str, qty: float | None, book_bid_qty: float,
                      book_ask_qty: float) -> float | None:
    """Displayed top-of-book qty / order qty on the taking side. NOT an impact measure."""
    if qty is None:
        return None
    q = float(qty)
    if not math.isfinite(q) or q <= 0:
        return None
    top = book_ask_qty if str(side).upper() in {"LONG", "BUY"} else book_bid_qty
    return float(top) / q


@dataclass(frozen=True)
class GateResult:
    rr_net: float
    ev_pct: float
    allowed: bool


def nexus_ev_rr_gate(*, entry: float, stop: float, target: float, win_prob: float | None,
                     round_trip_cost_bps: float, rr_floor: float) -> GateResult | None:
    """Exact algebra of nexus_ai.expected_value + the net R:R floor.

    Returns None when the win probability is unknown (EV cannot be judged).
    """
    e, s, t = (_finite_pos(entry, "entry"), _finite_pos(stop, "stop"),
               _finite_pos(target, "target"))
    if win_prob is None:
        return None
    p = max(0.0, min(1.0, float(win_prob)))
    cost = float(round_trip_cost_bps) / 1e4
    gain_net = abs(t - e) / e - cost
    loss_net = abs(e - s) / e + cost
    if loss_net <= 0:
        return None
    ev = p * gain_net - (1 - p) * loss_net
    rr = gain_net / loss_net
    return GateResult(rr_net=rr, ev_pct=ev * 100.0, allowed=(ev > 0 and rr >= rr_floor))


@dataclass(frozen=True)
class ShadowRecord:
    candidate_id: str
    symbol: str
    side: str
    setup: str
    entry: float
    stop: float
    target: float
    gross_rr: float
    confidence: float | None
    bid: float | None
    ask: float | None
    mid: float | None
    spread_bps: float | None
    bbo_valid: bool
    bbo_reason: str
    bbo_age_ms: int | None
    bbo_generation: int | None
    bbo_update_id: int | None
    costs: CostBreakdown | None
    rr_net_static: float | None
    rr_net_live: float | None
    rr_net_live_spread_only: float | None
    ev_static: float | None
    ev_live: float | None
    static_allowed: bool | None
    shadow_allowed: bool | None
    champion_decision: str
    champion_execution_allowed: bool
    champion_rr_net: float | None
    static_parity: bool | None
    status: str                     # OK | SHADOW_DATA_UNAVAILABLE | BBO_<reason>

    @property
    def would_change_decision(self) -> bool | None:
        if self.static_allowed is None or self.shadow_allowed is None:
            return None
        return self.static_allowed != self.shadow_allowed

    def as_row(self) -> dict:
        row = asdict(self)
        row["would_change_decision"] = self.would_change_decision
        return row


def _f(value, digits=3) -> str:
    if value is None:
        return "NA"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def format_record(r: ShadowRecord) -> str:
    c = r.costs
    return (
        f"[COST_SHADOW_BBO] candidate_id={r.candidate_id} symbol={r.symbol} side={r.side} "
        f"setup={r.setup} status={r.status} bid={_f(r.bid, 8)} ask={_f(r.ask, 8)} "
        f"mid={_f(r.mid, 8)} spread_bps={_f(r.spread_bps)} bbo_age_ms={_f(r.bbo_age_ms)} "
        f"bbo_valid={_f(r.bbo_valid)} bbo_reason={r.bbo_reason} "
        f"static_fee_bps={_f(c.static_fee_bps if c else None)} "
        f"static_slippage_bps={_f(c.static_slippage_bps if c else None)} "
        f"static_total_cost_bps={_f(c.static_total_cost_bps if c else None)} "
        f"live_spread_bps={_f(c.live_spread_bps if c else None)} "
        f"live_half_spread_bps={_f(c.live_half_spread_bps if c else None)} "
        f"estimated_impact_bps=NA impact_model={IMPACT_MODEL} "
        f"live_fee_bps={_f(c.live_fee_bps if c else None)} "
        f"live_spread_only_cost_bps={_f(c.live_spread_only_cost_bps if c else None)} "
        f"live_total_cost_bps={_f(c.live_plus_static_impact_cost_bps if c else None)} "
        f"sizing_slippage_floor_bps={_f(c.sizing_slippage_floor_bps if c else None)} "
        f"gross_rr={_f(r.gross_rr)} confidence={_f(r.confidence, 2)} "
        f"rr_net_static={_f(r.rr_net_static)} rr_net_live={_f(r.rr_net_live)} "
        f"rr_net_live_spread_only={_f(r.rr_net_live_spread_only)} "
        f"ev_static={_f(r.ev_static, 4)} ev_live={_f(r.ev_live, 4)} "
        f"static_allowed={_f(r.static_allowed)} shadow_allowed={_f(r.shadow_allowed)} "
        f"would_change_decision={_f(r.would_change_decision)} decision_scope=EV_RR_GATE "
        f"champion_decision={r.champion_decision} champion_rr_net={_f(r.champion_rr_net)} "
        f"static_parity={_f(r.static_parity)} "
        f"shadow_only=true decision_effect=NONE execution_effect=NONE"
    )


__all__ = ["BookMetrics", "CostBreakdown", "GateResult", "ShadowRecord", "book_metrics",
           "cost_breakdown", "format_record", "impact_floor_per_side", "nexus_ev_rr_gate",
           "top_book_coverage", "IMPACT_MODEL"]
