"""Real PostgreSQL proof for the cash-flow ledger compare-and-swap write."""
import asyncio
import os
import unittest


@unittest.skipUnless(os.environ.get("TEST_POSTGRES_DSN"), "isolated PostgreSQL required")
class CashFlowCasPostgresProof(unittest.TestCase):
    def test_cas_commits_all_or_nothing_and_refuses_stale_writer(self):
        async def run():
            import asyncpg

            from bot import database as db
            from bot.atomic_key_value import CompareAndSwapConflict, save_key_values_atomic_cas

            conn = await asyncpg.connect(os.environ["TEST_POSTGRES_DSN"])
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS key_value (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)"
            )
            await conn.execute("DELETE FROM key_value WHERE key LIKE 'test:cashflow_cas:%'")
            saved = (db._conn, db._is_pg, db._io_lock, db.DATABASE_URL)
            db._conn, db._is_pg, db._io_lock = conn, True, asyncio.Lock()
            db.DATABASE_URL = os.environ["TEST_POSTGRES_DSN"]
            try:
                ok = await save_key_values_atomic_cas(
                    [("test:cashflow_cas:ledger", "v1"), ("test:cashflow_cas:hwm", "6.0")],
                    expected={"test:cashflow_cas:ledger": None},
                    strict=True,
                )
                self.assertTrue(ok)
                with self.assertRaises(CompareAndSwapConflict):
                    await save_key_values_atomic_cas(
                        [("test:cashflow_cas:ledger", "v2"), ("test:cashflow_cas:hwm", "99")],
                        expected={"test:cashflow_cas:ledger": None},
                        strict=True,
                    )
                rows = dict(await conn.fetch(
                    "SELECT key, value FROM key_value WHERE key LIKE 'test:cashflow_cas:%'"
                ))
                self.assertEqual(rows, {"test:cashflow_cas:ledger": "v1", "test:cashflow_cas:hwm": "6.0"})
            finally:
                await conn.execute("DELETE FROM key_value WHERE key LIKE 'test:cashflow_cas:%'")
                db._conn, db._is_pg, db._io_lock, db.DATABASE_URL = saved
                await conn.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
