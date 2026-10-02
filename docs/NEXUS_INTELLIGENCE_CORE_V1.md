# NEXUS Intelligence Core v1

This branch is a clean-room, native NEXUS implementation of capabilities
benchmarked from mature open-source trading engines. It does not import, call,
depend on, or connect the runtime to NautilusTrader, Freqtrade, Hummingbot,
Jesse, LEAN, or Kronos.

## Architectural ideas absorbed

- NautilusTrader: deterministic state/event thinking and reconciliation-first
  execution.
- Freqtrade: anti-lookahead research discipline, feature contracts and OOS
  separation.
- Hummingbot: self-contained position-executor lifecycle.
- Jesse: deterministic research ergonomics and performance analysis.
- LEAN: separation between alpha, portfolio/risk and execution concerns.
- Kronos research ideas live in separate native NEXUS work; no external model
  dependency is introduced by this branch.

No upstream source code is vendored here.

## Implemented on this branch

### Research integrity

- Purged/embargoed chronological walk-forward splits.
- Explicit temporal anti-lookahead contracts.
- Bootstrap confidence intervals.
- Monte Carlo trade-path stress.
- Max drawdown, time under water, CVaR, effective sample size, expectancy,
  win rate, payoff, profit factor, Sharpe, Sortino and Calmar.
- Symbol, side, regime and volatility-tercile segmentation.
- Fee/slippage/funding, turnover and exposure attribution.
- Parameter and execution-cost sensitivity surfaces.
- Minimum four-fold OOS evidence contract.
- Train-only Platt and isotonic probability calibration evaluated OOS only.

### Binance USD-M research parity

- Checksum-verified Binance Vision archives.
- Native 15m/1h/4h kline and funding ingestion.
- Daily USD-M metrics ingestion for open interest and long/short context.
- Post-2026-06-25 metrics label normalization to information-availability time
  so start-labeled rows cannot leak the following five-minute interval.
- Daily bookDepth parsing and SHADOW-only imbalance diagnostics.
- Candidate-day bookDepth sampling avoids downloading irrelevant depth archives.
- SHADOW microstructure combines 1/2/5% depth imbalance, taker flow and
  candidate-cadence open-interest impulse.
- Cross-symbol opportunity ranking compares only candidates sharing the same
  decision timestamp and uses pre-trade inputs only.
- Ranking evidence reports top-1 expectancy, remaining-candidate expectancy,
  uplift and rank/outcome correlation after outcomes are known.
- Known-problem bookDepth periods can be quarantined rather than silently
  entering evidence.
- Conservative Binance execution proxy with taker fees, adverse slippage,
  funding settlements and stop-first same-bar ambiguity.
- Immutable research manifests with archive SHA-256 fingerprints.
- Primary edge and robustness reports derived from the exact same candidate
  population.

### Reproducibility and model governance

- Versioned feature schema and deterministic feature fingerprints.
- Numeric PSI and categorical distribution-shift diagnostics.
- Decision evidence bundles with canonical timestamps/hash verification.
- Champion/challenger registry with durable persistence.
- Promotion is fail-closed unless CI, OOS, SHADOW and explicit operator
  approval are all present.
- Post-trade attribution separates market edge, entry/exit execution effects,
  fees and funding.
- Stable candidate identity spans signal -> NEXUS -> execution-cost snapshot ->
  RiskManagerV3 sizing -> clientOid/durable ManagedOrder.
- Candidate idempotency is setup-scoped rather than minute-scoped.

### Execution architecture foundations

- Pure exchange-agnostic position-executor lifecycle and protection plan.
- Lifecycle has no exchange method and cannot authorize new risk.
- Shadow opportunity ranker has no sizing/dispatch authority.

## Runtime authority boundary

Research, calibration, drift, sensitivity, opportunity ranking and governance
modules do not grant LIVE permission. Existing ownership, fencing, durable
state, sizing, drawdown, Binance CROSS stress, protection readiness, final loss
budget and exchange dispatch authorities remain downstream.

The new position-executor lifecycle is intentionally not wired around those
authorities. Any future integration must sit behind them and prove it cannot
bypass them.

## Book depth policy

The current production NEXUS score still treats MICROSTRUCTURE as unavailable
and renormalizes its weight. Historical bookDepth is therefore collected only
as SHADOW research and is not required for exact parity of the currently active
score. Missing bookDepth is an observational gap; checksum/schema corruption
still fails closed. Known-problem depth periods are quarantined. It must not gain production weight without OOS + SHADOW evidence and
explicit operator approval.

## Evidence still required before any strategy promotion

The framework exists, but the branch does not claim a profitable edge. Before
any calibration/threshold/model can affect LIVE decisions:

1. Execute a sufficiently long Binance USD-M OOS evidence bundle over multiple
   market regimes and symbols.
2. Require the immutable manifest and checksum-verification report.
3. Require >=4 chronological purged/embargoed OOS folds.
4. Review primary uplift, bootstrap intervals, robustness by symbol/time,
   cost sensitivity and calibration metrics.
5. Accumulate SHADOW evidence and drift telemetry.
6. Require repeated cross-sectional ranking evidence showing whether top-ranked
   candidates add OOS expectancy without outcome leakage.
7. Require explicit operator approval through the champion/challenger gate.

A passing unit/CI suite proves engineering invariants, not trading profitability.

## Intentionally not changed

- No automatic strategy/model promotion.
- No threshold or leverage change.
- No weakening of risk or drawdown gates.
- No connection to external trading frameworks.
- No merge or Railway deploy performed by this branch.
- No branch-protection or Railway governance changes.
