"""Noise-resistant discretionary exits with explicit lifecycle telemetry.

Native SL/TP and emergency protection remain untouched. This module only
replaces the strategy-driven stagnation/invalidation/regime checker installed
by ``stagnation_time_hardening``.

Policy:
- stagnation remains eligible only after 4h / 16 completed 15m bars;
- CHoCH invalidation and regime-change exits are deferred during the Position's
  existing 90-minute minimum-hold window, preventing immediate churn from
  short-lived structure noise;
- once eligible, every strategy-driven close logs reason, duration, quantity,
  entry/current price and estimated gross/fees/net before dispatch, plus the
  exchange acknowledgement after dispatch.
"""
from __future__ import annotations

from datetime import datetime, timezone
import time

from bot.exchange import TAKER_FEE
from bot.stagnation_time_hardening import bars_since_open, STAGNATION_BARS, STAGNATION_MULT


def _opened_timestamp(opened_at) -> float:
    if isinstance(opened_at, datetime):
        dt = opened_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return float(opened_at)


def _duration_seconds(pos) -> float:
    try:
        return max(0.0, time.time() - _opened_timestamp(pos.opened_at))
    except Exception:
        return 0.0


def _min_hold_remaining(pos) -> float:
    configured = getattr(pos, "min_hold_until", None)
    if isinstance(configured, (int, float)) and not isinstance(configured, bool):
        configured_f = float(configured)
        if configured_f == configured_f and configured_f not in (float("inf"), float("-inf")):
            return max(0.0, configured_f - time.time())
    # Position historically defines a 90-minute minimum hold. Keep the same
    # policy even for a reconstructed object that lacks a usable min_hold_until.
    try:
        fallback_until = _opened_timestamp(pos.opened_at) + (90 * 60)
        return max(0.0, fallback_until - time.time())
    except Exception:
        return 0.0


def _pnl_estimate(pos, current: float) -> tuple[float, float, float]:
    qty = max(0.0, float(getattr(pos, "qty", 0.0) or 0.0))
    entry = float(getattr(pos, "entry", 0.0) or 0.0)
    direction = str(getattr(pos, "direction", "")).upper()
    if qty <= 0 or entry <= 0 or current <= 0:
        return 0.0, 0.0, 0.0
    gross = (current - entry) * qty if direction == "LONG" else (entry - current) * qty
    fees = (entry * qty + current * qty) * float(TAKER_FEE)
    return gross, fees, gross - fees


async def _close_with_telemetry(engine, log, sym: str, pos, current: float, reason: str):
    qty = float(getattr(pos, "qty", 0.0) or 0.0)
    entry = float(getattr(pos, "entry", 0.0) or 0.0)
    duration_s = _duration_seconds(pos)
    gross, fees, net = _pnl_estimate(pos, current)
    log.warning(
        "[EXIT_DECISION] symbol=%s reason=%s duration_s=%.1f duration_min=%.2f "
        "direction=%s qty=%.12g entry=%.8f current=%.8f "
        "gross_pnl_est=%.6f fees_roundtrip_est=%.6f net_pnl_est=%.6f action=CLOSE",
        sym,
        reason,
        duration_s,
        duration_s / 60.0,
        getattr(pos, "direction", "?"),
        qty,
        entry,
        current,
        gross,
        fees,
        net,
    )
    close_side = "Sell" if str(getattr(pos, "direction", "")).upper() == "LONG" else "Buy"
    result = await engine.client.place_order(
        symbol=sym,
        side=close_side,
        qty=qty,
        sl=0,
        tp=0,
        instruments=engine.instruments,
        reduce_only=True,
    )
    order_id = None
    if isinstance(result, dict):
        order_id = result.get("orderId") or result.get("order_id")
    log.warning(
        "[EXIT_DISPATCH] symbol=%s reason=%s result=%s order_id=%s "
        "duration_s=%.1f execution_effect=REDUCE_ONLY_CLOSE",
        sym,
        reason,
        "ACK" if result else "NO_ACK",
        order_id or "unknown",
        duration_s,
    )
    return result


