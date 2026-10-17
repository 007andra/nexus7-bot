"""LIVE technical-stop preservation and geometry validation.

The strategy already produces structure/ATR-based SL/TP levels and NEXUS owns
the cost-adjusted economic/EV decision. This layer is deliberately narrower:
it preserves technical geometry exactly and fails closed on malformed,
non-finite, zero-distance, or directionally invalid protection.

Cost-adjusted net R:R is retained as diagnostics only. Re-applying a second
minimum-net-R:R veto here duplicated the NEXUS economic gate and was observed
in production blocking otherwise valid candidates system-wide.
"""
import math
import os


def stop_price(entry, direction, leverage, round_trip_cost):
    """Legacy helper retained for backward compatibility; not used by install()."""
    values = (entry, leverage, round_trip_cost)
    if not all(math.isfinite(v) for v in values):
        raise ValueError("nonfinite stop input")
    if entry <= 0 or leverage != 50 or round_trip_cost < 0:
        raise ValueError("invalid stop input")
    distance = 0.5 / leverage - round_trip_cost
    if distance <= 0 or direction not in ("LONG", "SHORT"):
        raise ValueError("invalid stop budget")
    return entry * (1 - distance if direction == "LONG" else 1 + distance)


def required_target_price(entry, stop, direction, round_trip_cost, min_rr_net):
    """Legacy helper retained for regression compatibility; not used by install()."""
    values = (entry, stop, round_trip_cost, min_rr_net)
    if not all(math.isfinite(v) for v in values):
        raise ValueError("nonfinite target input")
    if entry <= 0 or stop <= 0 or round_trip_cost < 0 or min_rr_net <= 0:
        raise ValueError("invalid target input")
    if direction not in ("LONG", "SHORT"):
        raise ValueError("invalid target direction")
    stop_distance = abs(entry - stop) / entry
    target_distance = min_rr_net * (stop_distance + round_trip_cost) + round_trip_cost
    if not math.isfinite(target_distance) or target_distance <= 0:
        raise ValueError("invalid target distance")
    return entry * (1 + target_distance if direction == "LONG" else 1 - target_distance)


def _farther_target(current, required, entry, direction):
    """Legacy helper retained for backward compatibility; not used by install()."""
    current = float(current or 0.0)
    if direction == "LONG":
        return max(current, required) if current > entry else required
    return min(current, required) if 0 < current < entry else required


def validate_technical_geometry(entry, sl, tp, direction, round_trip_cost, min_rr_net):
    """Validate untouched protection geometry; net R:R is diagnostic only."""
    values = (entry, sl, tp, round_trip_cost, min_rr_net)
    if not all(math.isfinite(float(v)) for v in values):
        raise ValueError("nonfinite technical geometry")
    entry, sl, tp = float(entry), float(sl), float(tp)
    round_trip_cost, min_rr_net = float(round_trip_cost), float(min_rr_net)
    if entry <= 0 or sl <= 0 or tp <= 0 or round_trip_cost < 0 or min_rr_net <= 0:
        raise ValueError("invalid technical geometry")
    if direction not in ("LONG", "SHORT"):
        raise ValueError("invalid technical direction")
    if direction == "LONG" and not (sl < entry < tp):
        raise ValueError("invalid long technical geometry")
    if direction == "SHORT" and not (tp < entry < sl):
        raise ValueError("invalid short technical geometry")
    stop_distance = abs(entry - sl) / entry
    target_distance = abs(tp - entry) / entry
    if stop_distance <= 0 or target_distance <= 0:
        raise ValueError("zero technical distance")
    net_reward = target_distance - round_trip_cost
    net_risk = stop_distance + round_trip_cost
    net_rr_est = net_reward / net_risk if net_risk > 0 else 0.0
    if not math.isfinite(net_rr_est):
        raise ValueError("nonfinite technical net rr")
    return {
        "stop_distance_pct": stop_distance * 100.0,
        "target_distance_pct": target_distance * 100.0,
        "estimated_net_rr": net_rr_est,
        "net_rr_meets_reference": net_rr_est >= min_rr_net,
    }


