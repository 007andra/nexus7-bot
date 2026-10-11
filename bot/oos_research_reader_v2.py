"""Dedicated read-only PostgreSQL reader for research/OOS diagnostics.

Research readouts (MIN_ORDER_*, BBO calibration, prospective OOS reviews...)
used to run on the single operational connection behind ``database._io_lock``.
Their heavy full-payload SELECTs competed with ownership renewal, persistence
and reconciliation, and their ``wait_for`` timeouts poisoned the shared
asyncpg connection (2026-10-11 incidents).

With ``OOS_RESEARCH_READER_V2`` enabled, read-only research snapshots go
through this reader instead:

* its own asyncpg connection, opened from ``OOS_DIAGNOSTIC_DATABASE_URL``
  (least-privilege DSN), read-only at session level, with server-side
  ``statement_timeout`` and ``lock_timeout``;
* never the operational connection: when unavailable the caller gets an
  ordinary exception and the readout is skipped (fail closed);
* cancellation isolation: a query is never cancelled through asyncpg's
  shared cancel futures. On timeout or caller cancellation the connection is
  terminated and discarded, so it can never be left poisoned;
* bounded concurrency (semaphore with a bounded wait) and independent,
  rate-limited reconnection;
* SELECT-only; ``_exec`` is rejected.

It grants no trading authority and never writes. OFF by default.
"""
from __future__ import annotations

import asyncio
import math
import os
import time
from collections import deque

FLAG = "OOS_RESEARCH_READER_V2"
DSN_ENV = "OOS_DIAGNOSTIC_DATABASE_URL"
APPLICATION_NAME = "nexus_oos_research_v2"

STATEMENT_TIMEOUT_MS = 2500
LOCK_TIMEOUT_MS = 1000
QUERY_TIMEOUT_S = 3.0
CONNECT_TIMEOUT_S = 3.0
ACQUIRE_TIMEOUT_S = 1.0
MAX_CONCURRENCY = 1
RECONNECT_MIN_INTERVAL_S = 5.0
SUMMARY_EVERY_S = 300.0
_SAMPLES = 512


def enabled() -> bool:
    return os.environ.get(FLAG, "false").strip().lower() in {"1", "true", "yes", "on"}


class ResearchReaderError(RuntimeError):
    """Research read failed; the operational database was not touched."""


class ResearchReaderUnavailable(ResearchReaderError):
    """No dedicated research connection (missing DSN, backoff or connect error)."""


class ResearchReaderBusy(ResearchReaderError):
    """Concurrency limit reached and the bounded wait expired."""


class ResearchReaderTimeout(ResearchReaderError):
    """Query exceeded the reader's own budget; its connection was discarded."""


class ResearchReaderRejected(ResearchReaderError):
    """Statement is not a read-only SELECT."""


def _percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def _consume(task):
    # Retrieve the outcome of an abandoned fetch so asyncio never warns.
    if not task.cancelled():
        task.exception()


