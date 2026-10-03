"""
BGX Capital — Database Layer v2
PostgreSQL (Railway) com fallback SQLite.
Persiste: trades, signals, risk_events, news_events,
          market_snapshots, performance, consecutive_losses.
Fault-tolerant: se DB cair, bot continua operando.
"""
import os, json, asyncio
from datetime import datetime, timezone, date
from functools import wraps
from bot.logger import log

DATABASE_URL = os.environ.get("DATABASE_URL", "").replace("postgres://", "postgresql://")
SQLITE_PATH  = "/tmp/bgx_capital.db"
_conn        = None
_is_pg       = False
_io_lock     = asyncio.Lock()


class PersistenceError(RuntimeError):
    """Critical durable-state operation could not be confirmed."""


def _serialized_io(func):
    @wraps(func)
    async def wrapped(*args, **kwargs):
        async with _io_lock:
            return await func(*args, **kwargs)
    return wrapped


def _pg_sql(sql: str) -> str:
    """Translate qmark placeholders to asyncpg placeholders."""
    index = 0
    parts = []
    for char in sql:
        if char == "?":
            index += 1
            parts.append(f"${index}")
        else:
            parts.append(char)
    return "".join(parts)


def configured_postgres_unavailable() -> bool:
    """True when production requested PostgreSQL but only fallback/no DB exists."""
    return DATABASE_URL.startswith("postgresql") and (not _conn or not _is_pg)


async def init():
    global _conn, _is_pg
    if _conn:
        return
    if DATABASE_URL.startswith("postgresql"):
        try:
            import asyncpg
            _conn  = await asyncpg.connect(DATABASE_URL)
            _is_pg = True
            await _create_tables()
            try:
                ident = await _conn.fetchrow("SELECT current_database(), current_schema()")
                from bot.state_authority_observability import (
                    database_authority_fingerprint,
                    database_authority_id,
                )
                authority_id = database_authority_id()
                fingerprint = database_authority_fingerprint()
                log.info(
                    "[DB_IDENTITY] backend=postgres database=%s schema=%s "
                    "authority_id=%s fingerprint=%s credentials=REDACTED",
                    ident[0], ident[1], authority_id, fingerprint,
                )
                if authority_id in ("UNSET", "INVALID_NONSECRET_ID"):
                    log.warning(
                        "[DB_IDENTITY] operational_authority=UNKNOWN reason=%s "
                        "database_service_deletion_safe=false",
                        authority_id,
                    )
            except Exception as ident_exc:
                log.warning("[DB_IDENTITY] PostgreSQL identity unavailable: %s", ident_exc)
            log.info("✅ PostgreSQL conectado")
            return
        except Exception as e:
            # LIVE risk-critical state must never silently fall back to
            # ephemeral SQLite when PostgreSQL was explicitly configured.
            live_confirmed = os.environ.get("LIVE_TRADING_CONFIRMED", "") == "I_UNDERSTAND_THE_RISK"
            paper = os.environ.get("PAPER_TRADE", "true").strip().lower() == "true"
            if live_confirmed and not paper:
                log.critical("PostgreSQL indisponível em LIVE — persistência fail-closed")
                _conn = None
                _is_pg = False
                raise PersistenceError("configured PostgreSQL unavailable in LIVE") from e
            log.warning(f"PostgreSQL falhou ({e}) → usando SQLite fora de LIVE")
    # SQLite fallback
    try:
        import aiosqlite, os as _os
        _os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)
        _conn  = await aiosqlite.connect(SQLITE_PATH)
        _is_pg = False
        await _create_tables()
        storage_mode = os.environ.get("SHADOW_STORAGE_MODE", "").strip().upper()
        if storage_mode == "EPHEMERAL":
            log.warning("[STORAGE_IDENTITY] mode=EPHEMERAL backend=sqlite path=%s durable=false production_authority=false", SQLITE_PATH)
        log.info(f"✅ SQLite: {SQLITE_PATH}")
    except Exception as e:
        log.error(f"DB init falhou: {e} — sem persistência")
        _conn = None


