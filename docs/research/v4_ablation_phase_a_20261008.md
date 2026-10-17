# NEXUS V4 — Phase A: canonical shadow enrollment and gross provenance

Preregistered protocol: [issue #602](https://github.com/007andra/nexus7-bot/issues/602), created **2026-10-08 22:16:36 UTC**. Authoritative exact cutoff: `1791497796.0` Unix seconds. Only stored candidates with precise JSON `captured_epoch > cutoff` can count. Any pre-issue sample is *discovery*, not V4 evidence.

## Activation and status
New flag `NEXUS_V4_PROSPECTIVE_ABLATION_SHADOW` defaults **false**; the proposed deployment will have **no V4 database query** unless the operator *separately* authorizes enabling it. If enabled in an approved deployment under the drawdown hard gate, a bounded read-only snapshot runs no more frequently than every 600 seconds, with a 3s fail-closed timeout; no table creation, DB write, Binance call, risk change, threshold change or order action. Emission is `[V4_PROSPECTIVE_ABLATION_PHASE_A]`.

The existing scanner produces canonical candidate records and hypothetical gross candle observations independently of V4. This observer only READS their existing PostgreSQL tables. The server-side `captured_epoch` REAL query uses a 512s buffer for float4 rounding, then strict JSON cutoff guards reject all earlier candidates; all outcome SQL uses parameters and is bounded to at most 100 candidate IDs.

**Eligibility**: `population=HARD_GATE_SHADOW`, `shadow_only=true`, `live_eligible=false`, `nexus_called=true`, `nexus_allowed` actually boolean, `decision_effect=NONE`, `execution_effect=NONE`, table candidate ID equals payload ID equals symbol/side/setup encoded in ID. Top-level canonical field only. Counterfactual-only `counterfactual_nexus_v1.execution_allowed=true` never counts. Invalid post-cutoff rows trigger `AUDIT_FAIL_CLOSED`, not a positive result.

**Fixed cohort**: first 100 unique future canonical *approved* candidate IDs, ordered by precise captured time then ID, max 15 per symbol, no changes based on outcomes. Canonical NEXUS rejections are tracked descriptively and never turned into buys/sells. Challenger adds exactly one veto: `SHORT + TRENDING_DOWN + MOMENTUM`. All other champion-accepted opportunities remain challenger-accepted. Vetoed positions represent **no trade** (flat cash), not a winning short/long.

**Outcomes**: require exact candidate ID and horizon, explicit `OBSERVED`, `return_basis=hypothetical_entry_gross`, finite return/MFE/MAE, observation start exactly next closed 15-minute boundary, and 60/240-minute maturity. `UNKNOWN_CACHE_GAP`, future-looking, mismatched or malformed outcomes remain NOT_PROVEN. Partial group/horizon gross means are descriptive only; do not compare unpaired 60/240 averages as proof of an exit mechanism.

## Explicit unsupported proofs — this is intentionally Phase A

- **No durable membership ledger / sealed snapshot yet**. The sample is reconstructed deterministically from existing immutable candidate IDs at each poll. A late-inserted older captured event could change a not-yet-sealed sample: implement a write-once enrollment ledger with CAS/fencing and replay proof in a subsequent isolated step before treating enrollment as sealed. `durable_member_freeze_proven=false`.
- **No policy-equivalent stop/TP path**: current `future_return` is a hypothetical gross close, ignores protective STOP_MARKET fills, take-profit touch order, partial TP/trailing, position liquidation/gaps, realistic execution and slippage/funding. `stop_tp_execution_proven=false`.
- **No independently validated net policy return, resampled cluster confidence intervals, leave-one-symbol-out, two disjoint time-block robustness, paired policy portfolio accounting, confirmed Binance min-order feasibility or equal-capital comparison**. `net_proven=false`; `gross_only=true`.
- Hence **never** set `READY_FOR_MANUAL_REVIEW`, `promotion_allowed`, `live_allowed` or any entry/execution authority in Phase A, even with 100/100 observed and positive gross return. The frozen evaluation criteria from #602 remain unchanged and must be satisfied in a separate net validator.

## Isolation
This PR must not modify previous V2/V3 experiments, issue #602 registration, frozen CALIBRATION_GENERALIZATION_V1 cohort (EVIDENCE_FAIL), production RR 1.60, scoring, constraints, Binance adapter, leverage, ownership, historic HWM, drawdown, recovery or LIVE order dispatch. Tests enforce strict issue cutoff, canonical-only acceptance, veto exactness, symbol quota/order, rejected descriptive group, immature/gap/corrupt outcome exclusion, default-off, SELECT-only SQL and no premature pass.

**Release gate:** research-only Phase A code must pass Quality Check and Supply Chain Security on exact SHA. Merge/deploy and enabling the flag are distinct, separately authorized actions. No action can change the financial hard gate or authorize real trades tomorrow.
