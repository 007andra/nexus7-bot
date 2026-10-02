"""Durable LIVE exit geometry across restarts (Q-01B).

One record per trade lineage (the exact opening order id, the same identity
used by ``partial_exit_v1``/``rr_exit_v1``), written atomically as one value:

  immutable  opening_order_id, client_oid, symbol, direction, entry,
             initial_sl, initial_tp, initial_qty          (history of the trade)
  evolving   peak_price (monotonic favorable extreme), tp1_hit,
             last_known_qty (informational only)

The exchange stays the authority for CURRENT exposure (quantity, live stop);
this record is the authority for the trade's HISTORY. Nothing is invented:
no record / corrupt record / lineage mismatch -> initial risk stays unknown and
R-based exits fail closed while protection is untouched.
"""
from __future__ import annotations

import hashlib
import json
import math
import time

from bot.logger import log

SCHEMA_VERSION = 1


def _key(opening_order_id):
    from bot.durable_daily_stop import state_key
    token = hashlib.sha256(str(opening_order_id).encode()).hexdigest()[:32]
    return state_key("exit_geometry_v1") + ":" + token


def lineage_id(position):
    lineage = getattr(position, "_forensic_lineage", None) or {}
    if not isinstance(lineage, dict):
        return ""
    return str(lineage.get("opening_order_id") or lineage.get("order_id") or "").strip()


