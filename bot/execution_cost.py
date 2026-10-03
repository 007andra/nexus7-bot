"""Single execution-cost authority for one candidate evaluation.

One ``ExecutionCostSnapshot`` is built per candidate (by whichever consumer
runs first), attached to the signal, and reused by every later consumer:

* ``round_trip_cost_fraction``  — economic truth. Used by NEXUS EV / net R:R
  and by the technical-policy R:R diagnostics, so both layers report the same
  number for the same candidate.
* ``stress_round_trip_cost_fraction`` — conservative ceiling input:
  ``max(economic, static fallback)``. Used only by the pre-existing projected
  loss ceiling so that replacing a static assumption with a measurement can
  never loosen that gate.
* ``taker_fee`` / ``slippage_allowance`` — RiskManagerV3 stop-risk sizing.

Fees are exchange-aware: Binance USD-M reads ``/fapi/v1/commissionRate``
(read-only, cached); KuCoin keeps its existing ``/api/v1/trade-fees`` read.
When the live fee is unavailable the fallback is
``max(configured exchange taker fee, 6 bps)`` — 6 bps is the legacy NEXUS
default, retained so the fallback is never less conservative than before.
Slippage is half-spread + impact from the cached ticker when available,
otherwise the existing static per-symbol research assumption.
Funding is *not* included in pre-trade cost (15m holding horizon, discrete
8h settlements); every snapshot says so explicitly.

This module never places, amends or cancels orders.
"""
from __future__ import annotations

import itertools
import math
import os
import time
from dataclasses import dataclass, field

LEGACY_CONSERVATIVE_TAKER_FEE = 0.0006
DEFAULT_SLIPPAGE = 0.0005
FEE_CACHE_TTL_S = 3600.0
SNAPSHOT_MAX_AGE_S = 120.0
FUNDING_ASSUMPTION = "NOT_IN_PRETRADE_COST"

_SNAPSHOT_ATTR = "_bgx_cost_snapshot"
_FEE_CACHE: dict[tuple[str, str], tuple[float, float | None, str, float]] = {}
_SEQ = itertools.count(1)


