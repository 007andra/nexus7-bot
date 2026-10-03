"""Persistent NEXUS AI decision ledger + deterministic shadow evaluation.

Observability only. This module never submits, amends, cancels, or closes orders.
It records frozen decision inputs and evaluates what happened afterwards using
future CLOSED 15-minute candles. Same-candle SL+TP is marked AMBIGUOUS and is
excluded from expectancy to avoid optimistic attribution.
"""
import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone

from bot.logger import log

_DB_URL = os.environ.get("DATABASE_URL", "").replace("postgres://", "postgresql://")
_SQLITE = os.environ.get("NEXUS_DB_PATH", "/tmp/nexus_ai_history.db")
_conn = None
_is_pg = False
_lock = asyncio.Lock()
# Separate from the initialization lock. _init() calls refresh_metrics(), which
# calls _fetchall(); sharing the same lock would deadlock that bootstrap path.
# This lock serializes all operations on the module's single DB connection.
_io_lock = asyncio.Lock()
_metrics_cache = {
    "storage": "uninitialized",
    "total": 0,
    "approved": 0,
    "vetoed": 0,
    "pending_shadow": 0,
    "approved_shadow_n": 0,
    "approved_shadow_expectancy_r": None,
    "vetoed_shadow_n": 0,
    "vetoed_shadow_expectancy_r": None,
    "ai_value_added_r": None,
    "ambiguous": 0,
    "updated_at": None,
}

_DDL = """
CREATE TABLE IF NOT EXISTS nexus_decisions (
    decision_id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    approved INTEGER NOT NULL,
    nexus_score REAL,
    confidence REAL,
    regime TEXT,
    entry REAL NOT NULL,
    sl REAL NOT NULL,
    tp REAL NOT NULL,
    rr REAL,
    reason TEXT,
    raw_json TEXT,
    shadow_status TEXT DEFAULT 'PENDING',
    shadow_r REAL,
    shadow_exit_price REAL,
    shadow_evaluated_at REAL
)
"""


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _field(obj, *names, default=None):
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj.get(name)
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


async def _init():
    global _conn, _is_pg
    if _conn is not None:
        return
    async with _lock:
        if _conn is not None:
            return
        if _DB_URL.startswith("postgresql"):
            try:
                import asyncpg
                _conn = await asyncpg.connect(_DB_URL)
                _is_pg = True
                await _conn.execute(_DDL)
                await _conn.execute("CREATE INDEX IF NOT EXISTS idx_nexus_ts ON nexus_decisions(ts)")
                await _conn.execute("CREATE INDEX IF NOT EXISTS idx_nexus_shadow ON nexus_decisions(shadow_status, ts)")
                log.info("✅ NEXUS history: PostgreSQL conectado")
                await refresh_metrics()
                return
            except Exception as exc:
                log.warning(f"NEXUS history PostgreSQL indisponível ({type(exc).__name__}) → SQLite")
        try:
            import aiosqlite
            _conn = await aiosqlite.connect(_SQLITE)
            _is_pg = False
            await _conn.execute(_DDL.replace("INTEGER NOT NULL", "INTEGER NOT NULL"))
            await _conn.execute("CREATE INDEX IF NOT EXISTS idx_nexus_ts ON nexus_decisions(ts)")
            await _conn.execute("CREATE INDEX IF NOT EXISTS idx_nexus_shadow ON nexus_decisions(shadow_status, ts)")
            await _conn.commit()
            log.info(f"✅ NEXUS history: SQLite {_SQLITE}")
            await refresh_metrics()
        except Exception as exc:
            _conn = None
            log.error(f"NEXUS history init falhou: {type(exc).__name__}: {exc}")


def _pg_sql(sql):
    n = 0
    out = []
    for ch in sql:
        if ch == "?":
            n += 1
            out.append(f"${n}")
        else:
            out.append(ch)
    return "".join(out)


async def _execute(sql, params=()):
    await _init()
    if _conn is None:
        return
    async with _io_lock:
        try:
            if _is_pg:
                await _conn.execute(_pg_sql(sql), *params)
            else:
                await _conn.execute(sql, params)
                await _conn.commit()
        except Exception as exc:
            log.warning(f"NEXUS history write: {type(exc).__name__}: {exc}")


async def _fetchall(sql, params=()):
    await _init()
    if _conn is None:
        return []
    async with _io_lock:
        try:
            if _is_pg:
                return [tuple(r) for r in await _conn.fetch(_pg_sql(sql), *params)]
            async with _conn.execute(sql, params) as cur:
                return await cur.fetchall()
        except Exception as exc:
            log.warning(f"NEXUS history read: {type(exc).__name__}: {exc}")
            return []