def install(TradingEngine, log):
    if getattr(TradingEngine, "_operator_loss_policy_installed", False):
        return
    from bot.config import cfg
    from bot import execution_cost
    original_open = TradingEngine._open
    original_exit = TradingEngine._check_stagnation_and_invalidation

    def enabled(engine):
        return not getattr(engine, "paper_trade", True) and bool(
            getattr(getattr(engine, "pilot", None), "enabled", False)
        )

    async def open_with_technical_loss_geometry(self, sig, *args, **kwargs):
        if enabled(self):
            # Same per-candidate cost snapshot later reused by NEXUS EV/R:R, so
            # the technical estimated_net_rr and NEXUS rr_net agree for the
            # same candidate (audit P0-3). A snapshot failure falls back to the
            # conservative static cost and says so.
            try:
                snapshot, reused = await execution_cost.snapshot_for(self, sig)
                cost_fields = snapshot.log_fields() + f" cost_snapshot_reused={str(reused).lower()}"
                cost = snapshot.round_trip_cost_fraction
            except Exception as exc:  # noqa: BLE001 - diagnostics must not raise
                cost = execution_cost.static_round_trip_cost_fraction(getattr(sig, "symbol", ""))
                cost_fields = (
                    f"cost_snapshot_id=static_fallback cost_snapshot_error={type(exc).__name__}"
                )
            try:
                min_rr_net = float(os.environ.get(
                    "NEXUS_MIN_RR_NET",
                    str(round(float(cfg.MIN_RR_RATIO) * 0.80, 2)),
                ))
                original_sl = float(sig.sl)
                original_tp = float(sig.tp)
                original_tp1 = float(getattr(sig, "tp1", original_tp) or original_tp)
                original_tp2 = float(getattr(sig, "tp2", original_tp) or original_tp)
                diagnostics = validate_technical_geometry(
                    float(sig.entry), original_sl, original_tp, sig.direction, cost, min_rr_net
                )
                if (
                    float(sig.sl) != original_sl
                    or float(sig.tp) != original_tp
                    or float(getattr(sig, "tp1", original_tp1) or original_tp1) != original_tp1
                    or float(getattr(sig, "tp2", original_tp2) or original_tp2) != original_tp2
                ):
                    raise ValueError("technical geometry mutated unexpectedly")
                log.info(
                    "[TECHNICAL_STOP_POLICY] symbol=%s result=PASS source=strategy_structure_atr "
                    "sl=%.8f tp=%.8f stop_distance_pct=%.5f target_distance_pct=%.5f "
                    "entry_reference=%.8f gross_rr=%.4f "
                    "min_rr_net_reference=%.3f estimated_net_rr=%.4f net_rr_reference_met=%s "
                    "estimated_cost_pct=%.5f %s geometry_mutated=false economic_gate=NEXUS "
                    "risk_authority=RiskManagerV3 "
                    "final_quantity_policy=min(stop_risk_qty,operator_margin_cap_qty)",
                    sig.symbol, original_sl, original_tp,
                    diagnostics["stop_distance_pct"], diagnostics["target_distance_pct"],
                    float(sig.entry),
                    diagnostics["target_distance_pct"] / diagnostics["stop_distance_pct"],
                    min_rr_net, diagnostics["estimated_net_rr"],
                    diagnostics["net_rr_meets_reference"], cost * 100.0, cost_fields,
                )
            except (ValueError, TypeError, ArithmeticError) as exc:
                log.error(
                    "[TECHNICAL_STOP_POLICY] result=BLOCK symbol=%s reason=%s detail=%s "
                    "geometry_mutated=false economic_gate=NEXUS",
                    getattr(sig, "symbol", "unknown"), type(exc).__name__, str(exc),
                )
                return None
        return await original_open(self, sig, *args, **kwargs)

    async def no_discretionary_loss_exit(self):
        if enabled(self):
            return
        return await original_exit(self)

    TradingEngine._open = open_with_technical_loss_geometry
    TradingEngine._check_stagnation_and_invalidation = no_discretionary_loss_exit
    TradingEngine._operator_loss_policy_installed = True
