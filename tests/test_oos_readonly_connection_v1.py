"""Contract tests for the research-only OOS reader; stdlib unittest runner."""
import unittest
from unittest.mock import AsyncMock, patch

from bot.oos_readonly_connection_v1 import OOSReadOnlyConnection


class OOSReadOnlyConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_non_postgres_dsn(self):
        with self.assertRaisesRegex(ValueError, "REQUIRES_POSTGRES"):
            await OOSReadOnlyConnection.connect("sqlite:///tmp/test.db")

    async def test_read_only_session_and_close(self):
        conn = AsyncMock()
        with patch("asyncpg.connect", new_callable=AsyncMock, return_value=conn):
            reader = await OOSReadOnlyConnection.connect("postgresql://localhost/test")
        conn.execute.assert_awaited_once_with(
            "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"
        )
        await reader.close()
        conn.close.assert_awaited_once()

    async def test_rejects_writes_without_touching_connection(self):
        conn = AsyncMock()
        reader = OOSReadOnlyConnection(conn)
        with self.assertRaisesRegex(ValueError, "SELECT_ONLY"):
            await reader._fetchall("DELETE FROM trades")
        conn.fetch.assert_not_awaited()

    async def test_select_uses_own_connection(self):
        conn = AsyncMock()
        conn.fetch.return_value = [{"payload": "{}"}]
        reader = OOSReadOnlyConnection(conn)
        rows = await reader._fetchall(
            "SELECT payload FROM prospective_oos_cohort_v1 WHERE cohort_id=?",
            ("cohort",),
        )
        self.assertEqual(rows, [{"payload": "{}"}])
        conn.fetch.assert_awaited_once_with(
            "SELECT payload FROM prospective_oos_cohort_v1 WHERE cohort_id=$1",
            "cohort",
        )

    async def test_session_setup_failure_closes_connection(self):
        conn = AsyncMock()
        conn.execute.side_effect = RuntimeError("readonly unavailable")
        with patch("asyncpg.connect", new_callable=AsyncMock, return_value=conn):
            with self.assertRaisesRegex(RuntimeError, "readonly unavailable"):
                await OOSReadOnlyConnection.connect("postgresql://localhost/test")
        conn.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
