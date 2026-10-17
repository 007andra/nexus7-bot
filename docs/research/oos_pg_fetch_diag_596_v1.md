# OOS #596 — PostgreSQL client fetch diagnostic (draft, default OFF)

Feature flag: `OOS_PG_FETCH_DIAG_V1=false` by default.

Scope: only `_fetchall` inside an active prospective OOS timing probe on PostgreSQL. When enabled, log a metadata event only if the measured `asyncpg.Connection.fetch` await is >=500 ms. Fields are client SQL placeholder conversion time, client driver-await time, application lock wait, connection closed at call start, cancellation and row count. Never log SQL, parameters, identifiers, payloads, credentials or raw exception messages.

**Interpretation:** `client_driver_await_ms` includes server execution, network and asyncpg processing; it is NOT PostgreSQL server execution time. It cannot separate those components. No extra DB requests or connection are introduced.

No change to SQL, timeout, cohort, risk, execution, LIVE, HWM, strategy, schema or credentials. Disabled behavior uses the unchanged fetch path. On error the existing fail behavior remains; cancellation propagates.

## Review checklist

- Run focused unit tests and existing OOS timing, lock-owner and database tests.
- Run exact-head Quality Check and Supply Chain Security.
- Verify disabled path equivalence and no sensitive values in logs.
- No merge/deploy or Railway flag changes without separate authorization.
- If later enabled by explicit authorization, compare matched snapshots at the same SHA and row bands; record p50/p95/max, timeout rate, lock waits and fetch times.
- PostgreSQL server execution time remains unmeasured; further correlation requires independent, approved instrumentation.
