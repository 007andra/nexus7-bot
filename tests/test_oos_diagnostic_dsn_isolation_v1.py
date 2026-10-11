"""Offline unittest regressions for isolated OOS diagnostic credentials."""
import unittest
from unittest.mock import AsyncMock, patch

from bot import oos_readonly_connection_v1 as reader


class TestOOSDiagnosticDSNIsolation(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await reader.discard()

    async def test_missing_diagnostic_dsn_never_uses_trading_dsn(self):
        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://trading:secret@localhost/trading"}, clear=True):
            await reader.discard()
            with patch.object(reader.OOSReadOnlyConnection, "connect", new_callable=AsyncMock) as connect:
                with self.assertRaisesRegex(ValueError, "OOS_DIAGNOSTIC_DSN_MISSING"):
                    await reader.acquire()
                connect.assert_not_awaited()

    async def test_diagnostic_dsn_selected_instead_of_trading_dsn(self):
        diagnostic = "postgresql://nexus_oos_diagnostic:secret@localhost/diagnostic"
        with patch.dict("os.environ", {
            "DATABASE_URL": "postgresql://trading:secret@localhost/trading",
            "OOS_DIAGNOSTIC_DATABASE_URL": diagnostic,
        }, clear=True):
            await reader.discard()
            fake = AsyncMock()
            fake.is_closed.return_value = False
            with patch.object(reader.OOSReadOnlyConnection, "connect", new_callable=AsyncMock, return_value=fake) as connect:
                self.assertIs(await reader.acquire(), fake)
                connect.assert_awaited_once_with(diagnostic)
