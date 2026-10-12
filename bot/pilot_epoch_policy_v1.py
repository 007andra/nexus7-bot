"""Pure feasibility review of a PROPOSED controlled-pilot risk epoch (issue #620).

Nothing here activates an epoch, reads the environment, touches the database,
the historical HWM, MAX_DRAWDOWN, overrides or any order path. It answers one
question for human reviewers: given a proposed policy, authenticated equity and
an instrument, is the policy internally consistent and executable, and how much
can one worst-case trade lose?

Worst-case loss per trade (fraction of notional): stop distance + taker fee on
both legs + adverse slippage on both legs + a declared stop-gap stress. Sizing
is cost-inclusive, so a full worst-case loss equals ``risk_per_trade * equity``.

Hard caps mirror the proposal under review (1 position, 3x leverage) and the
existing code (epoch drawdown <= 30%, controlled re-entry risk <= 2%). The
result always carries ``activation_allowed=False``: approving and arming an
epoch remains a separate, explicit operator decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

HARD_MAX_LEVERAGE = 3
HARD_MAX_POSITIONS = 1
HARD_MAX_RISK_PER_TRADE = Decimal("0.02")
HARD_MAX_EPOCH_DRAWDOWN = Decimal("0.30")

AUTHORITY = {
    "activation_allowed": False,
    "historical_drawdown_unchanged": True,
    "historical_hwm_written": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
}


@dataclass(frozen=True)
class PilotPolicy:
    risk_per_trade: Decimal       # fraction of epoch start equity, e.g. 0.005
    daily_loss_limit: Decimal     # e.g. 0.02
    epoch_drawdown_limit: Decimal  # e.g. 0.05
    max_positions: int            # e.g. 1
    max_leverage: int             # e.g. 3


@dataclass(frozen=True)
class Instrument:
    min_notional: Decimal
    taker_fee: Decimal
    slippage: Decimal             # declared adverse slippage per leg
    stop_gap_stress: Decimal      # extra adverse fill beyond the stop
    maint_margin_rate: Decimal


def policy_errors(p: PilotPolicy) -> list[str]:
    errors = []
    for name in ("risk_per_trade", "daily_loss_limit", "epoch_drawdown_limit"):
        v = getattr(p, name)
        if not isinstance(v, Decimal) or not v.is_finite() or v <= 0:
            errors.append(f"{name.upper()}_INVALID")
    if errors:
        return errors
    if p.risk_per_trade > HARD_MAX_RISK_PER_TRADE:
        errors.append("RISK_PER_TRADE_ABOVE_HARD_CAP")
    if p.epoch_drawdown_limit > HARD_MAX_EPOCH_DRAWDOWN:
        errors.append("EPOCH_DRAWDOWN_ABOVE_HARD_CAP")
    if not (p.risk_per_trade <= p.daily_loss_limit <= p.epoch_drawdown_limit):
        errors.append("LIMITS_NOT_NESTED")  # trade <= day <= epoch
    if not isinstance(p.max_positions, int) or not 1 <= p.max_positions <= HARD_MAX_POSITIONS:
        errors.append("MAX_POSITIONS_ABOVE_HARD_CAP")
    if not isinstance(p.max_leverage, int) or not 1 <= p.max_leverage <= HARD_MAX_LEVERAGE:
        errors.append("LEVERAGE_ABOVE_HARD_CAP")
    return errors


def worst_loss_fraction(inst: Instrument, stop_distance: Decimal) -> Decimal:
    return stop_distance + 2 * inst.taker_fee + 2 * inst.slippage + inst.stop_gap_stress


def minimum_equity(p: PilotPolicy, inst: Instrument, stop_distance: Decimal) -> Decimal:
    """Smallest equity at which a cost-inclusive sized trade meets minNotional."""
    return inst.min_notional * worst_loss_fraction(inst, stop_distance) / p.risk_per_trade


def review(p: PilotPolicy, inst: Instrument, *, equity: Decimal, stop_distance: Decimal) -> dict:
    """Feasibility of ONE trade at ``stop_distance`` under the proposed policy."""
    blockers = policy_errors(p)
    if not isinstance(equity, Decimal) or not equity.is_finite() or equity <= 0:
        blockers.append("EQUITY_NOT_POSITIVE")
    if not isinstance(stop_distance, Decimal) or not 0 < stop_distance < 1:
        blockers.append("STOP_DISTANCE_INVALID")
    if blockers:
        return {**AUTHORITY, "status": "POLICY_INVALID", "blockers": tuple(blockers)}

    worst = worst_loss_fraction(inst, stop_distance)
    notional = p.risk_per_trade * equity / worst
    out = {
        **AUTHORITY,
        "worst_loss_fraction_of_notional": worst,
        "sized_notional": notional,
        "worst_trade_loss": p.risk_per_trade * equity,  # = notional * worst by construction
        "daily_loss_budget": p.daily_loss_limit * equity,
        "epoch_loss_budget": p.epoch_drawdown_limit * equity,
        "full_losses_until_daily_stop": int((p.daily_loss_limit / p.risk_per_trade)
                                            .to_integral_value(rounding=ROUND_FLOOR)),
        "full_losses_until_epoch_stop": int((p.epoch_drawdown_limit / p.risk_per_trade)
                                            .to_integral_value(rounding=ROUND_FLOOR)),
        "minimum_equity": minimum_equity(p, inst, stop_distance),
    }
    if notional < inst.min_notional:
        blockers.append("BELOW_MIN_NOTIONAL")
    if notional / p.max_leverage > equity:
        blockers.append("INSUFFICIENT_MARGIN")
    # isolated liquidation distance ~ 1/leverage - mmr; the worst stop fill
    # must be strictly closer than that
    if worst - 2 * inst.taker_fee >= Decimal(1) / p.max_leverage - inst.maint_margin_rate:
        blockers.append("LIQUIDATION_BEFORE_STOP")
    out["status"] = "FEASIBLE_PENDING_APPROVAL" if not blockers else "NOT_EXECUTABLE"
    out["blockers"] = tuple(blockers)
    return out
