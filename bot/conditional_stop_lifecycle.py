"""Durable ownership/idempotency lifecycle for BGX KuCoin conditional protection.

Only ``bgx-stop-`` orders are BGX-owned. During a live position, replacement
cleanup is narrower still: only clientOids recorded in the durable slot for the
same symbol/side/kind/lineage may be retired. Unknown/external orders are never
adopted or cancelled. Flat cleanup may retire legacy BGX-prefixed stops for the
proven-flat symbol because no live or pending exposure remains.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from urllib.parse import quote

from bot import database as db
from bot.execution_capability import assert_exchange_mutation_allowed
from bot.logger import log


_REGISTRY_KEY = "conditional_protection_registry_v1"
_BGX_PREFIX = "bgx-stop-"
_AMBIGUITY_GRACE_S = 15.0
_STOP_LIMIT = 50
_CACHE = {}
_LOCKS = {}


def _canon_symbol(value: str) -> str:
    symbol = str(value or "").upper()
    if symbol == "XBTUSDTM":
        return "BTCUSDT"
    if symbol.endswith("USDTM"):
        return f"{symbol[:-5]}USDT"
    return symbol


def is_bgx_owned(order: dict) -> bool:
    return (
        isinstance(order, dict)
        and str(order.get("clientOid") or "").startswith(_BGX_PREFIX)
    )


def _active(order: dict) -> bool:
    if not isinstance(order, dict):
        return False
    if order.get("stopTriggered") is True or order.get("isActive") is False:
        return False
    return str(order.get("status") or "").lower() not in {
        "done", "filled", "cancelled", "canceled", "triggered"
    }


def _engine(client):
    engine = getattr(client, "_engine", None)
    if engine is not None:
        return engine
    raw = getattr(client, "_client", None)
    return getattr(raw, "_engine", None) if raw is not None else None


def _production_runtime(client) -> bool:
    """True only after the canonical durable runtime has been initialized.

    TradingEngine unit fixtures intentionally do not call
    ``durable_execution.restore_engine_state`` and therefore have no durable
    order lock. Production does. This keeps offline tests transport-focused
    without introducing a production fallback: once runtime durability exists,
    registry reads/writes are strict PostgreSQL-backed operations.
    """
    engine = _engine(client)
    return engine is not None and hasattr(engine, "_durable_order_lock")


def _cache_key(client):
    engine = _engine(client)
    return id(engine) if engine is not None else id(client)


def _lock(client):
    key = _cache_key(client)
    if key not in _LOCKS:
        _LOCKS[key] = asyncio.Lock()
    return _LOCKS[key]


async def _validate_owner(client) -> bool:
    """Require existing ownership/fencing for production lifecycle mutations."""
    if not _production_runtime(client):
        return True
    engine = _engine(client)
    if not bool(getattr(engine, "_execution_ownership_valid", False)):
        return False
    raw_client = getattr(client, "_client", client)
    ownership = getattr(raw_client, "_execution_ownership", None)
    if ownership is None:
        ownership = getattr(client, "_execution_ownership", None)
    if ownership is None:
        return False
    try:
        from bot.execution_ownership import validate_execution_ownership
        await validate_execution_ownership(ownership)
        return True
    except Exception as exc:
        log.warning(
            "[PROTECTION_LIFECYCLE] ownership_valid=false error=%s",
            type(exc).__name__,
        )
        return False


async def _load_registry(client) -> dict:
    key = _cache_key(client)
    cached = _CACHE.get(key)
    if isinstance(cached, dict):
        return cached
    if not _production_runtime(client):
        state = {"version": 1, "slots": {}}
        _CACHE[key] = state
        return state
    raw = await db.load_key_value(_REGISTRY_KEY, strict=True)
    if not raw:
        state = {"version": 1, "slots": {}}
    else:
        state = json.loads(raw)
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError("invalid conditional protection registry")
        if not isinstance(state.get("slots"), dict):
            raise ValueError("invalid conditional protection slots")
    _CACHE[key] = state
    return state


async def _persist_registry(client, state: dict, reason: str) -> bool:
    if not _production_runtime(client):
        _CACHE[_cache_key(client)] = state
        return True
    if not await _validate_owner(client):
        return False
    payload = json.dumps(
        {
            "version": 1,
            "reason": str(reason)[:160],
            "slots": state.get("slots", {}),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    await db.save_key_value(_REGISTRY_KEY, payload, strict=True)
    _CACHE[_cache_key(client)] = state
    return True


def position_lineage(client, symbol: str, position: dict) -> str:
    engine = _engine(client)
    if engine is not None:
        trade_id = (getattr(engine, "_trade_ids", {}) or {}).get(symbol)
        parsed = 0
        if isinstance(trade_id, int) and not isinstance(trade_id, bool):
            parsed = trade_id
        elif isinstance(trade_id, str) and trade_id.isdigit():
            parsed = int(trade_id)
        if parsed > 0:
            return f"trade-{parsed}"
    side = str(position.get("side") or "").lower()
    return f"live-{_canon_symbol(symbol)}-{side or 'unknown'}"


def _slot_key(symbol: str, order_side: str, kind: str, lineage: str) -> str:
    return "|".join(
        (_canon_symbol(symbol), order_side.lower(), kind.upper(), str(lineage))
    )


def _logical_oid(slot_key: str, trigger: str, generation: int = 0) -> str:
    raw = f"{slot_key}|{trigger}|g{int(generation)}"
    return _BGX_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:30]


def _terminal_truth(data: dict) -> bool:
    if not isinstance(data, dict) or not data:
        return False
    if "isActive" in data and data.get("isActive") is False:
        return True
    return str(data.get("status") or "").lower() in {
        "done", "filled", "cancelled", "canceled", "triggered", "rejected"
    }


def protection_kind(order: dict, position_side: str) -> str:
    if not isinstance(order, dict):
        return ""
    side = str(order.get("side") or "").lower()
    stop = str(order.get("stop") or "").lower()
    pos_side = str(position_side or "").lower()
    if pos_side in {"buy", "long"} and side == "sell":
        return "SL" if stop == "down" else ("TP" if stop == "up" else "")
    if pos_side in {"sell", "short"} and side == "buy":
        return "SL" if stop == "up" else ("TP" if stop == "down" else "")
    return ""


async def owned_for_lineage(
    client, symbol: str, order_side: str, kind: str, lineage: str, client_oid: str
) -> bool:
    state = await _load_registry(client)
    rec = state.get("slots", {}).get(
        _slot_key(symbol, order_side, kind, lineage)
    )
    if not isinstance(rec, dict):
        return False
    return str(client_oid) in {
        str(value) for value in rec.get("owned_client_oids", [])
    }


async def prepare_candidate(
    client, symbol: str, position_side: str, order_side: str, kind: str,
    lineage: str, trigger: str,
) -> dict:
    """Return one durable logical candidate for the desired protection state.

    Ambiguous retries reuse the SAME clientOid. A new generation is allowed only
    after authoritative truth proves the previous same-desired candidate became
    terminal. Capacity-blocked candidates do not retry until inventory drops
    below the exchange limit.
    """
    async with _lock(client):
        state = await _load_registry(client)
        slots = state.setdefault("slots", {})
        key = _slot_key(symbol, order_side, kind, lineage)
        now = time.time()
        rec = slots.get(key) if isinstance(slots.get(key), dict) else None
        desired = str(trigger)

        if rec and rec.get("status") == "PENDING":
            same_desired = rec.get("desired_trigger") == desired
            if not same_desired:
                return {
                    "client_oid": rec.get("client_oid", ""),
                    "post_allowed": False,
                    "reason": "prior_candidate_unresolved",
                    "slot_key": key,
                }
            oid = str(rec.get("client_oid") or "")
            if rec.get("capacity_blocked") is True:
                return {
                    "client_oid": oid,
                    "post_allowed": False,
                    "reason": "capacity_blocked_reconcile_required",
                    "slot_key": key,
                }
            attempted_raw = rec.get("attempted_at")
            attempted_at = now if attempted_raw is None else float(attempted_raw)
            age = max(0.0, now - attempted_at)
            lookup = getattr(client, "get_order_by_client_oid", None)
            truth = None
            if callable(lookup):
                try:
                    truth = await lookup(oid)
                except Exception as exc:
                    log.warning(
                        "[PROTECTION_LIFECYCLE] candidate_lookup_unconfirmed "
                        "client_oid=%s error=%s",
                        oid,
                        type(exc).__name__,
                    )
            if isinstance(truth, dict) and truth:
                if truth.get("isActive") is True:
                    return {
                        "client_oid": oid,
                        "post_allowed": False,
                        "reason": "candidate_visible_active",
                        "slot_key": key,
                    }
                if _terminal_truth(truth):
                    rec["status"] = "RESOLVED_TERMINAL"
                    rec["resolved_at"] = now
                    await _persist_registry(
                        client, state, "candidate_terminal_truth"
                    )
                else:
                    return {
                        "client_oid": oid,
                        "post_allowed": False,
                        "reason": "candidate_truth_ambiguous",
                        "slot_key": key,
                    }
            else:
                if age < _AMBIGUITY_GRACE_S:
                    return {
                        "client_oid": oid,
                        "post_allowed": False,
                        "reason": "candidate_ambiguity_grace",
                        "slot_key": key,
                    }
                # Same unresolved desired state may retry transport only with
                # the same exchange idempotency key. It never mints UUID A/B/C.
                rec["attempted_at"] = now
                rec["updated_at"] = now
                if not await _persist_registry(
                    client, state, "candidate_same_identity_retry"
                ):
                    return {
                        "client_oid": oid,
                        "post_allowed": False,
                        "reason": "ownership_or_persistence_unavailable",
                        "slot_key": key,
                    }
                return {
                    "client_oid": oid,
                    "post_allowed": True,
                    "reason": "retry_same_identity",
                    "slot_key": key,
                }

        generation = 0
        owned = []
        previous_verified = ""
        if rec:
            owned = [
                str(value)
                for value in rec.get("owned_client_oids", [])
                if str(value)
            ]
            if rec.get("status") == "VERIFIED":
                previous_verified = str(rec.get("client_oid") or "")
            if (
                rec.get("desired_trigger") == desired
                and rec.get("status") == "RESOLVED_TERMINAL"
            ):
                generation = int(rec.get("generation", 0) or 0) + 1

        oid = _logical_oid(key, desired, generation)
        if oid not in owned:
            owned.append(oid)
        slots[key] = {
            "symbol": _canon_symbol(symbol),
            "position_side": str(position_side).lower(),
            "order_side": str(order_side).lower(),
            "kind": str(kind).upper(),
            "lineage": str(lineage),
            "desired_trigger": desired,
            "client_oid": oid,
            "generation": generation,
            "status": "PENDING",
            "attempted_at": now,
            "updated_at": now,
            "owned_client_oids": owned,
            "previous_verified_client_oid": previous_verified,
            "capacity_blocked": False,
        }
        if not await _persist_registry(client, state, "candidate_prepared"):
            return {
                "client_oid": oid,
                "post_allowed": False,
                "reason": "ownership_or_persistence_unavailable",
                "slot_key": key,
            }
        return {
            "client_oid": oid,
            "post_allowed": True,
            "reason": "new_candidate",
            "slot_key": key,
        }


async def mark_verified(client, slot_key: str, order_id: str = "") -> bool:
    async with _lock(client):
        state = await _load_registry(client)
        rec = state.setdefault("slots", {}).get(slot_key)
        if not isinstance(rec, dict):
            return False
        rec["status"] = "VERIFIED"
        rec["order_id"] = str(order_id or rec.get("order_id") or "")
        rec["verified_at"] = time.time()
        rec["updated_at"] = time.time()
        rec["capacity_blocked"] = False
        return await _persist_registry(client, state, "candidate_verified")


async def mark_capacity_blocked(
    client, slot_key: str, observed_count: int
) -> bool:
    async with _lock(client):
        state = await _load_registry(client)
        rec = state.setdefault("slots", {}).get(slot_key)
        if not isinstance(rec, dict):
            return False
        rec["status"] = "PENDING"
        rec["capacity_blocked"] = True
        rec["observed_stop_count"] = int(observed_count)
        rec["updated_at"] = time.time()
        return await _persist_registry(client, state, "kucoin_300004_capacity")


async def clear_capacity_if_recovered(
    client, slot_key: str, observed_count: int
) -> bool:
    """Permit the SAME pending identity to retry only after capacity is free."""
    if int(observed_count) >= _STOP_LIMIT:
        return False
    async with _lock(client):
        state = await _load_registry(client)
        rec = state.setdefault("slots", {}).get(slot_key)
        if not isinstance(rec, dict) or rec.get("capacity_blocked") is not True:
            return False
        rec["capacity_blocked"] = False
        rec["attempted_at"] = 0.0
        rec["updated_at"] = time.time()
        return await _persist_registry(client, state, "stop_capacity_recovered")


async def _cancel_order(client, order_id: str) -> bool:
    if not order_id or not await _validate_owner(client):
        return False
    endpoint = f"/api/v1/orders/{quote(str(order_id), safe='')}"
    assert_exchange_mutation_allowed("DELETE", endpoint)

    narrow = getattr(client, "cancel_order_by_id", None)
    if callable(narrow):
        return bool(await narrow(str(order_id)))

    ensure = getattr(client, "_ensure_session", None)
    auth = getattr(client, "_auth_headers", None)
    session = getattr(client, "_session", None)
    if not callable(ensure) or not callable(auth):
        return False
    await ensure()
    session = getattr(client, "_session", session)
    if session is None:
        return False
    from bot.kucoin import REST_BASE
    headers = auth("DELETE", endpoint)
    try:
        async with session.delete(
            REST_BASE + endpoint, headers=headers
        ) as response:
            data = await response.json()
            return isinstance(data, dict) and data.get("code") == "200000"
    except Exception as exc:
        log.warning(
            "[PROTECTION_GC] cancel_unconfirmed order_id=%s error=%s",
            order_id,
            type(exc).__name__,
        )
        return False


def inventory(
    orders, symbol: str, position_side: str, kind: str,
    canonical_oid: str, lineage_oids=None,
) -> dict:
    same_symbol = [
        row for row in (orders or [])
        if _active(row)
        and _canon_symbol(row.get("symbol")) == _canon_symbol(symbol)
    ]
    bgx = [row for row in same_symbol if is_bgx_owned(row)]
    external = [row for row in same_symbol if not is_bgx_owned(row)]
    allowed = {str(value) for value in (lineage_oids or []) if str(value)}
    same_kind = [
        row for row in bgx
        if protection_kind(row, position_side) == kind
        and (
            not allowed
            or str(row.get("clientOid") or "") in allowed
        )
    ]
    superseded = [
        row for row in same_kind
        if str(row.get("clientOid") or "") != canonical_oid
    ]
    canonical = [
        row for row in same_kind
        if str(row.get("clientOid") or "") == canonical_oid
    ]
    unknown_bgx = [
        row for row in bgx
        if allowed and str(row.get("clientOid") or "") not in allowed
    ]
    return {
        "bgx": bgx,
        "external": external,
        "same_kind": same_kind,
        "superseded": superseded,
        "canonical": canonical,
        "unknown_bgx": unknown_bgx,
    }


async def cleanup_superseded(
    client, symbol: str, position_side: str, kind: str, canonical_oid: str,
    read_stop_orders,
) -> tuple[bool, int, int]:
    """Retire only same-lineage BGX stops after canonical is verified."""
    if not await _validate_owner(client):
        return False, 0, 0
    state = await _load_registry(client)
    matching = None
    for rec in state.get("slots", {}).values():
        if not isinstance(rec, dict):
            continue
        owned = {str(value) for value in rec.get("owned_client_oids", [])}
        if canonical_oid in owned:
            matching = rec
            break
    if matching is None:
        return False, 0, 0

    lineage_oids = matching.get("owned_client_oids", [])
    before = await read_stop_orders(client, symbol)
    if before is None:
        return False, 0, 0
    inv = inventory(
        before, symbol, position_side, kind, canonical_oid, lineage_oids
    )
    if not inv["canonical"]:
        return False, len(inv["superseded"]), len(inv["external"])
    for row in inv["superseded"]:
        await _cancel_order(
            client, str(row.get("id") or row.get("orderId") or "")
        )
    after = await read_stop_orders(client, symbol)
    if after is None:
        return False, len(inv["superseded"]), len(inv["external"])
    after_inv = inventory(
        after, symbol, position_side, kind, canonical_oid, lineage_oids
    )
    ok = bool(after_inv["canonical"]) and not after_inv["superseded"]
    return ok, len(inv["superseded"]), len(after_inv["external"])


async def cleanup_flat_symbol(
    engine, symbol: str, *, exchange_position_qty: float,
    active_entry_confirmed_absent: bool,
) -> bool:
    """Retire only BGX-owned protection after authoritative flat preconditions."""
    try:
        exchange_qty = float(exchange_position_qty)
    except (TypeError, ValueError) as exc:
        log.error(
            "[PROTECTION_FLAT_GC] symbol=%s cleanup_status=FAILED "
            "error=%s",
            symbol,
            type(exc).__name__,
        )
        return False
    if not math.isfinite(exchange_qty) or abs(exchange_qty) > 0:
        return False
    if not active_entry_confirmed_absent:
        return False
    if symbol in (getattr(engine, "positions", {}) or {}):
        return False

    registry = getattr(engine, "orders", None)
    pending = list(registry.pending_orders() or []) if registry is not None else []
    if any(
        _canon_symbol(getattr(order, "symbol", "")) == _canon_symbol(symbol)
        for order in pending
    ):
        return False
    unresolved = (
        list(registry.unreconciled_filled_orders(symbol) or [])
        if registry is not None else []
    )
    if unresolved:
        return False

    client = getattr(engine, "client", None)
    if client is None or not await _validate_owner(client):
        return False

    if callable(getattr(client, "cancel_algo_order", None)):
        # P1-OPEN-1: Binance algo protection (bgx7-) is owned only through the
        # durable opening-lineage record; never by prefix, never via KuCoin.
        from bot.binance_stale_protection import VERIFIED, reconcile_symbol
        try:
            return await reconcile_symbol(client, symbol, flat_proven=True) == VERIFIED
        except Exception as exc:
            log.error("[FLAT_PROTECTION_CLEANUP] symbol=%s status=FAILED error=%s",
                      symbol, type(exc).__name__)
            return False

    from bot.conditional_stop_protection import read_stop_orders
    before = await read_stop_orders(client, symbol)
    if before is None:
        return False
    owned = [
        row for row in before
        if _active(row)
        and _canon_symbol(row.get("symbol")) == _canon_symbol(symbol)
        and is_bgx_owned(row)
    ]
    external_count = len([
        row for row in before
        if _active(row)
        and _canon_symbol(row.get("symbol")) == _canon_symbol(symbol)
        and not is_bgx_owned(row)
    ])
    for row in owned:
        await _cancel_order(
            client, str(row.get("id") or row.get("orderId") or "")
        )

    after = await read_stop_orders(client, symbol)
    if after is None:
        return False
    remaining = [
        row for row in after
        if _active(row)
        and _canon_symbol(row.get("symbol")) == _canon_symbol(symbol)
        and is_bgx_owned(row)
    ]
    ok = not remaining
    log.warning(
        "[PROTECTION_FLAT_GC] symbol=%s bgx_before=%s bgx_after=%s "
        "external_preserved=%s cleanup_status=%s",
        symbol,
        len(owned),
        len(remaining),
        external_count,
        "VERIFIED" if ok else "UNCONFIRMED",
    )
    if ok:
        async with _lock(client):
            state = await _load_registry(client)
            changed = False
            for rec in state.setdefault("slots", {}).values():
                if (
                    isinstance(rec, dict)
                    and _canon_symbol(rec.get("symbol")) == _canon_symbol(symbol)
                ):
                    rec["status"] = "FLAT_CLEANED"
                    rec["updated_at"] = time.time()
                    changed = True
            if changed:
                await _persist_registry(
                    client, state, "flat_cleanup_verified"
                )
    return ok
