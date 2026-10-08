"""P0 incident #584: real PostgreSQL durable CAS transaction isolation.

CI MUST provide TEST_POSTGRES_DSN and run this test explicitly against
postgres:16. All writes use disposable, namespaced test keys only.
"""
import asyncio
import os
import unittest
from unittest.mock import MagicMock

from bot import database as db
from bot.atomic_key_value import CompareAndSwapConflict, save_key_values_atomic_cas


@unittest.skipUnless(os.environ.get("TEST_POSTGRES_DSN"), "isolated PostgreSQL required")
class PostgresCasSessionIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        self.asyncpg = asyncpg
        self.test_dsn = os.environ["TEST_POSTGRES_DSN"]
        self.admin = await asyncpg.connect(self.test_dsn)
        await self.admin.execute(
            "CREATE TABLE IF NOT EXISTS key_value "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)"
        )
        self.prefix = "test:p0_584:isolation:"
        await self.admin.execute(
            "DELETE FROM key_value WHERE key LIKE $1", self.prefix + "%"
        )
        self.old = (db._conn, db._is_pg, db._io_lock, db.DATABASE_URL)
        self.poison = MagicMock()
        self.poison.transaction.side_effect = RuntimeError(
            "SAVEPOINT can only be used in transaction blocks"
        )
        db._conn = self.poison
        db._is_pg = True
        db._io_lock = asyncio.Lock()
        db.DATABASE_URL = self.test_dsn

    async def asyncTearDown(self):
        db._conn, db._is_pg, db._io_lock, db.DATABASE_URL = self.old
        await self.admin.execute(
            "DELETE FROM key_value WHERE key LIKE $1", self.prefix + "%"
        )
        await self.admin.close()

    async def get(self, key):
        return await self.admin.fetchval(
            "SELECT value FROM key_value WHERE key=$1", self.prefix + key
        )

    async def test_reproduced_savepoint_error_does_not_poison_cas(self):
        # PostgreSQL itself rejects SAVEPOINT without an outer transaction.
        with self.assertRaises(self.asyncpg.PostgresError) as raised:
            await self.admin.execute("SAVEPOINT broken")
        self.assertIn("SAVEPOINT can only be used in transaction blocks",
                      str(raised.exception))
        # The shared connection reports that exact poisoned transaction state.
        # CAS MUST NOT access it and instead use one independent transaction.
        ok = await save_key_values_atomic_cas(
            [(self.prefix + "ledger", "v1"), (self.prefix + "hwm", "22.79")],
            expected={self.prefix + "ledger": None},
            strict=True,
        )
        self.assertTrue(ok)
        self.assertEqual(await self.get("ledger"), "v1")
        self.assertEqual(await self.get("hwm"), "22.79")
        self.poison.transaction.assert_not_called()

    async def test_stale_cas_writer_fails_closed_with_zero_partial_writes(self):
        await save_key_values_atomic_cas(
            [(self.prefix + "ledger", "v1"), (self.prefix + "hwm", "22.79")],
            expected={self.prefix + "ledger": None},
        )
        with self.assertRaises(CompareAndSwapConflict):
            await save_key_values_atomic_cas(
                [(self.prefix + "ledger", "v2"), (self.prefix + "hwm", "0")],
                expected={self.prefix + "ledger": None},
            )
        self.assertEqual(await self.get("ledger"), "v1")
        self.assertEqual(await self.get("hwm"), "22.79")

    async def test_parallel_writers_on_absent_row_single_winner(self):
        # Replacing the in-process lock with a separate lock per caller models
        # writers from different processes; the server advisory lock must
        # serialize absent-key guards.
        old_lock = db._io_lock
        class NoopLock:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                return False
        db._io_lock = NoopLock()
        try:
            async def contender(suffix):
                try:
                    await save_key_values_atomic_cas(
                        [(self.prefix + "race", suffix)],
                        expected={self.prefix + "race": None},
                        strict=True,
                    )
                    return "written"
                except CompareAndSwapConflict:
                    return "conflict"
            outcomes = await asyncio.gather(contender("one"), contender("two"))
        finally:
            db._io_lock = old_lock
        self.assertEqual(sorted(outcomes), ["conflict", "written"])
        self.assertIn(await self.get("race"), ("one", "two"))

    async def test_cancellation_rolls_back_pending_transaction(self):
        # Block CAS on its advisory lock, cancel it, and confirm it did not
        # commit the guarded ledger. A fresh subsequent CAS must still work.
        lock_key = self.prefix + "cancel"
        async with self.admin.transaction():
            await self.admin.fetchval(
                "SELECT pg_advisory_xact_lock(hashtext($1)::bigint)", lock_key
            )
            task = asyncio.create_task(save_key_values_atomic_cas(
                [(lock_key, "v1")],
                expected={lock_key: None},
                strict=True,
            ))
            await asyncio.sleep(0.3)
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
        self.assertIsNone(await self.get("cancel"))
        await save_key_values_atomic_cas(
            [(lock_key, "v2")], expected={lock_key: None}, strict=True
        )
        self.assertEqual(await self.get("cancel"), "v2")


if __name__ == "__main__":
    unittest.main()
