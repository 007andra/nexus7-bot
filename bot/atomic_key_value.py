"""Atomic multi-key durable persistence for critical key_value state.

This module intentionally performs one database transaction for a group of
key/value updates. It does not change execution authorization, strategy, risk,
leverage, sizing, or exchange state.
"""
from __future__ import annotations

import os

from bot import database as db
from bot.logger import log


async def save_key_values_atomic(items, *, strict: bool = False, hwm_transition=None) -> bool:
    """Persist all ``items`` or none of them.

    ``items`` is an iterable of ``(key, value)`` pairs. PostgreSQL uses one
    asyncpg transaction; SQLite uses BEGIN IMMEDIATE plus explicit commit/
    rollback. The database module's shared I/O lock serializes this transaction
    with the rest of the process' durable-state operations.
    """
    pairs = [(str(key), str(value)) for key, value in items]
    if not pairs:
        raise ValueError("atomic key/value write requires at least one item")
    if any(not key for key, _ in pairs):
        raise ValueError("atomic key/value keys must be non-empty")
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("atomic key/value keys must be unique")

    journal_result = await _journal_guarded_if_enabled(
        pairs, expected=None, hwm_transition=hwm_transition,
    )
    if journal_result is not None:
        return journal_result

    conn = db._conn
    if not conn:
        if strict:
            raise db.PersistenceError("atomic key/value write: database unavailable")
        return False

    async with db._io_lock:
        try:
            ts = db._now()
            if db._is_pg:
                async with conn.transaction():
                    for key, value in pairs:
                        await conn.execute(
                            "INSERT INTO key_value (key,value,updated_at) VALUES ($1,$2,$3) "
                            "ON CONFLICT (key) DO UPDATE SET value=$2,updated_at=$3",
                            key, value, ts,
                        )
            else:
                await conn.execute("BEGIN IMMEDIATE")
                for key, value in pairs:
                    await conn.execute(
                        "INSERT OR REPLACE INTO key_value (key,value,updated_at) VALUES (?,?,?)",
                        (key, value, ts),
                    )
                await conn.commit()
            return True
        except Exception as exc:
            if not db._is_pg:
                try:
                    await conn.rollback()
                except Exception as rollback_exc:
                    log.error("atomic key/value rollback failed: %s", rollback_exc)
            log.error("atomic key/value write failed: %s", exc)
            if strict:
                raise db.PersistenceError("atomic key/value write failed") from exc
            return False




async def _journal_guarded_if_enabled(pairs, *, expected, hwm_transition):
    """Opt-in only: transactional journal+HWM+other key_value rows or no write.

    This branch remains disabled by default and is NOT a production rollout.
    """
    if os.environ.get("HWM_JOURNAL_INTEGRATION_V1", "").lower() != "true":
        return None
    from bot import hwm_journal_guard_v1 as guard
    if not guard.has_peak_change(pairs):
        if hwm_transition is not None:
            raise db.PersistenceError("HWM journal intent without HWM write")
        return None
    async with db._io_lock:
        try:
            return await guard.write_with_journal(
                pairs, expected=expected, event=hwm_transition,
            )
        except CompareAndSwapConflict:
            raise
        except Exception as exc:
            # Never downgrade a journal failure to a legacy write, including
            # when an older caller requested strict=False.
            raise db.PersistenceError("HWM journal transaction refused") from exc


class CompareAndSwapConflict(db.PersistenceError):
    """A guarded key changed between the caller's read and its atomic write."""


