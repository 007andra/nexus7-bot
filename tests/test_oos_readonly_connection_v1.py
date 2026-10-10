"""Contract tests for the opt-in OOS read-only adapter (no live DB)."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from bot.oos_readonly_connection_v1 import OOSReadOnlyConnection


@pytest.mark.asyncio
async def test_rejects_non_postgres_dsn():
    with pytest.raises(ValueError, match="REQUIRES_POSTGRES"):
        await OOSReadOnlyConnection.connect("sqlite:///tmp/test.db")


@pytest.mark.asyncio
async def test_read_only_session_and_close():
    conn = AsyncMock()
    with patch("asyncpg.connect", new_callable=AsyncMock, return_value=conn):
        reader = await OOSReadOnlyConnection.connect("postgresql://localhost/test")
    conn.execute.assert_awaited_once_with(
        "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"
    )
    await reader.close()
    conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_rejects_writes_without_touching_connection():
    conn = AsyncMock()
    reader = OOSReadOnlyConnection(conn)
    with pytest.raises(ValueError, match="SELECT_ONLY"):
        await reader._fetchall("DELETE FROM trades")
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_select_uses_own_connection():
    conn = AsyncMock()
    conn.fetch.return_value = [{"payload": "{}"}]
    reader = OOSReadOnlyConnection(conn)
    rows = await reader._fetchall("SELECT payload FROM prospective_oos_cohort_v1 WHERE cohort_id=?", ("cohort",))
    assert rows == [{"payload": "{}"}]
    conn.fetch.assert_awaited_once_with(
        "SELECT payload FROM prospective_oos_cohort_v1 WHERE cohort_id=$1", "cohort"
    )


@pytest.mark.asyncio
async def test_session_setup_failure_closes_connection():
    conn = AsyncMock()
    conn.execute.side_effect = RuntimeError("readonly unavailable")
    with patch("asyncpg.connect", new_callable=AsyncMock, return_value=conn):
        with pytest.raises(RuntimeError, match="readonly unavailable"):
            await OOSReadOnlyConnection.connect("postgresql://localhost/test")
    conn.close.assert_awaited_once()
