"""Persistent counterfactual audit for approved and rejected NEXUS candidates.

Observability only: this module never authorizes execution or changes positions,
orders, risk limits, thresholds, leverage or exchange settings.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from typing import Any

from bot import database as db
from bot.execution_model import estimated_round_trip_cost_pct

# Backward-compatible public diagnostic used by older tests/telemetry. Runtime
# evaluation below is symbol-aware and does not rely on this single reference.
_ESTIMATED_ROUND_TRIP_COST_PCT = estimated_round_trip_cost_pct("BTCUSDT")


def _decision_cost_pct(decision, symbol):
    ctx = getattr(decision, "_bgx_nexus_cost_context", None)
    return ((float(ctx.taker_fee) + float(ctx.slippage)) * 200.0
            if ctx is not None else estimated_round_trip_cost_pct(symbol))


def _persisted_cost_pct(metadata_raw, symbol):
    saved = json.loads(metadata_raw or "{}")
    return _finite(saved.get("estimated_round_trip_cost_pct"), estimated_round_trip_cost_pct(symbol))

_TABLE_READY = False
_TABLE_LOCK = asyncio.Lock()
_LAST_EVAL_MONO = 0.0
_EVAL_INTERVAL_S = 30.0
_HORIZONS = ((900, "p15_net_pct"), (1800, "p30_net_pct"),
             (3600, "p60_net_pct"), (7200, "p120_net_pct"),
             (14400, "p240_net_pct"))
_RECORDED_KEYS: set[str] = set()
_NEAR_MISS_LOGGED: set[str] = set()
_FUNNEL = {"nexus_total": 0, "nexus_approved": 0, "nexus_rejected": 0}
_FUNNEL_LAST_LOG_MONO = 0.0


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _directional_return_pct(direction: str, entry: float, current: float) -> float:
    entry = _finite(entry)
    current = _finite(current)
    if entry <= 0 or current <= 0:
        return 0.0
    raw = (current - entry) / entry * 100.0
    return raw if str(direction).upper() == "LONG" else -raw


def _signal_key(symbol: str, direction: str, entry_type: str, epoch: float) -> str:
    bucket = int(float(epoch) // 900)
    return f"{symbol}:{str(direction).upper()}:{entry_type or 'UNKNOWN'}:{bucket}"


def _blocker_class(reason: str) -> str:
    """Normalize NEXUS WAIT reasons for false-negative calibration."""
    text = str(reason or "").lower()
    if any(x in text for x in ("r:r", "rr ", "ev ", "expected value", "líquido", "liquido")):
        return "EV_RR"
    if any(x in text for x in ("regime", "compat=", "incompatível", "incompativel")):
        return "REGIME"
    if any(x in text for x in ("score", "threshold", "qualidade")):
        return "SCORE"
    if any(x in text for x in ("timeframe", "mtf", "diverge", "conflito")):
        return "MTF"
    if any(x in text for x in ("data", "dados", "stale", "antigos", "candles insuficientes")):
        return "DATA"
    if any(x in text for x in ("news", "notícia", "noticia", "macro")):
        return "NEWS_RISK"
    if "timeout" in text:
        return "TIMEOUT"
    return "OTHER"


def _metadata_blocker(metadata_raw: Any, fallback: str, log) -> str:
    """Read persisted blocker metadata without letting malformed telemetry hide a row."""
    try:
        meta = json.loads(metadata_raw or "{}")
    except (TypeError, ValueError) as exc:
        log.debug(
            "[OPPORTUNITY_AUDIT] metadata_parse_fallback blocker=%s error=%s "
            "decision_effect=NONE execution_effect=NONE",
            fallback, type(exc).__name__,
        )
        return fallback
    if not isinstance(meta, dict):
        log.debug(
            "[OPPORTUNITY_AUDIT] metadata_type_fallback blocker=%s type=%s "
            "decision_effect=NONE execution_effect=NONE",
            fallback, type(meta).__name__,
        )
        return fallback
    return str(meta.get("blocker_class") or fallback)


def _compact_model_snapshot(decision) -> dict[str, dict]:
    """Return model evidence already attached to the final NEXUS decision."""
    out: dict[str, dict] = {}
    for item in getattr(decision, "models", None) or []:
        if isinstance(item, dict):
            name = str(item.get("name", "UNKNOWN")).upper()
            direction = str(item.get("direction", "WAIT")).upper()
            confidence = _finite(item.get("confidence"))
            risk = _finite(item.get("risk"))
            available = bool(item.get("available", True))
            model_reason = str(item.get("reason", ""))[:240]
        else:
            name = str(getattr(item, "name", "UNKNOWN")).upper()
            raw_direction = getattr(item, "direction", "WAIT")
            direction = str(getattr(raw_direction, "value", raw_direction)).upper()
            confidence = _finite(getattr(item, "confidence", 0.0))
            risk = _finite(getattr(item, "risk_score", 0.0))
            available = bool(getattr(item, "available", True))
            model_reason = str(getattr(item, "reason", ""))[:240]
        out[name] = {
            "direction": direction,
            "confidence": round(confidence, 2),
            "risk": round(risk, 2),
            "available": available,
            "reason": model_reason,
        }
    return out


def _structure_conflict_context(engine, sig, decision, reason: str) -> dict:
    """Describe 15m local-structure opposition behind an ensemble/MTF veto.

    This is strictly observational. It consumes the same timestamp-confirmed
    candle policy used by NEXUS and never changes the returned decision.
    """
    text = str(reason or "").lower()
    if "diverge do mtf" not in text:
        return {}

    models = _compact_model_snapshot(decision)
    structure_model = models.get("STRUCTURE") or {}
    candidate_direction = str(getattr(sig, "direction", "")).upper()
    structure_direction = str(structure_model.get("direction", "WAIT")).upper()
    if candidate_direction not in {"LONG", "SHORT"}:
        return {}
    if structure_direction not in {"LONG", "SHORT"}:
        return {}
    if structure_direction == candidate_direction:
        return {}

    context = {
        "classification": "LOCAL_STRUCTURE_OPPOSES_MTF_UNRESOLVED",
        "candidate_mtf_direction": candidate_direction,
        "structure_model_direction": structure_direction,
        "trend_model": models.get("TREND"),
        "structure_model": structure_model,
        "closed_candle_source": "timestamp_boundary",
        "execution_effect": "NONE",
    }

    try:
        from bot.indicators import smc_analysis
        from bot.nexus_decision_consistency import closed_mtf

        k15 = engine.client.get_cached_klines(sig.symbol, "15", 200)
        k1h = engine.client.get_cached_klines(sig.symbol, "60", 100)
        k4h = engine.client.get_cached_klines(sig.symbol, "240", 120)
        c15, _, _ = closed_mtf(k15, k1h, k4h)
        if len(c15) < 20:
            context["smc_available"] = False
            context["smc_reason"] = f"closed_15m={len(c15)}<20"
            return context

        highs = [float(k["h"]) for k in c15]
        lows = [float(k["l"]) for k in c15]
        closes = [float(k["c"]) for k in c15]
        smc = smc_analysis(highs, lows, closes)
        compact_smc = {
            "structure": str(smc.get("structure", "UNKNOWN")),
            "hh": bool(smc.get("hh")),
            "hl": bool(smc.get("hl")),
            "lh": bool(smc.get("lh")),
            "ll": bool(smc.get("ll")),
            "bos": bool(smc.get("bos")),
            "bos_dir": str(smc.get("bos_dir", "NONE")),
            "choch": bool(smc.get("choch")),
            "last_swing_high": _finite(smc.get("last_swing_high")),
            "last_swing_low": _finite(smc.get("last_swing_low")),
            "closed_15m": len(c15),
        }
        context["smc_available"] = True
        context["smc_15m"] = compact_smc
        if compact_smc["choch"]:
            context["classification"] = "LOCAL_STRUCTURE_OPPOSES_MTF_CHOCH"
        elif compact_smc["bos"]:
            context["classification"] = "LOCAL_STRUCTURE_OPPOSES_MTF_WITH_BOS"
        else:
            context["classification"] = "LOCAL_STRUCTURE_OPPOSES_MTF_NO_BOS"
    except Exception as exc:
        context["smc_available"] = False
        context["smc_reason"] = type(exc).__name__
    return context


async def _ensure_table(log) -> bool:
    global _TABLE_READY
    if _TABLE_READY:
        return True
    async with _TABLE_LOCK:
        if _TABLE_READY:
            return True
        sql = """CREATE TABLE IF NOT EXISTS opportunity_audit (
            signal_key TEXT PRIMARY KEY,
            created_at TEXT,
            created_epoch REAL,
            symbol TEXT,
            direction TEXT,
            entry_type TEXT,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            strategy_score REAL,
            nexus_score REAL,
            nexus_confidence REAL,
            nexus_regime TEXT,
            approved INTEGER,
            decision_reason TEXT,
            mfe_pct REAL DEFAULT 0,
            mae_pct REAL DEFAULT 0,
            last_net_pct REAL DEFAULT 0,
            p15_net_pct REAL,
            p30_net_pct REAL,
            p60_net_pct REAL,
            p120_net_pct REAL,
            p240_net_pct REAL,
            hypothetical_status TEXT DEFAULT 'OPEN',
            hit_epoch REAL,
            last_eval_epoch REAL,
            metadata TEXT
        )"""
        ok = await db._exec(sql)
        if ok:
            await db._exec(
                "CREATE INDEX IF NOT EXISTS idx_opportunity_audit_created "
                "ON opportunity_audit(created_epoch)"
            )
            await db._exec(
                "CREATE INDEX IF NOT EXISTS idx_opportunity_audit_symbol "
                "ON opportunity_audit(symbol)"
            )
            _TABLE_READY = True
            log.info(
                "[OPPORTUNITY_AUDIT] persistent counterfactual table ready; "
                "execution_effect=NONE"
            )
        return bool(ok)


def _decision_reason(decision) -> str:
    reasoning = getattr(decision, "reasoning", None) or []
    if reasoning:
        return str(reasoning[-1])[:1000]
    warnings = getattr(decision, "warnings", None) or []
    if warnings:
        return str(warnings[-1])[:1000]
    return str(getattr(decision, "decision", "UNKNOWN"))[:1000]


async def _record(engine, sig, decision, log) -> None:
    if not await _ensure_table(log):
        return
    now = time.time()
    key = str(getattr(sig, "_bgx_setup_id", "") or "") or _signal_key(
        sig.symbol, sig.direction, getattr(sig, "entry_type", "UNKNOWN"), now
    )
    if key in _RECORDED_KEYS:
        return

    existing = await db._fetchall(
        "SELECT signal_key FROM opportunity_audit WHERE signal_key=? LIMIT 1", (key,)
    )
    if existing:
        _RECORDED_KEYS.add(key)
        return

    approved = 1 if getattr(decision, "execution_allowed", None) is True else 0
    reason = _decision_reason(decision)
    blocker = "APPROVED" if approved else _blocker_class(reason)
    estimated_cost = _decision_cost_pct(decision, sig.symbol)
    structure_conflict = (
        _structure_conflict_context(engine, sig, decision, reason)
        if not approved and blocker == "MTF"
        else {}
    )
    metadata_obj = {
        "rr": _finite(getattr(sig, "rr", 0.0)),
        "expected_pnl": _finite(getattr(sig, "expected_pnl", 0.0)),
        "signal_regime": str(getattr(sig, "regime", "UNKNOWN")),
        "tf_4h": str(getattr(sig, "tf_4h", "")),
        "tf_1h": str(getattr(sig, "tf_1h", "")),
        "tf_15m": str(getattr(sig, "tf_15m", "")),
        "blocker_class": blocker,
        "estimated_round_trip_cost_pct": round(estimated_cost, 5),
    }
    if structure_conflict:
        metadata_obj["structure_conflict_shadow"] = structure_conflict
    metadata = json.dumps(metadata_obj, separators=(",", ":"), sort_keys=True)
    sql = """INSERT INTO opportunity_audit (
        signal_key,created_at,created_epoch,symbol,direction,entry_type,
        entry_price,stop_loss,take_profit,strategy_score,nexus_score,
        nexus_confidence,nexus_regime,approved,decision_reason,metadata
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(signal_key) DO NOTHING"""
    params = (
        key,
        __import__("datetime").datetime.fromtimestamp(
            now, tz=__import__("datetime").timezone.utc
        ).isoformat(),
        now,
        sig.symbol,
        sig.direction,
        getattr(sig, "entry_type", "UNKNOWN"),
        _finite(sig.entry),
        _finite(sig.sl),
        _finite(sig.tp),
        _finite(getattr(sig, "score", 0.0)),
        _finite(getattr(decision, "setup_quality", 0.0)),
        _finite(getattr(decision, "confidence", 0.0)),
        str(getattr(decision, "market_regime", "UNKNOWN")),
        approved,
        reason,
        metadata,
    )
    inserted = await db._exec(sql, params)
    _RECORDED_KEYS.add(key)
    if inserted:
        global _FUNNEL_LAST_LOG_MONO
        _FUNNEL["nexus_total"] += 1
        _FUNNEL["nexus_approved" if approved else "nexus_rejected"] += 1
        compact_reason = " ".join(str(reason).split())[:240]
        log.info(
            "[OPPORTUNITY_AUDIT] candidate=%s symbol=%s side=%s approved=%s "
            "blocker=%s strategy_score=%.1f nexus_score=%.1f nexus_confidence=%.3f "
            "nexus_regime=%s reason=%s cost=%.3f%% entry=%.8f "
            "decision_effect=NONE execution_effect=NONE",
            key, sig.symbol, sig.direction, bool(approved), blocker,
            _finite(getattr(sig, "score", 0.0)),
            _finite(getattr(decision, "setup_quality", 0.0)),
            _finite(getattr(decision, "confidence", 0.0)),
            str(getattr(decision, "market_regime", "UNKNOWN")),
            compact_reason, estimated_cost, _finite(sig.entry),
        )
        now_mono = time.monotonic()
        if now_mono - _FUNNEL_LAST_LOG_MONO >= 60.0:
            _FUNNEL_LAST_LOG_MONO = now_mono
            log.info(
                "[DECISION_FUNNEL] nexus_total=%d nexus_approved=%d nexus_rejected=%d "
                "scope=process_lifetime telemetry_only=true decision_effect=NONE "
                "execution_effect=NONE",
                _FUNNEL["nexus_total"], _FUNNEL["nexus_approved"],
                _FUNNEL["nexus_rejected"],
            )
        if structure_conflict:
            log.info(
                "[NEXUS_STRUCTURE_CONFLICT_SHADOW] symbol=%s side=%s class=%s "
                "structure_side=%s bos=%s choch=%s closed_15m=%s "
                "decision_effect=NONE execution_effect=NONE",
                sig.symbol,
                sig.direction,
                structure_conflict.get("classification"),
                structure_conflict.get("structure_model_direction"),
                (structure_conflict.get("smc_15m") or {}).get("bos"),
                (structure_conflict.get("smc_15m") or {}).get("choch"),
                (structure_conflict.get("smc_15m") or {}).get("closed_15m"),
            )


async def _evaluate_pending(engine, log) -> None:
    global _LAST_EVAL_MONO
    now_mono = time.monotonic()
    if now_mono - _LAST_EVAL_MONO < _EVAL_INTERVAL_S:
        return
    _LAST_EVAL_MONO = now_mono
    if not await _ensure_table(log):
        return

    now = time.time()
    rows = await db._fetchall(
        """SELECT signal_key,created_epoch,symbol,direction,entry_price,
                  stop_loss,take_profit,mfe_pct,mae_pct,
                  p15_net_pct,p30_net_pct,p60_net_pct,p120_net_pct,p240_net_pct,
                  hypothetical_status,approved,decision_reason,metadata
           FROM opportunity_audit
           WHERE created_epoch>=? AND (p240_net_pct IS NULL OR last_eval_epoch IS NULL)
           ORDER BY created_epoch ASC LIMIT 250""",
        (now - 18000.0,),
    )
    if not rows:
        return

    updated = 0
    near_miss_threshold = max(0.0, _finite(os.environ.get("OPPORTUNITY_NEAR_MISS_PCT", "0.50"), 0.50))
    for row in rows:
        try:
            (key, created_epoch, symbol, direction, entry, sl, tp,
             old_mfe, old_mae, p15, p30, p60, p120, p240, status,
             approved, decision_reason, metadata_raw) = row
            ticker = engine.client.get_cached_ticker(symbol) or {}
            current = _finite(ticker.get("lastPrice"))
            if current <= 0:
                continue
            cost_pct = _persisted_cost_pct(metadata_raw, symbol)
            raw_pct = _directional_return_pct(direction, entry, current)
            net_pct = raw_pct - cost_pct
            mfe = max(_finite(old_mfe), raw_pct)
            mae = min(_finite(old_mae), raw_pct)
            age = max(0.0, now - _finite(created_epoch))
            horizon_values = [p15, p30, p60, p120, p240]
            for idx, (seconds, _) in enumerate(_HORIZONS):
                if age >= seconds and horizon_values[idx] is None:
                    horizon_values[idx] = round(net_pct, 5)

            new_status = status or "OPEN"
            hit_epoch = None
            if new_status == "OPEN":
                is_long = str(direction).upper() == "LONG"
                tp_hit = (is_long and current >= _finite(tp)) or (
                    not is_long and current <= _finite(tp)
                )
                sl_hit = (is_long and current <= _finite(sl)) or (
                    not is_long and current >= _finite(sl)
                )
                if tp_hit and sl_hit:
                    new_status = "AMBIGUOUS"
                    hit_epoch = now
                elif tp_hit:
                    new_status = "TP_OBSERVED"
                    hit_epoch = now
                elif sl_hit:
                    new_status = "SL_OBSERVED"
                    hit_epoch = now

            await db._exec(
                """UPDATE opportunity_audit SET
                   mfe_pct=?,mae_pct=?,last_net_pct=?,p15_net_pct=?,
                   p30_net_pct=?,p60_net_pct=?,p120_net_pct=?,p240_net_pct=?,
                   hypothetical_status=?,hit_epoch=COALESCE(hit_epoch,?),last_eval_epoch=?
                   WHERE signal_key=?""",
                (round(mfe, 5), round(mae, 5), round(net_pct, 5),
                 horizon_values[0], horizon_values[1], horizon_values[2],
                 horizon_values[3], horizon_values[4], new_status, hit_epoch,
                 now, key),
            )
            updated += 1

            net_mfe = mfe - cost_pct
            if not int(approved or 0) and net_mfe >= near_miss_threshold and key not in _NEAR_MISS_LOGGED:
                blocker = _metadata_blocker(
                    metadata_raw, _blocker_class(decision_reason), log
                )
                _NEAR_MISS_LOGGED.add(key)
                log.warning(
                    "[OPPORTUNITY_NEAR_MISS] symbol=%s side=%s blocker=%s "
                    "net_mfe=%.3f%% raw_mfe=%.3f%% est_cost=%.3f%% age_s=%.0f "
                    "execution_effect=NONE",
                    symbol, direction, blocker, net_mfe, mfe, cost_pct, age,
                )
        except Exception as exc:
            log.debug(
                "[OPPORTUNITY_AUDIT] evaluation skipped key=%s error=%s",
                row[0] if row else "unknown", type(exc).__name__,
            )
    if updated:
        log.info(
            "[OPPORTUNITY_AUDIT] evaluated=%d pending counterfactual paths; "
            "sample_source=cached_ticker execution_effect=NONE",
            updated,
        )


async def observe(engine, sig, decision, log) -> None:
    """Best-effort telemetry; any failure is isolated from trading decisions."""
    try:
        await _evaluate_pending(engine, log)
        await _record(engine, sig, decision, log)
    except Exception as exc:
        log.warning(
            "[OPPORTUNITY_AUDIT] telemetry_error=%s execution_effect=NONE",
            type(exc).__name__,
        )