def _finite(value, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def exchange_name() -> str:
    from bot import exchange

    return str(exchange.EXCHANGE_NAME)


def configured_taker_fee() -> float:
    from bot import exchange

    rate = _finite(getattr(exchange, "TAKER_FEE", LEGACY_CONSERVATIVE_TAKER_FEE),
                   LEGACY_CONSERVATIVE_TAKER_FEE)
    return rate if 0 <= rate < 0.02 else LEGACY_CONSERVATIVE_TAKER_FEE


def fallback_taker_fee() -> float:
    """Conservative fee used whenever the account commission cannot be read."""
    return max(configured_taker_fee(), LEGACY_CONSERVATIVE_TAKER_FEE)


def static_slippage_rate(symbol: str) -> float:
    """Historical deterministic one-way slippage assumption (majors 1x, alts 2x)."""
    base = _finite(os.environ.get("BACKTEST_SLIPPAGE", DEFAULT_SLIPPAGE), DEFAULT_SLIPPAGE)
    if base < 0 or base >= 0.05:
        base = DEFAULT_SLIPPAGE
    sym = str(symbol or "").upper()
    return base if any(major in sym for major in ("BTC", "ETH", "SOL")) else base * 2.0


def static_round_trip_cost_fraction(symbol: str) -> float:
    return 2.0 * fallback_taker_fee() + 2.0 * static_slippage_rate(symbol)


def ticker_slippage(ticker, symbol: str) -> tuple[float | None, float | None]:
    """(one-way slippage, spread_bps) from bid/ask, or (None, None)."""
    if not isinstance(ticker, dict):
        return None, None
    bid, ask = _finite(ticker.get("bid")), _finite(ticker.get("ask"))
    if bid <= 0 or ask <= 0 or ask < bid:
        return None, None
    mid = (bid + ask) / 2.0
    full_spread = (ask - bid) / mid
    major = any(base in str(symbol or "").upper() for base in ("BTC", "ETH", "SOL"))
    impact_floor = 0.00010 if major else 0.00020
    estimate = min(max(full_spread / 2.0 + impact_floor, impact_floor), 0.01)
    return estimate, full_spread * 10_000.0


@dataclass(frozen=True)
class ExecutionCostSnapshot:
    snapshot_id: str
    candidate_id: str
    exchange: str
    symbol: str
    entry_reference: float
    taker_fee: float
    maker_fee: float | None
    entry_slippage: float
    exit_slippage: float
    spread_bps: float | None
    fee_source: str
    slippage_source: str
    observed_at: float
    funding_assumption: str = FUNDING_ASSUMPTION
    funding_rate: float | None = None
    extra: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def fallback(self) -> bool:
        return "fallback" in self.fee_source or "fallback" in self.slippage_source

    @property
    def round_trip_cost_fraction(self) -> float:
        return 2.0 * self.taker_fee + self.entry_slippage + self.exit_slippage

    @property
    def stress_round_trip_cost_fraction(self) -> float:
        return max(self.round_trip_cost_fraction, static_round_trip_cost_fraction(self.symbol))

    @property
    def one_way_slippage(self) -> float:
        """Symmetric one-way value for ``nexus_ai.expected_value`` (cost=2f+2s)."""
        return (self.entry_slippage + self.exit_slippage) / 2.0

    @property
    def slippage_allowance(self) -> float:
        return self.entry_slippage + self.exit_slippage

    def age_s(self, now: float | None = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.observed_at)

    def log_fields(self) -> str:
        spread = "NA" if self.spread_bps is None else f"{self.spread_bps:.3f}"
        funding = "NA" if self.funding_rate is None else f"{self.funding_rate:.8f}"
        return (
            f"candidate_id={self.candidate_id} cost_snapshot_id={self.snapshot_id} "
            f"exchange={self.exchange} taker_bps={self.taker_fee * 1e4:.3f} "
            f"entry_slip_bps={self.entry_slippage * 1e4:.3f} "
            f"exit_slip_bps={self.exit_slippage * 1e4:.3f} spread_bps={spread} "
            f"round_trip_cost_pct={self.round_trip_cost_fraction * 100:.5f} "
            f"fee_source={self.fee_source} slippage_source={self.slippage_source} "
            f"funding_assumption={self.funding_assumption} funding_rate={funding} "
            f"cost_fallback={str(self.fallback).lower()} cost_age_s={self.age_s():.1f}"
        )


def rr_breakdown(entry: float, sl: float, tp: float, cost_fraction: float) -> dict:
    """Gross and net R:R with the exact formula used by ``nexus_ai.expected_value``."""
    entry, sl, tp, cost = float(entry), float(sl), float(tp), float(cost_fraction)
    if not all(math.isfinite(v) for v in (entry, sl, tp, cost)) or entry <= 0:
        raise ValueError("nonfinite rr input")
    loss_gross = abs(entry - sl) / entry
    gain_gross = abs(tp - entry) / entry
    if loss_gross <= 0:
        raise ValueError("zero stop distance")
    loss_net = loss_gross + cost
    gain_net = gain_gross - cost
    return {
        "gross_rr": gain_gross / loss_gross,
        "net_rr": gain_net / loss_net if loss_net > 0 else 0.0,
        "stop_distance": loss_gross,
        "target_distance": gain_gross,
        "cost": cost,
    }


def candidate_id(sig) -> str:
    setup = getattr(sig, "_bgx_setup_id", None)
    if setup:
        return str(setup)
    return f"{getattr(sig, 'symbol', 'UNKNOWN')}:{getattr(sig, 'direction', '?')}:{_finite(getattr(sig, 'entry', 0.0)):.10g}"


async def fetch_taker_fee(client, symbol: str) -> tuple[float, float | None, str]:
    """(taker, maker, source) for the active exchange; never raises."""
    venue = exchange_name()
    key = (venue, str(symbol))
    now = time.monotonic()
    cached = _FEE_CACHE.get(key)
    if cached and cached[3] > now:
        return cached[0], cached[1], cached[2]

    taker, maker, source = fallback_taker_fee(), None, "conservative_fallback"
    try:
        if venue == "binance" and hasattr(client, "_get"):
            from bot.binance import to_binance

            data = await client._get(
                "/fapi/v1/commissionRate", {"symbol": to_binance(symbol)}, auth=True,
            )
            rate = _finite(data.get("takerCommissionRate"), -1.0) if isinstance(data, dict) else -1.0
            if 0 <= rate < 0.02:
                taker, source = rate, "binance_commission_rate"
                mk = _finite(data.get("makerCommissionRate"), -1.0)
                maker = mk if 0 <= mk < 0.02 else None
        elif venue == "kucoin":
            from bot.kucoin_execution_model import fetch_actual_taker_fee

            rate, src = await fetch_actual_taker_fee(client, symbol)
            if src == "kucoin_actual_fee":
                taker, source = float(rate), src
    except Exception:  # noqa: BLE001 - any read failure keeps the conservative fallback
        taker, maker, source = fallback_taker_fee(), None, "conservative_fallback"

    _FEE_CACHE[key] = (taker, maker, source, now + FEE_CACHE_TTL_S)
    return taker, maker, source


def attached_snapshot(sig) -> ExecutionCostSnapshot | None:
    snap = getattr(sig, _SNAPSHOT_ATTR, None)
    return snap if isinstance(snap, ExecutionCostSnapshot) else None


def reusable_snapshot(sig, now: float | None = None) -> ExecutionCostSnapshot | None:
    snap = attached_snapshot(sig)
    if snap is None:
        return None
    if snap.symbol != str(getattr(sig, "symbol", "")):
        return None
    if not math.isclose(snap.entry_reference, _finite(getattr(sig, "entry", 0.0)),
                        rel_tol=1e-12, abs_tol=0.0):
        return None
    if snap.age_s(now) > SNAPSHOT_MAX_AGE_S:
        return None
    return snap


async def build_snapshot(engine, sig) -> ExecutionCostSnapshot:
    symbol = str(sig.symbol)
    client = getattr(engine, "client", None)
    taker, maker, fee_source = await fetch_taker_fee(client, symbol)
    ticker = None
    getter = getattr(client, "get_cached_ticker", None)
    if callable(getter):
        try:
            ticker = getter(symbol)
        except Exception:  # noqa: BLE001 - missing ticker uses the static assumption
            ticker = None
    slip, spread_bps = ticker_slippage(ticker, symbol)
    if slip is None:
        slip, slip_source = static_slippage_rate(symbol), "static_symbol_fallback"
    else:
        slip_source = "ticker_half_spread_plus_impact"
    now = time.time()
    return ExecutionCostSnapshot(
        snapshot_id=f"cost-{symbol}-{int(now * 1000)}-{next(_SEQ)}",
        candidate_id=candidate_id(sig),
        exchange=exchange_name(),
        symbol=symbol,
        entry_reference=_finite(getattr(sig, "entry", 0.0)),
        taker_fee=float(taker),
        maker_fee=maker,
        entry_slippage=float(slip),
        exit_slippage=float(slip),
        spread_bps=spread_bps,
        fee_source=fee_source,
        slippage_source=slip_source,
        observed_at=now,
    )


async def snapshot_for(engine, sig) -> tuple[ExecutionCostSnapshot, bool]:
    """Return (snapshot, reused). Builds and attaches when none is reusable."""
    snap = reusable_snapshot(sig)
    if snap is not None:
        return snap, True
    snap = await build_snapshot(engine, sig)
    attach_snapshot(sig, snap)
    return snap, False


def attach_snapshot(sig, snap: ExecutionCostSnapshot) -> bool:
    """Attach to the signal; False for frozen/slotted doubles (snapshot still returned)."""
    try:
        setattr(sig, _SNAPSHOT_ATTR, snap)
        return True
    except (AttributeError, TypeError):
        return False


def stress_cost_fraction(sig, symbol: str) -> tuple[float, str]:
    """Cost for projected-loss ceilings: attached snapshot or static fallback."""
    snap = attached_snapshot(sig) if sig is not None else None
    if snap is not None and snap.symbol == symbol:
        return snap.stress_round_trip_cost_fraction, snap.snapshot_id
    return static_round_trip_cost_fraction(symbol), "static_fallback"