# ── DDL ─────────────────────────────────────────────────────────
_DDL = [
    """CREATE TABLE IF NOT EXISTS trades (
        id SERIAL PRIMARY KEY,
        timestamp TEXT, strategy TEXT, side TEXT,
        symbol TEXT, entry_price REAL, exit_price REAL,
        size REAL, leverage INTEGER, pnl REAL, fees REAL,
        duration_minutes REAL, score_entrada INTEGER,
        status TEXT DEFAULT 'open',
        -- ITEM 2: componentes do score no momento da entrada (JSON).
        -- Sem isso, score_weights.calibrate_from_history() nunca teria
        -- dados e os pesos (+10/+5/+3) seguiriam sendo palpites manuais.
        score_features TEXT,
        -- ITEM 4: métricas de expectancy (o que realmente mede edge).
        -- risk_amount = |entry - sl| × qty  → 1R do trade
        -- r_multiple  = pnl / risk_amount   → resultado em múltiplos de R
        risk_amount REAL,
        r_multiple REAL,
        direction TEXT,
        exit_reason TEXT
    )""",
    """ALTER TABLE trades ADD COLUMN score_features TEXT""",
    """ALTER TABLE trades ADD COLUMN risk_amount REAL""",
    """ALTER TABLE trades ADD COLUMN r_multiple REAL""",
    """ALTER TABLE trades ADD COLUMN direction TEXT""",
    """ALTER TABLE trades ADD COLUMN exit_reason TEXT""",
    """CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)""",
    """CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)""",
    """CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp)""",
    """CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(timestamp)""",
    """CREATE INDEX IF NOT EXISTS idx_decisions_id ON decisions(id)""",
    """CREATE TABLE IF NOT EXISTS signals (
        id SERIAL PRIMARY KEY,
        timestamp TEXT, strategy TEXT, direction TEXT,
        symbol TEXT, score_total INTEGER,
        score_tecnico INTEGER, score_orderflow INTEGER,
        score_macro INTEGER, score_news INTEGER,
        entrou INTEGER DEFAULT 0, motivo_rejeicao TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS risk_events (
        id SERIAL PRIMARY KEY,
        timestamp TEXT, tipo_evento TEXT,
        descricao TEXT, pnl_acumulado REAL
    )""",
    """CREATE TABLE IF NOT EXISTS news_events (
        id SERIAL PRIMARY KEY,
        timestamp TEXT, titulo TEXT, fonte TEXT,
        classificacao TEXT, score_confianca REAL, impacto_no_score REAL
    )""",
    """CREATE TABLE IF NOT EXISTS market_snapshots (
        id SERIAL PRIMARY KEY,
        timestamp TEXT, symbol TEXT,
        open_interest REAL, funding_rate REAL,
        cvd REAL, btc_dominance REAL, fear_greed_index REAL
    )""",
    """CREATE TABLE IF NOT EXISTS performance (
        id SERIAL PRIMARY KEY,
        periodo TEXT, strategy TEXT,
        win_rate REAL, profit_factor REAL,
        sharpe_ratio REAL, sortino_ratio REAL,
        max_drawdown REAL, expectancy_por_trade REAL,
        total_trades INTEGER, updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS consecutive_losses (
        id SERIAL PRIMARY KEY,
        count INTEGER DEFAULT 0, last_loss TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS key_value (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS decisions (
        id SERIAL PRIMARY KEY,
        timestamp TEXT, symbol TEXT, type TEXT, score INTEGER, reason TEXT
    )""",
]

_DDL_SQLITE = [
    s.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
     .replace("TEXT PRIMARY KEY", "TEXT PRIMARY KEY")
    for s in _DDL
]


async def _create_tables():
    if not _conn:
        return
    stmts = _DDL if _is_pg else _DDL_SQLITE
    for stmt in stmts:
        try:
            if _is_pg:
                await _conn.execute(stmt)
            else:
                await _conn.execute(stmt)
                await _conn.commit()
        except Exception as e:
            log.warning(f"DDL: {e}")


def _now():
    return datetime.now(timezone.utc).isoformat()


async def _exec(sql: str, params: tuple = (), *, strict: bool = False):
    if not _conn:
        if strict:
            raise PersistenceError("database unavailable")
        return False
    async with _io_lock:
        try:
            if _is_pg:
                await _conn.execute(_pg_sql(sql), *params)
            else:
                await _conn.execute(sql, params)
                await _conn.commit()
            return True
        except Exception as e:
            log.error(f"DB exec: {e}")
            if strict:
                raise PersistenceError("database write failed") from e
            return False


