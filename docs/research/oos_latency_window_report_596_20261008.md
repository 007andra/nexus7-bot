# Issue #596 — OOS metadata performance window (read-only evidence)

## Ground truth — 2026-10-08 ~23:30 UTC

Production Railway deployment `98ee3871-9440-4a89-8b58-daceffc515ef` on SHA `87488e9070676f1142ebeb677af0a6c28ace9006` is healthy and has `OOS_METADATA_FROZEN_READONLY_V1`, OOS lock owner trace and V4 shadow enabled. Exact deployment logs yielded **one post-activation OOS snapshot**. Six to twelve confirmed observations do not yet exist. Do not invent more samples or call #596 fully resolved.

- Read-first at 23:27:44 UTC: OK elapsed=807.196ms; metadata=309.610ms; metadata_fetch=309.526ms; db_exec=0ms; exec_calls=0; lock_wait=0.111ms; rows_fetched=5210; mode=IMMUTABLE_READ_FIRST; timeout budget 3000ms.
- Legacy at 23:21:01 UTC, previous Railway deployment `5f575fd6` and **same SHA but flag default OFF**: OK elapsed=1676.500ms; metadata=451.838ms; db_exec=150.357ms; metadata_fetch=301.376ms; outcomes lock wait=753.842ms; rows=5210; mode=LEGACY_DDL_GUARDED.
- Prior incident 22:34:21 UTC, older code: TIMEOUT after 3016.165ms, metadata_fetch=1683.975ms. The cause of this fetch spike is still unknown.

The whole snapshot before/after elapsed-time difference is confounded by highly different lock wait; one matched row-count observation does NOT establish causal total-time speedup.

## Run the offline report

```bash
python -m bot.oos_snapshot_window_report_v1 --optimized optimized.log --baseline legacy.log
```

Each input is an existing *redacted* Railway runtime text-log export, containing complete `[PROSPECTIVE_OOS_SNAPSHOT_LATENCY_V1]` lines from **one known deployment**. Never commit original logs/credentials to GitHub. Only publish the resulting bounded, aggregate JSON.

The parser enforces exact metadata mode per file, 3000ms timeout, unique timestamps, finite nonnegative measurements and required metric fields; it explicitly counts TIMEOUT/ERROR/CANCELLED as attempted snapshots. The newest twelve per file are considered, with an 8-minute maximum cadence gap. It reports median/max and a nearest-rank p95 only with at least six consecutive snapshots. **For n=6–12, nearest-rank p95 is identical to the max and is a sparse tail estimate.**

The output is always `research_only=true`, `live_allowed=false`, `promotion_allowed=false`, `causal_speedup_proven=false`; no automated comparison can release trading. `INSUFFICIENT_OR_NONCONSECUTIVE` is an evidence shortfall, not a failure of the bot. The report never outputs SQL, symbols, candidate IDs, raw payloads, secrets or freeform owner values.

## Gates for the next technical decision

1. Gather **natural** six-to-twelve consecutive post-activation OOS snapshots from the same SHA/deployment, without changing scan cadence; record any missing logs/drop notices.
2. Where available, obtain comparable six-to-twelve consecutive legacy snapshots from a single same-SHA previous deployment. Without that, analyze optimized performance descriptively only.
3. Check medians, p95=max, maximum, timeout rate, metadata fetch, DB DDL exec, total lock wait and outcomes lock wait, row-count trends and adverse cancellation counts.
4. If large lock waits persist with UNHELD_AT_START, investigate queue fairness and owner transitions separately. Owner at wait start is not proof of whole-wait causality.
5. If metadata fetch is still elevated despite near-zero lock wait, request separately authorized read-only PostgreSQL server wait/statement diagnostics. Do not expose PostgreSQL through TCP proxy, install extensions, change schema or download credentials.
6. The 3s OOS timeout, historical HWM/drawdown, freeze of OOS EVIDENCE_FAIL, V4 preregistration and LIVE Risk Gate remain untouched. Latency performance cannot by itself justify opening real orders.

**Scope:** offline research helper plus tests, no runtime hook, deployment, Railway configuration or risk mutation.