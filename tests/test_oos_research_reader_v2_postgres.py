"""Real PostgreSQL proof: a stuck OOS read cannot delay the ownership heartbeat.

CI provides TEST_POSTGRES_DSN. A loopback proxy injects ~70 ms one-way latency
(ams -> sfo in production). Research reads hit a table another session holds
ACCESS EXCLUSIVE, so they hang exactly like a slow production readout.
"""
import asyncio
import time
import unittest

from tests.oos_isolation_harness import DSN, Rig

ONE_WAY_MS = 70
LOCK_HOLD_S = 3.0


@unittest.skipUnless(DSN, "isolated PostgreSQL required (TEST_POSTGRES_DSN)")
class ResearchReaderPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rig = await Rig(one_way_ms=ONE_WAY_MS, candidates=300).__aenter__()

    async def asyncTearDown(self):
        await self.rig.__aexit__(None, None, None)

    async def _baseline_heartbeat_ms(self):
        await self.rig.heartbeat_once()  # warm-up: first call creates the lease row
        samples = sorted([await self.rig.heartbeat_once() for _ in range(5)])
        return samples[len(samples) // 2]

    async def _heartbeats_during_stuck_read(self, research_on):
        import asyncpg
        rig = self.rig
        locker = await asyncpg.connect(DSN)
        try:
            tx = locker.transaction()
            await tx.start()
            await locker.execute(
                f"LOCK TABLE {rig.schema.name}.hard_gate_shadow_candidates_v1 "
                "IN ACCESS EXCLUSIVE MODE")
            with rig.research_flag(research_on):
                rig.scan._MIN_ORDER_FRONTIER_LAST_EMIT = 0.0
                readout = asyncio.create_task(rig.scan._maybe_emit_min_order_frontier(rig.db))
                await asyncio.sleep(0.4)  # research query is now waiting on the lock
                samples = []
                deadline = time.monotonic() + LOCK_HOLD_S - 0.5
                while time.monotonic() < deadline:
                    samples.append(await rig.heartbeat_once())
                result = await asyncio.wait_for(readout, timeout=10)
            await tx.rollback()
        finally:
            await locker.close()
        return samples, result

    async def test_stuck_research_read_does_not_block_heartbeat_when_isolated(self):
        base = await self._baseline_heartbeat_ms()
        samples, result = await self._heartbeats_during_stuck_read(research_on=True)
        self.assertIsNone(result)  # readout failed closed (lock_timeout), no report
        self.assertGreaterEqual(len(samples), 2)
        # Heartbeat keeps its normal cost while the research read is stuck.
        self.assertLess(max(samples), base * 1.5 + 150, (base, samples))
        self.assertFalse(self.rig.db._io_lock.locked())

    async def test_shared_path_blocks_heartbeat_control(self):
        """Control: with the flag off the same stuck read holds db._io_lock."""
        base = await self._baseline_heartbeat_ms()
        samples, _ = await self._heartbeats_during_stuck_read(research_on=False)
        self.assertGreater(max(samples), base + 800, (base, samples))

    async def test_reader_session_is_read_only_server_side(self):
        import asyncpg
        reader = self.rig.rr._reader
        await reader._fetchall("SELECT 1")
        with self.assertRaises(asyncpg.exceptions.ReadOnlySQLTransactionError):
            await reader._conn.execute(
                f"CREATE TABLE {self.rig.schema.name}.must_not_exist(a int)")

    async def test_timed_out_research_query_leaves_no_server_backend(self):
        import asyncpg
        rig = self.rig
        reader = rig.rr._reader
        reader.query_timeout_s = 0.5
        locker = await asyncpg.connect(DSN)
        try:
            tx = locker.transaction()
            await tx.start()
            await locker.execute(
                f"LOCK TABLE {rig.schema.name}.hard_gate_shadow_candidates_v1 "
                "IN ACCESS EXCLUSIVE MODE")
            with self.assertRaises(rig.rr.ResearchReaderTimeout):
                await reader._fetchall(
                    "SELECT payload FROM hard_gate_shadow_candidates_v1 LIMIT 1")
            await asyncio.sleep(1.0)
            active = await locker.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name=$1",
                "nexus_oos_research_v2")
            await tx.rollback()
        finally:
            await locker.close()
        self.assertEqual(active, 0)  # terminated, not left waiting on the lock

    async def test_transport_loss_then_independent_recovery(self):
        import asyncpg
        rig = self.rig
        reader = rig.rr._reader
        await reader._fetchall("SELECT 1")
        admin = await asyncpg.connect(DSN)
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE application_name=$1", "nexus_oos_research_v2")
        finally:
            await admin.close()
        await asyncio.sleep(0.3)
        rows = None
        for _ in range(2):  # the closed session is detected; at most one failed read
            try:
                rows = await reader._fetchall("SELECT 41 + 1 AS v")
                break
            except rig.rr.ResearchReaderError:
                continue
        self.assertEqual(rows[0]["v"], 42)
        self.assertEqual(reader.stats["connects"], 2)  # recovered on its own session
        # The operational connection was never involved.
        self.assertEqual(await rig.db._conn.fetchval("SELECT 7"), 7)

    async def test_research_round_runs_through_reader_without_operational_lock(self):
        rig = self.rig
        acquisitions = []
        original = rig.db._io_lock.acquire

        async def spy():
            acquisitions.append(time.perf_counter())
            return await original()

        rig.db._io_lock.acquire = spy
        try:
            with rig.research_flag(True):
                await rig.research_round()
        finally:
            del rig.db._io_lock.acquire
        self.assertEqual(acquisitions, [])
        self.assertGreater(rig.rr._reader.stats["ok"], 0)


if __name__ == "__main__":
    unittest.main()
