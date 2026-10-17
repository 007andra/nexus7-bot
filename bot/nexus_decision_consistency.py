"""Closed-candle parity, HTF regime parity and score telemetry for NEXUS.

The canonical strategy and NEXUS must consume the same *confirmed* market bars.
Historically both layers inferred "the open candle" from list length/position,
which can discard an already-closed bar or keep a still-forming one. This module
now delegates closed-bar selection to the timestamp/timeframe integrity policy.

It also installs an independent hard candle-integrity check at the NEXUS data
validation boundary. Critically stale, gapped, or timestamp-less production
series fail closed even if the softer DataQuality score would otherwise remain
numerically acceptable.

Regime detection continues to use confirmed 4H context while a NEXUS entry
decision is being evaluated. Read-only score decomposition is preserved. No
threshold, risk limit or exchange permission is changed here.
"""
from __future__ import annotations

from bot.nexus_probability import heuristic_win_probability
from contextvars import ContextVar
from typing import Any, Dict, Iterable, Tuple

from bot.market_data_integrity import (
    closed_candles,
    has_usable_timestamps,
    validate_nexus_candles,
)


_MIN_CONFIRMED = {"15m": 60, "1h": 40, "4h": 20}
_INTERVAL = {"15m": "15", "1h": "60", "4h": "240"}
_HTF_REGIME_CONTEXT: ContextVar[tuple | None] = ContextVar(
    "nexus_htf_regime_context", default=None
)


def _closed_view(klines: Iterable[dict] | None, timeframe: str) -> list:
    """Return a copy ending at the last timestamp-confirmed candle.

    Synthetic unit fixtures with non-exchange timestamps retain the historical
    fallback solely so unrelated isolated tests can run. Production KuCoin
    series always carry real timestamps and therefore take the strict path.
    """
    data = list(klines or [])
    if has_usable_timestamps(data):
        return closed_candles(data, _INTERVAL[timeframe], require_timestamps=True)

    minimum = _MIN_CONFIRMED[timeframe]
    if len(data) > minimum:
        return data[:-1]
    return data


def closed_mtf(k15, k1h, k4h) -> Tuple[list, list, list]:
    return (
        _closed_view(k15, "15m"),
        _closed_view(k1h, "1h"),
        _closed_view(k4h, "4h"),
    )


