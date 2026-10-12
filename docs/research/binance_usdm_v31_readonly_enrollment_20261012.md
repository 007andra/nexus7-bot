# Binance USD-M V3.1: bounded prospective roster adapter (RESEARCH ONLY)

**Protocol:** https://github.com/007andra/nexus7-bot/issues/658

**Frozen preregistration created_at:** 2026-10-12T19:39:52Z, UNIX 1791833992.0 seconds. This is a NEW population; historical V1/V2/V3 evidence or entries before the effective start MUST NOT count.

## Scope of this first implementation

The module `bot.short_down_bos_v31_readonly_enrollment` creates a **provisional, never promoted** candidate roster proposal from a single bounded read on the pre-existing `hard_gate_shadow_candidates_v1` table via a distinct read-only research session. It has:
- No automated wiring, scheduler, env feature enablement, Binance API client, exchange mutation, or live authority.
- No INSERT/UPDATE/DELETE/DDL, no shared trading DB fallback, and strictly one SELECT bounded to 1,000 eligible-or-other post-cutoff rows ordered by captured time and candidate ID.
- Exact top-level canonical NEXUS approval + SHORT/TRENDING_DOWN/BOS_BREAK + hard-gate shadow flags. Counterfactual approvals do not count.
- A cutoff strictly later than the GitHub issue creation and later than verified implementation commit and separately authorized collection-start times. The timestamp/SHA/reference input in the source must be externally attested; this module does NOT verify GitHub authorizations.
- Deterministic first 40 eligible futures signals, cap 10 per symbol; at least six symbols needed before any study decision. Equal-time candidate ID ordering. Old/duplicate rows excluded, conflicting ID or invalid original geometry causes fail-closed instead of cherry-picked replacement.
- The private in-memory roster may contain candidate IDs. **Only** `public_summary` fields can be written into public comments/logs; never log the raw dict if `private=True`.
- A truncated 1,000-row source returns `SCAN_TRUNCATED_NOT_PROVEN`, never readiness.

## Why the roster is not yet authoritative

This code is only a **single observational snapshot**, not an append-only signed/durable enrollment ledger. Without an authorized immutable manifest and a strictly prospective data acquisition service, late-arriving records could change a recomputed 'first 40' roster. Consequently `roster_durable=false`, `net_evidence_ready=false`, `promotion_allowed=false`, `live_allowed=false` on ALL outputs, even at 40/40. Production cannot auto-enroll candidates by importing this module.

This proposal does not evaluate executable fills or Binance fees, order size, fresh BBO, bid/ask, partial stops, funding and 60m/240m closed bars. These inputs are required by issue #658 to claim NET. It is **unsafe** to reuse `bot.nexus_oos_real_replay` KuCoin assumptions for a Binance USD-M experiment.

A later separately reviewed milestone may implement: (a) an append-only immutable research manifest with signed cutoff, (b) complete Binance USD-M market-data and fee provenance, (c) pessimistic deterministic 60m/240m paired fills and net returns and (d) 10k-replicate UTC-4h clustered bootstrap after 40 fully matured future entries. None is present or enabled here. An issue comment or CI green result does NOT start enrollment.

## Operator checklist before any runtime use

1. Obtain an independent quant/security code review, exact-SHA green CI, and authorization specifically for RESEARCH-only instrumentation.
2. Independently attest the commit SHA/timestamp and one unambiguous human-approved collection-start epoch; choose cutoff max(issue.created_at, approved implementation commit, capture start). Do not fabricate these values or use current time merely to force a PASS.
3. Use only authorized read-only diagnostic role and separate read connection, never production trading credentials.
4. Lock and persist immutable roster state before new observations; do not re-sort/replace past enrolled records after outcomes become known.
5. Gather contemporaneous Binance USD-M execution evidence and both closed forward horizons, and evaluate NET under #658. All missing evidence = NOT_PROVEN.
6. Separate the final LIVE decision via release board #620; no change to 30% drawdown limit, HWM, blocked entries, orders, deposits or leverage.

## Regression checks

`python -m unittest tests.test_short_down_bos_v31_readonly_enrollment -v`

Full CI (Quality Check and Supply Chain Security) is required. Zero real trading actions are in the test suite.


## Milestone 2 — deterministic append-only hash-chain *proposal*

`bot.short_down_bos_v31_manifest` adds a **pure, offline** manifest state transition to the same draft PR. It is NOT connected to runtime, PostgreSQL, filesystems, S3, Binance or live order execution. Every chronological natural approval becomes an immutable dataclass event with SHA256 hash of the full original entry/SL/TP geometry, symbol, timestamp, candidate ID, disposition, sequence and previous digest. A duplicate ID with changed geometry, backward timestamp, conflicting sequence, omission, altered event, or changed preapproved cutoff fails closed.

An over-cap symbol creates a `SKIPPED_SYMBOL_CAP` event, not an invisible replacement. Entries after the first forty are `SKIPPED_AFTER_40`; outcome quality is never an input. `verify_manifest` recomputes the entire chain and independently checks per-symbol caps, chronological order, monotonic sequence, original geometry and allowed decisions. A separately supplied trusted `head_digest` can detect rewrites relative to a **genuinely protected external anchor**.

**IMPORTANT security boundary:** A SHA256 chain by itself does NOT stop a privileged writer from replacing all events and recalculating digests. The module neither signs events nor provides durable append-only storage, independent notarization or a trusted history. All output continues to expose `roster_durable=false`, `net_evidence_ready=false`, `promotion_allowed=false` and `live_allowed=false`. The runtime cannot truthfully claim that the 40 have been enrolled. A future separately reviewed storage adapter must guarantee atomic compare-and-append / UNIQUE candidate ID, append-only immutable rows, source-verifiable cutoff, protected external head anchors and crash-safe resume, under a research-only database role, with no trading DB write privileges.

