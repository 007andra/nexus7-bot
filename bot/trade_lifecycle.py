"""Durable trade lifecycle per lineage — the provenance half of restart ownership.

An opening order id only proves "BGX opened a trade in the past"; it never
proves that a present position is that trade. Ownership after restart also
requires this record (one per opening order id, one atomic value):

  version, opening_order_id, client_oid, symbol, direction, entry,
  opening_qty            base qty of the BGX opening fill
  confirmed_reduced_qty  reductions proven to come from BGX orders
  status                 OPEN | CLOSED   (CLOSED is terminal and monotonic)
  opened_at_ms, closed_at_ms, close_reason

OWNED = status OPEN and exchange qty == opening_qty - confirmed_reduced_qty.
CLOSED is written only from authoritative exchange evidence (a validated
position snapshot without the symbol, or with the opposite side in one-way
mode). A missing record is never treated as OPEN. Records are retained for
audit; the status, not deletion or retention, removes authority.
"""
from __future__ import annotations

import hashlib
import json
import math
import time

from bot.logger import log

SCHEMA_VERSION = 1
OPEN = "OPEN"
CLOSED = "CLOSED"


def _key(opening_order_id):
    from bot.durable_daily_stop import state_key
    token = hashlib.sha256(str(opening_order_id).encode()).hexdigest()[:32]
    return state_key("trade_lifecycle_v1") + ":" + token


def lineage_id(position):
    lineage = getattr(position, "_forensic_lineage", None) or {}
    if not isinstance(lineage, dict):
        return ""
    return str(lineage.get("opening_order_id") or lineage.get("order_id") or "").strip()


