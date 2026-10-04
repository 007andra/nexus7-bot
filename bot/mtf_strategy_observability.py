"""Core-owned, read-only observability decorator for Analyzer.analyze_mtf.

The wrapped strategy result is computed first and returned unchanged. Shadow
A/B telemetry is best-effort and cannot change a production signal, score,
threshold, risk rule, exchange state, or execution permission.
"""
from __future__ import annotations

from functools import wraps

from bot.logger import log


def observe_analyze_mtf(func):
    """Decorate ``Analyzer.analyze_mtf`` without changing its return value."""
    @wraps(func)
    def wrapped(self, symbol, k15, k1h, k4h,
                min_score=60, fee_mult=2.0, vol_mult=1.0):
        result = func(
            self, symbol, k15, k1h, k4h,
            min_score=min_score,
            fee_mult=fee_mult,
            vol_mult=vol_mult,
        )

        from bot.hard_gate_shadow_context import active
        if active():
            return result

        # Study only canonical setups that are HOLD because the 15m activity
        # ratio is below 0.40 while every other strategy-stage condition can
        # still be satisfied. This observer never returns a Signal or calls the
        # exchange/NEXUS path.
        try:
            from bot import volume_gate_shadow as volume_shadow
            volume_shadow.observe(
                symbol, k15, k1h, k4h,
                production_result=result,
                min_score=min_score,
                fee_mult=fee_mult,
                log=log,
            )
        except Exception as exc:
            log.debug(
                "[VOLUME_GATE_SHADOW] observability_failed error=%s "
                "decision_effect=NONE execution_effect=NONE",
                type(exc).__name__,
            )

        # Study canonical 55-59-style near misses without lowering the live
        # score floor. Enrollment requires every downstream strategy filter to
        # pass, so this cohort isolates score as the sole strategy-stage block.
        # Exact NEXUS evaluation and forward outcome tracking are read-only.
        try:
            from bot import score_floor_shadow as score_shadow
            score_shadow.observe(
                symbol, k15, k1h, k4h,
                production_result=result,
                min_score=min_score,
                fee_mult=fee_mult,
                log=log,
            )
        except Exception as exc:
            log.debug(
                "[SCORE_FLOOR_SHADOW] observability_failed error=%s "
                "decision_effect=NONE execution_effect=NONE",
                type(exc).__name__,
            )

        # Calibrate the existing pre-NEXUS session penalty without changing it.
        # Only Analyzer-approved signals that the current UTC-session table would
        # push below min_score are tracked. Their confirmed 15m TP/SL path is
        # observational and the production result is returned unchanged.
        try:
            from bot import session_penalty_shadow as session_shadow
            session_shadow.observe(
                symbol, k15, k1h, k4h,
                production_result=result,
                min_score=min_score,
                log=log,
            )
        except Exception as exc:
            log.debug(
                "[SESSION_PENALTY_SHADOW] observability_failed error=%s "
                "decision_effect=NONE execution_effect=NONE",
                type(exc).__name__,
            )

        try:
            # Lazy import avoids a module-import cycle: mtf_shadow consumes
            # strategy helpers, while strategy owns this decorator explicitly.
            from bot import mtf_shadow as ms

            before = ms.snapshot().get("unique_states", 0)
            ms.observe(
                symbol, k15, k1h, k4h,
                production_result=result,
                min_score=min_score,
                fee_mult=fee_mult,
                vol_mult=vol_mult,
            )
            snap = ms.snapshot()
            unique = snap.get("unique_states", 0)
            if unique != before and (unique == 1 or unique % 25 == 0):
                log.info(
                    "[MTF_SHADOW] unique=%s eligible=%s survivors=%s "
                    "nexus_approved=%s nexus_vetoed=%s execution_effect=NONE",
                    unique,
                    snap.get("eligible_4h_dir_1h_neutral", 0),
                    snap.get("shadow_pre_ai_survivors", 0),
                    snap.get("shadow_nexus_approved", 0),
                    snap.get("shadow_nexus_vetoed", 0),
                )
        except Exception as exc:
            log.debug(
                "[MTF_SHADOW] observability_failed error=%s "
                "decision_effect=NONE execution_effect=NONE",
                type(exc).__name__,
            )

        # Separate cohort for the fast-transition case that production cannot
        # currently trade: confirmed 4H is still opposite/neutral, while the
        # forming 4H has flipped into strong confirmed 1H+15M alignment.
        # This is deliberately observational until its forward outcomes show
        # positive expectancy after costs.
        try:
            from bot import htf_transition_shadow as transition
            transition.observe(symbol, k15, k1h, k4h, result, log)
        except Exception as exc:
            log.debug(
                "[HTF_TRANSITION_SHADOW] observability_failed error=%s "
                "decision_effect=NONE execution_effect=NONE",
                type(exc).__name__,
            )
        return result

    return wrapped