def _positive(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def build_record(position):
    oid = lineage_id(position)
    lineage = getattr(position, "_forensic_lineage", None) or {}
    record = {
        "version": SCHEMA_VERSION,
        "opening_order_id": oid,
        "client_oid": str(lineage.get("client_oid") or "") if isinstance(lineage, dict) else "",
        "symbol": str(position.symbol),
        "direction": str(position.direction),
        "entry": float(position.entry),
        "initial_sl": getattr(position, "initial_sl", None),
        "initial_tp": float(position.tp),
        "initial_qty": float(getattr(position, "qty_original", position.qty)),
        "peak_price": float(getattr(position, "peak_price", position.entry)),
        "tp1_hit": bool(getattr(position, "tp1_hit", False)),
        "last_known_qty": float(position.qty),
    }
    reason = validate(record)
    if reason:
        raise ValueError(reason)
    return record


def validate(record, *, symbol=None, direction=None, opening_order_id=None, entry=None):
    """Return a rejection reason or ``None``. Never partially accepts a record."""
    if not isinstance(record, dict):
        return "not_mapping"
    if record.get("version") != SCHEMA_VERSION:
        return "unsupported_version"
    oid = str(record.get("opening_order_id") or "")
    if not oid:
        return "lineage_missing"
    d = record.get("direction")
    if d not in ("LONG", "SHORT"):
        return "direction_invalid"
    values = {k: _positive(record.get(k)) for k in
              ("entry", "initial_sl", "initial_tp", "initial_qty", "peak_price")}
    for k, v in values.items():
        if v is None:
            return f"{k}_invalid"
    e, sl, tp, peak = values["entry"], values["initial_sl"], values["initial_tp"], values["peak_price"]
    if d == "LONG" and not (sl < e < tp and peak >= e):
        return "long_geometry_invalid"
    if d == "SHORT" and not (tp < e < sl and peak <= e):
        return "short_geometry_invalid"
    if not isinstance(record.get("tp1_hit"), bool):
        return "tp1_hit_invalid"
    if opening_order_id is not None and oid != str(opening_order_id):
        return "lineage_mismatch"
    if symbol is not None and str(record.get("symbol")) != str(symbol):
        return "lineage_mismatch_symbol"
    if direction is not None and d != direction:
        return "lineage_mismatch_direction"
    if entry is not None:
        live = _positive(entry)
        if live is None or abs(live - e) > max(1e-12, e * 1e-3):
            return "lineage_mismatch_entry"
    return None


def _comparable(record):
    return {k: v for k, v in record.items() if k != "updated_at_ms"}


async def persist(position, reason, *, tick_size=None):
    """Write the record when the trade history changed materially."""
    from bot import database as db
    if not lineage_id(position) or getattr(position, "initial_sl", None) is None:
        return False
    record = build_record(position)
    previous = getattr(position, "_exit_geometry_persisted", None)
    if isinstance(previous, dict) and not _material_change(previous, record, tick_size):
        return True
    record["updated_at_ms"] = int(time.time() * 1000)
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if await db.save_key_value(_key(record["opening_order_id"]), encoded, strict=True) is not True:
        raise db.PersistenceError("exit geometry persistence unconfirmed")
    position._exit_geometry_persisted = _comparable(record)
    log.info(
        "[EXIT_GEOMETRY_PERSISTED] symbol=%s side=%s lineage=%s initial_sl=%s peak_price=%s "
        "tp1_hit=%s remaining_qty=%s schema_version=%s reason=%s",
        record["symbol"], record["direction"], record["opening_order_id"][:16],
        record["initial_sl"], record["peak_price"], record["tp1_hit"],
        record["last_known_qty"], SCHEMA_VERSION, reason,
    )
    return True


def _material_change(previous, record, tick_size):
    if any(previous.get(k) != record.get(k) for k in
           ("opening_order_id", "direction", "entry", "initial_sl", "initial_tp",
            "initial_qty", "tp1_hit", "last_known_qty")):
        return True
    tick = _positive(tick_size) or 0.0
    old, new = float(previous.get("peak_price") or 0.0), record["peak_price"]
    favorable = new - old if record["direction"] == "LONG" else old - new
    return favorable > 0 and favorable >= tick


async def sync(engine):
    """LIVE loop hook: persist new peaks / partial state, never raising."""
    for symbol, position in list((getattr(engine, "positions", {}) or {}).items()):
        try:
            info = (getattr(engine, "instruments", None) or {}).get(symbol) or {}
            await persist(position, "loop_sync", tick_size=info.get("tickSize"))
        except Exception as exc:
            log.error("[EXIT_GEOMETRY_PERSIST_FAILED] symbol=%s error=%s", symbol, type(exc).__name__)


async def _exchange_stop(client, symbol, direction):
    """Most protective verified full-coverage stop currently on the exchange."""
    from bot.conditional_stop_protection import _instrument_info, _protective_order, read_stop_orders
    rows = await client.get_positions()
    row = next((r for r in rows or [] if r.get("symbol") == symbol), None)
    if row is None:
        return None
    triggers = []
    inline = _positive(row.get("stopLoss"))
    if inline:
        triggers.append(inline)
    orders = await read_stop_orders(client, symbol)
    info = _instrument_info(client, symbol)
    for order in orders or []:
        qualifies, full_close, amount = _protective_order(order, row, symbol, info)
        price = _positive(order.get("stopPrice"))
        if qualifies and price and (full_close or amount > 0):
            triggers.append(price)
    if not triggers:
        return None
    return max(triggers) if direction == "LONG" else min(triggers)


def apply_record(position, record):
    """Apply a VALIDATED record: history from durable state, exposure untouched.

    qty (current exposure) and the live stop are never taken from the record;
    the known peak can only be extended, never reduced (INV-MFE-DURABILITY-001).
    """
    position.initial_sl = float(record["initial_sl"])
    position.tp = float(record["initial_tp"])
    position.qty_original = float(record["initial_qty"])
    position.tp1_hit = bool(getattr(position, "tp1_hit", False) or record["tp1_hit"])
    peak = float(record["peak_price"])
    live_peak = _positive(getattr(position, "peak_price", None)) or peak
    position.peak_price = max(peak, live_peak) if position.direction == "LONG" else min(peak, live_peak)
    position._exit_geometry_persisted = _comparable(record)


async def restore(engine, symbol):
    """Re-attach durable history to a position recovered by the ownership proof."""
    from bot import database as db
    position = (getattr(engine, "positions", {}) or {}).get(symbol)
    if position is None:
        return False
    try:
        stop = await _exchange_stop(engine.client, symbol, position.direction)
    except Exception as exc:
        stop = None
        log.error("[EXIT_GEOMETRY_RESTORE] symbol=%s current_stop_read_failed=%s", symbol,
                  type(exc).__name__)
    if stop is not None:
        # CURRENT protection comes from the exchange, never from a startup estimate.
        position.sl = position.trailing_sl = stop
    oid = lineage_id(position)
    reason = None
    record = None
    if not oid:
        reason = "lineage_missing"
    else:
        try:
            raw = await db.load_key_value(_key(oid), strict=True)
            record = json.loads(raw) if raw else None
            reason = "exit_geometry_unavailable" if record is None else validate(
                record, symbol=symbol, direction=position.direction,
                opening_order_id=oid, entry=position.entry)
        except Exception as exc:
            reason = f"read_failed:{type(exc).__name__}"
    if reason:
        position.initial_sl = None
        tag = "EXIT_GEOMETRY_LINEAGE_MISMATCH" if reason.startswith("lineage_mismatch") \
            else "EXIT_GEOMETRY_RESTORE_REJECTED"
        log.critical(
            "[%s] symbol=%s side=%s lineage=%s reason=%s r_exits=FAIL_CLOSED "
            "protection=UNCHANGED schema_version=%s",
            tag, symbol, position.direction, oid[:16] or "NONE", reason, SCHEMA_VERSION,
        )
        return False
    apply_record(position, record)
    log.warning(
        "[EXIT_GEOMETRY_RESTORED] symbol=%s side=%s lineage=%s initial_sl=%s peak_price=%s "
        "tp1_hit=%s remaining_qty=%s current_sl=%s schema_version=%s qty_source=EXCHANGE "
        "peak_note=last_known_no_downtime_backfill",
        symbol, position.direction, oid[:16], position.initial_sl, position.peak_price,
        position.tp1_hit, position.qty, position.sl, SCHEMA_VERSION,
    )
    return True


async def reduce_evidence(symbol, opening_order_id):
    """Durable proof that BGX itself reduced this exact trade lineage."""
    from types import SimpleNamespace

    from bot import database as db
    from bot.confirmed_rr_exit import identity
    stub = SimpleNamespace(_forensic_lineage={"opening_order_id": opening_order_id})
    key, idem = identity(symbol, stub)
    key = key.replace("rr_exit_v1:", "partial_exit_v1:")
    idem = idem.replace("rr-", "partial-", 1)
    raw = await db.load_key_value(key, strict=True)
    state = json.loads(raw) if raw else None
    if isinstance(state, dict) and state.get("idem") == idem and (
            state.get("filled") is True or str(state.get("order_id") or "")):
        return True
    raw = await db.load_key_value(_key(opening_order_id), strict=True)
    record = json.loads(raw) if raw else None
    return bool(isinstance(record, dict) and validate(record, symbol=symbol,
                opening_order_id=opening_order_id) is None and record.get("tp1_hit") is True)


def install(TradingEngine, log_obj=log) -> None:
    if getattr(TradingEngine, "_exit_geometry_durability_installed", False):
        return
    original_load = TradingEngine._load_existing_positions

    async def _load_existing_with_exit_geometry(self, *args, **kwargs):
        result = await original_load(self, *args, **kwargs)
        if getattr(self, "paper_trade", True):
            return result
        for symbol in sorted(getattr(self, "_recovered_position_symbols", set()) or set()):
            try:
                await restore(self, symbol)
            except Exception as exc:
                position = (getattr(self, "positions", {}) or {}).get(symbol)
                if position is not None:
                    position.initial_sl = None
                log_obj.critical("[EXIT_GEOMETRY_RESTORE_REJECTED] symbol=%s error=%s "
                                 "r_exits=FAIL_CLOSED", symbol, type(exc).__name__)
        return result

    TradingEngine._load_existing_positions = _load_existing_with_exit_geometry
    TradingEngine._exit_geometry_durability_installed = True
    log_obj.warning(
        "[EXIT_GEOMETRY_DURABILITY] installed=true lineage=opening_order_id "
        "history=durable current_exposure=exchange invented=false"
    )
