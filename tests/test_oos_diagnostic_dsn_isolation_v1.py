"""Regression checks for isolated OOS diagnostic credentials."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from bot import oos_readonly_connection_v1 as reader


@pytest.mark.asyncio
async def test_dedicated_reader_never_falls_back_to_trading_dsn(monkeypatch):
    monkeypatch.delenv("OOS_DIAGNOSTIC_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://trading:secret@localhost/trading")
    await reader.discard()
    with patch.object(reader.OOSReadOnlyConnection, "connect", new_callable=AsyncMock) as connect:
        with pytest.raises(ValueError, match="OOS_DIAGNOSTIC_DSN_MISSING"):
            await reader.acquire()
        connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_dedicated_reader_uses_diagnostic_dsn(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://trading:secret@localhost/trading")
    diagnostic = "postgresql://nexus_oos_diagnostic:secret@localhost/diagnostic"
    monkeypatch.setenv("OOS_DIAGNOSTIC_DATABASE_URL", diagnostic)
    await reader.discard()
    fake = AsyncMock()
    fake.is_closed.return_value = False
    with patch.object(reader.OOSReadOnlyConnection, "connect", new_callable=AsyncMock, return_value=fake) as connect:
        assert await reader.acquire() is fake
        connect.assert_awaited_once_with(diagnostic)
    await reader.discard()
