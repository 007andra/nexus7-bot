# RCA #594 — Same-candidate BBO-versus-static cost evidence (NO LIVE)

Base production: `8854d07a61219d6fae7288f683b627554055101b`, Railway bot `51fcf0a9-111a-4674-ba83-6841cdc206e7`. Research draft, no new API key, exchange calls, orders, DB query, storage, scheduler or risk changes.

## Decision-driving contradiction

The frozen MIN_ORDER counterfactual R3 (493 matched hypothetical candidates, 98 approved/395 rejected) shows **negative after-snapshot-cost mean returns at both horizons for every approved setup**. BOS_BREAK approved n=41, gross 60m +0.2842%, candidate snapshot proxy 60m **−0.0187%** and 240m **−0.7909%**. R3 `cost_snapshot.spread_bps` is absent in all 493 records, so those proxies are not independently verified execution net.

Separately, the existing Binance public-WS BBO V3 cost-only sample contains 2,540 valid records (25 symbols), static average 30.607bps vs BBO-modeled mean 16.937bps (2026-10-13 01:03:50Z runtime). **The 2,540-record BBO sample is a different population; applying its mean to 41 R3 BOS candidates would manufacture an edge.** Its fee/impact and quote assumptions are not actual fills or stop/TP executions.

## Specific newly implemented evidence test

This research-only PR adds `bot/rca594_matched_bbo_v1.py`, and minimally integrates it into the already periodic in-memory `counterfactual_approval_failure_analysis_v1` report:
- Check the **R3 candidate's own persisted** `bbo_cost_only_observation` for exact candidate_id, symbol, side, setup, regime, COST_ONLY cohort and shadow-only/no-LIVE flags. It must include a valid BBO with quote generation and update ID, age <=2,000ms, positive bid/ask, a recomputable nonnegative spread and nonnegative BBO-modeled total cost >= the observable spread. Missing, redacted, inconsistent or stale quotes = **unmatched**, never assumed to cost zero.
- Preserve the complete same-R3 observed-outcome denominators for each setup, 60m/240m, approved/rejected arms. For each matched quote, count `bbo_n`. Only if the same candidate ALSO contains its original valid static fee/slippage cost snapshot, compare both **on the exact same observations**, using `paired_n`; no mean difference between unrelated cohorts or unequal subsets.
- The existing `COUNTERFACTUAL_APPROVAL_FAILURE_WORST_GROUPS_{60,240}M_V1` log adds fixed-vocabulary `matched_bbo=` group counts and covered-only modeled means `bm` and original static snapshot modeled means `sm`, or NA when missing. No candidate IDs or untrusted labels are emitted. Missing or inconsistent group counts = `matched_bbo=NOT_PROVEN`.
- No executable-net claim even if paired BBO model means are positive. Missing actual taker-tier, quote depth, funding, protective fill ordering, SL/TP, stop/market trigger, price impact, latency at 60/240m exits, external source authentication, cross-sample dependency and independent prospective cohort prevent promotion. Model assumptions are explicitly reported.

**Gate:** If the number of eligible same-candidate R3 BBO observations is zero, explicitly report `bn=0`/NA; do NOT borrow BBO V3 averages, retrofit older records, restart frozen cohorts, or call the absence a profitable or losing BBO-conditioned strategy. If there is overlap, evaluate it purely descriptively; no selection of winners conditioned on return, and original frozen OOS remains EVIDENCE_FAIL.

## Runtime and research authority

All changes are in this isolated research PR. Exact-head Quality Check, Supply Chain Security and independent code review required before merging. Any authorized merge into `migration/binance-usdm` triggers Railway deployment; confirm a source-stamped log from the **new** SHA and unchanged `DRAWDOWN_HARD_GATE`, zero-equity accounting and private stream. This observer has `decision_effect=NONE`, `execution_effect=NONE`, `promotion_allowed=false`, `live_allowed=false`. No new Binance key requested.

Follow-up only after observation: Determine whether a previously preregistered future independent SHORT/DOWN/BOS study (#609, offline-only draft PR #610) can obtain *same-candidate* timely costs **and closed-candle stop/TP protected paths**, plus durable enrollment proof. Its present offline evaluator is NOT linked to production and can never bootstrap source authenticity from these R3 comparisons.
