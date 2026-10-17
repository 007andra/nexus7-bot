# NEXUS Evolution Program — Nonblocking Research First

Baseline production SHA: `038ea5e7b13d4d1072ecf590365b230d22042e34`
Production branch: `migration/binance-usdm`

## Operating contract

New intelligence enters as `shadow_only` first. Research modules may observe,
measure, simulate, rank and compare, but they cannot authorize or veto LIVE
orders. Existing execution-integrity controls remain authoritative: duplicate
order prevention, identity/fill bounds, durable predispatch, ownership/fencing,
exchange/account capability, valid sizing and post-fill protection.

Promotion requires an immutable experiment record and explicit promotion ID.
No feature is promoted merely because a backtest improves.

## Implemented foundation in this tranche

- `research_authority.py`: formal authority boundary and progressive modes.
- `binance_execution_parity.py`: backtest/shadow/paper/LIVE plan parity contract.
- `opportunity_ranker_v2.py`: research-only capital-allocation ranking.
- `portfolio_risk_v2.py`: shrinkage covariance, BTC/ETH beta hooks, risk contribution,
  cluster concentration and Expected Shortfall.
- `trade_research_metrics.py`: MFE/MAE, margin efficiency, execution-quality score,
  Monte Carlo paths and empirical risk-of-ruin estimate.
- `experiment_registry.py`: immutable deterministic experiment fingerprints plus
  promotion/rollback evidence helpers.
- `release_manifest_v2.py`: reproducible SHA/parameters/policy/version manifest.

## Existing foundation reused instead of duplicated

The production branch already contains OOS evidence datasets, counterfactual
coverage, calibration readiness with Brier/ECE, chronological walk-forward,
portfolio timestamp-cluster bootstrap, robustness decomposition, exchange-aware
cost snapshots, entry-latency telemetry and isolated research subprocesses.

## Rollout sequence

### Phase A — evidence/control plane
1. Freeze release manifests per deployed SHA.
2. Persist all candidate outcomes, including rejects and counterfactuals.
3. Attach Binance execution-parity snapshots to candidate/trade evidence.
4. Publish NEXUS edge dashboard from persisted evidence.
5. Register every experiment with dataset fingerprint, SHA, seed and verdict.

### Phase B — simulator and execution quality
6. Complete Binance USD-M simulator: filters, mark price, fees, funding, cross
   maintenance brackets, partials, gaps, STOP_MARKET, slippage and liquidation.
7. Add expected-vs-realized slippage calibration and VWAP/depth impact replay.
8. Add fill/protection latency SLOs and execution-quality time series.
9. Add capacity and liquidity-cap diagnostics.

### Phase C — calibration and selection
10. Extend probability calibration by setup/regime/symbol/side/volatility bucket.
11. Run champion/challenger shadow streams on identical opportunities.
12. Feed calibrated probability, net EV, liquidity, costs, diversification,
    tail risk and margin efficiency into Ranker v2 in shadow only.
13. Run sensitivity/multiple-testing/robustness batteries before promotion.

### Phase D — portfolio/risk research
14. Feed rolling returns into Portfolio Risk v2.
15. Add BTC/ETH factors, alt clusters, concentration and ES reporting.
16. Run Monte Carlo sequence stress and risk-of-ruin across candidate policies.
17. Define NORMAL/CAUTION/RECOVERY/HARD-STOP research policy table and compare
    counterfactually; do not mutate LIVE thresholds during the experiment.

### Phase E — market intelligence
18. Dynamic universe ranking by volume, spread, depth, OI, funding, volatility,
    execution quality and data reliability.
19. Real aggressor-side CVD/order-flow evidence and footprint diagnostics.
20. Spoofing/iceberg remains telemetry-only until OOS predictive value exists.
21. News event-study and macro-regime context become quantitative features only
    after leakage-safe forward validation.

### Phase F — exits and attribution
22. Persist MFE/MAE and holding-time outcome paths.
23. Evaluate trailing, break-even, TP1/TP2 and regime exits OOS by setup/regime.
24. Attribute every realized R to signal, NEXUS, sizing, slippage, fee, funding
    and exit logic.

### Phase G — reliability/release
25. Add anomaly detection for fills, PnL, spread, HWM, DB lag and data gaps.
26. Formalize SLOs for market freshness, WS, ACK, DB, reconciliation and protection.
27. Run restart/WS-loss/DB-loss/5xx/partial-fill/stop-delay disaster drills.
28. Stage every new authoritative feature before controlled LIVE canary.
29. Roll back to champion automatically at release-control level when formal
    rollback evidence is met; never let research code directly self-promote.

## Explicit non-goals

- No martingale or loss-chasing.
- No automatic leverage increase after losses.
- No automatic drawdown-limit increase to avoid a safety state.
- No threshold tuning against the held-out TEST period.
- No experimental feature can become LIVE authority by configuration accident.

The optimization target is reproducible net edge under realistic costs and
operational constraints, not maximum feature count.