def _series(klines: list) -> tuple[list, list, list, list]:
    return (
        [float(k["c"]) for k in klines],
        [float(k["h"]) for k in klines],
        [float(k["l"]) for k in klines],
        [float(k.get("v", 0)) for k in klines],
    )


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _score_snapshot(nexus_ai, *, symbol: str, k15: list, k1h: list, k4h: list,
                    entry: float, sl: float, tp: float, funding=None,
                    oi_delta=None, ls_ratio=None, news_score=None,
                    ticker=None, oi=None, orderbook=None) -> Dict[str, Any]:
    """Recompute deterministic score ingredients for observability only."""
    try:
        closes, highs, lows, volumes = _series(k15)
        if not closes:
            return {"ok": False, "reason": "no_closed_15m"}

        dq = nexus_ai.validate_data(
            symbol, k15, k1h, k4h, ticker=ticker, funding=funding,
            oi=oi, orderbook=orderbook,
        )
        regime, _ = nexus_ai.detect_regime(closes, highs, lows, volumes)
        mtf = nexus_ai.analyze_mtf(k15, k1h, k4h)
        models = nexus_ai.run_ensemble(
            closes, highs, lows, volumes,
            funding=funding, oi_delta=oi_delta, ls_ratio=ls_ratio,
        )
        fusion = nexus_ai._fuse(models, regime)
        direction = fusion.get("direction")
        win_prob = heuristic_win_probability(fusion.get("confidence"))
        ev = nexus_ai.expected_value(win_prob, entry, sl, tp)
        if direction is None or not ev.get("valid"):
            return {
                "ok": True,
                "regime": getattr(regime, "value", str(regime)),
                "regime_source": "4H_CONFIRMED",
                "mtf": mtf.get("detail", ""),
                "fusion_direction": getattr(direction, "value", str(direction)),
                "fusion_confidence": _safe_float(fusion.get("confidence")),
                "fusion_risk": _safe_float(fusion.get("risk")),
                "data_quality": _safe_float(getattr(dq, "score", 0.0)),
                "ev_valid": bool(ev.get("valid")),
                "rr_net": _safe_float(ev.get("rr_net")),
                "ev_pct": _safe_float(ev.get("ev_pct")),
            }

        sc = nexus_ai._score_components(models, mtf, ev["rr_net"], direction, regime)
        component_score = _safe_float(sc.get("total"))
        risk_penalty = _safe_float(fusion.get("risk")) * 0.20
        after_risk = max(0.0, component_score - risk_penalty)

        news_adjustment = 0.0
        if news_score is not None:
            news_value = _safe_float(news_score)
            direction_value = getattr(direction, "value", str(direction))
            aligned = (
                (direction_value == "LONG" and news_value > 0)
                or (direction_value == "SHORT" and news_value < 0)
            )
            adjustment = min(5.0, abs(news_value) / 20.0)
            news_adjustment = adjustment if aligned else -adjustment

        after_news = after_risk + news_adjustment
        data_quality = _safe_float(getattr(dq, "score", 0.0))
        estimated_final = round(
            max(0.0, min(100.0, after_news * data_quality / 100.0)), 2
        )
        return {
            "ok": True,
            "regime": getattr(regime, "value", str(regime)),
            "regime_source": "4H_CONFIRMED",
            "mtf": mtf.get("detail", ""),
            "fusion_direction": getattr(direction, "value", str(direction)),
            "fusion_confidence": _safe_float(fusion.get("confidence")),
            "fusion_risk": _safe_float(fusion.get("risk")),
            "components": sc.get("components", {}),
            "unavailable": sc.get("unavailable", []),
            "component_score": round(component_score, 2),
            "risk_penalty": round(risk_penalty, 2),
            "news_adjustment": round(news_adjustment, 2),
            "data_quality": round(data_quality, 2),
            "estimated_final": estimated_final,
            "rr_net": _safe_float(ev.get("rr_net")),
            "ev_pct": _safe_float(ev.get("ev_pct")),
        }
    except Exception as exc:
        return {"ok": False, "reason": type(exc).__name__}


def _attach_score_snapshot(decision, snapshot: dict, log) -> bool:
    """Attach observability-only score telemetry; never affects the decision.

    Replaces a bare ``except Exception: pass`` flagged by the silent-except
    audit. Frozen/slotted decisions (or any other attribute failure) are
    reported at debug level and the unchanged decision is still returned.
    """
    try:
        setattr(decision, "_bgx_score_snapshot", snapshot)
        return True
    except Exception as exc:  # noqa: BLE001 - telemetry must never alter the decision
        debug = getattr(log, "debug", None)
        if callable(debug):
            debug(
                "[NEXUS_SCORE_SNAPSHOT] attach_failed=%s decision_type=%s "
                "decision_effect=NONE execution_effect=NONE",
                type(exc).__name__, type(decision).__name__,
            )
        return False


