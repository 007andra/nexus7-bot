"""Real PostgreSQL + asyncpg reproduction of the 2026-10-11 engine exits.

CI provides TEST_POSTGRES_DSN (postgres:16). A loopback TCP proxy delays only
PostgreSQL CancelRequest connections, as the ams -> sfo round trip did in
production, so asyncpg's shared ``cancel_waiter`` is still pending when the
next diagnostic query starts. The diagnostics are the real
``hard_gate_shadow_scan`` emitters running on the real ``bot.database`` layer.

Before the fix: frontier -> TimeoutError, capital -> TimeoutError, then the
counterfactual NEXUS emitter raised CancelledError into the caller with
``Task.cancelling() == 0`` and the engine loop exited. After the fix every
emitter reports an ordinary error and the cycle completes.

Only a disposable schema is created; no exchange or production DB is touched.
"""
import asyncio
import os
import struct
import unittest
import uuid
from unittest.mock import patch
from urllib.parse import urlparse

DSN = os.environ.get("TEST_POSTGRES_DSN", "")
CANCEL_REQUEST = 80877102
SSL_REQUEST = 80877103
CANCEL_DELAY_S = 3.0


class _SlowCancelProxy:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.cancel_requests = 0
        self._server = None
        self._tasks = set()

    async def start(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def close(self):
        self._server.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _read_packet(self, reader):
        head = await reader.readexactly(8)
        size, code = struct.unpack("!II", head)
        return head + await reader.readexactly(size - 8), code

    async def _pipe(self, reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            packet, code = await self._read_packet(reader)
            if code == SSL_REQUEST:  # keep the stream plaintext for inspection
                writer.write(b"N")
                await writer.drain()
                packet, code = await self._read_packet(reader)
            if code == CANCEL_REQUEST:
                self.cancel_requests += 1
                await asyncio.sleep(CANCEL_DELAY_S)
            up_reader, up_writer = await asyncio.open_connection(self.host, self.port)
            up_writer.write(packet)
            await up_writer.drain()
            await asyncio.gather(self._pipe(reader, up_writer), self._pipe(up_reader, writer))
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
        finally:
            self._tasks.discard(task)


@unittest.skipUnless(DSN, "isolated PostgreSQL required (TEST_POSTGRES_DSN)")
class SpuriousCancelPostgresRegression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        from bot import database as db
        from bot import hard_gate_shadow_scan as scan

        self.db, self.scan = db, scan
        parsed = urlparse(DSN)
        self.schema = "nx_spurious_" + uuid.uuid4().hex[:10]
        admin = await asyncpg.connect(DSN)
        try:
            await admin.execute(f"CREATE SCHEMA {self.schema}")
            # Every diagnostic's first query reads this relation; make it slow
            # enough that only wait_for timeouts can end it.
            await admin.execute(
                f"CREATE VIEW {self.schema}.risk_epoch_shadow_v1 AS "
                "SELECT 'T'::text AS epoch_id, '{\"started_epoch\": 0}'::text AS payload "
                "FROM pg_sleep(20)"
            )
            # Finishes on its own before the delayed CancelRequest arrives: the
            # shape that leaves asyncpg's cancel_sent_waiter cancelled forever.
            await admin.execute(
                f"CREATE VIEW {self.schema}.slow_short AS SELECT 1 AS v FROM pg_sleep(1.7)"
            )
        finally:
            await admin.close()

        self.proxy = _SlowCancelProxy(parsed.hostname or "127.0.0.1", parsed.port or 5432)
        proxy_port = await self.proxy.start()
        self.conn = await asyncpg.connect(
            host="127.0.0.1", port=proxy_port, user=parsed.username,
            password=parsed.password, database=(parsed.path or "/").lstrip("/"),
            ssl=False, server_settings={"search_path": self.schema},
        )
        self._saved = (db._conn, db._is_pg, db.DATABASE_URL,
                       db._recovery_task, db._recovery_last_attempt)
        db._conn, db._is_pg = self.conn, True
        user = parsed.username or ""
        auth = f"{user}:{parsed.password}@" if parsed.password else f"{user}@"
        db.DATABASE_URL = (
            f"postgresql://{auth}127.0.0.1:{proxy_port}/{(parsed.path or '/').lstrip('/')}"
            f"?sslmode=disable&search_path={self.schema}"
        )
        db._recovery_task, db._recovery_last_attempt = None, float("-inf")
        env = patch.dict(os.environ, {
            "RISK_EPOCH_SHADOW_ID": "T",
            "MIN_ORDER_FRONTIER_AUDIT_V1": "true",
            "MIN_ORDER_CAPITAL_ADEQUACY_V1": "true",
            "MIN_ORDER_COUNTERFACTUAL_NEXUS_V1": "true",
            "MIN_ORDER_COUNTERFACTUAL_VALIDATION_V1": "true",
        })
        env.start()
        self.addCleanup(env.stop)
        for name in ("_MIN_ORDER_FRONTIER_LAST_EMIT", "_MIN_ORDER_CAPITAL_LAST_EMIT",
                     "_MIN_ORDER_CF_NEXUS_LAST_EMIT", "_MIN_ORDER_CF_VALIDATION_LAST_EMIT"):
            p = patch.object(scan, name, 0.0)
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        import asyncpg
        task = self.db._recovery_task
        if task is not None and not task.done():
            await asyncio.wait_for(asyncio.shield(task), timeout=20)
        for conn in {id(c): c for c in (self.conn, self.db._conn) if c is not None}.values():
            conn.terminate()
        (self.db._conn, self.db._is_pg, self.db.DATABASE_URL,
         self.db._recovery_task, self.db._recovery_last_attempt) = self._saved
        await self.proxy.close()
        admin = await asyncpg.connect(DSN)
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE pid <> pg_backend_pid() AND query LIKE '%pg_sleep(20)%'"
            )
            await admin.execute(f"DROP SCHEMA IF EXISTS {self.schema} CASCADE")
        finally:
            await admin.close()

    async def test_diagnostic_timeout_cascade_does_not_cancel_engine_cycle(self):
        scan, db = self.scan, self.db
        stages = (
            scan._maybe_emit_min_order_frontier,
            scan._maybe_emit_min_order_capital_adequacy,
            scan._maybe_emit_min_order_counterfactual_nexus,
            scan._maybe_emit_min_order_counterfactual_validation,
        )

        async def engine_cycle():
            # Mirrors the engine main loop: CancelledError means "shutdown".
            try:
                for stage in stages:
                    await stage(db)
                return "COMPLETED"
            except asyncio.CancelledError:
                return f"CAUGHT_CANCELLED cancelling={asyncio.current_task().cancelling()}"

        task = asyncio.create_task(engine_cycle())
        outcome = await asyncio.wait_for(task, timeout=30)
        self.assertGreaterEqual(self.proxy.cancel_requests, 1)
        self.assertEqual(outcome, "COMPLETED")
        self.assertFalse(task.cancelled())
        self.assertFalse(db._io_lock.locked())

    async def test_ownership_validation_on_poisoned_connection_is_ordinary_error(self):
        from bot import execution_ownership as eo
        from bot.execution_capability import ExecutionCapability
        db = self.db

        async def poison():
            for _ in range(2):  # timeout, then timeout while awaiting cancel_waiter
                try:
                    await asyncio.wait_for(db._fetchall("SELECT payload FROM risk_epoch_shadow_v1"), 1.0)
                except asyncio.TimeoutError:
                    pass

        await poison()
        owner = eo.ExecutionOwnership("o", 1, "s", "a", "h", "2099-01-01T00:00:00+00:00")
        with patch.object(eo, "current_execution_capability",
                          return_value=ExecutionCapability.LIVE):
            with self.assertRaises(Exception) as ctx:
                await eo.validate_execution_ownership(owner)
        self.assertIsInstance(ctx.exception, db.DatabaseOperationAborted)

    async def test_permanently_poisoned_connection_is_replaced(self):
        """cancel_sent_waiter cancelled -> every query fails forever without recovery."""
        db = self.db
        old = db._conn
        try:
            await asyncio.wait_for(db._fetchall("SELECT v FROM slow_short"), 1.5)
        except asyncio.TimeoutError:
            pass
        try:  # waits on the in-flight CancelRequest and is cancelled there
            await asyncio.wait_for(db._fetchone("SELECT 1"), 1.5)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(CANCEL_DELAY_S + 1)  # cancel and query long finished
        with self.assertRaises(db.DatabaseOperationAborted):
            await db._fetchone("SELECT 1")  # still poisoned: proves the hazard
        recovery = db._recovery_task
        self.assertIsNotNone(recovery)
        self.assertTrue(await asyncio.wait_for(recovery, timeout=20))
        self.assertIsNot(db._conn, old)
        self.assertTrue(old.is_closed())
        row = await db._fetchone("SELECT 41 + 1")
        self.assertEqual(row[0], 42)
        self.assertFalse(db._io_lock.locked())


if __name__ == "__main__":
    unittest.main()
