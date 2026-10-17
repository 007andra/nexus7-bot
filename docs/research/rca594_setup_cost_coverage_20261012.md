# NEXUS-7 — R3 setup-level cost-snapshot coverage diagnostic

Status: **RESEARCH ONLY; PR draft; not deployed**. Base: production merge `77a3bb0b8946d773433e360b5e35435e682e5339` / Railway `dc04b737`, 12 Oct 2026 BRT.

## Explicit provenance

The already-running counterfactual failure analysis joins `hard_gate_shadow_candidates_v1` (population HARD_GATE_SHADOW, epoch R3), each candidate's `cost_snapshot`, and natural observed `hard_gate_shadow_outcomes_v1` 60/240m. The executable NEXUS client is **not** called for this report.

The pre-existing public R3 two-arm aggregate output reports 493 candidates (98 allowed, 395 rejected). See issue #594 for per-setup gross future-return sums. At 60m, BOS_BREAK n=41 approved had gross +0.2842%; MOMENTUM n=29 approved gross −0.6443%; PULLBACK n=28 approved gross −0.0816%. At 240m every approved setup had a negative gross future-return mean. The global cost snapshot fraction across all 98 approvals was ~0.003108 (~31.08 bps), with `spread_bps` valid n=0 in existing feature comparison. **DO NOT** subtract a global cost from any individual setup: that would falsely impute costs and overstate evidence.

## New per-setup paired data-coverage proof (no additional database queries)

The draft adds two fields to the same in-memory `counterfactual_approval_failure_analysis_v1.build_report` result: `setup_cost_coverage_60m` and `setup_cost_coverage_240m`. In each horizon, use only observed candidate/outcome pairs, preserving setup AND approval-arm membership; do not pair unrelated quotes or reuse historical populations.

The existing `COUNTERFACTUAL_APPROVAL_FAILURE_WORST_GROUPS_{60,240}M_V1` logging path will append `setup_cost_proxy=` for MOMENTUM / BOS_BREAK / PULLBACK / OTHER. Each aggregate reports:
- `an/rn`: total valid gross-outcome candidate IDs in that group and arm;
- `acn/rcn`: count with **valid per-candidate** attached candidate-time estimated round-trip fee + entry/exit slippage (`cost_fraction`) between 0 and 10%;
- `aspn/rspn`: subgroup count with a nonnegative finite `spread_bps` in the stored snapshot (not proof of fresh executable BBO);
- `apmean/rpmean`: mean `future_return - candidate_cost_fraction` among **cost-covered observations only**. A missing cost cannot be treated as zero and cannot contribute to proxy mean.

The code refuses unmatched expected subgroup denominators and inconsistent coverage or nonfinite sums as `setup_cost_proxy=NOT_PROVEN`; untrusted setup names are collapsed to OTHER, and no IDs are logged.

**CRITICAL:** This is only a **fee/slippage snapshot gross-minus-cost proxy**. It does not model exact Binance ACK/fill, current/order-book depth, correct entry/exit timing, SL/TP precedence on ambiguous 15m OHLC, funding events, actual taker/maker fee tier, liquidation risk, order qty/tick/step or source-independent net PnL. Even if `acn==an`, `aspn==an`, and `apmean>0`, `real_executable_net_proven=false funding_proven=false protected_stop_tp_proven=false live_allowed=false`. Never promote the strategy or deposit money due to a positive proxy.

## Acceptance criteria

1. Exact-head Quality Check and Supply Chain Security PASS; tests cover stale/missing/negative snapshot costs, separate approved/rejected denominators, unknown setup redaction, correct 60/240 horizon and forced non-LIVE output.
2. Independent diff review and optional operator approval to merge; this base branch autodeploys to Railway. No risk/strategy/exchange mutations are in this change.
3. Verify source-timestamped `setup_cost_proxy=` output for BOTH horizons on the exact merged deployment SHA; missing group coverage stays NOT_PROVEN, never zero-cost imputation.
4. Compare observed cost-covered group size to group n. If cost not complete and source spread/funding/protection missing, **do not report an economically proven edge**. Independently preregister an untouched, future paired exit protocol if a falsifiable setup survives; retain prior frozen V1/V4 cohorts.
5. No change to current risk gate, override=false, historical HWM, margin, Binance 50x exchange config, order execution, deposits, pilot arm, or protection authority.