def install(nexus_ai, log) -> None:
    if getattr(nexus_ai, "_closed_candle_consistency_installed", False):
        return

    original_decide = nexus_ai.decide
    original_detect_regime = nexus_ai.detect_regime
    original_validate_data = getattr(nexus_ai, "validate_data", None)

    # Independent NEXUS boundary: stale/gapped/timestamp-less production
    # candles are a hard data error, not a soft score penalty.
    if callable(original_validate_data):
        def validate_data_hard(symbol, k15, k1h, k4h, *args, **kwargs):
            dq = original_validate_data(symbol, k15, k1h, k4h, *args, **kwargs)
            ok, reason, _ = validate_nexus_candles(k15, k1h, k4h)
            if not ok:
                dq.mark_error(f"CRITICAL_CANDLE_INTEGRITY:{reason}", 100.0)
            return dq

        nexus_ai.validate_data = validate_data_hard

    def detect_regime_with_htf_context(closes, highs, lows, volumes):
        htf = _HTF_REGIME_CONTEXT.get()
        if htf is not None:
            return original_detect_regime(*htf)
        return original_detect_regime(closes, highs, lows, volumes)

    nexus_ai.detect_regime = detect_regime_with_htf_context

    def decide_closed_candles(symbol: str, k15: list, k1h: list, k4h: list, *args, **kwargs):
        c15, c1h, c4h = closed_mtf(k15, k1h, k4h)
        token = _HTF_REGIME_CONTEXT.set(_series(c4h))
        try:
            decision = original_decide(symbol, c15, c1h, c4h, *args, **kwargs)

            entry = _safe_float(kwargs.get("entry", args[0] if len(args) > 0 else 0.0))
            sl = _safe_float(kwargs.get("sl", args[1] if len(args) > 1 else 0.0))
            tp = _safe_float(kwargs.get("tp", args[2] if len(args) > 2 else 0.0))
            snapshot = _score_snapshot(
                nexus_ai,
                symbol=symbol,
                k15=c15, k1h=c1h, k4h=c4h,
                entry=entry, sl=sl, tp=tp,
                funding=kwargs.get("funding"),
                oi_delta=kwargs.get("oi_delta"),
                ls_ratio=kwargs.get("ls_ratio"),
                news_score=kwargs.get("news_score"),
                ticker=kwargs.get("ticker"),
                oi=kwargs.get("oi"),
                orderbook=kwargs.get("orderbook"),
            )
            log.info(
                "[NEXUS_SCORE_DECOMP] symbol=%s decision=%s final=%.2f "
                "estimated_final=%s component_score=%s risk_penalty=%s "
                "dq=%s news_adj=%s confidence=%s rr_net=%s ev=%s regime=%s "
                "regime_source=%s mtf=%s components=%s unavailable=%s "
                "closed_candles=15m:%d,1h:%d,4h:%d execution_effect=NONE",
                symbol,
                getattr(decision, "decision", "UNKNOWN"),
                _safe_float(getattr(decision, "setup_quality", 0.0)),
                snapshot.get("estimated_final", "N/A"),
                snapshot.get("component_score", "N/A"),
                snapshot.get("risk_penalty", "N/A"),
                snapshot.get("data_quality", "N/A"),
                snapshot.get("news_adjustment", "N/A"),
                snapshot.get("fusion_confidence", "N/A"),
                snapshot.get("rr_net", "N/A"),
                snapshot.get("ev_pct", "N/A"),
                snapshot.get("regime", "N/A"),
                snapshot.get("regime_source", "N/A"),
                snapshot.get("mtf", "N/A"),
                snapshot.get("components", {}),
                snapshot.get("unavailable", []),
                len(c15), len(c1h), len(c4h),
            )

            # Preserve read-only score telemetry on the decision so the async
            # engine can notify Telegram after returning from asyncio.to_thread.
            # Dynamic metadata is intentionally excluded from NexusDecision.to_dict().
            _attach_score_snapshot(decision, snapshot, log)
            return decision
        finally:
            _HTF_REGIME_CONTEXT.reset(token)

    nexus_ai.decide = decide_closed_candles
    nexus_ai._closed_candle_consistency_installed = True
    log.warning(
        "[NEXUS_CLOSED_CANDLE_PARITY] installed "
        "closed_by=timestamp_boundary strategy_ai_same_confirmed_candle_semantics=true "
        "entry_regime_source=4H_CONFIRMED hard_freshness_fail_closed=true "
        "score_decomposition=true thresholds_unchanged=true execution_effect=NONE"
    )
