"""Opt-in, research-only PostgreSQL reader for prospective OOS snapshots.

Never use this connection for trading, persistence or authority decisions.
The caller owns lifecycle and must close it on cancellation/shutdown.

OFF by default. When ``OOS_DEDICATED_READER_V1`` is enabled the prospective
OOS snapshot reads through its own read-only session instead of the shared
trading connection and its ``_io_lock``. If the dedicated session cannot be
opened or fails, the snapshot is skipped (fail closed); it never falls back
to the trading connection, never runs DDL and never writes.
"""
from __future__ import annotations

import asyncio
import os
import time

FLAG = "OOS_DEDICATED_READER_V1"
# Server-side ceiling so an abandoned query cannot keep running after the
# client-side 3s snapshot budget has expired.
STATEMENT_TIMEOUT_MS = 2500
APPLICATION_NAME = "nexus_oos_readonly_v1"


def enabled() -> bool:
    return os.environ.get(FLAG, "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


class OOSReadOnlyConnection:
    """Dedicated asyncpg session; fails closed instead of using the trading DB."""

    def __init__(self, connection):
        self._connection = connection

    @classmethod
    async def connect(cls, dsn: str, *, connect_timeout: float = 2.0):
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("OOS_READER_REQUIRES_POSTGRES")
        import asyncpg
        connection = await asyncio.wait_for(
            asyncpg.connect(
                dsn,
                timeout=connect_timeout,
                # Startup values: RESET ALL/DISCARD cannot drop read-only mode.
                server_settings={
                    "default_transaction_read_only": "on",
                    "statement_timeout": str(STATEMENT_TIMEOUT_MS),
                    "application_name": APPLICATION_NAME,
                },
            ),
            timeout=connect_timeout + 0.5,
        )
        try:
            await connection.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        except BaseException:
            await connection.close()
            raise
        return cls(connection)

    def is_closed(self) -> bool:
        try:
            return self._connection.is_closed() is not False
        except Exception:
            return True

    async def _fetchall(self, sql: str, params: tuple = (), *, strict: bool = True):
        if not strict:
            raise ValueError("OOS_READER_MUST_FAIL_CLOSED")
        normalized = sql.lstrip().upper()
        if not normalized.startswith("SELECT "):
            raise ValueError("OOS_READER_SELECT_ONLY")
        from bot.database import _pg_sql
        from bot.prospective_oos_snapshot_timing_v1 import active_probe
        probe = active_probe.get()
        started = time.perf_counter()
        rows = None
        cancelled = False
        try:
            rows = await self._connection.fetch(_pg_sql(sql), *params)
            return rows
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if probe is not None:
                # No shared lock is taken here, so the wait is zero by design.
                probe.record_fetch(
                    waited_ms=0.0,
                    fetched_ms=(time.perf_counter() - started) * 1000,
                    rows=None if rows is None else len(rows),
                    lock_acquired=True,
                    cancelled=cancelled,
                )

    async def close(self):
        await self._connection.close()


_reader: OOSReadOnlyConnection | None = None


async def acquire(dsn: str | None = None) -> OOSReadOnlyConnection:
    """Return the cached dedicated reader, opening it when needed."""
    global _reader
    if _reader is not None and not _reader.is_closed():
        return _reader
    _reader = None
    if dsn is None:
        dsn = os.environ.get("DATABASE_URL", "")
    _reader = await OOSReadOnlyConnection.connect(dsn)
    return _reader


async def discard() -> None:
    """Drop the cached reader after any failure, timeout or cancellation."""
    global _reader
    reader, _reader = _reader, None
    if reader is None:
        return
    try:
        await asyncio.wait_for(reader.close(), timeout=1.0)
    except asyncio.CancelledError:
        _terminate(reader)
        raise
    except Exception:
        # A connection that cannot close cleanly is abandoned, not reused.
        _terminate(reader)


def _terminate(reader: OOSReadOnlyConnection) -> None:
    try:
        reader._connection.terminate()
    except Exception as exc:
        from bot.logger import log
        log.warning("[OOS_DEDICATED_READER_V1] terminate_failed error=%s", type(exc).__name__)
