# #596 — opt-in shared-lock possession and OOS event-loop scheduling diagnostics

**Scope:** diagnostic-only change on branch audit/oos-lock-hold-loop-lag-596-v1. It MUST remain OFF in production unless separately authorized following CI and risk review. Baseline is 9217bf82052065969ea02b7659af760593fef3d3, Railway bot in AMS, PostgreSQL and durable volume in SFO. This is **not** permission to relocate, change risk limits, alter frozen OOS evidence or enable LIVE trading.

## Why two signals

The current OOS snapshot has three sequential reads under the module-level shared DB lock. A slow asyncpg fetch occupies the critical section, but the existing max_wait_owner_at_start is a noncausal single snapshot. The 2026-10-09 00:25:04 UTC failure had 1507.471ms metadata fetch with almost no metadata lock wait; the outcomes stage was cancelled while waiting 668.584ms for lock acquisition. At 00:30:11 UTC an OK snapshot had 1071.018ms total lock wait, mostly candidates. These demand separate observation of **lock-holder durations** and **event-loop timer scheduling lag**, not an assumption that the server or intercontinental network alone is responsible.

## Control plane (default OFF)

* OOS_DB_LOCK_HOLD_DIAGNOSTIC_V1=false: if explicitly TRUE on a future reviewed deployment, attach SlowHoldReporter to the already-existing LockHolderTracker. The existing _io_lock stays a single asyncio.Lock; the existing asyncpg connection stays a single connection. Successful/failed/cancelled DB operations follow the same path, including strict PersistenceError semantics. Only acquisitions with **holder duration >= 125ms** may emit an allowlisted holder class, observed held and pre-acquisition wait in milliseconds, and cancellation flag, **after lock release**. At most **eight** such log lines per 60s per process. It never prints SQL, query bindings, account balances, symbols, credentials, candidate IDs, key strings or exact values. No added SQL or durable state.
* OOS_EVENT_LOOP_LAG_DIAGNOSTIC_V1=false: if explicitly TRUE on a future reviewed deployment, add a small asyncio.call_later(50ms) callback series **only during the existing 3-second OOS snapshot**. It records maximum timer scheduling drift, count of timer ticks with >=100ms lag and sample count, stopping/cancelling the callback in the snapshot finally clause. No task, thread, DB call, exchange call, OOS change, new network request or persisted state. A timer lag is NOT definitive causal attribution of database, network or lock wait.

If the flags are absent/false, the default connection/lock paths and OOS 3-second deadline are unchanged. Production variables were **not changed** by the PR.

## Evidence interpretation

* A slow-hold record for a class like serialized:key_value_write, fetchall:shadow_outcomes or fetchall:shadow_candidates indicates that this category actually held the shared lock for the reported duration, but it does NOT by itself prove which later waiter it delayed. Compare timestamps with OOS per-stage lock waits; for definitive waiter-holder pairing, a separate reviewed ID-free acquisition timeline would be required.
* A high timer scheduling drift supports event-loop scheduling jitter as one possible contributor. A low observed drift does not rule out PostgreSQL or network waiting because async I/O yields control to the loop.
* No slow-hold event may mean no possession exceeded 125ms, instrument disabled, or events beyond the bounded rate cap. It never proves no contention.
* The standalone server-side #607 PostgreSQL pg_stat_activity/pg_stat_statements probe remains a separate needed experiment; it was not executed by this PR. No SSH/SQL functionality exists in the connected Railway tools.

## Required validation / hold points

1. CI: run tests/test_oos_async_contention_observability_v1.py and the existing #596 lock-owner, OOS timeout, metadata fast path, persistence/CAS, safety tests; verify no live/threshold/OOS change. The new unit tests exercise opt-in/opt-out, secret redaction, rate cap, slow acquisitions, cancellation before/after lock, callback exceptions, the DB wrapper, deterministic scheduler lag and OOS timeout cleanup.
2. Verify current production commit and deployed SHA match before even considering a merge; review PR diff and release policy. A merge would trigger a deployment depending on Railway settings; **do not merge or deploy this PR as part of the investigation**.
3. If separately authorized in the future, enable ONLY the diagnostic flag(s) for one controlled production observation window, leaving risk, HWM, cohort, timeout, Binance connectivity and region unchanged. Use short defined rollout and rollback plan. Disable and verify flags after capture; compare normal OOS natural snapshots without manipulating the sample/cadence.

**Never** increase OOS timeout, change Postgres region, bypass Binance HTTP 451, overwrite HWM/drawdown, loosen the loss gate, alter frozen cohort results or place LIVE orders to collect diagnostics.
