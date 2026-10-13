# Capital-independent market observation V1 — prereg #666 (NO LIVE)

## Frozen source of authority

- Primary prereg: https://github.com/007andra/nexus7-bot/issues/666
- Immutable GitHub issue creation time: **2026-10-13T13:27:05Z**.
- Cohort ID: CAPITAL_FREE_SHORT_DOWN_BOS_MARKET_OOS_V1
- Production frozen config reference: 45ebd59d4ca54072e1233ce0667085d92b70ab1d.
- Original canonical #609: **unchanged**, https://github.com/007andra/nexus7-bot/issues/609.
- Original #610 implementation: **unchanged**; it is NOT a branch dependency.
- New code here is independently reviewable on a separate, non-LIVE branch.

## Implemented right now

1. bot/capital_free_market_oos_666_v1.py is a pure standard-library-only
   deterministic envelope/filter/enrollment-shape and exact-time market
   return verifier. It has no trading engine, account, API, DB, env or time
   imports; there is no live call site or schedule.
2. tests/test_capital_free_market_oos_666_v1.py includes synthetic-only
   regressions for t0, source completeness, identity, 25-symbol universe,
   first-60 selection, 12-per-symbol quota, duplicate conflict, 60m/240m
   exact timestamp, censoring, and non-LIVE authority invariants.
3. Every result is OFFLINE_CONTRACT_UNATTESTED. Not even 60 correctly shaped
   fixtures can set source_attested or cohort_membership_proven. The returned
   fractional market direction is **NOT executable NET, fills or profit**.

## Required before *any* real enrollment, all pending

- Approve the exact research implementation SHA via Quality Check,
  Supply Chain Security and independent human review.
- Before collecting future observations, record **effective_t0** as the
  maximum of issue creation, passing implementation CI completion and first
  separately authorized session start; freeze it in an immutable audit record.
  This code does NOT select that cutoff on its own.
- Demonstrate a source-approved rights-compliant durable private market data
  route, with real verified contiguous closed 15m/1h/4h as-of capture-time
  candles and 60m/240m outcome coverage. REST HTTP 451 seen in issue #609
  means that endpoint is NOT validated on the hosted runner. Public
  WebSocket aggTrades alone are insufficient historical kline evidence.
- Validate reproducible *balance-independent signal generation* from a
  code-pinned, non-mutating version of Analyzer.analyze_mtf. Current module
  only validates candidate envelopes provided by a caller; there is **no**
  running collector or installed research scanner. No new production signals
  were produced by this work.
- Add immutable source authentication and append-only cohort membership
  manifest, independent outcome collection and frozen cluster-bootstrap
  statistics (10,000 replicates, seed 609202613, by symbol/UTC 4h bucket)
  and leave-one-symbol-out analysis as specified by #666. Do not allow
  acceptance gates to fire before those are reviewed and tested.
- Never reuse pre-t0 signals, R3 observations, #609 candidates or synthetic
  regression fixtures to increase the first-60 sample size.

## Explicit non-effects

No GitHub Actions market-collection workflow or tag, no new API key,
no service deployment, no Live/trading/risk/stop/TP/sizing/rule changes,
no capital top-up, no Backblaze bucket, no fee assumption, no risk/HWM
rewrite, no market tape published and **no real prospective candidates
enrolled**. Do not merge the draft PR without manual review and exact-SHA
green CI; never interpret an offline PASS as economic or LIVE readiness.

Rollback (research branch only): close the draft PR and delete its branch
after preserving the prereg issue and commit SHAs for audit. Production
branch and Railway remain unchanged.
