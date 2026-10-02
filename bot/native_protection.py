"""NOVO-F013A-1 — native protection of an opening lineage (read-only).

A protected LIVE entry is ``POST /api/v1/st-orders`` carrying
``triggerStopDownPrice`` / ``triggerStopUpPrice`` (kucoin_native_tpsl). The
exchange then holds the protective legs as conditional close orders. What the
repository proves about those legs (fixtures + kucoin_native_tpsl readback):

  * the OPENING order detail (by orderId / clientOid) echoes the triggers it
    was sent with — ``_protection_equivalent`` already relies on that echo;
  * each leg is an active conditional order on the same symbol, opposite side,
    ``closeOrder``/``reduceOnly``, ``stop`` = down|up, ``stopPrice`` = trigger;
  * a leg carries NO parent/child reference and no BGX clientOid.

No field links a leg to its parent, so the association is defensive and
conjunctive — never "any stop on the symbol":

  native leg  <=>  active protective order (full close / full coverage)
                   AND kind (SL/TP) and side match the position direction
                   AND NOT a BGX-registry clientOid (those are F-001A stops)
                   AND trigger == the trigger echoed by THIS opening order
                       (exchange evidence) or, without an echo, the trigger
                       this process sent for THIS order (dispatch evidence)
                   AND, when both timestamps exist, the leg was not created
                       before the opening order (stale legs of an older trade).

  BGX stop    <=>  BGX clientOid recorded in the F-001A registry slot of
                   the CURRENT opening lineage (``open-<opening_order_id>``,
                   ``owned_for_strong_lineage``). Weak lineages
                   (``live-SYMBOL-side``, ``trade-<id>``), the prefix, the side
                   or an equal trigger never prove ownership (NOVO-F013A-1c).

Everything else (foreign/manual stops, stale legs of another trade, position
``stopLoss`` without provenance, ATR or liquidation estimates) is never a
source of the trade geometry.

INV-NATIVE-PROTECTION-AUTHORITY-001  native protection of the same opening
    lineage is the first authority for reconstructing the geometry.
INV-NO-FALLBACK-STRATEGY-001  ATR / liquidation fallbacks never become the
    geometry of a BGX trade: unprovable geometry is UNCONFIRMED.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from bot.logger import log

CREATED_SKEW_MS = 5_000


def _pos(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _same(a, b, tick):
    if a is None or b is None:
        return False
    tolerance = max((_pos(tick) or 0.0) * 1.0001, abs(float(b)) * 1e-9)
    return abs(float(a) - float(b)) <= tolerance


def _ms(value):
    number = _pos(value)
    if number is None:
        return None
    # KuCoin timestamps may be ms or ns; normalise to ms.
    return number / 1e6 if number > 1e14 else number


@dataclass
class NativeProtection:
    sl: float | None = None              # most protective lineage stop (exchange truth)
    tp: float | None = None              # native / lineage TP on the exchange
    native_sl: float | None = None       # trigger of THIS opening order
    native_tp: float | None = None
    level_source: str = "none"           # order_echo | dispatch | none
    sl_source: str = "none"              # native_leg | bgx_lineage_stop | none
    readable: bool = False
    ignored: list = field(default_factory=list)
    foreign_tighter: float | None = None  # any non-lineage stop would trigger first
    other_lineage_tighter: float | None = None  # ... and it is a BGX stop of another/legacy lineage
    foreign: list = field(default_factory=list)
    other_bgx: list = field(default_factory=list)

    @property
    def sl_confirmed(self):
        return self.readable and self.sl is not None


def native_levels(status, direction):
    """(sl, tp) echoed by the opening order itself, or (None, None)."""
    if not isinstance(status, dict) or status.get("_synthetic") or status.get("_unknown"):
        return None, None
    down, up = _pos(status.get("triggerStopDownPrice")), _pos(status.get("triggerStopUpPrice"))
    return (down, up) if direction == "LONG" else (up, down)


def _closing_target(order, row, direction, price):
    from bot.conditional_stop_protection import _order_active
    mark = _pos(row.get("markPrice")) or _pos(row.get("avgPrice", row.get("entryPrice")))
    close_side = "sell" if direction == "LONG" else "buy"
    return bool(_order_active(order) and mark
                and str(order.get("side", "")).lower() == close_side
                and (order.get("closeOrder") is True or order.get("reduceOnly") is True)
                and (price > mark if direction == "LONG" else price < mark))


async def discover(client, symbol, direction, *, order_id="", client_oid="", status=None,
                   planned_sl=None, planned_tp=None, tick=None, row=None, lineage_owned=None):
    """Read-only: which ACTIVE exchange protection belongs to this opening lineage.

    ``status``: fresh detail of the opening order (read here when omitted).
    ``lineage_owned(order, kind)``: STRONG-lineage ownership of a BGX stop.
    """
    from bot.conditional_stop_lifecycle import is_bgx_owned
    from bot.conditional_stop_protection import _instrument_info, _protective_order, read_stop_orders
    result = NativeProtection()
    if status is None and order_id:
        getter = getattr(client, "get_order_status", None)
        if callable(getter):
            try:
                status = await getter(order_id)
            except Exception:
                status = None
    echo_ok = isinstance(status, dict) and not status.get("_synthetic") and not status.get("_unknown") \
        and str(status.get("orderId") or status.get("id") or order_id) == str(order_id) \
        and (not client_oid or not status.get("clientOid") or str(status["clientOid"]) == str(client_oid))
    echo_sl, echo_tp = native_levels(status, direction) if echo_ok else (None, None)
    if echo_sl is not None:
        result.native_sl, result.native_tp, result.level_source = echo_sl, echo_tp, "order_echo"
        if _pos(planned_sl) and not _same(echo_sl, planned_sl, tick):
            log.warning("[NATIVE_PROTECTION_RECOVERY] symbol=%s opening_order_id=%s "
                        "echo_sl=%s dispatch_sl=%s authority=order_echo", symbol,
                        str(order_id)[:16], echo_sl, planned_sl)
    elif _pos(planned_sl):
        result.native_sl, result.native_tp = _pos(planned_sl), _pos(planned_tp)
        result.level_source = "dispatch"
    created_floor = _ms((status or {}).get("createdAt") if echo_ok else None)

    if row is None:
        rows = await client.get_positions()
        row = next((r for r in rows or [] if r.get("symbol") == symbol
                    and (_pos(r.get("size")) or 0) > 0), None)
    if row is None:
        return result
    orders = await read_stop_orders(client, symbol)
    if orders is None:
        return result
    result.readable = True
    info = _instrument_info(client, symbol)
    stops, tps = [], []
    for order in orders:
        # Same classification as the protection-readiness readers: a closing
        # conditional order on the protective side of the mark is a STOP (it
        # bounds the loss), on the other side a TAKE PROFIT.
        price = _pos(order.get("stopPrice"))
        if price is None:
            continue
        qualifies, full, amount = _protective_order(order, row, symbol, info)
        if qualifies and (full or amount > 0):
            kind = "SL"
        elif _closing_target(order, row, direction, price):
            kind = "TP"
        else:
            continue
        source = None
        bgx = is_bgx_owned(order)
        if bgx:
            # INV-PROTECTION-LINEAGE-001: a BGX stop is this trade's only through
            # the durable STRONG mapping (opening order id). Symbol, side, prefix
            # or an equal trigger price never prove ownership.
            if lineage_owned is not None and await lineage_owned(order, kind):
                source = "bgx_lineage_stop"
        else:
            native = result.native_sl if kind == "SL" else result.native_tp
            created = _ms(order.get("createdAt"))
            stale = created is not None and created_floor is not None \
                and created + CREATED_SKEW_MS < created_floor
            if _same(price, native, tick) and not stale:
                source = "native_leg"
        if source is None:
            result.ignored.append((kind, price, str(order.get("clientOid") or order.get("id") or "")[:24]))
            (result.other_bgx if bgx else result.foreign).append((kind, price))
            continue
        (stops if kind == "SL" else tps).append((price, source))
    if stops:
        # INV-MONOTONIC-WITHIN-LINEAGE-001: Q-01C "most protective" is applied
        # ONLY among stops whose ownership by the current lineage is proven.
        best = max(stops) if direction == "LONG" else min(stops)
        result.sl, result.sl_source = best

        def tighter(levels):
            found = [p for k, p in levels if k == "SL"
                     and (p > result.sl if direction == "LONG" else p < result.sl)
                     and not _same(p, result.sl, tick)]
            return (max(found) if direction == "LONG" else min(found)) if found else None
        # A tighter stop NOT owned by this lineage (manual/external, unattributed
        # native leg, or a BGX stop of another/legacy lineage) would exit the
        # trade first: it is never adopted as geometry and never silently
        # ignored -> caller fails closed (UNCONFIRMED). It is not cancelled here.
        result.other_lineage_tighter = tighter(result.other_bgx)
        result.foreign_tighter = tighter(result.foreign + result.other_bgx)
    if tps:
        result.tp = (min(tps) if direction == "LONG" else max(tps))[0]
    if result.ignored:
        log.warning("[NATIVE_PROTECTION_RECOVERY] symbol=%s opening_order_id=%s "
                    "ignored_not_this_lineage=%s", symbol, str(order_id)[:16], result.ignored)
    return result
