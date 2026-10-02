# NEXUS Native Market Language — research candidate

This is a native NEXUS implementation of market-sequence concepts that are
useful in the MIT-licensed shiyu-coder/Kronos research project. It is not a
runtime integration and it does not import, call or download Kronos.

## What NEXUS absorbed

1. **Hierarchical candle representation**
   - Each OHLCV candle becomes a coarse market state plus a fine-grained state.
   - Coarse state captures direction, volatility regime and relative volume.
   - Fine state captures normalized return, wick imbalance and body strength.

2. **Strictly causal normalization**
   - Candle i is normalized only with observations from indexes < i.
   - Future candles cannot modify historical tokens.
   - Robust median/MAD scaling avoids dependence on global/future statistics.

3. **Temporal market context**
   - UTC minute, hour, weekday, day and month are available as context.
   - The autoregressive transition layer conditions on hour/weekday when enough
     matching history exists and backs off safely when it does not.

4. **Autoregressive multi-step forecasting**
   - The model learns conditional next-token transitions from the current
     rolling market sequence.
   - Forecast paths are generated recursively over multiple future steps.

5. **Probabilistic scenario generation**
   - Temperature and nucleus/top-p sampling are supported.
   - NEXUS generates multiple paths instead of relying on one point forecast.

6. **Distributional output**
   - P(up), P(down)
   - mean and median terminal return
   - q10 / q90
   - forecast dispersion
   - median maximum upside / downside
   - first-step entropy
   - bounded confidence

7. **Abstention**
   - The candidate does not force a LONG/SHORT vote.
   - Insufficient data or weak directional edge returns available=False with an
     explicit ABSTAIN/DATA_UNAVAILABLE reason.

8. **Batch multi-asset forecasting**
   - forecast_batch evaluates assets independently to prevent cross-symbol
     state contamination.

9. **Walk-forward evidence primitive**
   - walk_forward_evaluate performs strict prefix-only forecasts and reports
     coverage, directional accuracy when available and Brier score for P(up).

## Runtime contract for this PR

The market-language subsystem is research-only. It is deliberately **not**
installed by bot/runtime_overlays.py, not appended to nexus_ai.run_ensemble,
and not referenced by bot/engine.py.

Therefore it cannot change:
- ensemble direction or confidence;
- final score or execution_allowed;
- quantity, leverage, SL/TP;
- portfolio stress, durable state, ownership or fencing;
- exchange dispatch or place_order.

Promotion into the production ensemble requires a separate reviewed change
after out-of-sample/walk-forward evidence demonstrates incremental edge after
fees and slippage.

## External dependency policy

No torch dependency.
No transformers dependency.
No Hugging Face dependency.
No Kronos weights.
No network inference.
No external repository connection.

Attribution/design reference: shiyu-coder/Kronos (MIT), copyright 2025 ShiYu.
The NEXUS implementation is independently written for the existing NEXUS
research/data contracts.


## Binance USD-M OOS evidence

The research candidate now includes a Binance-specific, public, read-only replay path:

- bot/market_language_oos.py
- bot/market_language_binance_replay.py

The replay pages public /fapi/v1/klines history, discards any still-open candle,
and evaluates the model with strict prefix-only chronology. By default, a
4-candle horizon on 15m data is evaluated every 4 candles, so realized labels do
not overlap.

Evidence metrics include:
- Brier score for P(up)
- Brier skill against the empirical climatology baseline
- directional accuracy when the model does not abstain
- signal coverage
- gross directional return
- net directional return after the NEXUS conservative round-trip fee/slippage assumption
- net win rate
- moving-block bootstrap 95% CI for mean net return

The evidence gate fails closed unless sample size, coverage, probability skill,
positive net return and a strictly positive bootstrap lower bound all pass.

Example offline invocation:

    python -m bot.market_language_binance_replay \
      --symbols BTCUSDT ETHUSDT SOLUSDT XRPUSDT DOGEUSDT \
      --limit-15m 6000 --warmup 240 --horizon 4

A successful research result still does not activate MODEL H. Production
promotion remains a separate reviewed change.