async def _fetchone(sql: str, params: tuple = (), *, strict: bool = False):
    if not _conn:
        if strict:
            raise PersistenceError("database unavailable")
        return None
    async with _io_lock:
        try:
            if _is_pg:
                return await _conn.fetchrow(_pg_sql(sql), *params)
            async with _conn.execute(sql, params) as cur:
                return await cur.fetchone()
        except Exception as exc:
            log.warning("DB fetchone failed: %s", exc)
            if strict:
                raise PersistenceError("database read failed") from exc
            return None


async def _fetchall(sql: str, params: tuple = (), *, strict: bool = False):
    if not _conn:
        if strict:
            raise PersistenceError("database unavailable")
        return []
    async with _io_lock:
        try:
            if _is_pg:
                return await _conn.fetch(_pg_sql(sql), *params)
            async with _conn.execute(sql, params) as cur:
                return await cur.fetchall()
        except Exception as exc:
            log.warning("DB fetchall failed: %s", exc)
            if strict:
                raise PersistenceError("database read failed") from exc
            return []


@_serialized_io
async def save_trade_open(symbol, side, entry, size, leverage, score,
                          strategy="MTF", score_features: dict = None,
                          sl: float = 0, direction: str = "") -> int:
    import json as _json
    feats = _json.dumps(score_features or {})
    risk_amount = abs(entry - sl) * size if sl and entry else 0.0
    sql = """INSERT INTO trades
        (timestamp,strategy,side,symbol,entry_price,size,leverage,
         score_entrada,status,score_features,risk_amount,direction)
        VALUES (?,?,?,?,?,?,?,?,'open',?,?,?)"""
    params = (_now(), strategy, side, symbol, entry, size, leverage,
              score, feats, risk_amount, direction or side)
    if not _conn:
        raise PersistenceError("trade open blocked: database unavailable")
    try:
        if _is_pg:
            row = await _conn.fetchrow(_pg_sql(sql) + " RETURNING id", *params)
            trade_id = int(row[0]) if row else 0
        else:
            cursor = await _conn.execute(sql, params)
            trade_id = int(cursor.lastrowid or 0)
            await _conn.commit()
        if trade_id <= 0:
            raise PersistenceError("trade open insert returned no id")
        return trade_id
    except PersistenceError:
        raise
    except Exception as exc:
        if not _is_pg:
            try:
                await _conn.rollback()
            except Exception as rollback_exc:
                log.error("save_trade_open rollback failed: %s", rollback_exc)
        log.error("save_trade_open: %s", exc)
        raise PersistenceError("trade open insert failed") from exc


@_serialized_io
async def save_paper_open_atomic(
    symbol, side, entry, size, leverage, score, state_key, state_factory,
    strategy="MTF", score_features: dict = None, sl: float = 0,
    direction: str = "",
) -> int:
    if not _conn:
        raise PersistenceError("PAPER open blocked: database unavailable")
    feats = json.dumps(score_features or {})
    risk_amount = abs(entry - sl) * size if sl and entry else 0.0
    sql = """INSERT INTO trades
        (timestamp,strategy,side,symbol,entry_price,size,leverage,
         score_entrada,status,score_features,risk_amount,direction)
        VALUES (?,?,?,?,?,?,?,?,'open',?,?,?)"""
    params = (_now(), strategy, side, symbol, entry, size, leverage,
              score, feats, risk_amount, direction or side)
    ts = _now()
    try:
        if _is_pg:
            async with _conn.transaction():
                row = await _conn.fetchrow(_pg_sql(sql) + " RETURNING id", *params)
                trade_id = int(row[0]) if row else 0
                if trade_id <= 0:
                    raise PersistenceError("PAPER open insert returned no id")
                state_value = state_factory(trade_id)
                await _conn.execute(
                    "INSERT INTO key_value (key,value,updated_at) VALUES ($1,$2,$3) "
                    "ON CONFLICT (key) DO UPDATE SET value=$2,updated_at=$3",
                    state_key, state_value, ts,
                )
        else:
            await _conn.execute("BEGIN IMMEDIATE")
            cursor = await _conn.execute(sql, params)
            trade_id = int(cursor.lastrowid or 0)
            if trade_id <= 0:
                raise PersistenceError("PAPER open insert returned no id")
            state_value = state_factory(trade_id)
            await _conn.execute(
                "INSERT OR REPLACE INTO key_value (key,value,updated_at) VALUES (?,?,?)",
                (state_key, state_value, ts),
            )
            await _conn.commit()
        return trade_id
    except PersistenceError:
        if not _is_pg:
            try:
                await _conn.rollback()
            except Exception as rollback_exc:
                log.error("save_paper_open_atomic rollback failed: %s", rollback_exc)
        raise
    except Exception as exc:
        if not _is_pg:
            try:
                await _conn.rollback()
            except Exception as rollback_exc:
                log.error("save_paper_open_atomic rollback failed: %s", rollback_exc)
        log.error("save_paper_open_atomic: %s", exc)
        raise PersistenceError("atomic PAPER open failed") from exc