def install(TradingEngine, log) -> None:
    if getattr(TradingEngine, "_operator_exit_policy_installed", False):
        return

    async def _check_stagnation_and_invalidation(self):
        from bot.indicators import atr as calc_atr
        from bot.strategy import detect_regime

        for sym, pos in list(self.positions.items()):
            try:
                k15 = self.client.get_cached_klines(sym, "15", limit=100) or []
                if len(k15) < 20:
                    continue

                closes = [float(k["c"]) for k in k15[:-1]]
                highs = [float(k["h"]) for k in k15[:-1]]
                lows = [float(k["l"]) for k in k15[:-1]]
                cur = float(getattr(pos, "current_price", 0.0) or closes[-1])

                atr_val = float(calc_atr(highs, lows, closes, 14)[-1])
                if atr_val <= 0:
                    continue

                movement = abs(cur - float(pos.entry))
                bars_open = bars_since_open(pos.opened_at)
                if bars_open >= STAGNATION_BARS and movement < atr_val * STAGNATION_MULT:
                    log.info(
                        "[EXIT_TRIGGER] symbol=%s reason=STAGNATION bars_open=%s "
                        "movement=%.8f threshold=%.8f",
                        sym, bars_open, movement, atr_val * STAGNATION_MULT,
                    )
                    await _close_with_telemetry(self, log, sym, pos, cur, "STAGNATION_4H")
                    continue

                hold_remaining = _min_hold_remaining(pos)

                if len(closes) >= 10:
                    recent_c = closes[-10:]
                    recent_h = highs[-10:]
                    recent_l = lows[-10:]
                    choch_bear = (
                        recent_h[-1] < recent_h[-3]
                        and recent_l[-1] < recent_l[-3]
                        and recent_c[-1] < recent_c[-3]
                    )
                    choch_bull = (
                        recent_l[-1] > recent_l[-3]
                        and recent_h[-1] > recent_h[-3]
                        and recent_c[-1] > recent_c[-3]
                    )
                    invalidated = (
                        (pos.direction == "LONG" and choch_bear)
                        or (pos.direction == "SHORT" and choch_bull)
                    )
                    if invalidated and not pos.tp1_hit:
                        if hold_remaining > 0:
                            log.info(
                                "[EXIT_DEFERRED] symbol=%s reason=CHOCH_OPPOSITE "
                                "min_hold_remaining_s=%.1f execution_effect=NONE",
                                sym, hold_remaining,
                            )
                        else:
                            await _close_with_telemetry(
                                self, log, sym, pos, cur, "CHOCH_OPPOSITE"
                            )
                            continue

                k4h = self.client.get_cached_klines(sym, "240", limit=120) or []
                if len(k4h) >= 20:
                    c4h = [float(k["c"]) for k in k4h[:-1]]
                    h4h = [float(k["h"]) for k in k4h[:-1]]
                    l4h = [float(k["l"]) for k in k4h[:-1]]
                    atr4h = float(calc_atr(h4h, l4h, c4h, 14)[-1])
                    regime_now = detect_regime(c4h, h4h, l4h, atr4h)
                    if regime_now in ("RANGING", "COMPRESSED", "CHOPPY") and not pos.tp1_hit:
                        if hold_remaining > 0:
                            log.info(
                                "[EXIT_DEFERRED] symbol=%s reason=REGIME_%s "
                                "min_hold_remaining_s=%.1f execution_effect=NONE",
                                sym, regime_now, hold_remaining,
                            )
                        else:
                            await _close_with_telemetry(
                                self, log, sym, pos, cur, f"REGIME_{regime_now}"
                            )
                            continue
            except Exception as exc:
                log.error("_check_stagnation_and_invalidation %s: %s", sym, exc)

    TradingEngine._check_stagnation_and_invalidation = _check_stagnation_and_invalidation
    TradingEngine._operator_exit_policy_installed = True
    log.critical(
        "[EXIT_POLICY] installed min_hold=90m for CHoCH/regime exits "
        "stagnation=4h native_sl_tp_unchanged=true telemetry=enabled"
    )
