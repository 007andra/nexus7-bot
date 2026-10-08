# #596 — Prospective OOS snapshot latency attribution (research only)

## Problem

Production's `_maybe_emit_prospective_oos_cohort` wraps
`oos.snapshot(db)` in a fixed **3.0-second** `asyncio.wait_for`.
A timeout may occur before the read-only research snapshot finishes. The
existing `_fetchall` shares a DB I/O lock with unrelated consumers. Until
latency is measured, the cause cannot be attributed to DB network round trip,
lock contention, JSON parsing, or metadata setup. Timed-out research remains
non-authoritative.

## What the instrumentation does

- Scope: **only** `PROSPECTIVE_OOS_COHORT_V1` during its report snapshot.
  Other database readers/writers see a default `ContextVar=None`.
- Emits one `[PROSPECTIVE_OOS_SNAPSHOT_LATENCY_V1]` record per attempted
  research snapshot, including successful, timed-out and errored attempts.
- Records `elapsed_ms` (wall-clock), `metadata_ms`, `candidates_ms`,
  `outcomes_ms`, `compute_ms`, exact wait duration at the shared DB I/O
  lock, time spent within the DB fetch after acquiring the lock, total fetch
  calls, counts of rows returned, fetch cancellations and whether lock was
  never acquired.
- `stage` means the last/cancelled logical stage, not causal proof.
  The `metadata` stage can contain schema/metadata initialization. Only
  measured `db_fetch_ms` covers actual `_fetchall` query scope (client
  fetch including network/deserialization); it does **not** isolate server
  execution time from transport and should never be labeled query planner time.
- Never emits SQL text, row contents, symbol, candidate ID, balance,
  credentials, connection string or private account data.

## Fixed authority boundaries

- `asyncio.wait_for(..., timeout=3.0)` remains unchanged.
- No changes to SQL, payload filters, transaction modes, connection settings,
  account state, scanner policy, signal selection, strategy, leverage, HWM,
  drawdown, stop, trade dispatch, Binance endpoints or LIVE gating.
- The existing error telemetry `[PROSPECTIVE_OOS_COHORT_V1] status=ERROR`
  still fires on timeout, with `promotion_allowed=false` and
  `live_allowed=false`. A timed-out research snapshot still returns `None`.
- This implementation does **not** cure timeouts. It makes each stage
  measurable so root cause can be established from repeat observations.

## Evidence process after a separately reviewed release

Observe a meaningful series of attempts, including successful cycles and
any timeout, and analyze empirical distributions. Stratify by stage and
compare `lock_wait_ms` to `db_fetch_ms` and `compute_ms` before proposing
an index, narrower SELECT, connection placement or any timeout adjustment.
Investigate the active I/O lock and DB fetch separately. Avoid making
claims of cause from a single candle-time coincidence.

## Test coverage

`python -m unittest -v tests.test_oos_snapshot_timing_596` verifies
redacted fields, no-op default `ContextVar`, lock-held vs DB-fetch
timeouts, cancellation, unchanged 3.0-second budget, error isolation and
same report return on success. Source is kept on independent PR/branch for
#596, not mixed with OOS dataset/export PR #595.

No production Railway credential is required for these tests.
