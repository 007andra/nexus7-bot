# NEXUS Intelligence Core v1

This branch is a clean-room implementation of capabilities benchmarked from
mature open-source trading engines. It does not import, call, depend on, or
connect NEXUS to NautilusTrader, Freqtrade, Hummingbot, Jesse or LEAN.

## Sources of architectural ideas

- NautilusTrader: deterministic event/state thinking and reconciliation-first design.
- Freqtrade: anti-lookahead research discipline, feature contracts, hyperparameter/OOS separation.
- Hummingbot: self-contained position executor lifecycle and triple-barrier-style protection plan.
- Jesse: deterministic research ergonomics and performance analysis.
- LEAN: strict separation between alpha, portfolio/risk and execution concerns.

No upstream source code is vendored here.

## New native NEXUS capabilities

- Purged/embargoed walk-forward: bot.research_walk_forward. Runtime authority: NONE.
- Temporal anti-lookahead contract: bot.research_walk_forward. Runtime authority: NONE.
- Bootstrap confidence intervals: bot.research_statistics. Runtime authority: NONE.
- Monte Carlo trade-path stress: bot.research_statistics. Runtime authority: NONE.
- Feature schema/version/fingerprint: bot.feature_contract. Runtime authority: NONE.
- Numeric/categorical drift metrics: bot.feature_contract. Runtime authority: NONE.
- Champion/challenger evidence gate: bot.champion_challenger. Explicit operator approval required.
- Post-trade attribution: bot.trade_attribution. Runtime authority: NONE.
- Position-executor lifecycle: bot.execution_plan. It cannot call an exchange.
- Cross-symbol opportunity ranking: bot.opportunity_ranker. SHADOW/RESEARCH only.

## Safety boundary

These modules deliberately have no import of bot.binance, bot.exchange,
bot.engine, risk/sizing authorities or order transport. The executor lifecycle
is pure state and exposes new_risk_allowed=False. The first tranche therefore
adds research and architecture capabilities without changing LIVE policy.

## Next integration tranches

1. Binance-native historical dataset adapter: klines 15m/1h/4h, funding,
   exchangeInfo filter snapshots and deterministic local caching.
2. Research orchestrator: purged walk-forward across at least four OOS windows,
   regime/symbol/side/volatility segmentation, bootstrap CI and Monte Carlo.
3. Feature snapshot persistence: attach schema version, fingerprint and
   candidate_id to NEXUS decisions for bit-exact replay.
4. Drift telemetry: feature/score/regime/spread/slippage/funding distribution
   monitoring in SHADOW only.
5. Champion/challenger persistence and operator-reviewed promotion workflow.
6. Execution-plan adapter behind the existing durable execution, ownership,
   fencing, sizing, CROSS stress and protection authorities.
7. Attribution wiring after final accounting so every closed trade separates
   market edge from entry slippage, exit slippage, fees and funding.

## Definition of done for this tranche

- No trading policy or threshold changes.
- No external repository dependency.
- New modules have direct tests.
- No module in this tranche can submit an order.
- Promotion remains fail-closed without explicit operator approval.
