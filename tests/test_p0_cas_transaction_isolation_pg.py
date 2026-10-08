"""P0 incident #584: real PostgreSQL durable CAS transaction isolation.

CI MUST provide TEST_POSTGRES_DSN and run this test explicitly against
postgres:16. All writes use disposable, namespaced test keys only.
"""
import asyncio
import os
import sys
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

    async def wait_for_backend(self, pid, *, query_fragment, wait_type):
        async def poll():
            while True:
                row = await self.admin.fetchrow(
                    "SELECT query, wait_event_type FROM pg_stat_activity WHERE pid=$1", pid
                )
                if row and query_fragment in row[0] and row[1] == wait_type:
                    return
                await asyncio.sleep(0.01)
        await asyncio.wait_for(poll(), timeout=5)

    async def test_real_failed_begin_leaves_shared_driver_state_desynchronized(self):
        # A causal reproducer for one possible mechanism, NOT proof that this
        # unprotected overlap was the production incident's initiating event.
        shared = await self.asyncpg.connect(self.test_dsn)
        sleeper = asyncio.create_task(shared.execute("SELECT pg_sleep(0.5)"))
        try:
            await self.wait_for_backend(
                shared.get_server_pid(), query_fragment="pg_sleep", wait_type="Timeout"
            )
            with self.assertRaises(self.asyncpg.InterfaceError):
                await shared.transaction().start()
            await sleeper
            self.assertFalse(shared.is_in_transaction())
            with self.assertRaisesRegex(
                self.asyncpg.PostgresError,
                "SAVEPOINT can only be used in transaction blocks",
            ):
                await shared.transaction().start()
            db._conn = shared
            key = self.prefix + "real_poison"
            self.assertTrue(await save_key_values_atomic_cas(
                [(key, "durable")], expected={key: None}, strict=True
            ))
            self.assertEqual(await self.get("real_poison"), "durable")
        finally:
            await asyncio.gather(sleeper, return_exceptions=True)
            await shared.close()

    async def test_server_error_after_first_write_rolls_back_all_keys(self):
        # PostgreSQL rejects NUL in text in the SECOND statement, after the
        # first INSERT really ran. This is not merely a guard-before-write test.
        first = self.prefix + "partial_first"
        second = self.prefix + "partial_second"
        with self.assertRaises(db.PersistenceError):
            await save_key_values_atomic_cas(
                [(first, "must_rollback"), (second, "invalid\x00text")],
                expected={first: None}, strict=True,
            )
        self.assertIsNone(await self.get("partial_first"))
        self.assertIsNone(await self.get("partial_second"))
        self.assertTrue(await save_key_values_atomic_cas(
            [(first, "recovered"), (second, "recovered")],
            expected={first: None, second: None}, strict=True,
        ))

    async def test_cancel_after_first_write_rolls_back_and_releases_session(self):
        first = self.prefix + "cancel_first"
        second = self.prefix + "cancel_second"
        await self.admin.execute(
            "INSERT INTO key_value(key,value) VALUES($1,'old')", second
        )
        blocker = await self.asyncpg.connect(self.test_dsn)
        task = None
        try:
            async with blocker.transaction():
                await blocker.fetchrow(
                    "SELECT value FROM key_value WHERE key=$1 FOR UPDATE", second
                )
                task = asyncio.create_task(save_key_values_atomic_cas(
                    [(first, "uncommitted"), (second, "new")],
                    expected={first: None}, strict=True,
                ))
                async def blocked_writer():
                    while True:
                        rows = await self.admin.fetch(
                            "SELECT pid FROM pg_stat_activity WHERE datname=current_database() "
                            "AND wait_event_type='Lock' AND query LIKE 'INSERT INTO key_value%'"
                        )
                        if rows:
                            return rows[0][0]
                        if task.done():
                            await task
                            self.fail("CAS finished without reaching the blocked second write")
                        await asyncio.sleep(0.01)
                pid = await asyncio.wait_for(blocked_writer(), timeout=5)
                self.assertIsNone(await self.get("cancel_first"))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5)
            self.assertIsNone(await self.get("cancel_first"))
            self.assertEqual(await self.get("cancel_second"), "old")
            self.assertFalse(await self.admin.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1)", pid
            ))
            self.assertTrue(await save_key_values_atomic_cas(
                [(first, "recovered"), (second, "new")],
                expected={first: None, second: "old"}, strict=True,
            ))
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await blocker.close()

    async def test_independent_process_writers_and_restart_reject_stale_guard(self):
        program = '''
import asyncio, os, sys
import asyncpg
from bot import database as db
from bot.atomic_key_value import save_key_values_atomic_cas, CompareAndSwapConflict
async def main():
    db.DATABASE_URL = os.environ["TEST_POSTGRES_DSN"]
    db._conn = await asyncpg.connect(db.DATABASE_URL)
    db._is_pg = True
    db._io_lock = asyncio.Lock()
    try:
        try:
            await save_key_values_atomic_cas([(sys.argv[1], sys.argv[2])],
                                             expected={sys.argv[1]: None}, strict=True)
            print("RESULT=written")
        except CompareAndSwapConflict:
            print("RESULT=conflict")
    finally:
        await db._conn.close()
asyncio.run(main())
'''
        key = self.prefix + "process_race"
        async def contender(value):
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", program, key, value,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
                self.assertEqual(proc.returncode, 0)
                return next(line for line in stdout.decode().splitlines()
                            if line.startswith("RESULT="))
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
        results = await asyncio.gather(contender("one"), contender("two"))
        self.assertEqual(sorted(results), ["RESULT=conflict", "RESULT=written"])
        before = await self.get("process_race")
        self.assertEqual(await contender("restart"), "RESULT=conflict")
        self.assertEqual(await self.get("process_race"), before)


if __name__ == "__main__":
    unittest.main()