No production deployment or flag enabling is authorized by CI alone. No protocol #658 changes, no account access, no new capital transaction or LIVE order.

New standalone suite: `python -m unittest tests.test_short_down_bos_v31_manifest -v`.


## Milestone 3 — separated PostgreSQL transactional adapter, OFFLINE ONLY

Files:
- `bot/short_down_bos_v31_postgres_adapter.py`: caller-supplied **isolated research** PostgreSQL connection, required `nexus_v31_writer` role and dedicated database name `nexus7_research_*`, witnessed previous-chain SHA-256 digest, `SERIALIZABLE` transaction, `SELECT ... FOR UPDATE` head lock, full on-read chain verification, append event `INSERT` plus compare-and-swap head `UPDATE` in the **same transaction**; no automatic database provisioning or credential reading.
- `docs/research/sql/v31_isolated_manifest.sql`: **DBA-only proposal**, not an executed migration. Existing events protected by privilege denial and a rejection trigger, and header updates constrained to one supported digest step. No real schema or extra Railway service was created.
- `tests/test_short_down_bos_v31_postgres_adapter.py`: offline fake DB models transaction rollback, stale external witness, idempotent replay, collision, tampered rows, role/db mismatch, missing genesis, CAS race, and reader-only verification. These are simulated tests; real PostgreSQL ownership/DDL + concurrent-transaction integration must pass under an isolated CI DB before production consideration.

**Hard release boundary:** This adapter has **NO autonomous external anchor or cryptographic-signature verification**. `PriorHeadWitness` is merely a caller-provided digest and reference, and must be authenticated **by an independent service** outside the mutable database. A compromised caller with a forged witness can defeat this contract. The new digest returned after committing must be independently anchored before the *next* append. The observed chain is not externally notarized in current production. This library emits `roster_durable=false` on every path: no authorization for retrospective promotion, 40/40 results, funds, orders, risk/HWM or live trading.

The research writer is not authorized to connect to or create the NEXUS production trading database, change production environment variables, create resources with cost, or run the schema proposal. Actual provisioned isolated DB, trusted external signer, immutable/durable manifest, ordering source and independent human review must be separately authorized and verified before any prospective collection starts. A green CI proves code checks, not real PostgreSQL durability.

### Additional true PostgreSQL proof (disposable CI only)

`tests/test_short_down_bos_v31_postgres_real_disposable.py` runs **only** when `GITHUB_ACTIONS=true` and `TEST_POSTGRES_DSN` matches the exact isolated GitHub Quality Check PostgreSQL fixture `postgresql://proof:postgres@127.0.0.1:5432/nexus_release_proof`. It creates a unique ephemeral `nexus7_research_ci_*` database, provisions the non-login `nexus_v31_writer` role inside this disposable fixture, executes the unshipped DDL there, verifies a committed append, stale-witness refusal, event UPDATE denial, header mutation denial, and concurrent SERIALIZABLE writer race (one winner). The test drops its disposable database after completion. This **does not touch Railway** or prove an independent external signing service exists.

## NET evaluator (`bot/short_down_bos_v31_net_evaluator.py`)

Pure implementation of the #658 execution contract and frozen statistics, over caller-supplied captured evidence. It does not collect anything: no Binance, database, clock or scheduler.

- Per candidate, ONE shared closed-bar path drives both horizons, so pre-60m stops/TPs/partials are identical by construction. The result is `PROVEN` only if both horizons are proven.
- Decimal only; floats are refused for exchange evidence. Candidate geometry from captured JSON uses its shortest repr. Adverse tick/step rounding.
- Fee: `BINANCE_USDM_COMMISSION_RATE` taker rate observed at or before entry, applied on each leg's own notional. Filters (tick/step/minNotional) as-of entry.
- Entry sells at a fresh bid (|Δt| ≤ 2 s) minus the declared `DECLARED_STRESS_FLOOR` slippage. A time exit buys at a fresh ask exactly at entry+60m/240m plus the floor. Stops fill at max(stop, gap open) plus the floor. LIMIT TPs need trade-through, and an exact touch is `INDETERMINATE`.
- The protection plan must equal the frozen candidate stop and final target. Partial TPs may move the stop from the next bar only. Trailing is `NOT_PROVEN` (`TRAILING_NOT_MODELED`) until a reviewed model exists.
- `INDETERMINATE` covers: stop+TP in one bar, a TP inside the entry bar or a horizon-straddling bar, a stop inside a horizon-straddling bar, and a funding settlement inside a fill window.
- Funding is taken only from Regular/Special settlement events with a complete coverage proof of the exposure interval. A SHORT receives positive rates.
- Cohort: duplicate IDs fail closed. Rows are never dropped or replaced. Any `INDETERMINATE`/`NOT_PROVEN` row makes the cohort `NOT_PROVEN`. PASS criteria are exactly #658's: mean diff > 0.001; lower 2.5th percentile (nearest rank 250/10,000, seed 59031012, UTC 4h clusters, ≥ 10 clusters) > 0; mean net60 > 0; share net60 > 0 ≥ 0.60; every LOSO > 0; ≥ 6 symbols; ≤ 10 per symbol. A PASS is only `EVIDENCE_READY_FOR_MANUAL_REVIEW`. `live_allowed=false` and `promotion_allowed=false` always.

Still missing before any real V3.1 number exists: an authenticated external anchor, a reviewed and authorized research-only collector that captures the above evidence prospectively (commissionRate, bookTicker at entry and both horizons, closed bars, funding with coverage), and 40 real post-cutoff enrollments. Valid V3.1 sample: **0/40**.
