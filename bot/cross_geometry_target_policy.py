"""Preserve the strategy take-profit when CROSS liquidation safety tightens SL.

F-003: an ADJUSTED (compressed-stop) geometry is no longer applied by
``kucoin_contract_risk_hardening`` — a technical stop outside the liquidation
safe zone is NO TRADE. This helper only shapes the diagnostic geometry dict;
it never moves an order's stop.

The underlying KuCoin CROSS-risk hardening may need to move an ATR stop closer
to entry so the configured 50x position remains stop-effective before the
liquidation boundary. Historically it scaled TP inward by the same fraction to
preserve *gross* R:R. That is not cost invariant: exchange fees and market
slippage are fixed percentages of notional, so compressing both distances can
turn an initially valid net R:R into a second-NEXUS veto.

This policy keeps the original strategy TP unchanged and tightens only the SL.
It never pushes TP farther than the strategy requested. The resulting gross R:R
is recomputed from the exact adjusted stop and original target, and the normal
second NEXUS review remains mandatory. All downstream sizing, liquidation,
market-risk, exposure, durable execution and native protection gates remain
unchanged.
"""
from __future__ import annotations

import math
from typing import Any


_POLICY_FLAG = "_preserve_strategy_target_policy_installed"


def apply_target_policy(result: dict, sig: Any) -> dict:
    """Return geometry with original TP preserved for an ADJUSTED stop.

    Non-adjusted results are returned unchanged. Any malformed adjusted result
    fails closed rather than silently falling back to permissive geometry.
    """
    if not isinstance(result, dict) or result.get("status") != "ADJUSTED":
        return result

    try:
        entry = float(sig.entry)
        original_tp = float(sig.tp)
        adjusted_sl = float(result["sl"])
        direction = str(sig.direction).upper()
    except (TypeError, ValueError, AttributeError, KeyError):
        return {"status": "BLOCK", "reason": "target_policy_invalid_geometry"}

    if direction not in {"LONG", "SHORT"}:
        return {"status": "BLOCK", "reason": "target_policy_invalid_direction"}
    if not all(math.isfinite(v) and v > 0 for v in (entry, original_tp, adjusted_sl)):
        return {"status": "BLOCK", "reason": "target_policy_invalid_geometry"}
    if direction == "LONG" and not (adjusted_sl < entry < original_tp):
        return {"status": "BLOCK", "reason": "target_policy_invalid_long_geometry"}
    if direction == "SHORT" and not (original_tp < entry < adjusted_sl):
        return {"status": "BLOCK", "reason": "target_policy_invalid_short_geometry"}

    adjusted_risk = abs(entry - adjusted_sl)
    original_reward = abs(original_tp - entry)
    if adjusted_risk <= 0 or original_reward <= 0:
        return {"status": "BLOCK", "reason": "target_policy_zero_distance"}

    revised = dict(result)
    revised["tp"] = original_tp
    revised["rr"] = original_reward / adjusted_risk
    revised["reason"] = "compressed_stop_preserve_original_strategy_target"
    revised["target_policy"] = "PRESERVE_ORIGINAL_STRATEGY_TARGET"
    revised["original_target_preserved"] = True
    return revised


def install(hardening_module, log) -> None:
    """Install policy on the module-level geometry function, never class APIs."""
    if getattr(hardening_module, _POLICY_FLAG, False):
        return

    original_geometry = getattr(hardening_module, "_geometry_from_exact_mmr", None)
    if not callable(original_geometry):
        raise RuntimeError("cross_geometry_function_unavailable")

    def geometry_with_original_target(liquidation, sig, leverage: int) -> dict:
        base = original_geometry(liquidation, sig, leverage)
        revised = apply_target_policy(base, sig)
        if isinstance(base, dict) and base.get("status") == "ADJUSTED":
            log.warning(
                "[CROSS_GEOMETRY_TARGET_POLICY] symbol=%s status=%s "
                "tp_policy=PRESERVE_ORIGINAL_STRATEGY_TARGET "
                "gross_rr_before=%s gross_rr_after=%s leverage_unchanged=true "
                "thresholds_unchanged=true",
                getattr(sig, "symbol", "UNKNOWN"),
                revised.get("status") if isinstance(revised, dict) else "INVALID",
                base.get("rr"),
                revised.get("rr") if isinstance(revised, dict) else None,
            )
        return revised

    hardening_module._geometry_from_exact_mmr = geometry_with_original_target
    setattr(hardening_module, _POLICY_FLAG, True)
    log.warning(
        "[CROSS_GEOMETRY_TARGET_POLICY] installed diagnostic_only=true stop_compression_applied=false "
        "original_strategy_tp_preserved=true target_extension=false "
        "second_nexus_recheck_required=true leverage_unchanged=true "
        "score_threshold_unchanged=true rr_threshold_unchanged=true"
    )
