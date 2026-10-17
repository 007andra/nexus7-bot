"""Projected-loss diagnostic retained for observability after final sizing.

The final quantity is ``min(stop_risk_qty, operator_margin_cap_qty)`` (see
``final_sizing_invariants``). The historical 50%-of-entry-initial-margin ceiling is measured here. Runtime
final sizing consumes ``diagnose()`` and treats any non-PASS result as a
candidate-local rejection; it never pauses the scanner/runtime globally.

RiskManagerV3 owns the monetary stop-risk budget and Binance CROSS stress owns
account-level solvency/liquidation safety. This module remains arithmetic and
telemetry only: it does not resize, move technical stops, or mutate execution
state itself.
"""
import math


def measure(qty, entry, stop, direction, leverage, cost_fraction):
    """Return the exact arithmetic consumed by validate(), without deciding PASS/BLOCK."""
    values = (qty, entry, stop, leverage, cost_fraction)
    if any(isinstance(v, bool) or not math.isfinite(float(v)) for v in values):
        raise ValueError('nonfinite loss budget')
    qty, entry, stop, leverage, cost_fraction = map(float, values)
    if min(qty, entry, stop, leverage) <= 0 or cost_fraction < 0:
        raise ValueError('invalid loss budget')
    if not ((direction == 'LONG' and stop < entry) or (direction == 'SHORT' and stop > entry)):
        raise ValueError('invalid stop direction')

    notional = qty * entry
    margin = notional / leverage
    stop_fraction = abs(entry - stop) / entry
    projected = qty * (abs(entry - stop) + entry * cost_fraction)
    limit = margin * 0.50
    projected_loss_pct_notional = projected / notional * 100.0
    allowed_loss_pct_notional = limit / notional * 100.0
    return {
        'qty': qty,
        'entry': entry,
        'stop': stop,
        'direction': direction,
        'leverage': leverage,
        'cost_fraction': cost_fraction,
        'notional': notional,
        'margin': margin,
        'stop_fraction': stop_fraction,
        'projected_loss': projected,
        'loss_limit': limit,
        'projected_loss_pct_notional': projected_loss_pct_notional,
        'allowed_loss_pct_notional': allowed_loss_pct_notional,
        'headroom_usdt': limit - projected,
        'headroom_pct': allowed_loss_pct_notional - projected_loss_pct_notional,
    }


def validate(qty, entry, stop, direction, leverage, cost_fraction):
    metrics = measure(qty, entry, stop, direction, leverage, cost_fraction)
    projected = metrics['projected_loss']
    limit = metrics['loss_limit']
    if projected > limit + max(1e-12, limit * 1e-12):
        raise ValueError('projected loss exceeds 50pct entry margin')
    return projected, limit


def diagnose(qty, entry, stop, direction, leverage, cost_fraction):
    """Return legacy-ceiling diagnostics without affecting execution.

    Result is one of PASS, WARN, or UNAVAILABLE. Unlike validate(), this helper
    never raises for ordinary input/geometry failures and is suitable for
    observability-only runtime call sites.
    """
    try:
        metrics = measure(qty, entry, stop, direction, leverage, cost_fraction)
    except (TypeError, ValueError, ArithmeticError) as exc:
        return "UNAVAILABLE", reason_from_exception(exc), None

    projected = metrics["projected_loss"]
    limit = metrics["loss_limit"]
    tolerance = max(1e-12, limit * 1e-12)
    if projected > limit + tolerance:
        return "WARN", "projected_loss_exceeds_50pct_entry_margin", metrics
    return "PASS", "within_50pct_entry_margin", metrics


def reason_from_exception(exc):
    text = str(exc)
    mapping = {
        'projected loss exceeds 50pct entry margin': 'projected_loss_exceeds_50pct_entry_margin',
        'nonfinite loss budget': 'nonfinite_loss_budget',
        'invalid loss budget': 'invalid_loss_budget',
        'invalid stop direction': 'invalid_stop_direction',
    }
    return mapping.get(text, type(exc).__name__)


def emit_telemetry(
    log, *, symbol, setup_id, stage, qty, entry, stop, direction, leverage,
    cost_fraction, result, specific_reason, risk_v3_advisory_qty=None,
):
    """Best-effort logging only. Any telemetry failure is isolated from trading."""
    try:
        metrics = measure(qty, entry, stop, direction, leverage, cost_fraction)
        risk_qty = (
            'NA' if risk_v3_advisory_qty is None
            else f"{float(risk_v3_advisory_qty):.12g}"
        )
        logger = log.info if str(result).upper() == 'PASS' else log.warning
        logger(
            "[FINAL_LOSS_BUDGET] symbol=%s setup_id=%s stage=%s result=%s "
            "specific_reason=%s qty=%.12g qty_authority=FINAL_SIZING_INVARIANT "
            "stop_risk_qty=%s risk_v3_qty_authority=BINDING_UPPER_BOUND "
            "entry=%.12g stop=%.12g direction=%s leverage=%.12g margin=%.12g "
            "stop_fraction=%.12g cost_fraction=%.12g projected_loss=%.12g "
            "loss_limit=%.12g projected_loss_pct_notional=%.8f "
            "allowed_loss_pct_notional=%.8f headroom_usdt=%.12g "
            "headroom_pct=%.8f decision_effect=NONE execution_effect=OBSERVABILITY_ONLY",
            symbol, setup_id or 'UNKNOWN', stage, str(result).upper(),
            specific_reason, metrics['qty'], risk_qty, metrics['entry'],
            metrics['stop'], metrics['direction'], metrics['leverage'],
            metrics['margin'], metrics['stop_fraction'], metrics['cost_fraction'],
            metrics['projected_loss'], metrics['loss_limit'],
            metrics['projected_loss_pct_notional'],
            metrics['allowed_loss_pct_notional'], metrics['headroom_usdt'],
            metrics['headroom_pct'],
        )
    except Exception as exc:
        _report_telemetry_failure(log, symbol, setup_id, stage, result, exc)


def _report_telemetry_failure(log, symbol, setup_id, stage, result, exc):
    """Report a telemetry failure; a broken logger must never reach trading.

    Returns True when the failure was reported, False when the logger itself
    failed (then there is nothing else that could safely record it).
    """
    try:
        log.warning(
            "[FINAL_LOSS_BUDGET] symbol=%s setup_id=%s stage=%s result=%s "
            "telemetry_error=%s decision_effect=NONE execution_effect=NONE",
            symbol, setup_id or 'UNKNOWN', stage, str(result).upper(),
            type(exc).__name__,
        )
        return True
    except Exception as log_exc:  # noqa: BLE001 - logging must not raise into sizing
        del log_exc
        return False
