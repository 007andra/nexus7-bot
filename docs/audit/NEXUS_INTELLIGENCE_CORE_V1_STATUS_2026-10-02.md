# NEXUS Intelligence Core v1 — Engineering Status — 2026-10-02

## Scope

Branch: `feature/nexus-intelligence-core-v1`

Target: `migration/binance-usdm`

Purpose: close the research, reproducibility, governance and execution-
architecture gaps identified by the 2026-09-29 engineering readiness audit
without connecting NEXUS to third-party trading frameworks or changing LIVE
risk policy.

## Closed or materially advanced gaps

| Gap | Branch status |
|---|---|
| KuCoin-only OOS evidence path | Binance USD-M public-data replay implemented |
| Unverified historical archives | Binance Vision SHA-256 sidecars verified fail-closed |
| Walk-forward without full purge/embargo protocol | Native purged/embargoed splitter + OOS protocol |
| Limited research statistics | Bootstrap, Monte Carlo, CVaR, T-U-W, Calmar, ESS and cost attribution |
| Calibration metrics without train-only fit | Platt + isotonic fit on train, evaluate on OOS only |
| No feature contract/drift layer | Versioned schema/fingerprint + PSI/TVD drift |
| No champion/challenger governance | Durable fail-closed registry; explicit operator approval required |
| No single candidate lineage | Stable candidate_id through NEXUS, cost, sizing and durable order |
| Minute-scoped entry idempotency | Candidate-scoped idempotency namespace |
| Primary/robustness population drift risk | One-population Binance OOS evidence bundle |
| Current exchange filters treated as historical risk | Point-in-time instrument-rule snapshots |
| Historical metrics timestamp leakage risk | Information-availability normalization |
| No microstructure research primitive | bookDepth parser + SHADOW-only imbalance signal |

## Safety invariants preserved

- No external framework import/runtime connection.
- No automatic promotion.
- No new exchange mutation endpoint.
- No risk/score/leverage threshold changed by this branch.
- Existing durable intent remains persisted before exchange dispatch.
- Candidate lineage is persisted with the durable order before dispatch.
- Calibration, drift, sensitivity and microstructure research have
  `execution_effect=NONE`.
- Research results cannot directly alter runtime configuration.

## Important methodological boundary

The existence of a Binance-native backtest/replay framework does not prove an
edge. Engineering validation and alpha validation are separate gates.

`AI_EDGE_PROVEN` may be emitted only when the existing statistical edge gate
passes and the active NEXUS decision inputs have historical parity. Otherwise
the evidence path remains fail-closed.

## Remaining work before promotion can be considered

1. Run long-horizon Binance USD-M evidence across several regimes.
2. Review the resulting immutable manifest and candidate population.
3. Require at least four non-overlapping/purged OOS windows.
4. Review bootstrap interval, per-symbol/time robustness, tail loss and cost
   sensitivity.
5. Review raw vs calibrated Brier/log-loss/ECE OOS.
6. Accumulate SHADOW performance/drift evidence.
7. Obtain explicit operator approval; no automatic promotion.
8. Keep any future executor integration behind current ownership, fencing,
   durable-state, CROSS-stress, sizing and protection authorities.

## Separate infrastructure governance

Railway checkSuites/pre-deploy configuration and GitHub branch protection are
operator/infrastructure changes and are intentionally outside this PR.

## Release status

Do not merge or deploy solely because this document exists. The PR must pass
the repository Quality Check and Supply Chain Security workflows first.
