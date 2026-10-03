"""P1-OPEN-1 — durable map: Binance protective algo order -> opening lineage.

Every protective algo order the NEXUS creates (``clientAlgoId = bgx7-…``) is
recorded BEFORE the POST with the opening lineage that owns it (the entry's
exchange orderId when known, plus its clientOid). Only this record proves that
an algo order belongs to a given trade; the ``bgx7-`` prefix, side, type or
trigger never do (INV-PROTECTION-LINEAGE-001).

The map lives in the client (authoritative for this process) and is persisted
under the stable financial namespace so a restart can continue a cleanup.
Persistence failure never blocks placing protection (a naked position is worse
than an unmapped stop); an unpersisted record simply becomes "unmapped" after a
restart, which the cleanup treats as unresolved (fail closed, never cancelled).
"""
from __future__ import annotations

import json
import time

from bot.logger import log


def _key() -> str:
    from bot.financial_namespace import stable_scope
    return "binance_algo_lineage_v1:" + stable_scope()


def _state(client) -> dict:
    state = getattr(client, "_algo_registry", None)
    if not isinstance(state, dict):
        state = client._algo_registry = {}
    return state


async def load(client) -> bool:
    """Merge the durable map into the client's map. False when unreadable."""
    if getattr(client, "_algo_registry_loaded", False):
        return True
    from bot import database as db
    try:
        raw = await db.load_key_value(_key(), strict=True)
        durable = json.loads(raw) if raw else {}
        if not isinstance(durable, dict):
            raise ValueError("invalid algo lineage registry")
    except Exception as exc:
        log.critical("[BINANCE_STALE_PROTECTION] registry=UNREADABLE error=%s "
                     "unmapped_orders_treated_as=UNRESOLVED", type(exc).__name__)
        return False
    state = _state(client)
    for client_algo_id, record in durable.items():
        if isinstance(record, dict) and client_algo_id not in state:
            state[str(client_algo_id)] = dict(record)
    client._algo_registry_loaded = True
    return True


async def _persist(client) -> bool:
    from bot import database as db
    try:
        encoded = json.dumps(_state(client), sort_keys=True, separators=(",", ":"))
        return await db.save_key_value(_key(), encoded, strict=True) is True
    except Exception as exc:
        log.critical("[BINANCE_STALE_PROTECTION] registry=PERSIST_FAILED error=%s "
                     "in_memory_mapping=KEPT", type(exc).__name__)
        return False


async def record(client, client_algo_id: str, *, symbol: str, kind: str,
                 opening_order_id: str, opening_client_oid: str) -> bool:
    _state(client)[str(client_algo_id)] = {
        "symbol": str(symbol), "kind": str(kind),
        "opening_order_id": str(opening_order_id or ""),
        "opening_client_oid": str(opening_client_oid or ""),
        "created_ms": int(time.time() * 1000),
    }
    if not await load(client):
        # Never overwrite durable records written by a previous process with a
        # partial in-memory view; the record still holds for this process.
        log.critical("[BINANCE_STALE_PROTECTION] registry=NOT_PERSISTED client_oid=%s "
                     "in_memory_mapping=KEPT", client_algo_id)
        return False
    return await _persist(client)


def lookup(client, client_algo_id: str):
    record = _state(client).get(str(client_algo_id))
    return dict(record) if isinstance(record, dict) else None


async def forget(client, client_algo_ids) -> bool:
    state = _state(client)
    removed = [cid for cid in client_algo_ids if state.pop(str(cid), None) is not None]
    if not removed or not getattr(client, "_algo_registry_loaded", False):
        return True
    return await _persist(client)


def lineage_token(record) -> str:
    """Canonical lineage of a record: opening orderId, else opening clientOid."""
    if not isinstance(record, dict):
        return ""
    return str(record.get("opening_order_id") or record.get("opening_client_oid") or "")
