# MODEL H V3 — multiscale analog mixture

V3 is a research architecture only. It does not modify NEXUS runtime.

## New information introduced

The previous MODEL H relied mainly on one autoregressive candle-token stream.
V3 adds independent causal information sources:

- 15-minute market-language probability;
- 1-hour market-language probability from complete, closed 15m aggregation;
- 4-hour market-language probability from complete, closed 15m aggregation;
- nearest historical analogs in normalized momentum/volatility/range/volume
  feature space;
- regime-aware analog retrieval as an optional expert;
- an explicit cost-aware expected-move gate;
- validation-fitted probability calibration with Bayesian shrinkage.

No future candle is used to construct a feature or analog outcome at a decision
timestamp. An analog is eligible only when its own forward outcome would already
have been known.

## Anti-overfitting protocol

The original BTC/ETH/SOL/XRP/DOGE universe is now development-only.

The final V3 evaluation transfers the frozen architecture to an entirely unseen
symbol universe:

- LINKUSDT
- ADAUSDT
- AVAXUSDT
- LTCUSDT
- BCHUSDT

These symbols are not used to choose the horizon, mixture profile, analog
settings, cost gate, or calibration table.

## Promotion screen

Even a passing research screen has no production authority. The screen requires:

- at least four unseen holdout symbols with sufficient history;
- >= 50 signaled holdout observations;
- >= 12% coverage;
- positive net mean after fees/slippage;
- 95% bootstrap lower bound above zero;
- directional accuracy above 50%;
- positive Brier skill versus naive prevalence;
- positive net result on at least 3 holdout symbols.

Funding is explicitly not included in this short-horizon research metric.

No runtime overlay, NEXUS score, engine, risk, sizing, leverage, SL/TP, durable
execution or exchange dispatch code is imported or changed by V3.