async def save_trade_close(trade_id: int, exit_price: float, pnl: float,
                           fees: float, duration_min: float,
                           exit_reason: str = "", *, strict: bool = False):
    row = await _fetchone(
        "SELECT risk_amount FROM trades WHERE id=?", (trade_id,), strict=strict
    )
    if strict and not row:
        raise PersistenceError(f"trade {trade_id} not found for close")
    risk_amount = float(row[0]) if row and row[0] else 0.0
    r_multiple  = (pnl / risk_amount) if risk_amount > 0 else 0.0
    sql = """UPDATE trades SET exit_price=?,pnl=?,fees=?,
             duration_minutes=?,status='closed',r_multiple=?,exit_reason=?
             WHERE id=?"""
    await _exec(sql, (exit_price, pnl, fees, duration_min,
                      round(r_multiple, 4), exit_reason, trade_id), strict=strict)
    await _update_performance()


@_serialized_io
async def _save_paper_close_atomic_locked(
    trade_id: int, exit_price: float, pnl: float, fees: float,
    duration_min: float, exit_reason: str, state_key: str, state_value: str,
):
    if not _conn:
        raise PersistenceError("PAPER close blocked: database unavailable")
    ts = _now()
    try:
        if _is_pg:
            async with _conn.transaction():
                row = await _conn.fetchrow(
                    "SELECT risk_amount FROM trades WHERE id=$1 FOR UPDATE", trade_id
                )
                if not row:
                    raise PersistenceError(f"trade {trade_id} not found for PAPER close")
                risk_amount = float(row[0] or 0)
                r_multiple = (pnl / risk_amount) if risk_amount > 0 else 0.0
                result = await _conn.execute(
                    "UPDATE trades SET exit_price=$1,pnl=$2,fees=$3,"
                    "duration_minutes=$4,status='closed',r_multiple=$5,exit_reason=$6 "
                    "WHERE id=$7 AND status='open'",
                    exit_price, pnl, fees, duration_min,
                    round(r_multiple, 4), exit_reason, trade_id,
                )
                if result != "UPDATE 1":
                    raise PersistenceError(f"trade {trade_id} was not open")
                await _conn.execute(
                    "INSERT INTO key_value (key,value,updated_at) VALUES ($1,$2,$3) "
                    "ON CONFLICT (key) DO UPDATE SET value=$2,updated_at=$3",
                    state_key, state_value, ts,
                )
        else:
            await _conn.execute("BEGIN IMMEDIATE")
            async with _conn.execute(
                "SELECT risk_amount FROM trades WHERE id=?", (trade_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                raise PersistenceError(f"trade {trade_id} not found for PAPER close")
            risk_amount = float(row[0] or 0)
            r_multiple = (pnl / risk_amount) if risk_amount > 0 else 0.0
            cursor = await _conn.execute(
                "UPDATE trades SET exit_price=?,pnl=?,fees=?,duration_minutes=?,"
                "status='closed',r_multiple=?,exit_reason=? "
                "WHERE id=? AND status='open'",
                (exit_price, pnl, fees, duration_min,
                 round(r_multiple, 4), exit_reason, trade_id),
            )
            if cursor.rowcount != 1:
                raise PersistenceError(f"trade {trade_id} was not open")
            await _conn.execute(
                "INSERT OR REPLACE INTO key_value (key,value,updated_at) VALUES (?,?,?)",
                (state_key, state_value, ts),
            )
            await _conn.commit()
        return True
    except PersistenceError:
        if not _is_pg:
            try:
                await _conn.rollback()
            except Exception as rollback_exc:
                log.error("save_paper_close_atomic rollback failed: %s", rollback_exc)
        raise
    except Exception as exc:
        if not _is_pg:
            try:
                await _conn.rollback()
            except Exception as rollback_exc:
                log.error("save_paper_close_atomic rollback failed: %s", rollback_exc)
        log.error("save_paper_close_atomic: %s", exc)
        raise PersistenceError("atomic PAPER close failed") from exc


async def save_paper_close_atomic(
    trade_id: int, exit_price: float, pnl: float, fees: float,
    duration_min: float, exit_reason: str, state_key: str, state_value: str,
):
    result = await _save_paper_close_atomic_locked(
        trade_id, exit_price, pnl, fees, duration_min,
        exit_reason, state_key, state_value,
    )
    await _update_performance()
    return result


async def save_signal(symbol, direction, scores: dict, entrou: bool, motivo=""):
    sql = """INSERT INTO signals
        (timestamp,symbol,direction,score_total,score_tecnico,
         score_orderflow,score_macro,score_news,entrou,motivo_rejeicao)
        VALUES (?,?,?,?,?,?,?,?,?,?)"""
    await _exec(sql, (
        _now(), symbol, direction,
        scores.get("total", 0), scores.get("tecnico", 0),
        scores.get("orderflow", 0), scores.get("macro", 0),
        scores.get("news", 0), 1 if entrou else 0, motivo,
    ))


async def save_news(titulo, fonte, classif, confianca, impacto):
    sql = """INSERT INTO news_events
        (timestamp,titulo,fonte,classificacao,score_confianca,impacto_no_score)
        VALUES (?,?,?,?,?,?)"""
    await _exec(sql, (_now(), titulo[:500], fonte, classif, confianca, impacto))


async def save_snapshot(symbol, oi, fr, cvd=0, btc_dom=0, fear_greed=0):
    sql = """INSERT INTO market_snapshots
        (timestamp,symbol,open_interest,funding_rate,cvd,btc_dominance,fear_greed_index)
        VALUES (?,?,?,?,?,?,?)"""
    await _exec(sql, (_now(), symbol, oi, fr, cvd, btc_dom, fear_greed))


async def log_decision(symbol: str, tipo: str, score: int, reason: str):
    sql = "INSERT INTO decisions (timestamp, symbol, type, score, reason) VALUES (?,?,?,?,?)"
    await _exec(sql, (_now(), symbol, tipo, score, reason))


async def save_risk_event(tipo, descricao, pnl_acum=0):
    sql = "INSERT INTO risk_events (timestamp,tipo_evento,descricao,pnl_acumulado) VALUES (?,?,?,?)"
    await _exec(sql, (_now(), tipo, descricao, pnl_acum))


async def update_consecutive_losses(pnl: float):
    row = await _fetchone("SELECT count FROM consecutive_losses ORDER BY id DESC LIMIT 1")
    current = row[0] if row else 0
    new_count = (current + 1) if pnl < 0 else 0
    await _exec("INSERT INTO consecutive_losses (count,last_loss) VALUES (?,?)",
                (new_count, _now()))
    if new_count >= 3:
        await save_risk_event(
            "CONSECUTIVE_LOSSES",
            f"{new_count} perdas consecutivas — registrado, bot continua operando",
        )
    return new_count


async def get_consecutive_losses() -> int:
    row = await _fetchone("SELECT count FROM consecutive_losses ORDER BY id DESC LIMIT 1")
    return row[0] if row else 0


async def _update_performance():
    import numpy as np
    rows = await _fetchall("SELECT pnl,fees FROM trades WHERE status='closed'")
    if not rows:
        return
    pnls  = [r[0] for r in rows]
    arr   = np.array(pnls)
    wins  = arr[arr > 0]
    losses= arr[arr <= 0]
    total = len(arr)
    wr    = len(wins)/total*100 if total else 0
    pf    = abs(wins.sum()/losses.sum()) if losses.sum() != 0 else 0
    sharpe= float(arr.mean()/arr.std()) if arr.std() > 0 and total > 1 else 0
    neg   = arr[arr < 0]
    sortino = float(arr.mean()/neg.std()) if len(neg) > 1 and neg.std() > 0 else 0
    cum   = np.cumsum(arr)
    peak  = np.maximum.accumulate(cum)
    max_dd= float((peak - cum).max()) if len(cum) else 0
    exp   = float(arr.mean()) if total else 0
    sql = """INSERT INTO performance
        (periodo,strategy,win_rate,profit_factor,sharpe_ratio,sortino_ratio,
         max_drawdown,expectancy_por_trade,total_trades,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)"""
    await _exec(sql, (
        date.today().isoformat(), "ALL",
        round(wr,1), round(pf,2), round(sharpe,3),
        round(sortino,3), round(max_dd,4), round(exp,4),
        total, _now(),
    ))


async def get_stats() -> dict:
    perf = await _fetchone("SELECT * FROM performance ORDER BY id DESC LIMIT 1")
    recent = await _fetchall(
        """SELECT symbol,side,entry_price,exit_price,pnl,fees,
                  score_entrada,duration_minutes,timestamp
           FROM trades WHERE status='closed'
           ORDER BY id DESC LIMIT 30"""
    )
    today = date.today().isoformat()
    today_rows = await _fetchall(
        "SELECT pnl,fees FROM trades WHERE status='closed' AND date(timestamp)=?",
        (today,)
    )
    consecutive = await get_consecutive_losses()
    today_pnl  = sum(r[0] for r in today_rows) if today_rows else 0
    today_fees = sum(r[1] for r in today_rows) if today_rows else 0
    today_wins = sum(1 for r in today_rows if r[0] > 0)
    return {
        "performance": {
            "win_rate": round(perf[3], 1) if perf else 0,
            "profit_factor": round(perf[4], 2) if perf else 0,
            "sharpe_ratio": round(perf[5], 3) if perf else 0,
            "sortino_ratio": round(perf[6], 3) if perf else 0,
            "max_drawdown": round(perf[7], 4) if perf else 0,
            "expectancy_por_trade": round(perf[8], 4) if perf else 0,
            "total_trades": perf[9] if perf else 0,
        },
        "today": {
            "pnl_net": round(today_pnl - today_fees, 4),
            "pnl_gross": round(today_pnl, 4),
            "fees": round(today_fees, 4),
            "trades": len(today_rows),
            "wins": today_wins,
        },
        "consecutive_losses": consecutive,
        "bot_paused": False,
        "recent_trades": [
            {
                "symbol": r[0], "side": r[1],
                "entry": round(r[2], 4), "exit": round(r[3], 4) if r[3] else None,
                "pnl": round(r[4], 4), "fees": round(r[5], 4),
                "score": r[6], "hold_min": round(r[7], 1) if r[7] else 0,
                "time": r[8],
            }
            for r in (recent or [])
        ],
    }


@_serialized_io
async def get_recent_decisions(limit: int = 60) -> list:
    if not _conn:
        return []
    try:
        _lim = max(1, min(1000, int(limit)))
        query = (
            "SELECT timestamp, symbol, type, score, reason "
            "FROM decisions ORDER BY id DESC LIMIT " + str(_lim)
        )
        if _is_pg:
            rows = await _conn.fetch(query)
            return [{"timestamp": str(r["timestamp"]), "symbol": r["symbol"],
                     "type": r["type"], "score": r["score"], "reason": r["reason"] or ""} for r in rows]
        async with _conn.execute(query) as cur:
            rows = await cur.fetchall()
        return [{"timestamp": str(r[0]), "symbol": r[1],
                 "type": r[2], "score": r[3], "reason": r[4] or ""} for r in rows]
    except Exception as e:
        log.error(f"get_recent_decisions: {e}")
        return []


@_serialized_io
async def save_key_value(key: str, value: str, *, strict: bool = False):
    if not _conn:
        if strict:
            raise PersistenceError(f"save_key_value {key}: database unavailable")
        return False
    try:
        ts = datetime.now(timezone.utc).isoformat()
        if _is_pg:
            await _conn.execute(
                "INSERT INTO key_value (key, value, updated_at) VALUES ($1,$2,$3) "
                "ON CONFLICT (key) DO UPDATE SET value=$2, updated_at=$3",
                key, value, ts
            )
        else:
            await _conn.execute(
                "INSERT OR REPLACE INTO key_value (key, value, updated_at) VALUES (?,?,?)",
                (key, value, ts)
            )
            await _conn.commit()
        return True
    except Exception as e:
        log.warning(f"save_key_value {key}: {e}")
        if strict:
            raise PersistenceError(f"save_key_value {key} failed") from e
        return False


async def load_key_value(key: str, *, strict: bool = False) -> str:
    """Load a durable value; NOVO-03: fall back to the previous release's key.

    Financial state moved from Railway-ID scopes to a stable financial scope.
    When the stable key is absent, the legacy key (same state, previous
    namespace) is read so an upgrade or a Railway project/environment change
    never makes a daily stop, HWM peak or exit idempotency disappear."""
    value = await _load_key_value_raw(key, strict=strict)
    if value is not None:
        return value
    try:
        from bot.financial_namespace import legacy_key_for
        legacy = legacy_key_for(key)
    except Exception:
        legacy = None
    if not legacy:
        return value
    value = await _load_key_value_raw(legacy, strict=strict)
    if value is not None:
        log.warning("[FINANCIAL_NAMESPACE] key_family=%s source=LEGACY_SCOPE migrated_on_next_write=true",
                    str(key).split(":", 1)[0])
    return value


@_serialized_io
async def _load_key_value_raw(key: str, *, strict: bool = False) -> str:
    if not _conn:
        if strict:
            raise PersistenceError(f"load_key_value {key}: database unavailable")
        return None
    try:
        if _is_pg:
            row = await _conn.fetchrow("SELECT value FROM key_value WHERE key=$1", key)
        else:
            async with _conn.execute("SELECT value FROM key_value WHERE key=?", (key,)) as cur:
                row = await cur.fetchone()
        return row[0] if row else None
    except Exception as exc:
        log.warning("load_key_value %s failed: %s", key, exc)
        if strict:
            raise PersistenceError(f"load_key_value {key} failed") from exc
        return None


@_serialized_io
async def get_trades_with_features(limit: int = 1000) -> list:
    if not _conn:
        return []
    try:
        query = (
            "SELECT symbol, direction, pnl, score, score_features, closed_at "
            "FROM trades ORDER BY closed_at ASC LIMIT " + str(int(limit))
        )
        if _is_pg:
            rows = await _conn.fetch(query)
            return [dict(r) for r in rows]
        cur  = await _conn.execute(query)
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        log.debug(f"get_trades_with_features: {e} (coluna score_features ausente?)")
        return []


async def get_expectancy_stats(days: int = None) -> dict:
    where = "WHERE status='closed'"
    params = ()
    if days:
        where += " AND timestamp >= ?"
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        params = (cutoff,)
    rows = await _fetchall(
        f"SELECT pnl, r_multiple, risk_amount FROM trades {where}", params
    )
    if not rows:
        return {"status": "no_data", "n_trades": 0,
                "reason": "nenhum trade fechado registrado ainda"}
    pnls = [float(r[0] or 0) for r in rows]
    rs   = [float(r[1] or 0) for r in rows if r[1] is not None]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n      = len(pnls)
    win_rate = len(wins) / n * 100 if n else 0.0
    avg_win  = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    payoff   = (avg_win / avg_loss) if avg_loss > 0 else 0.0
    pf       = (sum(wins) / abs(sum(losses))) if losses else float("inf")
    expectancy_r = sum(rs) / len(rs) if rs else 0.0
    expectancy_usd = sum(pnls) / n if n else 0.0
    breakeven_wr = (1 / (1 + payoff) * 100) if payoff > 0 else 100.0
    margem = win_rate - breakeven_wr
    max_wins = max_losses = cur_w = cur_l = 0
    for p in pnls:
        if p > 0:
            cur_w += 1; cur_l = 0; max_wins = max(max_wins, cur_w)
        elif p < 0:
            cur_l += 1; cur_w = 0; max_losses = max(max_losses, cur_l)
    if n < 30:
        verdict = f"amostra pequena ({n} trades) — sem significância estatística"
    elif expectancy_r > 0.1 and margem > 5:
        verdict = f"edge positivo: {margem:+.1f}pp acima do breakeven"
    elif expectancy_r > 0:
        verdict = f"marginal: apenas {margem:+.1f}pp acima do breakeven"
    else:
        verdict = f"SEM EDGE: expectancy {expectancy_r:.3f}R — estratégia perde no longo prazo"
    return {
        "status": "ok",
        "n_trades": n,
        "win_rate": round(win_rate, 1),
        "breakeven_wr": round(breakeven_wr, 1),
        "margem_pp": round(margem, 1),
        "expectancy_R": round(expectancy_r, 4),
        "expectancy_usd": round(expectancy_usd, 4),
        "payoff_ratio": round(payoff, 2),
        "profit_factor": round(pf, 2) if pf != float("inf") else None,
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "max_win_streak": max_wins,
        "max_loss_streak": max_losses,
        "total_pnl": round(sum(pnls), 4),
        "verdict": verdict,
    }


async def close():
    global _conn
    if not _conn:
        return
    try:
        await _conn.close()
        log.info("🗄️ Conexão com o banco encerrada")
    except Exception as e:
        log.warning(f"Erro ao fechar banco: {e}")
    finally:
        _conn = None
