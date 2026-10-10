"""Fail-closed research execution cost arithmetic for #609.

All inputs must be independently evidenced; hypothetical net is not a fill.
Costs are provided explicitly in basis points, not inferred from RR estimates.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

REQUIRED = ("entry_price", "exit_price", "quantity", "capital_usdt",
            "entry_fee_bps", "exit_fee_bps", "spread_bps",
            "entry_slippage_bps", "exit_slippage_bps", "funding_usdt")


def _d(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise InvalidOperation("nonfinite")
    return number


def evaluate_costs(*, side: str, evidence: dict, proofs: dict) -> dict:
    """Hypothetical signed net PnL with doubled trading friction stress.

    funding_usdt is signed: positive = cost paid; negative = received.
    funding proof must include settlement timing and position exposure.
    """
    missing = [key for key in REQUIRED if evidence.get(key) is None]
    required_proofs = ("historical_path", "entry_feasible", "quantity_filters",
                       "fee_schedule", "spread_at_decision",
                       "slippage_assumption", "funding_settlement",
                       "capital_basis", "time_alignment")
    missing += [f"PROOF_{key}" for key in required_proofs if proofs.get(key) is not True]
    if side not in ("SHORT", "LONG"):
        missing.append("VALID_SIDE")
    if missing:
        return {"status": "NET_PROOF_MISSING", "missing": missing,
                "live_allowed": False, "execution_effect": "NONE"}
    try:
        d = {key: _d(evidence[key]) for key in REQUIRED}
    except (InvalidOperation, ValueError, TypeError):
        return {"status": "NET_PROOF_MISSING", "missing": ["NONFINITE_OR_INVALID_INPUT"],
                "live_allowed": False, "execution_effect": "NONE"}
    if any(d[key] <= 0 for key in ("entry_price", "exit_price", "quantity", "capital_usdt")):
        return {"status": "NET_PROOF_MISSING", "missing": ["NONPOSITIVE_NOTIONAL_OR_CAPITAL"],
                "live_allowed": False, "execution_effect": "NONE"}
    if any(d[key] < 0 for key in ("entry_fee_bps", "exit_fee_bps", "spread_bps",
                                "entry_slippage_bps", "exit_slippage_bps")):
        return {"status": "NET_PROOF_MISSING", "missing": ["NEGATIVE_FRICTION"],
                "live_allowed": False, "execution_effect": "NONE"}
    entry_notional = d["entry_price"] * d["quantity"]
    exit_notional = d["exit_price"] * d["quantity"]
    gross = (d["entry_price"] - d["exit_price"]) * d["quantity"]
    if side == "LONG":
        gross = -gross
    bps = Decimal("10000")
    fees = (entry_notional * d["entry_fee_bps"] + exit_notional * d["exit_fee_bps"]) / bps
    # Conservative: full observed spread charged once, in addition to explicit slippage.
    spread = entry_notional * d["spread_bps"] / bps
    slippage = (entry_notional * d["entry_slippage_bps"] +
                exit_notional * d["exit_slippage_bps"]) / bps
    funding = d["funding_usdt"]
    base = gross - fees - spread - slippage - funding
    # Stress doubles nonnegative trading friction; funding settlement is unchanged.
    stress = gross - 2 * (fees + spread + slippage) - funding
    return {"status": "HYPOTHETICAL_NET_CALCULATED",
            "gross_usdt": str(gross),
            "base_net_usdt": str(base), "stressed_net_usdt": str(stress),
            "base_return_pct_capital": str(100 * base / d["capital_usdt"]),
            "stressed_return_pct_capital": str(100 * stress / d["capital_usdt"]),
            "live_allowed": False, "promotion_allowed": False,
            "execution_effect": "NONE"}
