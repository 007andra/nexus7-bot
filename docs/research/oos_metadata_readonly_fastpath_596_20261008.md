# Issue #596 — OOS metadata DDL overhead and PostgreSQL latency: controlled experiment

## Verified facts (8 October 2026)

The production OOS pipeline calls `prospective_oos_cohort_v1.snapshot` under the existing 3-second `asyncio.wait_for`. It calls `ensure_cohort` every time. On an already initialized database `ensure_cohort` issues **one `CREATE TABLE IF NOT EXISTS`** through `db._exec`, followed by **one `SELECT payload ... WHERE cohort_id=?`** through `db._fetchall`; both use the shared `database._io_lock`. This is a concrete *extra round trip* on every healthy snapshot, not a proof of the previous 1683.975ms read spike's cause.

Exact Railway logs:
- 2026-10-08 22:34:21 UTC: snapshot TIMEOUT 3016.165ms, metadata 1964.342ms, `metadata_fetch_ms=1683.975`, total lock wait 140.152ms, `db_exec_ms=140.166`; outcome fetch cancelled.
- 2026-10-08 23:06:29 UTC on merged #604: snapshot OK 870.528ms, metadata 424.061ms, `metadata_fetch_ms=283.557`, `db_exec_ms=140.395`, total lock wait 0.120ms, owner=NOT_OBSERVED (none >=100ms).
- Railway Postgres service `7dc9b26e-d1ba-45f9-886c-6b3b7d18e09c` 1-hour **aggregate** CPU avg about 0.0076 / max about 0.0221 at verification. Aggregates cannot attribute milliseconds at incident timestamp. The Railway bot service had **0 exported application spans** and both Railway tracing toggles were OFF; thus no production server-specific query time, wire latency, fetch decode latency or PostgreSQL wait-event breakdown has been measured yet.

## Proposed one-change metadata experiment (OFF by default)

`OOS_METADATA_FROZEN_READONLY_V1=false` by default. On `true`, first read the **same immutable cohort row by the same parameterized SELECT**; if it exists, return the identical JSON without issuing redundant `CREATE TABLE IF NOT EXISTS`. If absent, invoke unchanged `ensure_cohort(db)`: original schema guard, conflict-safe insert and frozen baseline resolution. Bad JSON raises unchanged; cancellations propagate and are not retried. Existing candidate/outcome population filters, sequencing, time horizons, timeout=3.0, Risk Gate, HWM, LIVE and strategy status are untouched. This is not a PostgreSQL query optimizer or a promise to cure transient network delay.

Existing timing logger adds `metadata_read_mode=IMMUTABLE_READ_FIRST|LEGACY_DDL_GUARDED`. It already exposes `db_exec_ms`, `metadata_fetch_ms`, `lock_wait_ms` and `elapsed_ms`. No new DB diagnostic query, SQL payload or TCP proxy; no credentials required. The application lock-owner instrumentation from PR #604 is unaffected.

## Measurement/acceptance checklist BEFORE any change in execution policy

1. Tests must establish exact legacy-default path, read-only existing-row path with **zero DDL/writes**, missing-row guarded fallback, immutable membership/start time, cancellation, fail-closed malformed metadata, frozen strategy decision and zero LIVE authority.
2. Exact-head Quality Check and Supply Chain Security must both pass. Separately authorize merge/deploy and experimental flag activation. Do not modify a Railway variable during CI.
3. Under normal live-blocked shadow scan, evaluate comparable consecutive 5-min OOS snapshots in both modes: p50, p95, max metadata, DDL round trip, fetch, lock wait, timeouts, cache/candidate row volume. **Do not claim measured performance improvement just because code removes a round trip.** Natural variation and rolling deployments confound single-snapshot comparisons.
4. If transient `metadata_fetch_ms` remains high with negligible lock wait, arrange an authorized **read-only PostgreSQL** diagnostic to inspect `pg_stat_activity` wait events and, if already installed and permitted, aggregated `pg_stat_statements` timing **without SQL or parameters in logs**. Use low-impact bounded requests and never create extensions, run `EXPLAIN ANALYZE`, modify indices/schema, expose DB via TCP proxy or export secret credentials as part of this experiment.
5. Any future optimization or decision changing the OOS timeout requires independent evidence and approval; frozen OOS `EVIDENCE_FAIL` does not change when telemetry becomes faster.

This is an isolated research-only, default-disabled PR. **No LIVE trading unlock, no historical drawdown/HWM reset, no risk override, no change to positions.**
