"""Distributed ownership fence for LIVE order dispatch.

This hardening does not change strategy, entry eligibility, leverage, sizing,
stop geometry or the F-003 risk-budget sizing. It only prevents two
simultaneous process instances from both dispatching LIVE entry orders.

PostgreSQL session advisory locks are used deliberately: ownership follows the
actual DB session and is released automatically by PostgreSQL when that session
ends. PAPER remains completely unaffected.
"""
from __future__ import annotations

import asyncio
import math

from bot import database as db

# Stable signed 64-bit advisory-lock key reserved for NEXUS-7 LIVE execution.
# It is intentionally code-owned rather than environment-configurable so two
# deployments cannot accidentally use different fencing namespaces.
_ADVISORY_LOCK_ID = 0x4E45585553370001
_guard = asyncio.Lock()
_owned_conn = None
_ownership_failure = None


async def _verified_reduction(client, args, kwargs):
    """Allow only exchange-enforced reduction of freshly observed exposure.

    A second process can never increase/reverse a position via reduceOnly;
    ambiguous/missing units, wrong side and oversized requests fail closed.
    This exception does not authorize entries or cancel existing protection.
    """
    names = ('symbol', 'side', 'qty', 'sl', 'tp', 'instruments', 'reduce_only', 'idem_key', 'single_submission')
    if len(args) > len(names):
        return False
    values = dict(zip(names, args))
    if set(values).intersection(kwargs):
        return False
    values.update(kwargs)
    if values.get('reduce_only') is not True:
        return False
    from bot.conditional_stop_protection import _instrument_info, _to_base_size
    try:
        qty = values.get('qty')
        if isinstance(qty, bool) or not math.isfinite(float(qty)) or float(qty) <= 0:
            return False
        rows = await asyncio.wait_for(client.get_positions(), timeout=10)
        if not isinstance(rows, list):
            return False
        matches = [p for p in rows if p.get('symbol') == values.get('symbol')]
        if len(matches) != 1:
            return False
        pos = matches[0]
        side = str(pos.get('side', '')).lower()
        expected = 'sell' if side in ('buy', 'long') else 'buy' if side in ('sell', 'short') else ''
        if not expected or str(values.get('side', '')).lower() != expected:
            return False
        unit = str(pos.get('sizeUnit', 'CONTRACTS')).upper()
        if unit not in ('CONTRACTS', 'BASE_ASSET'):
            return False
        raw = pos.get('size', pos.get('currentQty'))
        if isinstance(raw, bool):
            return False
        remaining = _to_base_size(abs(float(raw)), unit, _instrument_info(client, values.get('symbol')))
        return remaining > 0 and float(qty) <= remaining
    except Exception:
        return False


def _connection_closed(conn) -> bool:
    checker = getattr(conn, "is_closed", None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except Exception:
        return True


async def acquire(log) -> bool:
    """Return True only when this process owns the LIVE execution lease.

    The lock is acquired at most once per PostgreSQL connection. PostgreSQL
    advisory locks are re-entrant per session, so avoiding repeated acquisition
    prevents an accidental lock-count leak.
    """
    global _owned_conn, _ownership_failure

    async with _guard:
        _ownership_failure = None
        conn = getattr(db, "_conn", None)
        is_pg = bool(getattr(db, "_is_pg", False))

        if not conn or not is_pg:
            _ownership_failure = 'storage_unavailable'
            _owned_conn = None
            log.critical(
                "[LIVE_EXECUTION_FENCE] owner=false reason=postgres_required"
            )
            return False

        if _owned_conn is conn and not _connection_closed(conn):
            return True

        _owned_conn = None
        try:
            # database.py serializes use of its single process connection.
            async with db._io_lock:
                acquired = bool(
                    await conn.fetchval(
                        "SELECT pg_try_advisory_lock($1)", _ADVISORY_LOCK_ID
                    )
                )
        except Exception as exc:
            _ownership_failure = 'storage_unavailable'
            log.critical(
                "[LIVE_EXECUTION_FENCE] owner=false reason=lock_error error=%s",
                type(exc).__name__,
            )
            return False

        if not acquired:
            _ownership_failure = 'another_owner'
            log.critical(
                "[LIVE_EXECUTION_FENCE] owner=false "
                "reason=owned_by_other_instance dispatch=blocked"
            )
            return False

        _owned_conn = conn
        log.critical(
            "[LIVE_EXECUTION_FENCE] owner=true backend=postgres "
            "scope=live_place_order"
        )
        return True


def install(KuCoinClient, kucoin_module, log) -> None:
    """Wrap the final KuCoin order dispatcher with distributed ownership."""
    if getattr(KuCoinClient, "_live_execution_fence_installed", False):
        return

    previous_place_order = KuCoinClient.place_order

    async def _place_order_with_fence(self, *args, **kwargs):
        # PAPER semantics are intentionally byte-for-byte equivalent at the
        # behavioral boundary: no PostgreSQL ownership is required or queried.
        if bool(getattr(kucoin_module, "PAPER_TRADE", True)):
            return await previous_place_order(self, *args, **kwargs)

        if not await acquire(log):
            if _ownership_failure == 'storage_unavailable' and await _verified_reduction(self, args, kwargs):
                log.critical('[LIVE_EXECUTION_FENCE] ownership_unavailable=true action=VERIFIED_REDUCE_ONLY entries_blocked=true')
                return await previous_place_order(self, *args, **kwargs)
            raise RuntimeError(
                "LIVE order dispatch rejected: execution ownership fence unavailable"
            )

        return await previous_place_order(self, *args, **kwargs)

    KuCoinClient.place_order = _place_order_with_fence
    KuCoinClient._live_execution_fence_installed = True
    log.info(
        "[LIVE_EXECUTION_FENCE] installed=true "
        "normal_owner_entries_unchanged=true paper_unchanged=true"
    )
