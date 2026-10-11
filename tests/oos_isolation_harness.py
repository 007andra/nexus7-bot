"""Shared harness for OOS research-reader isolation tests and benchmarks.

Real PostgreSQL behind a loopback proxy that injects one-way latency (the
production bot runs in ams, PostgreSQL in sfo: ~140 ms RTT), a disposable
schema shaped like production, the real ownership heartbeat functions as
the operational workload and the real hard_gate_shadow_scan research
emitters as the research workload. Never touches an exchange.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import time
import uuid
from contextlib import ExitStack
from unittest.mock import patch
from urllib.parse import urlparse

DSN = os.environ.get("TEST_POSTGRES_DSN", "")
POPULATION = "HARD_GATE_SHADOW"
EPOCH_ID = "BENCH_EPOCH"
RESEARCH_FLAGS = (
    "MIN_ORDER_FRONTIER_AUDIT_V1", "MIN_ORDER_CAPITAL_ADEQUACY_V1",
    "MIN_ORDER_COUNTERFACTUAL_NEXUS_V1", "MIN_ORDER_COUNTERFACTUAL_VALIDATION_V1",
    "MIN_ORDER_COUNTERFACTUAL_GATE_ATTRIBUTION_V1",
    "MIN_ORDER_COUNTERFACTUAL_THRESHOLD_SENSITIVITY_V1",
    "MIN_ORDER_COUNTERFACTUAL_DECISION_REVIEW_V1",
)
LAST_EMIT = (
    "_MIN_ORDER_FRONTIER_LAST_EMIT", "_MIN_ORDER_CAPITAL_LAST_EMIT",
    "_MIN_ORDER_CF_NEXUS_LAST_EMIT", "_MIN_ORDER_CF_VALIDATION_LAST_EMIT",
    "_MIN_ORDER_CF_ATTRIBUTION_LAST_EMIT", "_MIN_ORDER_CF_SENSITIVITY_LAST_EMIT",
    "_MIN_ORDER_CF_REVIEW_LAST_EMIT",
)


def percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def summarize(values):
    return {
        "n": len(values),
        "p50": round(percentile(values, 50) or 0.0, 1),
        "p95": round(percentile(values, 95) or 0.0, 1),
        "p99": round(percentile(values, 99) or 0.0, 1),
        "max": round(max(values) if values else 0.0, 1),
    }


class LatencyProxy:
    """Loopback TCP proxy adding fixed one-way delay, order preserved."""

    def __init__(self, host, port, one_way_ms):
        self.host, self.port = host, port
        self.delay = one_way_ms / 1000.0
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

    async def _pump(self, reader, writer):
        queue = asyncio.Queue()

        async def deliver():
            try:
                while True:
                    due, data = await queue.get()
                    if data is None:
                        break
                    wait = due - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    writer.write(data)
                    await writer.drain()
            except Exception:
                pass
            finally:
                writer.close()

        sender = asyncio.ensure_future(deliver())
        try:
            while data := await reader.read(65536):
                queue.put_nowait((time.monotonic() + self.delay, data))
        except Exception:
            pass
        finally:
            queue.put_nowait((0, None))
            await asyncio.gather(sender, return_exceptions=True)

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            up_reader, up_writer = await asyncio.open_connection(self.host, self.port)
            await asyncio.gather(self._pump(reader, up_writer), self._pump(up_reader, writer))
        except Exception:
            writer.close()
        finally:
            self._tasks.discard(task)


def _payload(i, rng):
    return json.dumps({
        "candidate_id": f"HARD_GATE_SHADOW:SYM{i % 25}:LONG:MOMENTUM:{i}",
        "symbol": f"SYM{i % 25}USDT", "setup": ["MOMENTUM", "PULLBACK", "BOS_BREAK"][i % 3],
        "direction": "LONG" if i % 2 else "SHORT", "score": 55 + i % 40,
        "rr": round(1.2 + rng.random(), 3), "stop_pct": round(rng.random() / 50, 6),
        "min_order": {"binding": "MIN_NOTIONAL", "feasible": bool(i % 3),
                      "required_equity": round(5 + rng.random() * 20, 4)},
        "nexus": {"allowed": bool(i % 5 == 0), "ev": round(rng.random(), 4),
                  "confidence": round(rng.random() * 100, 2)},
        "features": {f"f{k}": round(rng.random(), 6) for k in range(40)},
    })


class Schema:
    """Disposable schema with production-like research volumes."""

    def __init__(self, candidates=2200, outcomes_per_candidate=2):
        self.name = "nx_oosbench_" + uuid.uuid4().hex[:8]
        self.candidates = candidates
        self.outcomes_per_candidate = outcomes_per_candidate

    async def create(self):
        import asyncpg
        conn = await asyncpg.connect(DSN)
        try:
            s = self.name
            await conn.execute(f"CREATE SCHEMA {s}")
            await conn.execute(f"CREATE TABLE {s}.key_value(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
            await conn.execute(
                f"CREATE TABLE {s}.risk_epoch_shadow_v1(epoch_id TEXT PRIMARY KEY, started_epoch REAL NOT NULL,"
                " start_equity REAL NOT NULL, historical_hwm REAL NOT NULL,"
                " lifetime_drawdown REAL NOT NULL, payload TEXT NOT NULL)")
            await conn.execute(
                f"CREATE TABLE {s}.hard_gate_shadow_candidates_v1(candidate_id TEXT PRIMARY KEY,"
                " captured_epoch REAL NOT NULL, symbol TEXT NOT NULL, population TEXT NOT NULL,"
                " payload TEXT NOT NULL)")
            await conn.execute(
                f"CREATE TABLE {s}.hard_gate_shadow_outcomes_v1(candidate_id TEXT NOT NULL,"
                " horizon INTEGER NOT NULL, population TEXT NOT NULL, payload TEXT NOT NULL,"
                " PRIMARY KEY(candidate_id,horizon))")
            await conn.execute(
                f"INSERT INTO {s}.risk_epoch_shadow_v1 VALUES($1,0,5.3,22.8,0.76,$2)",
                EPOCH_ID, json.dumps({"epoch_id": EPOCH_ID, "started_epoch": 0}))
            rng = random.Random(7)
            rows = [(f"c{i}", float(i), f"SYM{i % 25}USDT", POPULATION, _payload(i, rng))
                    for i in range(self.candidates)]
            await conn.copy_records_to_table("hard_gate_shadow_candidates_v1", schema_name=s,
                                             records=rows)
            outs = [(f"c{i}", h, POPULATION, json.dumps({"net_return": rng.random() - 0.5,
                                                         "MFE": rng.random(), "MAE": -rng.random()}))
                    for i in range(self.candidates) for h in (60, 240)[: self.outcomes_per_candidate]]
            await conn.copy_records_to_table("hard_gate_shadow_outcomes_v1", schema_name=s,
                                             records=outs)
            await conn.execute(f"ANALYZE {s}.hard_gate_shadow_candidates_v1")
        finally:
            await conn.close()

    async def drop(self):
        import asyncpg
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE pid <> pg_backend_pid() AND datname = current_database() "
                "AND query ILIKE $1", f"%{self.name}%")
            await conn.execute(f"DROP SCHEMA IF EXISTS {self.name} CASCADE")
        finally:
            await conn.close()


def dsn_via(port, schema):
    parsed = urlparse(DSN)
    user = parsed.username or ""
    auth = f"{user}:{parsed.password}@" if parsed.password else f"{user}@"
    return (f"postgresql://{auth}127.0.0.1:{port}/{(parsed.path or '/').lstrip('/')}"
            f"?sslmode=disable&search_path={schema}")


class Rig:
    """Wire bot.database + research reader to the proxied schema."""

    def __init__(self, one_way_ms=70, candidates=2200):
        self.one_way_ms = one_way_ms
        self.schema = Schema(candidates=candidates)
        self.stack = ExitStack()

    async def __aenter__(self):
        import asyncpg
        from bot import database as db
        from bot import execution_ownership as eo
        from bot import hard_gate_shadow_scan as scan
        from bot import oos_research_reader_v2 as rr
        from bot.execution_capability import ExecutionCapability
        self.db, self.eo, self.scan, self.rr = db, eo, scan, rr
        await self.schema.create()
        parsed = urlparse(DSN)
        self.proxy = LatencyProxy(parsed.hostname or "127.0.0.1", parsed.port or 5432,
                                  self.one_way_ms)
        port = await self.proxy.start()
        self.dsn = dsn_via(port, self.schema.name)
        self.op_conn = await asyncpg.connect(self.dsn)
        self._saved = (db._conn, db._is_pg, db.DATABASE_URL, rr._reader, db._io_lock,
                       db._recovery_task, db._recovery_last_attempt)
        db._conn, db._is_pg, db.DATABASE_URL = self.op_conn, True, self.dsn
        # Each test runs on a fresh event loop: never inherit a module lock or a
        # recovery task bound to (or left pending on) a previous loop.
        db._io_lock = asyncio.Lock()
        db._recovery_task, db._recovery_last_attempt = None, float("-inf")
        rr._reader = rr.ResearchReader(dsn=self.dsn)
        env = {"RISK_EPOCH_SHADOW_ID": EPOCH_ID, "LOG_LEVEL": "ERROR"}
        env.update({flag: "true" for flag in RESEARCH_FLAGS})
        self.stack.enter_context(patch.dict(os.environ, env))
        self.stack.enter_context(patch.object(
            eo, "current_execution_capability", return_value=ExecutionCapability.LIVE))
        return self

    async def __aexit__(self, *exc):
        db, rr = self.db, self.rr
        self.stack.close()
        try:
            await rr._reader.close()
        except Exception:
            pass
        for conn in {id(c): c for c in (self.op_conn, db._conn) if c is not None}.values():
            conn.terminate()
        task = db._recovery_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        (db._conn, db._is_pg, db.DATABASE_URL, rr._reader, db._io_lock,
         db._recovery_task, db._recovery_last_attempt) = self._saved
        await self.proxy.close()
        await self.schema.drop()

    def research_flag(self, on):
        return patch.dict(os.environ, {self.rr.FLAG: "true" if on else "false"})

    async def heartbeat_once(self):
        """Exactly the production renewal: acquire + validate through db._io_lock."""
        t0 = time.perf_counter()
        ownership = await self.eo.acquire_execution_ownership()
        await self.eo.validate_execution_ownership(ownership)
        return (time.perf_counter() - t0) * 1000.0

    async def lock_probe_once(self):
        """Operational lock wait as any persistence/reconciliation call sees it."""
        db = self.db
        t0 = time.perf_counter()
        async with db._io_lock:
            waited = (time.perf_counter() - t0) * 1000.0
            await db._conn.fetchval("SELECT 1")
        return waited, (time.perf_counter() - t0) * 1000.0

    async def research_round(self):
        scan = self.scan
        for name in LAST_EMIT:
            setattr(scan, name, 0.0)
        t0 = time.perf_counter()
        for emit in (scan._maybe_emit_min_order_frontier,
                     scan._maybe_emit_min_order_capital_adequacy,
                     scan._maybe_emit_min_order_counterfactual_nexus,
                     scan._maybe_emit_min_order_counterfactual_validation,
                     scan._maybe_emit_min_order_counterfactual_gate_attribution,
                     scan._maybe_emit_min_order_counterfactual_threshold_sensitivity,
                     scan._maybe_emit_min_order_counterfactual_decision_review):
            await emit(self.db)
        return (time.perf_counter() - t0) * 1000.0


async def run_mixed_load(rig, *, research_on, seconds, hb_interval=1.0, probe_interval=0.25):
    """Concurrent operational + research load; returns latency samples (ms)."""
    hb, lock_wait, op_total, research = [], [], [], []
    errors = {"heartbeat": 0}
    stop = asyncio.Event()

    async def heartbeat_loop():
        while not stop.is_set():
            try:
                hb.append(await rig.heartbeat_once())
            except Exception:
                errors["heartbeat"] += 1
            await asyncio.sleep(hb_interval)

    async def probe_loop():
        while not stop.is_set():
            waited, total = await rig.lock_probe_once()
            lock_wait.append(waited)
            op_total.append(total)
            await asyncio.sleep(probe_interval)

    async def research_loop():
        while not stop.is_set():
            research.append(await rig.research_round())
            await asyncio.sleep(0)

    with rig.research_flag(research_on):
        tasks = [asyncio.create_task(c()) for c in (heartbeat_loop, probe_loop, research_loop)]
        await asyncio.sleep(seconds)
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    return {
        "heartbeat_ms": summarize(hb), "lock_wait_ms": summarize(lock_wait),
        "operational_query_ms": summarize(op_total), "research_round_ms": summarize(research),
        "heartbeat_errors": errors["heartbeat"],
        "reader": rig.rr._reader.latency_summary() if research_on else None,
    }
