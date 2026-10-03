"""STATIC_COST vs LIVE_BBO_COST shadow comparison (SHADOW ONLY).

Problem: the Binance USD-M ``<symbol>@ticker`` (24hrTicker) stream carries no
best bid/ask; the runtime writes ``bid=0/ask=0`` into ``_ticker_cache`` and
``execution_cost.build_snapshot`` therefore always falls back to the static
per-symbol slippage (5 bps/side majors, 10 bps/side alts).

Correct BBO sources (Binance USD-M):
- WS ``<symbol>@bookTicker``: pushed on every best bid/ask change
  (fields u, E event time, T transaction time, b/B best bid/qty, a/A best ask/qty);
- REST ``GET /fapi/v1/ticker/bookTicker`` (already used by ``get_ticker``).

This module only *measures*. It never feeds ``execution_cost``, NEXUS, sizing,
MIN_ORDER, CROSS, predispatch or dispatch. Every record carries
``shadow_only=true decision_effect=NONE execution_effect=NONE``.

Cost decomposition (per side, fractions of price):
- half_spread = (ask - bid) / (2 * mid)          -> paid crossing the book;
- estimated_impact = runtime impact floor (1 bp majors, 2 bp alts, the same
  floor ``execution_cost.ticker_slippage`` already uses) while the order
  notional fits the top-of-book size on the taking side; beyond it, each extra
  top-of-book multiple adds one full spread (conservative walk proxy);
- live round trip = 2 * taker + 2 * (half_spread + impact).
Spread is a quote property; impact depends on order size vs displayed depth.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time

MAJORS = ("BTC", "ETH", "SOL")
DEFAULT_MAX_AGE_MS = 2_000


@dataclass(frozen=True)
class BBOSnapshot:
    symbol: str
    bid: float
    ask: float
    bid_qty: float
    ask_qty: float
    event_ms: int          # exchange event/transaction time (E or T)
    received_ms: int       # local receive time

    def validate(self, *, now_ms: int | None = None,
                 max_age_ms: int = DEFAULT_MAX_AGE_MS) -> tuple[bool, str]:
        """Fail-closed freshness/sanity check for telemetry use."""
        values = (self.bid, self.ask, self.bid_qty, self.ask_qty)
        if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v)
               for v in values):
            return False, "BBO_NON_NUMERIC"
        if self.bid <= 0 or self.ask <= 0:
            return False, "BBO_NON_POSITIVE"
        if self.ask < self.bid:
            return False, "BBO_CROSSED"
        if self.bid_qty <= 0 or self.ask_qty <= 0:
            return False, "BBO_EMPTY_SIDE"
        now = int(time.time() * 1000) if now_ms is None else int(now_ms)
        if now - int(self.received_ms) > max_age_ms:
            return False, "BBO_STALE_LOCAL"
        if int(self.received_ms) - int(self.event_ms) > max_age_ms:
            return False, "BBO_STALE_EXCHANGE_LAG"
        return True, "OK"

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return (self.ask - self.bid) / self.mid

    @classmethod
    def from_book_ticker(cls, payload: dict, received_ms: int) -> "BBOSnapshot":
        """Parse a Binance USD-M bookTicker WS/REST payload (raises on bad shape)."""
        event = payload.get("T") or payload.get("E") or payload.get("time")
        return cls(
            symbol=str(payload["s"] if "s" in payload else payload["symbol"]),
            bid=float(payload["b"] if "b" in payload else payload["bidPrice"]),
            ask=float(payload["a"] if "a" in payload else payload["askPrice"]),
            bid_qty=float(payload["B"] if "B" in payload else payload["bidQty"]),
            ask_qty=float(payload["A"] if "A" in payload else payload["askQty"]),
            event_ms=int(event),
            received_ms=int(received_ms),
        )


def impact_floor(symbol: str) -> float:
    return 0.00010 if any(m in str(symbol).upper() for m in MAJORS) else 0.00020


def estimated_impact(bbo: BBOSnapshot, side: str, order_notional: float) -> float:
    """Per-side impact beyond half-spread for a taker order of ``order_notional``."""
    if not math.isfinite(order_notional) or order_notional <= 0:
        raise ValueError("order notional must be positive")
    top_qty = bbo.ask_qty if str(side).upper() in {"LONG", "BUY"} else bbo.bid_qty
    top_price = bbo.ask if str(side).upper() in {"LONG", "BUY"} else bbo.bid
    top_notional = top_qty * top_price
    walk = max(0.0, order_notional / top_notional - 1.0)
    return impact_floor(bbo.symbol) + walk * bbo.spread


@dataclass(frozen=True)
class CostComparison:
    symbol: str
    status: str
    static_cost_bps: float
    live_spread_bps: float | None
    estimated_impact_bps: float | None
    live_total_cost_bps: float | None
    net_rr_static: float
    net_rr_live: float | None
    ev_static: float
    ev_live: float | None
    decision_static: bool
    decision_live: bool | None

    @property
    def would_change_decision(self) -> bool | None:
        if self.decision_live is None:
            return None
        return self.decision_live != self.decision_static

    def format(self) -> str:
        def f(v, d=3):
            return "NA" if v is None else f"{v:.{d}f}"
        wcd = self.would_change_decision
        return (
            f"[COST_SHADOW_BBO] symbol={self.symbol} status={self.status} "
            f"static_cost_bps={self.static_cost_bps:.3f} live_spread_bps={f(self.live_spread_bps)} "
            f"estimated_impact_bps={f(self.estimated_impact_bps)} "
            f"live_total_cost_bps={f(self.live_total_cost_bps)} "
            f"net_rr_static={self.net_rr_static:.3f} net_rr_live={f(self.net_rr_live)} "
            f"ev_static={self.ev_static:.4f} ev_live={f(self.ev_live, 4)} "
            f"would_change_decision={'NA' if wcd is None else str(wcd).lower()} "
            f"shadow_only=true decision_effect=NONE execution_effect=NONE"
        )


def _nexus_gates(win_prob: float, stop: float, target: float, round_trip: float,
                 rr_floor: float) -> tuple[float, float, bool]:
    """Same algebra as ``nexus_ai.expected_value`` + the net R:R floor."""
    gain_net, loss_net = target - round_trip, stop + round_trip
    rr = gain_net / loss_net if loss_net > 0 else 0.0
    ev = win_prob * gain_net - (1.0 - win_prob) * loss_net
    return rr, ev * 100.0, (ev > 0 and rr >= rr_floor)


def compare(*, symbol: str, side: str, stop_frac: float, target_frac: float, win_prob: float,
            taker_fee: float, static_slippage_per_side: float, order_notional: float,
            rr_floor: float, bbo: BBOSnapshot | None, now_ms: int | None = None,
            max_age_ms: int = DEFAULT_MAX_AGE_MS) -> CostComparison:
    static_rt = 2.0 * taker_fee + 2.0 * static_slippage_per_side
    rr_s, ev_s, ok_s = _nexus_gates(win_prob, stop_frac, target_frac, static_rt, rr_floor)
    if bbo is None:
        return CostComparison(symbol, "BBO_UNAVAILABLE", static_rt * 1e4, None, None, None,
                              rr_s, None, ev_s, None, ok_s, None)
    valid, reason = bbo.validate(now_ms=now_ms, max_age_ms=max_age_ms)
    if not valid:
        return CostComparison(symbol, reason, static_rt * 1e4, None, None, None,
                              rr_s, None, ev_s, None, ok_s, None)
    half = bbo.spread / 2.0
    impact = estimated_impact(bbo, side, order_notional)
    live_rt = 2.0 * taker_fee + 2.0 * (half + impact)
    rr_l, ev_l, ok_l = _nexus_gates(win_prob, stop_frac, target_frac, live_rt, rr_floor)
    return CostComparison(symbol, "OK", static_rt * 1e4, bbo.spread * 1e4, impact * 1e4,
                          live_rt * 1e4, rr_s, rr_l, ev_s, ev_l, ok_s, ok_l)


__all__ = ["BBOSnapshot", "CostComparison", "compare", "estimated_impact", "impact_floor"]