async def record_decision(sig, decision):
    """Freeze one valid NEXUS decision. No execution-side effects."""
    await _init()
    if _conn is None:
        return None
    approved = bool(_field(decision, "execution_allowed", default=False) is True)
    raw = decision.to_dict() if hasattr(decision, "to_dict") else (decision if isinstance(decision, dict) else {})
    if isinstance(raw, dict):
        raw = dict(raw)
        try:
            from bot.candidate_trace import candidate_id_from_decision
            _candidate_id = candidate_id_from_decision(decision)
            if _candidate_id:
                raw["_candidate_id"] = _candidate_id
        except Exception:
            pass
        raw["_signal_score"] = _to_float(getattr(sig, "score", None))
        raw["_signal_regime"] = str(
            getattr(sig, "regime", "UNKNOWN") or "UNKNOWN"
        )
        raw["_signal_entry_type"] = str(
            getattr(sig, "entry_type", "UNKNOWN") or "UNKNOWN"
        )
        try:
            ctx = getattr(decision, "_bgx_nexus_cost_context", None)
            snap = getattr(ctx, "snapshot", None)
            if snap is not None:
                raw["_cost"] = {
                    "snapshot_id": str(getattr(snap, "snapshot_id", "") or ""),
                    "candidate_id": str(getattr(snap, "candidate_id", "") or ""),
                    "exchange": str(getattr(snap, "exchange", "") or ""),
                    "taker_fee": _to_float(getattr(snap, "taker_fee", None)),
                    "entry_slippage": _to_float(
                        getattr(snap, "entry_slippage", None)
                    ),
                    "exit_slippage": _to_float(
                        getattr(snap, "exit_slippage", None)
                    ),
                    "spread_bps": _to_float(getattr(snap, "spread_bps", None)),
                    "funding_rate": _to_float(
                        getattr(snap, "funding_rate", None)
                    ),
                    "fee_source": str(
                        getattr(snap, "fee_source", "") or ""
                    ),
                    "slippage_source": str(
                        getattr(snap, "slippage_source", "") or ""
                    ),
                    "fallback": bool(getattr(snap, "fallback", False)),
                }
        except Exception:
            pass
    score = _to_float(_field(decision, "setup_quality", "score", "nexus_score"))
    confidence = _to_float(_field(decision, "confidence"))
    regime = str(_field(decision, "regime", default="UNKNOWN") or "UNKNOWN")
    reason = _field(decision, "primary_reason", "reasoning", "reason", default="approved" if approved else "ai_veto")
    if isinstance(reason, (list, dict)):
        reason = json.dumps(reason, ensure_ascii=False)[:1000]
    entry = float(getattr(sig, "entry", 0) or 0)
    sl = float(getattr(sig, "sl", 0) or 0)
    tp = float(getattr(sig, "tp", 0) or 0)
    if min(entry, sl, tp) <= 0:
        return None
    rr = abs(tp-entry) / abs(entry-sl) if abs(entry-sl) > 0 else None
    did = str(uuid.uuid4())
    await _execute(
        """INSERT INTO nexus_decisions
        (decision_id,ts,symbol,side,approved,nexus_score,confidence,regime,entry,sl,tp,rr,reason,raw_json,shadow_status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (did, time.time(), str(sig.symbol), str(sig.direction), 1 if approved else 0,
         score, confidence, regime, entry, sl, tp, rr, str(reason)[:1000],
         json.dumps(raw, ensure_ascii=False, default=str)[:12000], "PENDING"),
    )
    await refresh_metrics()
    return did


def _candle_ts(c):
    if not isinstance(c, dict):
        return None
    for k in ("ts", "timestamp", "time", "startAt", "start"):
        if k in c and c[k] is not None:
            try:
                x = float(c[k])
                if x > 1e12:
                    x /= 1000.0
                return x
            except (TypeError, ValueError):
                # A malformed timestamp candidate is skipped; other known keys
                # are still tried before declaring the candle unusable.
                continue
    return None


def _ohlc(c):
    if not isinstance(c, dict):
        return None
    try:
        return float(c["h"]), float(c["l"]), float(c["c"])
    except Exception:
        return None


async def evaluate_pending(client, horizon_candles=16):
    """Evaluate pending APPROVE and VETO decisions with future closed candles.

    No order API is called. Outcome is purely observational.
    """
    rows = await _fetchall(
        "SELECT decision_id,ts,symbol,side,entry,sl,tp FROM nexus_decisions WHERE shadow_status='PENDING' ORDER BY ts ASC LIMIT 100"
    )
    if not rows:
        return
    for did, ts, symbol, side, entry, sl, tp in rows:
        try:
            kl = client.get_cached_klines(symbol, "15", 200) or []
            if len(kl) < 5:
                continue
            future = []
            for c in kl:
                cts = _candle_ts(c)
                if cts is None or cts <= float(ts):
                    continue
                future.append(c)
            if not future:
                continue
            future = future[:int(horizon_candles)]
            risk = abs(float(entry)-float(sl))
            if risk <= 0:
                await _execute("UPDATE nexus_decisions SET shadow_status='INVALID',shadow_evaluated_at=? WHERE decision_id=?", (time.time(), did))
                continue
            status = None
            exit_px = None
            r = None
            for c in future:
                vals = _ohlc(c)
                if vals is None:
                    continue
                high, low, close = vals
                if str(side).upper() == "LONG":
                    hit_sl = low <= float(sl)
                    hit_tp = high >= float(tp)
                else:
                    hit_sl = high >= float(sl)
                    hit_tp = low <= float(tp)
                if hit_sl and hit_tp:
                    status, exit_px, r = "AMBIGUOUS", None, None
                    break
                if hit_sl:
                    status, exit_px, r = "SL", float(sl), -1.0
                    break
                if hit_tp:
                    status, exit_px = "TP", float(tp)
                    r = abs(float(tp)-float(entry)) / risk
                    break
            if status is None and len(future) >= int(horizon_candles):
                last = _ohlc(future[-1])
                if last:
                    close = last[2]
                    direction = 1.0 if str(side).upper() == "LONG" else -1.0
                    r = direction * (close-float(entry)) / risk
                    status, exit_px = "HORIZON", close
            if status is not None:
                await _execute(
                    "UPDATE nexus_decisions SET shadow_status=?,shadow_r=?,shadow_exit_price=?,shadow_evaluated_at=? WHERE decision_id=?",
                    (status, r, exit_px, time.time(), did),
                )
        except Exception as exc:
            log.debug(f"NEXUS shadow {symbol}: {type(exc).__name__}: {exc}")
    await refresh_metrics()


async def refresh_metrics():
    rows = await _fetchall("SELECT approved,shadow_status,shadow_r FROM nexus_decisions")
    if _conn is None:
        return _metrics_cache
    approved = vetoed = pending = ambiguous = 0
    ar, vr = [], []
    for a, status, r in rows:
        if int(a or 0):
            approved += 1
        else:
            vetoed += 1
        if status == "PENDING":
            pending += 1
        if status == "AMBIGUOUS":
            ambiguous += 1
        if r is not None and status not in ("PENDING", "AMBIGUOUS", "INVALID"):
            (ar if int(a or 0) else vr).append(float(r))
    aexp = sum(ar)/len(ar) if ar else None
    vexp = sum(vr)/len(vr) if vr else None
    _metrics_cache.update({
        "storage": "postgresql" if _is_pg else "sqlite_fallback",
        "total": approved + vetoed,
        "approved": approved,
        "vetoed": vetoed,
        "pending_shadow": pending,
        "approved_shadow_n": len(ar),
        "approved_shadow_expectancy_r": round(aexp, 4) if aexp is not None else None,
        "vetoed_shadow_n": len(vr),
        "vetoed_shadow_expectancy_r": round(vexp, 4) if vexp is not None else None,
        "ai_value_added_r": round(aexp-vexp, 4) if aexp is not None and vexp is not None else None,
        "ambiguous": ambiguous,
        "updated_at": _now_iso(),
        "method": "shadow_15m_16_candles; same-candle TP+SL excluded",
    })
    return _metrics_cache


def get_cached_metrics():
    return dict(_metrics_cache)


async def recent(limit=50):
    limit = max(1, min(int(limit), 200))
    rows = await _fetchall(
        f"SELECT decision_id,ts,symbol,side,approved,nexus_score,confidence,regime,entry,sl,tp,rr,reason,shadow_status,shadow_r FROM nexus_decisions ORDER BY ts DESC LIMIT {limit}"
    )
    keys = ["decision_id","ts","symbol","side","approved","nexus_score","confidence","regime","entry","sl","tp","rr","reason","shadow_status","shadow_r"]
    return [dict(zip(keys, r)) for r in rows]


async def recent_observations(limit=300):
    """Return recent shadow rows including private telemetry payloads."""
    limit = max(1, min(int(limit), 1000))
    rows = await _fetchall(
        f"SELECT ts,symbol,side,approved,nexus_score,confidence,regime,"
        f"raw_json,shadow_status,shadow_r FROM nexus_decisions "
        f"ORDER BY ts DESC LIMIT {limit}"
    )
    result = []
    for row in rows:
        (
            ts, symbol, side, approved, nexus_score, confidence, regime,
            raw_json, shadow_status, shadow_r,
        ) = row
        try:
            raw = json.loads(raw_json) if raw_json else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = {}
        result.append({
            "ts": _to_float(ts),
            "symbol": str(symbol or ""),
            "side": str(side or ""),
            "approved": bool(int(approved or 0)),
            "nexus_score": _to_float(nexus_score),
            "confidence": _to_float(confidence),
            "regime": str(regime or "UNKNOWN"),
            "raw": raw if isinstance(raw, dict) else {},
            "shadow_status": str(shadow_status or "UNKNOWN"),
            "shadow_r": _to_float(shadow_r),
        })
    return result


async def close():
    global _conn
    if _conn is None:
        return
    try:
        await _conn.close()
    except Exception as exc:
        # Shutdown must remain best-effort, but a failed close should not vanish.
        log.debug("NEXUS history close failed: %s: %s", type(exc).__name__, exc)
    _conn = None
