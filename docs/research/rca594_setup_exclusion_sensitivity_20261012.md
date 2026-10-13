# NEXUS-7 — OOS setup exclusion sensitivity (retrospective, research only)

Evidence source: Railway BGX Capital `nexus7-bot`, production deployment `05913de7-514b-4a13-86fa-f3848dd9b7ec`, production commit `49f0003`; snapshot log time **2026-10-13T00:09:30Z** (21:09 BRT Oct 12). Coarse aggregate log precision is six decimals. The underlying DB rows have not been independently read back from an authorized production SQL session. This diagnostic does **not** use a separate Binance key and never changes the production bot.

## Cohorts must not be mixed

- The main **CALIBRATION_GENERALIZATION_V1** frozen OOS has 651 candidates and matching 60m/240m outcomes, with 115 approved / 536 rejected. Observed mean gross lift: −0.002353 at 60m and −0.018095 at 240m. `EVIDENCE_FAIL`.
- The separate **REENTRY_V1_20261006_R3** counterfactual report has 493 candidates with matched outcomes; 98 approved / 395 rejected. Observed approved means −0.000951 at 60m and −0.018586 at 240m. The 29 approved `MOMENTUM` candidates (NOT necessarily all `SHORT+TRENDING_DOWN+MOMENTUM`) have −0.006443 and −0.032932 respectively. These are historical conditional associations, **not realized Binance trades**.
- In the same R3 cohort, within identical side/regime/setup strata, the overlapping n=94/98 approved and 393/395 rejected have weighted selection lifts −0.003575 (60m) and −0.024312 (240m); Simpson reversal not detected.
- Independent frozen **V4** ablation cohort at this timestamp: 24/100 approved, 0 naturally approved `SHORT+TRENDING_DOWN+MOMENTUM` exclusions, 24 retained, `net_proven=false`. It cannot yet validate exclusion benefits and should not be restarted.

## Reconstructable arithmetic on real aggregates

Formula for approval arm only:

`remaining_mean = (all_approved_n * all_approved_mean - excluded_approved_n * excluded_mean) / (all_approved_n - excluded_approved_n)`.

Using counts/rounded log means:

| Horizon | All approved (n=98) | MOMENTUM approved (n=29) | Remaining approved (n=69) | Gross no-cost break-even |
|---|---:|---:|---:|---:|
| 60m | −0.0951% | −0.6443% | **+0.1357%** | Total execution cost must be lower than 0.1357%, before uncertainty |
| 240m | −1.8586% | −3.2932% | **−1.2557%** | Negative even before any fee/slippage/funding |

**Important limitations:** This is an approved-only subtraction; it does NOT subtract `MOMENTUM` from the rejected arm because the published top-10 worst-group log does not provide the rejected MOMENTUM **count**, only its mean (+0.001230 at 60m, +0.001178 at 240m). Therefore no matched setup-exclusion lift is computable from these logs alone. Do not compare the 69 reduced approvals to all 395 original rejects and call that a policy test. Rounding and within-cohort selection bias add uncertainty. `spread_bps` has valid n=0 in the R3 60m feature comparison: cost snapshots alone cannot prove executable net outcomes. The existing 0.003108 average cost fraction for **all approved signals**, if quoted, is not a verified cost for the 69-signal subset.

## Next strictly constrained research validation

1. Use the existing **approved** production PostgreSQL read-only export route (PR #595 previously tested on synthetic PG16, not run on real production rows) to obtain per-setup **counts and unrounded sums** for both approved/rejected arms and both horizons from frozen candidate_id-matched outcomes, after approval of access. No public DB proxy, no secrets, no independent Binance API key, no changes to the trading DB.
2. Validate the new pure module `bot/oos_setup_exclusion_sensitivity_v1.py` on a trusted aggregate `counterfactual_approval_failure_analysis_v1.build_report` payload; it rejects missing/misaligned groups, weights and NaN inputs. Existing logs alone support only `approved_only_remainder`.
3. Check confidence bounds with symbol/time clustered dependence, tail losses, correct directional mark convention, stop-first ambiguity, actual execution fees/funding/slippage and order feasibility; do not infer results from raw future_return only.
4. Pre-register an **unchanged** prospective policy challenger using future canonical signals if a falsifiable hypothesis survives. Frozen V2/V4/V1 cohorts remain immutable and cannot be promoted retrospectively. Current production selection, scoring, R:R, MAX_DRAWDOWN and HWM remain untouched.

**RELEASE:** `promotion_allowed=false`, `live_allowed=false`, `execution_effect=NONE`. Deployment remains `05913de7`, equity 0 USDT and risk hard gate. No claim that excluding MOMENTUM makes the bot ready to trade.
