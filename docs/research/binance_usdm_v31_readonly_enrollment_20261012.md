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
