"""Opt-in, research-only PostgreSQL reader for prospective OOS snapshots.

Never use this connection for trading, persistence or authority decisions.
The caller owns lifecycle and must close it on cancellation/shutdown.
"""
from __future__ import annotations

import asyncio


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
            asyncpg.connect(dsn, timeout=connect_timeout),
            timeout=connect_timeout + 0.5,
        )
        try:
            await connection.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        except BaseException:
            await connection.close()
            raise
        return cls(connection)

    async def _fetchall(self, sql: str, params: tuple = (), *, strict: bool = True):
        if not strict:
            raise ValueError("OOS_READER_MUST_FAIL_CLOSED")
        normalized = sql.lstrip().upper()
        if not normalized.startswith("SELECT "):
            raise ValueError("OOS_READER_SELECT_ONLY")
        from bot.database import _pg_sql
        return await self._connection.fetch(_pg_sql(sql), *params)

    async def close(self):
        await self._connection.close()
