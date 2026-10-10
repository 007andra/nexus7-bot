# OOS #596 — shared DB lock holder attribution (read-only diagnostic)

Last incident validation: Railway deployment `6b25dc0b-86de-4097-939e-6a036ddf6eac`, SHA `56605b9c...`.
- 2026-10-08 22:29:13 UTC: OOS snapshot OK, elapsed=2811.516ms / 3000ms and *lock wait* =1709.265ms across metadata, candidates and outcomes. Query fetch summed 860.061ms. Lock holder was **not** observable in this prior version.
- 2026-10-08 22:34:21 UTC: OOS snapshot TIMEOUT during outcomes at 3016.165ms. Lock wait=140.152ms; database `fetch` wall-clock total=2645.438ms, metadata `fetch` alone=1683.975ms. Only 1753 rows returned; the cancelled outcomes call has no row count. Server processing vs network vs asyncpg decoding is unknown.
- 2026-10-08 22:39:30 UTC: OOS snapshot recovered, elapsed=1125.368ms; lock wait=0.022ms.

## This isolated PR changes

Feature flag `OOS_DB_LOCK_OWNER_TRACE_V1` defaults **false**. When false, the exact existing `asyncio.Lock` instance is returned to all DB callers, without a new SQL query, I/O lock wrapper or altered scheduling.

With explicit enablement, every acquisition of the **same** `bot.database._io_lock` uses a small context manager that records only one static operation category while holding the original lock. Supported paths are `_exec`, `_fetchone`, `_fetchall`, and `_serialized_io` decorators (notably durable key-value read/write and trade operations). It never records SQL text, parameters, user keys, API credentials, symbols or candidate IDs. The sampler's safe labels are hardcoded and sanitized against an allowlist.

Only a prospective OOS snapshot with its existing `ContextVar` probe records **owner-at-wait-start** when it attempts to acquire the DB lock. The existing OOS `[PROSPECTIVE_OOS_SNAPSHOT_LATENCY_V1]` line gains:
- `max_wait_owner_at_start` — allowlisted operation class associated with the *largest* individual OOS wait above 100ms
- `max_wait_owner_ms`
- `owner_attributed_wait_events`, `owner_unattributed_wait_events`
- `lock_owner_snapshot_not_causal=true`

**Attribution caveat:** This is a single instantaneous sample at the beginning of the wait, not a lock ownership trace across the entire queue delay. A later holder may have taken the lock during the wait; `UNHELD_AT_START` / `UNKNOWN_AT_START` are honest outcomes. It is deliberately **not** a claim of causal root cause. Values under 100ms are not classified. Labels of other tasks never contain raw SQL or identifiers.

## Follow-on decision matrix

If a repeated spike shows `serialized:key_value_write`, inspect the exact durability transaction contention pattern and concurrency separately, with PostgreSQL cancellation tests; do not weaken HWM/account writes. If a spike shows `fetchall:shadow_outcomes`, inspect OOS/shadow read query plans, dataset cardinality and read cadence separately. If the wait is negligible yet `metadata_fetch_ms` dominates, distinguish PostgreSQL execution from transport/client using authorized DB-side `pg_stat_statements`/server telemetry, with safe permissions and no exported secrets. Do not assume a single cause on one sample.

No `timeout=3.0` change, SQL/query/retry change, risk/HWM change, strategy/order/trading action, V4 study enrollment change, LIVE permission or automatic promotion. The frozen OOS `EVIDENCE_FAIL` remains unchanged. Unit tests enforce redaction, true concurrent owner attribution, cancellation cleanup and default-disabled parity.

## Deployment boundary

Code is in an **unmerged Draft PR** pending exact-head GitHub CI and explicit operator review/approval. Merge and enabling `OOS_DB_LOCK_OWNER_TRACE_V1=true` are **separate actions**; do not auto-deploy or change the production flag without operator authorization. Do not increase the OOS timeout without measurements explaining both lock-wait and DB-fetch outliers.