async def save_key_values_atomic_cas(items, *, expected, strict: bool = True, hwm_transition=None) -> bool:
    """Atomically persist ``items`` only if every ``expected`` key is unchanged.

    ``expected`` maps key -> raw value the caller read (``None`` = key absent).
    The comparison and all writes happen inside one transaction under the same
    shared I/O lock as ``save_key_values_atomic``, so two writers (the runtime
    and an operator CLI) can never silently overwrite each other's ledger.
    A mismatch raises ``CompareAndSwapConflict`` and writes nothing.
    """
    pairs = [(str(key), str(value)) for key, value in items]
    if not pairs:
        raise ValueError("atomic key/value write requires at least one item")
    if any(not key for key, _ in pairs):
        raise ValueError("atomic key/value keys must be non-empty")
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("atomic key/value keys must be unique")
    guards = {str(key): (None if value is None else str(value)) for key, value in dict(expected).items()}
    if not guards:
        raise ValueError("compare-and-swap requires at least one expected key")

    journal_result = await _journal_guarded_if_enabled(
        pairs, expected=guards, hwm_transition=hwm_transition,
    )
    if journal_result is not None:
        return journal_result

    conn = db._conn
    if not conn:
        if strict:
            raise db.PersistenceError("atomic key/value CAS write: database unavailable")
        return False

    async with db._io_lock:
        try:
            ts = db._now()
            if db._is_pg:
                # The runtime database connection is shared by long-running
                # readers and bounded/cancellable shadow tasks.  An interrupted
                # transaction on that shared session can leave asyncpg believing
                # an outer transaction exists while PostgreSQL has already
                # rolled it back.  The next nested transaction then issues a
                # SAVEPOINT outside a transaction and the durable risk refresh
                # fails indefinitely.  Never use that session for critical CAS.
                #
                # A fresh PostgreSQL session is isolated for this ONE atomic
                # commit. A connection or transaction failure raises without
                # retrying or changing the risk ledger/HWM.
                if not db.DATABASE_URL.startswith("postgresql"):
                    raise db.PersistenceError(
                        "atomic PostgreSQL CAS requires configured PostgreSQL DSN"
                    )
                import asyncpg
                cas_conn = await asyncpg.connect(db.DATABASE_URL, timeout=10)
                try:
                    async with cas_conn.transaction():
                        # Lock absent keys too: SELECT FOR UPDATE alone cannot
                        # serialize concurrent CAS writers on a missing row.
                        # Sorted advisory locks prevent lock-order deadlocks.
                        for key in sorted(set(guards) | {key for key, _ in pairs}):
                            await cas_conn.fetchval(
                                "SELECT pg_advisory_xact_lock(hashtext($1)::bigint)", key
                            )
                        for key, want in guards.items():
                            row = await cas_conn.fetchrow(
                                "SELECT value FROM key_value WHERE key=$1 FOR UPDATE", key,
                            )
                            have = row[0] if row else None
                            if have != want:
                                raise CompareAndSwapConflict(
                                    f"compare-and-swap conflict on {key}"
                                )
                        for key, value in pairs:
                            await cas_conn.execute(
                                "INSERT INTO key_value (key,value,updated_at) VALUES ($1,$2,$3) "
                                "ON CONFLICT (key) DO UPDATE SET value=$2,updated_at=$3",
                                key, value, ts,
                            )
                finally:
                    # A cancelled CAS must not leak a transaction/session.
                    # asyncpg terminates the session if close is interrupted.
                    await cas_conn.close(timeout=3)
            else:
                await conn.execute("BEGIN IMMEDIATE")
                for key, want in guards.items():
                    async with conn.execute("SELECT value FROM key_value WHERE key=?", (key,)) as cur:
                        row = await cur.fetchone()
                    have = row[0] if row else None
                    if have != want:
                        raise CompareAndSwapConflict(f"compare-and-swap conflict on {key}")
                for key, value in pairs:
                    await conn.execute(
                        "INSERT OR REPLACE INTO key_value (key,value,updated_at) VALUES (?,?,?)",
                        (key, value, ts),
                    )
                await conn.commit()
            return True
        except Exception as exc:
            if not db._is_pg:
                try:
                    await conn.rollback()
                except Exception as rollback_exc:
                    log.error("atomic key/value CAS rollback failed: %s", rollback_exc)
            if isinstance(exc, CompareAndSwapConflict):
                log.error("atomic key/value CAS conflict: %s", exc)
                raise
            log.error("atomic key/value CAS write failed: %s", exc)
            if strict:
                raise db.PersistenceError("atomic key/value CAS write failed") from exc
            return False