class ResearchReader:
    """Duck-types the read-only part of ``bot.database`` for research snapshots."""

    _is_pg = True

    def __init__(self, *, dsn=None, connect=None, max_concurrency=MAX_CONCURRENCY,
                 query_timeout_s=QUERY_TIMEOUT_S, acquire_timeout_s=ACQUIRE_TIMEOUT_S,
                 connect_timeout_s=CONNECT_TIMEOUT_S,
                 reconnect_min_interval_s=RECONNECT_MIN_INTERVAL_S,
                 statement_timeout_ms=STATEMENT_TIMEOUT_MS,
                 lock_timeout_ms=LOCK_TIMEOUT_MS, clock=time.monotonic):
        self._dsn = dsn
        self._connect_fn = connect
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))
        self.query_timeout_s = float(query_timeout_s)
        self.acquire_timeout_s = float(acquire_timeout_s)
        self.connect_timeout_s = float(connect_timeout_s)
        self.reconnect_min_interval_s = float(reconnect_min_interval_s)
        self.statement_timeout_ms = int(statement_timeout_ms)
        self.lock_timeout_ms = int(lock_timeout_ms)
        self._clock = clock
        self._conn = None
        self._connect_lock = asyncio.Lock()
        self._last_connect_failure = float("-inf")
        self._wait_ms = deque(maxlen=_SAMPLES)
        self._fetch_ms = deque(maxlen=_SAMPLES)
        self._last_summary = None
        self.stats = {
            "queries": 0, "ok": 0, "errors": 0, "timeouts": 0, "busy": 0,
            "unavailable": 0, "rejected": 0, "discards": 0, "connects": 0,
            "connect_failures": 0,
        }

    # ── connection lifecycle ───────────────────────────────────────────
    def _resolve_dsn(self):
        dsn = self._dsn if self._dsn is not None else os.environ.get(DSN_ENV, "")
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ResearchReaderUnavailable("OOS_RESEARCH_DSN_MISSING")
        return dsn

    async def _open(self, dsn):
        if self._connect_fn is not None:
            return await self._connect_fn(dsn)
        import asyncpg
        return await asyncpg.connect(
            dsn,
            timeout=self.connect_timeout_s,
            server_settings={
                "default_transaction_read_only": "on",
                "statement_timeout": str(self.statement_timeout_ms),
                "lock_timeout": str(self.lock_timeout_ms),
                "idle_in_transaction_session_timeout": "5000",
                "application_name": APPLICATION_NAME,
            },
        )

    def _connected(self):
        conn = self._conn
        if conn is None:
            return False
        try:
            return conn.is_closed() is False
        except Exception:
            return False

    async def _ensure_connection(self):
        if self._connected():
            return self._conn
        async with self._connect_lock:
            if self._connected():
                return self._conn
            if self._clock() - self._last_connect_failure < self.reconnect_min_interval_s:
                raise ResearchReaderUnavailable("OOS_RESEARCH_RECONNECT_BACKOFF")
            dsn = self._resolve_dsn()
            try:
                conn = await asyncio.wait_for(
                    self._open(dsn), timeout=self.connect_timeout_s + 0.5
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_connect_failure = self._clock()
                self.stats["connect_failures"] += 1
                raise ResearchReaderUnavailable(
                    f"OOS_RESEARCH_CONNECT_FAILED:{type(exc).__name__}"
                ) from None
            self._conn = conn
            self.stats["connects"] += 1
            return conn

    def _discard(self):
        """Terminate synchronously: never await a possibly poisoned connection."""
        conn, self._conn = self._conn, None
        if conn is None:
            return
        self.stats["discards"] += 1
        terminate = getattr(conn, "terminate", None)
        if callable(terminate):
            try:
                terminate()
            except Exception:
                pass

    async def close(self):
        self._discard()

    # ── query path ─────────────────────────────────────────────────────
    async def _fetchall(self, sql: str, params: tuple = (), *, strict: bool = False):
        self.stats["queries"] += 1
        if not str(sql).lstrip().upper().startswith(("SELECT ", "WITH ")):
            self.stats["rejected"] += 1
            raise ResearchReaderRejected("OOS_RESEARCH_SELECT_ONLY")
        enter = time.perf_counter()
        try:
            await asyncio.wait_for(self._semaphore.acquire(), self.acquire_timeout_s)
        except asyncio.TimeoutError:
            self.stats["busy"] += 1
            raise ResearchReaderBusy("OOS_RESEARCH_CONCURRENCY_LIMIT") from None
        acquired = time.perf_counter()
        self._wait_ms.append((acquired - enter) * 1000.0)
        try:
            try:
                conn = await self._ensure_connection()
            except ResearchReaderUnavailable:
                self.stats["unavailable"] += 1
                raise
            from bot.database import _pg_sql
            fetch = asyncio.ensure_future(conn.fetch(_pg_sql(sql), *params))
            fetch.add_done_callback(_consume)
            try:
                done, _ = await asyncio.wait({fetch}, timeout=self.query_timeout_s)
            except asyncio.CancelledError:
                # Caller cancelled (e.g. its own wait_for). The fetch is not
                # cancelled through asyncpg; the connection is dropped instead.
                self._discard()
                from bot.db_cancel_safety import is_spurious_cancel
                if is_spurious_cancel():
                    self.stats["errors"] += 1
                    raise ResearchReaderError("OOS_RESEARCH_SPURIOUS_CANCEL") from None
                raise
            if not done:
                self._discard()
                self.stats["timeouts"] += 1
                raise ResearchReaderTimeout("OOS_RESEARCH_QUERY_TIMEOUT")
            if fetch.cancelled():
                self._discard()
                self.stats["errors"] += 1
                raise ResearchReaderError("OOS_RESEARCH_FETCH_CANCELLED")
            exc = fetch.exception()
            if exc is not None:
                self._discard()
                self.stats["errors"] += 1
                raise ResearchReaderError(
                    f"OOS_RESEARCH_FETCH_FAILED:{type(exc).__name__}"
                ) from exc
            rows = fetch.result()
            self.stats["ok"] += 1
            self._fetch_ms.append((time.perf_counter() - acquired) * 1000.0)
            return rows
        finally:
            self._semaphore.release()
            self._maybe_log_summary()

    async def _fetchone(self, sql: str, params: tuple = (), *, strict: bool = False):
        rows = await self._fetchall(sql, params, strict=strict)
        return rows[0] if rows else None

    async def _exec(self, *args, **kwargs):
        self.stats["rejected"] += 1
        raise ResearchReaderRejected("OOS_RESEARCH_READ_ONLY")

    # ── observability ──────────────────────────────────────────────────
    def latency_summary(self):
        def pct(values, p):
            v = _percentile(list(values), p)
            return None if v is None else round(v, 3)
        return {
            **self.stats,
            "wait_ms_p50": pct(self._wait_ms, 50), "wait_ms_p95": pct(self._wait_ms, 95),
            "wait_ms_p99": pct(self._wait_ms, 99),
            "fetch_ms_p50": pct(self._fetch_ms, 50), "fetch_ms_p95": pct(self._fetch_ms, 95),
            "fetch_ms_p99": pct(self._fetch_ms, 99),
        }

    def _maybe_log_summary(self):
        now = self._clock()
        if self._last_summary is not None and now - self._last_summary < SUMMARY_EVERY_S:
            return
        self._last_summary = now
        try:
            from bot.logger import log
            fields = " ".join(f"{k}={v}" for k, v in self.latency_summary().items())
            log.info(
                "[OOS_RESEARCH_READER_V2] %s operational_connection_used=false "
                "research_only=true decision_effect=NONE execution_effect=NONE", fields,
            )
        except Exception:
            pass


_reader: ResearchReader | None = None


def reader() -> ResearchReader:
    """Process-wide research reader (created lazily, connects on first query)."""
    global _reader
    if _reader is None:
        _reader = ResearchReader()
    return _reader


def route(db):
    """Return the dedicated reader when enabled; otherwise the given handle."""
    return reader() if enabled() else db


async def shutdown():
    global _reader
    current, _reader = _reader, None
    if current is not None:
        await current.close()


__all__ = [
    "FLAG", "DSN_ENV", "ResearchReader", "ResearchReaderBusy", "ResearchReaderError",
    "ResearchReaderRejected", "ResearchReaderTimeout", "ResearchReaderUnavailable",
    "enabled", "reader", "route", "shutdown",
]