def _qty(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def validate(record):
    if not isinstance(record, dict):
        return "not_mapping"
    if record.get("version") != SCHEMA_VERSION:
        return "unsupported_version"
    if not str(record.get("opening_order_id") or ""):
        return "lineage_missing"
    if record.get("direction") not in ("LONG", "SHORT"):
        return "direction_invalid"
    if record.get("status") not in (OPEN, CLOSED):
        return "status_invalid"
    entry = _qty(record.get("entry"))
    if entry is None or entry <= 0:
        return "entry_invalid"
    opening, reduced = _qty(record.get("opening_qty")), _qty(record.get("confirmed_reduced_qty"))
    if opening is None or opening <= 0 or reduced is None or reduced > opening * (1 + 1e-9):
        return "quantity_invalid"
    return None


def expected_remaining(record):
    return max(0.0, float(record["opening_qty"]) - float(record["confirmed_reduced_qty"]))


async def load(opening_order_id):
    """Return a validated record or None when absent; corrupt -> ValueError."""
    from bot import database as db
    raw = await db.load_key_value(_key(opening_order_id), strict=True)
    if not raw:
        return None
    record = json.loads(raw)
    reason = validate(record)
    if reason:
        raise ValueError(f"trade_lifecycle_{reason}")
    if str(record["opening_order_id"]) != str(opening_order_id):
        raise ValueError("trade_lifecycle_lineage_mismatch")
    return record


async def _save(record):
    from bot import database as db
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if await db.save_key_value(_key(record["opening_order_id"]), encoded, strict=True) is not True:
        raise db.PersistenceError("trade lifecycle persistence unconfirmed")


async def open_trade(position, opening_qty, reason="entry_confirmed"):
    """Create the OPEN record once; never resurrect or overwrite a lineage."""
    oid = lineage_id(position)
    qty = _qty(opening_qty)
    if not oid or qty is None or qty <= 0 or position.direction not in ("LONG", "SHORT"):
        return False
    if await load(oid) is not None:
        return False
    lineage = getattr(position, "_forensic_lineage", None) or {}
    record = {
        "version": SCHEMA_VERSION, "opening_order_id": oid,
        "client_oid": str(lineage.get("client_oid") or "") if isinstance(lineage, dict) else "",
        "symbol": str(position.symbol), "direction": position.direction,
        "entry": float(position.entry),
        "opening_qty": qty, "confirmed_reduced_qty": 0.0, "status": OPEN,
        "opened_at_ms": int(time.time() * 1000), "closed_at_ms": None, "close_reason": None,
    }
    await _save(record)
    log.info("[TRADE_LINEAGE_OPEN] symbol=%s side=%s opening_order_id=%s opening_qty=%s reason=%s",
             record["symbol"], record["direction"], oid[:16], qty, reason)
    return True


async def record_reduction(position, residual_qty, bgx_order_qty, reason):
    """Credit a reduction to BGX only up to the size of the BGX reduce order.

    ``residual_qty`` is the exchange quantity read right after the BGX fill;
    any extra, unexplained reduction is NOT absorbed, so the lineage stops
    matching the exchange and restart ownership fails closed.
    """
    oid = lineage_id(position)
    record = await load(oid) if oid else None
    residual, order_qty = _qty(residual_qty), _qty(bgx_order_qty)
    if record is None or record["status"] != OPEN or residual is None or order_qty is None:
        return False
    observed = max(0.0, record["opening_qty"] - residual)
    proven = min(observed, record["confirmed_reduced_qty"] + order_qty, record["opening_qty"])
    if proven <= record["confirmed_reduced_qty"] + 1e-12:
        return False
    record["confirmed_reduced_qty"] = proven
    await _save(record)
    log.info(
        "[TRADE_LINEAGE_REDUCED] symbol=%s side=%s opening_order_id=%s opening_qty=%s "
        "confirmed_reduced_qty=%s expected_remaining=%s exchange_qty=%s reason=%s",
        record["symbol"], record["direction"], oid[:16], record["opening_qty"], proven,
        expected_remaining(record), residual, reason,
    )
    return True


async def record_entry_fill(opening_order_id, cumulative_qty, reason):
    """F-013A: raise opening_qty to the exchange-proven cumulative fill of the
    SAME opening order (late fills). Monotonic, OPEN lineages only; never
    used for exposure that the opening order does not explain."""
    record = await load(opening_order_id)
    qty = _qty(cumulative_qty)
    if record is None or record["status"] != OPEN or qty is None or \
            qty <= record["opening_qty"] * (1 + 1e-9):
        return False
    previous = record["opening_qty"]
    record["opening_qty"] = qty
    await _save(record)
    log.warning(
        "[TRADE_LINEAGE_ENTRY_FILL] symbol=%s side=%s opening_order_id=%s opening_qty=%s->%s "
        "confirmed_reduced_qty=%s reason=%s", record["symbol"], record["direction"],
        str(opening_order_id)[:16], previous, qty, record["confirmed_reduced_qty"], reason)
    return True


async def close(opening_order_id, reason):
    """Monotonic OPEN -> CLOSED; CLOSED never returns to OPEN."""
    record = await load(opening_order_id)
    if record is None or record["status"] == CLOSED:
        return False
    record.update(status=CLOSED, closed_at_ms=int(time.time() * 1000), close_reason=str(reason)[:80])
    await _save(record)
    log.warning(
        "[TRADE_LINEAGE_TERMINAL] symbol=%s side=%s opening_order_id=%s trade_status=CLOSED "
        "opening_qty=%s confirmed_reduced_qty=%s reason=%s",
        record["symbol"], record["direction"], str(opening_order_id)[:16],
        record["opening_qty"], record["confirmed_reduced_qty"], reason,
    )
    return True


def _flat_or_flipped(rows, symbol, direction):
    """True only from a validated snapshot: no row, or opposite side (one-way)."""
    side = "Buy" if direction == "LONG" else "Sell"
    live = [r for r in rows or [] if r.get("symbol") == symbol and float(r.get("size") or 0) > 0]
    return not live or all(str(r.get("side")) != side for r in live)


async def terminalize_positions(positions, rows, reason):
    """Close the lineage of each position the AUTHORITATIVE snapshot proves gone."""
    closed = []
    for symbol, position in list((positions or {}).items()):
        oid = lineage_id(position)
        if oid and _flat_or_flipped(rows, symbol, position.direction):
            try:
                if await close(oid, reason):
                    closed.append(symbol)
            except Exception as exc:
                log.error("[TRADE_LINEAGE_TERMINAL_FAILED] symbol=%s error=%s", symbol, type(exc).__name__)
    return closed


async def reconcile_at_boot(engine):
    """Before any adoption: terminalize OPEN lineages the exchange proves closed."""
    try:
        rows = await engine.client.get_positions()   # raises when not authoritative
    except Exception as exc:
        log.critical("[TRADE_LINEAGE_BOOT] reconcile_skipped=%s adoption_remains_fail_closed=true",
                     type(exc).__name__)
        return
    snapshot = getattr(getattr(engine, "orders", None), "snapshot", None)
    for record in (snapshot() if callable(snapshot) else []) or []:
        oid = str(record.get("order_id") or "") if isinstance(record, dict) else ""
        if not oid or str(record.get("state")) != "FILLED" or \
                not str(record.get("client_oid") or "").startswith("bgx7-"):
            continue
        try:
            lifecycle = await load(oid)
            if lifecycle and lifecycle["status"] == OPEN and _flat_or_flipped(
                    rows, lifecycle["symbol"], lifecycle["direction"]):
                await close(oid, "boot_exchange_flat")
        except Exception as exc:
            log.error("[TRADE_LINEAGE_BOOT] opening_order_id=%s error=%s", oid[:16], type(exc).__name__)


def install(TradingEngine, log_obj=log) -> None:
    if getattr(TradingEngine, "_trade_lifecycle_installed", False):
        return
    original_load = TradingEngine._load_existing_positions

    async def _load_existing_after_lifecycle_reconcile(self, *args, **kwargs):
        if not getattr(self, "paper_trade", True):
            await reconcile_at_boot(self)   # terminal state resolved BEFORE adoption
        return await original_load(self, *args, **kwargs)

    TradingEngine._load_existing_positions = _load_existing_after_lifecycle_reconcile
    TradingEngine._trade_lifecycle_installed = True
    log_obj.warning("[TRADE_LIFECYCLE] installed=true terminal=monotonic missing_record=NOT_OWNED "
                    "boot_reconcile_before_adoption=true")
