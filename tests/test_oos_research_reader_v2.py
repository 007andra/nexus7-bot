"""Dedicated research reader: isolation, fail-closed and recovery contracts."""
import asyncio
import os
import unittest
from unittest.mock import patch

from bot import oos_research_reader_v2 as rr


def _poisoned():
    fut = asyncio.get_running_loop().create_future()
    fut.cancel()
    return fut


class FakeConn:
    def __init__(self, behavior="ok", delay=0.0):
        self.behavior = behavior
        self.delay = delay
        self.terminated = False
        self.fetches = 0

    def is_closed(self):
        return self.terminated

    def terminate(self):
        self.terminated = True

    async def fetch(self, sql, *params):
        self.fetches += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.terminated:
            raise ConnectionResetError("terminated")
        if self.behavior == "reset":
            raise ConnectionResetError("transport lost")
        if self.behavior == "poisoned":
            await _poisoned()
        return [{"payload": "{}"}]


class Factory:
    def __init__(self, *behaviors, fail_first=0):
        self.behaviors = list(behaviors)
        self.conns = []
        self.fail_first = fail_first
        self.attempts = 0

    async def __call__(self, dsn):
        self.attempts += 1
        if self.attempts <= self.fail_first:
            raise OSError("connect refused")
        spec = self.behaviors.pop(0) if self.behaviors else ("ok", 0.0)
        conn = FakeConn(*spec)
        self.conns.append(conn)
        return conn


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class OperationalTripwire:
    """Stands in for bot.database: any use proves the isolation is broken."""

    _is_pg = True

    async def _fetchall(self, *a, **k):
        raise AssertionError("operational connection used by research read")

    async def _exec(self, *a, **k):
        raise AssertionError("operational connection used by research read")


def _reader(factory, **kw):
    kw.setdefault("query_timeout_s", 0.2)
    kw.setdefault("acquire_timeout_s", 0.1)
    return rr.ResearchReader(dsn="postgresql://u@h/db", connect=factory, **kw)


class RoutingTests(unittest.TestCase):
    def test_flag_off_keeps_operational_handle(self):
        db = object()
        with patch.dict(os.environ, {rr.FLAG: "false"}):
            self.assertIs(rr.route(db), db)

    def test_flag_on_routes_to_dedicated_reader(self):
        db = object()
        with patch.dict(os.environ, {rr.FLAG: "true"}):
            self.assertIsInstance(rr.route(db), rr.ResearchReader)


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_select_only_and_exec_rejected(self):
        reader = _reader(Factory())
        with self.assertRaises(rr.ResearchReaderRejected):
            await reader._fetchall("DELETE FROM t")
        with self.assertRaises(rr.ResearchReaderRejected):
            await reader._exec("CREATE TABLE x(a int)")
        self.assertEqual(len(reader._fetch_ms), 0)

    async def test_missing_dsn_fails_closed_without_operational_fallback(self):
        reader = rr.ResearchReader(dsn="")
        with self.assertRaises(rr.ResearchReaderUnavailable):
            await reader._fetchall("SELECT 1")

    async def test_query_ok_and_connection_reused(self):
        f = Factory(("ok", 0.0))
        reader = _reader(f)
        await reader._fetchall("SELECT 1")
        await reader._fetchall("SELECT 1")
        self.assertEqual(f.attempts, 1)
        self.assertEqual(reader.stats["ok"], 2)

    async def test_own_timeout_discards_connection_then_reconnects(self):
        f = Factory(("ok", 5.0), ("ok", 0.0))
        reader = _reader(f)
        with self.assertRaises(rr.ResearchReaderTimeout):
            await reader._fetchall("SELECT pg_sleep(5)")
        self.assertTrue(f.conns[0].terminated)
        await reader._fetchall("SELECT 1")
        self.assertEqual(f.attempts, 2)

    async def test_caller_wait_for_cancel_terminates_instead_of_reusing(self):
        f = Factory(("ok", 5.0), ("ok", 0.0))
        reader = _reader(f, query_timeout_s=10)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(reader._fetchall("SELECT 1"), 0.05)
        self.assertTrue(f.conns[0].terminated)
        self.assertEqual(await reader._fetchall("SELECT 1"), [{"payload": "{}"}])
        self.assertEqual(f.attempts, 2)

    async def test_driver_level_cancel_is_ordinary_error_and_discards(self):
        f = Factory(("poisoned", 0.0), ("ok", 0.0))
        reader = _reader(f)
        with self.assertRaises(rr.ResearchReaderError):
            await reader._fetchall("SELECT 1")
        self.assertTrue(f.conns[0].terminated)
        await reader._fetchall("SELECT 1")

    async def test_transport_loss_then_recovery(self):
        f = Factory(("reset", 0.0), ("ok", 0.0))
        reader = _reader(f)
        with self.assertRaises(rr.ResearchReaderError):
            await reader._fetchall("SELECT 1")
        self.assertEqual(reader.stats["discards"], 1)
        await reader._fetchall("SELECT 1")
        self.assertEqual(reader.stats["ok"], 1)

    async def test_reconnect_is_rate_limited(self):
        clock = Clock()
        f = Factory(("ok", 0.0), fail_first=1)
        reader = _reader(f, clock=clock, reconnect_min_interval_s=5)
        with self.assertRaises(rr.ResearchReaderUnavailable):
            await reader._fetchall("SELECT 1")
        with self.assertRaises(rr.ResearchReaderUnavailable):
            await reader._fetchall("SELECT 1")
        self.assertEqual(f.attempts, 1)  # backoff: no hot reconnect loop
        clock.t += 6
        await reader._fetchall("SELECT 1")
        self.assertEqual(f.attempts, 2)

    async def test_concurrency_limit_rejects_after_bounded_wait(self):
        f = Factory(("ok", 0.5))
        reader = _reader(f, query_timeout_s=2, acquire_timeout_s=0.05, max_concurrency=1)
        first = asyncio.create_task(reader._fetchall("SELECT 1"))
        await asyncio.sleep(0.01)
        with self.assertRaises(rr.ResearchReaderBusy):
            await reader._fetchall("SELECT 1")
        await first
        self.assertEqual(reader.stats["busy"], 1)

    async def test_semaphore_released_on_every_path(self):
        f = Factory(("reset", 0.0), ("ok", 5.0), ("ok", 0.0))
        reader = _reader(f)
        for _ in range(2):
            with self.assertRaises(rr.ResearchReaderError):
                await reader._fetchall("SELECT 1")
        self.assertFalse(reader._semaphore.locked())
        await reader._fetchall("SELECT 1")

    async def test_real_cancel_of_caller_propagates(self):
        f = Factory(("ok", 5.0))
        reader = _reader(f, query_timeout_s=10)
        task = asyncio.create_task(reader._fetchall("SELECT 1"))
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(f.conns[0].terminated)
        self.assertFalse(reader._semaphore.locked())

    async def test_latency_summary_reports_percentiles(self):
        reader = _reader(Factory(("ok", 0.0)))
        for _ in range(5):
            await reader._fetchall("SELECT 1")
        s = reader.latency_summary()
        for key in ("wait_ms_p50", "wait_ms_p95", "wait_ms_p99",
                    "fetch_ms_p50", "fetch_ms_p95", "fetch_ms_p99"):
            self.assertIsNotNone(s[key])


class HardGateWiringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = rr._reader

    def tearDown(self):
        rr._reader = self._saved

    async def test_readout_with_reader_unavailable_never_touches_operational(self):
        from bot import hard_gate_shadow_scan as scan
        rr._reader = rr.ResearchReader(dsn="")  # unavailable: fail closed
        with patch.dict(os.environ, {
            rr.FLAG: "true", "MIN_ORDER_COUNTERFACTUAL_NEXUS_V1": "true",
            "RISK_EPOCH_SHADOW_ID": "T",
        }), patch.object(scan, "_MIN_ORDER_CF_NEXUS_LAST_EMIT", 0.0):
            self.assertIsNone(
                await scan._maybe_emit_min_order_counterfactual_nexus(OperationalTripwire())
            )
        self.assertEqual(rr._reader.stats["unavailable"], 1)

    async def test_readout_uses_dedicated_reader_when_enabled(self):
        from bot import hard_gate_shadow_scan as scan
        f = Factory(("ok", 0.0), ("ok", 0.0))

        class _Conn(FakeConn):
            async def fetch(self, sql, *params):
                return []

        async def connect(dsn):
            f.attempts += 1
            return _Conn()

        rr._reader = rr.ResearchReader(dsn="postgresql://u@h/db", connect=connect)
        with patch.dict(os.environ, {
            rr.FLAG: "true", "MIN_ORDER_FRONTIER_AUDIT_V1": "true",
            "RISK_EPOCH_SHADOW_ID": "T",
        }), patch.object(scan, "_MIN_ORDER_FRONTIER_LAST_EMIT", 0.0):
            report = await scan._maybe_emit_min_order_frontier(OperationalTripwire())
        self.assertIsNotNone(report)
        self.assertGreaterEqual(rr._reader.stats["ok"], 1)

    async def test_flag_off_keeps_exact_previous_path(self):
        from bot import hard_gate_shadow_scan as scan
        calls = []

        class _Db:
            _is_pg = True

            async def _fetchall(self, sql, params=(), *, strict=False):
                calls.append(sql)
                return []

        with patch.dict(os.environ, {
            rr.FLAG: "false", "MIN_ORDER_FRONTIER_AUDIT_V1": "true",
            "RISK_EPOCH_SHADOW_ID": "T",
        }), patch.object(scan, "_MIN_ORDER_FRONTIER_LAST_EMIT", 0.0):
            await scan._maybe_emit_min_order_frontier(_Db())
        self.assertEqual(len(calls), 1)

    async def test_cohort_snapshot_never_runs_ddl_through_reader(self):
        from bot import prospective_oos_cohort_v1 as cohort

        class _Conn(FakeConn):
            async def fetch(self, sql, *params):
                return []  # metadata missing

        async def connect(dsn):
            return _Conn()

        reader = rr.ResearchReader(dsn="postgresql://u@h/db", connect=connect)
        with self.assertRaises(RuntimeError) as ctx:
            await cohort.load_frozen_metadata_for_snapshot(reader)
        self.assertIn("OOS_READONLY_METADATA_MISSING", str(ctx.exception))
        self.assertEqual(reader.stats["rejected"], 0)


if __name__ == "__main__":
    unittest.main()
