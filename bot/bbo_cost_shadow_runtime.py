"""Runtime wiring for the Binance BBO cost shadow (SHADOW ONLY).

Two identity-preserving hooks, both fail-open for shadow data only:

1. ``BinanceClient.start_websocket``: awaits the original unchanged, then
   schedules a SEPARATE ``/public`` ``@bookTicker`` connection
   (``binance_bbo_feed``). The trading ``/market`` connection, its handler
   (``_handle_ws_message``, RUNTIME_CONTRACT-protected) and market-data health
   are not touched. The BBO cache lives in this module's registry; nothing in
   the trading path can read it.
2. ``TradingEngine._nexus_validate``: awaits the original and returns the exact
   champion object. A BBO snapshot copy is taken, and the comparison, log line
   and append-only persistence run in a background task.

No exchange private endpoint, order, position, sizing, risk, recovery,
leverage, universe, score, CROSS, predispatch, dispatch or SL/TP path is
called or modified. Any shadow failure yields SHADOW_DATA_UNAVAILABLE and
never blocks or alters trading. Existing fail-closed trading gates are
unchanged.

shadow_only=true decision_effect=NONE execution_effect=NONE live_authority_unchanged=true
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import hashlib
import json
import math
import os
import time
import weakref

from bot import bbo_cost_shadow_v1 as model
from bot import binance_bbo_feed as feed

FLAG = "NEXUS_BBO_COST_SHADOW"
PERSIST_FLAG = "NEXUS_BBO_COST_SHADOW_PERSIST"
SPREAD_REEMIT_BPS = 0.5  # observational de-dup only; never a decision input
AUTHORITY = {
    "shadow_only": True, "decision_effect": "NONE", "execution_effect": "NONE",
    "live_authority_unchanged": True, "decision_scope": "EV_RR_GATE",
    "impact_model": model.IMPACT_MODEL,
}

_TABLE_SQL = """CREATE TABLE IF NOT EXISTS nexus_bbo_cost_shadow_v1 (
    observation_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    observed_at REAL NOT NULL,
    bid REAL,
    ask REAL,
    mid REAL,
    spread_bps REAL,
    bbo_age_ms REAL,
    bbo_valid INTEGER NOT NULL,
    static_total_cost_bps REAL,
    live_total_cost_bps REAL,
    gross_rr REAL,
    rr_net_static REAL,
    rr_net_live REAL,
    ev_static REAL,
    ev_live REAL,
    static_allowed INTEGER,
    shadow_allowed INTEGER,
    would_change_decision INTEGER,
    authority_json TEXT NOT NULL,
    production_sha TEXT NOT NULL
)"""

_INSERT_SQL = """INSERT INTO nexus_bbo_cost_shadow_v1 (
    observation_id,candidate_id,symbol,side,observed_at,bid,ask,mid,spread_bps,
    bbo_age_ms,bbo_valid,static_total_cost_bps,live_total_cost_bps,gross_rr,
    rr_net_static,rr_net_live,ev_static,ev_live,static_allowed,shadow_allowed,
    would_change_decision,authority_json,production_sha
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(observation_id) DO NOTHING"""

try:
    _REGISTRY: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
except Exception:  # pragma: no cover
    _REGISTRY = {}
_FALLBACK_REGISTRY: dict[int, tuple] = {}


def enabled() -> bool:
    return os.environ.get(FLAG, "false").strip().lower() in {"1", "true", "yes", "on"}


def persist_enabled() -> bool:
    return os.environ.get(PERSIST_FLAG, "false").strip().lower() in {"1", "true", "yes", "on"}


def rr_floor() -> float:
    """Same expression as nexus_ai.decide."""
    from bot.config import cfg
    return float(os.environ.get("NEXUS_MIN_RR_NET", str(round(cfg.MIN_RR_RATIO * 0.80, 2))))


def cache_for(client) -> feed.BBOCache | None:
    try:
        entry = _REGISTRY.get(client)
    except TypeError:
        entry = _FALLBACK_REGISTRY.get(id(client))
    return entry[0] if entry else None


def snapshot_for_research(client, symbol):
    """Owner-side immutable handoff; no feed start, REST or decision evaluation."""
    if not enabled():
        return None
    cache = cache_for(client)
    return cache.snapshot(symbol) if cache is not None else None


def _register(client, cache, task) -> None:
    try:
        _REGISTRY[client] = (cache, task)
    except TypeError:
        _FALLBACK_REGISTRY[id(client)] = (cache, task)


def start_feed(client, symbols, log, *, connect=None, ws_base: str | None = None):
    """Idempotent: one BBO feed per client. Returns the task or None."""
    existing = cache_for(client)
    if existing is not None:
        return None
    from bot import binance as binance_mod

    syms = sorted({binance_mod.to_binance(s) for s in symbols if s})
    if not syms:
        return None
    cache = feed.BBOCache(syms)
    url = feed.stream_url(ws_base or binance_mod.WS_BASE, syms)
    task = asyncio.create_task(feed.run_feed(cache, url, log, connect=connect),
                               name="bbo-cost-shadow-feed")
    task.add_done_callback(lambda t: _consume(t, log))
    _register(client, cache, task)
    log.info("[BBO_SHADOW_FEED] installed=true route=/public streams=%d "
             "trading_market_ws_untouched=true shadow_only=true "
             "decision_effect=NONE execution_effect=NONE", len(syms))
    return task


def _consume(task, log) -> None:
    try:
        task.exception()
    except asyncio.CancelledError:
        return
    except Exception as exc:  # pragma: no cover - final containment
        log.warning("[BBO_SHADOW_FEED] task_error=%s decision_effect=NONE execution_effect=NONE",
                    type(exc).__name__)


def _candidate_id(sig) -> str:
    for attr in ("_bgx_setup_id", "candidate_id"):
        value = getattr(sig, attr, None)
        if value:
            return str(value)
    return (f"{getattr(sig, 'symbol', 'UNKNOWN')}:{str(getattr(sig, 'direction', 'NA')).upper()}:"
            f"{getattr(sig, 'entry_type', 'NA')}:{getattr(sig, '_bgx_formation_bucket', 'NA')}")


def _num(value) -> float | None:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def build_record(sig, decision, view: feed.BBOView | None, *, floor: float) -> model.ShadowRecord:
    """Pure: never mutates sig/decision; reads copies only."""
    from bot import execution_cost
    from bot.nexus_probability import heuristic_win_probability

    symbol = str(getattr(sig, "symbol", "UNKNOWN"))
    side = str(getattr(sig, "direction", "NA")).upper()
    entry, stop, target = (_num(getattr(sig, k, None)) for k in ("entry", "sl", "tp"))
    snap_meta = getattr(decision, "_bgx_score_snapshot", None)
    snap_meta = dict(snap_meta) if isinstance(snap_meta, dict) else {}
    confidence = _num(snap_meta.get("fusion_confidence"))
    champion_rr = _num(snap_meta.get("rr_net"))
    win_prob = heuristic_win_probability(confidence) if confidence is not None else None
    gross_rr = (abs(target - entry) / abs(entry - stop)) if entry and stop and target and entry != stop else None
    champion_decision = str(getattr(getattr(decision, "decision", None), "value",
                                    getattr(decision, "decision", "UNKNOWN")))
    champion_allowed = getattr(decision, "execution_allowed", False) is True

    def _base(status, **extra):
        values = dict(
            candidate_id=_candidate_id(sig), symbol=symbol, side=side,
            setup=str(getattr(sig, "entry_type", "NA")),
            regime=str(getattr(sig, "regime", None) or getattr(decision, "market_regime", None) or "UNKNOWN"),
            entry=entry or 0.0, stop=stop or 0.0,
            target=target or 0.0, gross_rr=gross_rr or 0.0, confidence=confidence,
            bid=None, ask=None, mid=None, spread_bps=None, bbo_valid=False,
            bbo_reason=view.reason if view else "MISSING_BOOK",
            bbo_age_ms=view.age_ms if view else None,
            bbo_generation=view.quote.generation if view and view.quote else None,
            bbo_update_id=view.quote.update_id if view and view.quote else None,
            costs=None, rr_net_static=None, rr_net_live=None, rr_net_live_spread_only=None,
            ev_static=None, ev_live=None, static_allowed=None, shadow_allowed=None,
            champion_decision=champion_decision, champion_execution_allowed=champion_allowed,
            champion_rr_net=champion_rr, static_parity=None, status=status,
        )
        values.update(extra)
        return model.ShadowRecord(**values)

    if None in (entry, stop, target) or entry == stop:
        return _base("SHADOW_DATA_UNAVAILABLE")
    static = execution_cost.reusable_snapshot(sig)
    if static is None:
        return _base("SHADOW_DATA_UNAVAILABLE")
    book = None
    if view is not None and view.valid and view.quote is not None:
        book = model.book_metrics(view.quote.bid, view.quote.ask)
    costs = model.cost_breakdown(symbol=symbol, taker_fee=static.taker_fee,
                                 entry_slippage=static.entry_slippage,
                                 exit_slippage=static.exit_slippage, book=book)
    g_static = model.nexus_ev_rr_gate(entry=entry, stop=stop, target=target, win_prob=win_prob,
                                      round_trip_cost_bps=costs.static_total_cost_bps, rr_floor=floor)
    rr_static = g_static.rr_net if g_static else (
        model.nexus_ev_rr_gate(entry=entry, stop=stop, target=target, win_prob=0.5,
                               round_trip_cost_bps=costs.static_total_cost_bps, rr_floor=floor).rr_net)
    parity = None if champion_rr is None else abs(rr_static - champion_rr) < 0.01
    extra = dict(costs=costs, rr_net_static=rr_static,
                 ev_static=g_static.ev_pct if g_static else None,
                 static_allowed=g_static.allowed if g_static else None, static_parity=parity)
    if book is None:
        return _base(f"BBO_{view.reason if view else 'MISSING_BOOK'}", **extra)
    g_live = model.nexus_ev_rr_gate(entry=entry, stop=stop, target=target, win_prob=win_prob,
                                    round_trip_cost_bps=costs.live_plus_static_impact_cost_bps,
                                    rr_floor=floor)
    g_spread = model.nexus_ev_rr_gate(entry=entry, stop=stop, target=target, win_prob=win_prob or 0.5,
                                      round_trip_cost_bps=costs.live_spread_only_cost_bps,
                                      rr_floor=floor)
    q = view.quote
    return _base("OK", bid=q.bid, ask=q.ask, mid=book.mid, spread_bps=book.spread_bps,
                 bbo_valid=True, bbo_reason="OK",
                 rr_net_live=g_live.rr_net if g_live else None,
                 rr_net_live_spread_only=g_spread.rr_net if g_spread else None,
                 ev_live=g_live.ev_pct if g_live else None,
                 shadow_allowed=g_live.allowed if g_live else None, **extra)


class EmitDeduper:
    """Once per candidate; re-emit only on a material spread move or validity change."""

    def __init__(self, max_keys: int = 4096):
        self._seen: OrderedDict = OrderedDict()
        self._max = max_keys

    def should_emit(self, record: model.ShadowRecord) -> bool:
        key = record.candidate_id
        state = (record.bbo_valid, record.status)
        prev = self._seen.get(key)
        if prev is not None:
            prev_state, prev_spread = prev
            moved = (record.spread_bps is not None and prev_spread is not None
                     and abs(record.spread_bps - prev_spread) >= SPREAD_REEMIT_BPS)
            if prev_state == state and not moved:
                return False
        self._seen[key] = (state, record.spread_bps)
        self._seen.move_to_end(key)
        while len(self._seen) > self._max:
            self._seen.popitem(last=False)
        return True


def observation_id(record: model.ShadowRecord) -> str:
    raw = "|".join(str(x) for x in (record.candidate_id, record.bbo_generation,
                                     record.bbo_update_id, record.status, record.spread_bps))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def persistence_row(record: model.ShadowRecord, observed_at: float) -> tuple:
    c = record.costs

    def b(v):
        return None if v is None else int(bool(v))

    return (
        observation_id(record), record.candidate_id, record.symbol, record.side, float(observed_at),
        record.bid, record.ask, record.mid, record.spread_bps, record.bbo_age_ms,
        int(record.bbo_valid), c.static_total_cost_bps if c else None,
        c.live_plus_static_impact_cost_bps if c else None, record.gross_rr,
        record.rr_net_static, record.rr_net_live, record.ev_static, record.ev_live,
        b(record.static_allowed), b(record.shadow_allowed), b(record.would_change_decision),
        json.dumps(AUTHORITY, sort_keys=True),
        os.environ.get("RAILWAY_GIT_COMMIT_SHA", "UNKNOWN") or "UNKNOWN",
    )


async def persist(db, record: model.ShadowRecord, observed_at: float) -> bool:
    """Append-only: INSERT ... ON CONFLICT DO NOTHING. Never UPDATE or DELETE."""
    await db._exec(_TABLE_SQL)
    return bool(await db._exec(_INSERT_SQL, persistence_row(record, observed_at)))


async def observe(sig, decision, view, log, deduper: EmitDeduper, *, db=None,
                  observed_at: float | None = None) -> model.ShadowRecord | None:
    """Background task body. Never raises."""
    try:
        record = build_record(sig, decision, view, floor=rr_floor())
    except Exception as exc:  # noqa: BLE001 - shadow data is fail-open
        log.warning("[COST_SHADOW_BBO] status=SHADOW_DATA_UNAVAILABLE stage=build error=%s "
                    "shadow_only=true decision_effect=NONE execution_effect=NONE",
                    type(exc).__name__)
        return None
    try:
        if deduper.should_emit(record):
            log.info(model.format_record(record))
            if persist_enabled():
                if db is None:
                    from bot import database as db
                await persist(db, record, time.time() if observed_at is None else observed_at)
    except Exception as exc:  # noqa: BLE001 - shadow IO is fail-open
        log.warning("[COST_SHADOW_BBO] status=SHADOW_DATA_UNAVAILABLE stage=emit error=%s "
                    "shadow_only=true decision_effect=NONE execution_effect=NONE",
                    type(exc).__name__)
    return record


def install(TradingEngine, BinanceClient, log) -> None:
    if getattr(TradingEngine, "_bbo_cost_shadow_installed", False):
        return
    if not enabled():
        TradingEngine._bbo_cost_shadow_installed = True
        log.info("[COST_SHADOW_BBO] enabled=false feature_flag=%s "
                 "decision_effect=NONE execution_effect=NONE", FLAG)
        return

    deduper = EmitDeduper()
    original_start = BinanceClient.start_websocket
    original_validate = TradingEngine._nexus_validate

    async def start_websocket_with_bbo_shadow(self, symbols, *args, **kwargs):
        result = await original_start(self, symbols, *args, **kwargs)
        try:
            start_feed(self, list(symbols), log)
        except Exception as exc:  # noqa: BLE001 - shadow feed is fail-open
            log.warning("[BBO_SHADOW_FEED] status=SHADOW_DATA_UNAVAILABLE start_error=%s "
                        "decision_effect=NONE execution_effect=NONE", type(exc).__name__)
        return result

    async def nexus_validate_with_bbo_shadow(self, sig, *args, **kwargs):
        decision = await original_validate(self, sig, *args, **kwargs)
        try:
            cache = cache_for(getattr(self, "client", None))
            view = cache.snapshot(getattr(sig, "symbol", "")) if cache is not None else None
            task = asyncio.create_task(observe(sig, decision, view, log, deduper),
                                       name="bbo-cost-shadow-observe")
            task.add_done_callback(lambda t: _consume(t, log))
        except Exception as exc:  # noqa: BLE001 - scheduling can never alter the champion
            log.warning("[COST_SHADOW_BBO] status=SHADOW_DATA_UNAVAILABLE stage=schedule error=%s "
                        "decision_effect=NONE execution_effect=NONE", type(exc).__name__)
        return decision  # identity-preserving: the exact champion object

    BinanceClient.start_websocket = start_websocket_with_bbo_shadow
    TradingEngine._nexus_validate = nexus_validate_with_bbo_shadow
    TradingEngine._bbo_cost_shadow_installed = True
    log.info("[COST_SHADOW_BBO] installed=true source=binance_public_bookTicker "
             "route=/public trading_market_ws_untouched=true champion_authority_unchanged=true "
             "persistence=append_only shadow_only=true decision_effect=NONE "
             "execution_effect=NONE live_authority_unchanged=true")


__all__ = ["AUTHORITY", "EmitDeduper", "build_record", "cache_for", "enabled", "install",
           "observation_id", "observe", "persist", "persistence_row", "start_feed"]
