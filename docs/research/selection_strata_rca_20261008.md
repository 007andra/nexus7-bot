# Research RCA: approval selection after matching side × regime × setup

**Scope:** discovery and observability only, anchored to the existing MIN_ORDER blocked counterfactual cohort on the exact SHA. No permission for LIVE, new hypotheses on frozen OOS, risk resets, or retroactive sample edits.

## Problem
The production 2026-10-08 21:59 UTC telemetry says `NEGATIVE_SELECTION_CONFIRMED_BOTH_HORIZONS`, with 82/407 observed allowed at 60m (avg -0.001028), 325 rejected (+0.002577); 69 approved at 240m (avg -0.017360), 321 rejected (+0.003967). Approval concentration is ~95% SHORT and TRENDING_DOWN in the same counterfactual study. Pooled means confound approval state with directional/regime/setup composition. The separate frozen prospective OOS cohort is `EVIDENCE_FAIL`; this cannot retroactively fix that result.

## Observational method
For each horizon separately, use *only* exact recorded `OBSERVED` matched candidate outcomes already returned by the existing evaluator. Partition by side+regime+setup, requiring at least five valid observed approved and five rejected per stratum. Compute each stratum's approved-minus-rejected mean hypothetical gross return. Report min-arm-count-weighted mean of eligible stratum effects and the associated overlap coverage of approved and rejected samples. Emit statuses `COMPARABLE_OVERLAP` or `INSUFFICIENT_OVERLAP` with `NA` if no eligible overlap. Report whether pooled and within-stratum association have opposing signs (possible Simpson reversal).

This is NOT matching on entry time, symbol, market conditions, stop width, capital, correlation, economics, fees, slippage or actual fills. It cannot establish causality, a positive trading edge, or that rejection should be inverted. Low overlapping sample/strong imbalance is explicit, never imputed. Do not use the frozen OOS evaluation cohort to tune a new rule. Future candidate trials need a separate preregistered cutoff and real independent forward evaluation. The already-registered alternative 60m/240m exit experiment in issue #590 remains independent.

## Telemetry and authority
- New summary tags: `COUNTERFACTUAL_SELECTION_STRATA_60M_V1`, `COUNTERFACTUAL_SELECTION_STRATA_240M_V1`; the old reports and approval thresholds remain unchanged.
- No new DB queries, persistent tables, IDs in log text, external I/O, Binance calls, scheduling or timeout adjustments.
- Report function is pure and risk/strategy/LIVE-authority free; no mutation or orders.
- `association_not_causation=true`, `research_only=true`, `promotion_allowed=false`, `live_allowed=false`, `decision_effect=NONE`, `execution_effect=NONE`.
- This change is in a separate PR; does not merge the draft #595 export or modify #590 eligibility, HWM, drawdown, leverage, RR 1.60, risk sizing, dispatch, exchange state or LIVE gates.

## Go/no-go on 2026-10-09
`NO-GO` for new money order entries while hard historic drawdown exceeds the limit or the frozen OOS fails. Successful deployment/CI and healthy Binance endpoints prove runtime functionality, not profitable selection. Even if this analysis reports positive within-stratum lift, this alone cannot clear the risk or economic validation blocker. A conditional supervised micro-pilot would require independently sufficient prospective net evidence, operating risk constraints genuinely satisfied, current-SHA tests and actual execution safeguards reviewed, and separate explicit authorization.
