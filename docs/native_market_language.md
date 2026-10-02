# NEXUS Native Market Language — MODEL H

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
   - MODEL H does not force a LONG/SHORT vote.
   - Insufficient data or weak directional edge returns available=False with an
     explicit ABSTAIN/DATA_UNAVAILABLE reason, so the fusion engine excludes it.

8. **Batch multi-asset forecasting**
   - forecast_batch evaluates assets independently to prevent cross-symbol
     state contamination.

9. **Walk-forward evidence primitive**
   - walk_forward_evaluate performs strict prefix-only forecasts and reports
     coverage, directional accuracy when available and Brier score for P(up).

## Runtime contract

MODEL H is a normal ModelOutput in the existing NEXUS ensemble. It has no
method or field for order quantity, leverage or exchange dispatch. Existing MTF,
regime compatibility, EV, score threshold, risk, portfolio stress, durable
state, ownership, fencing and exchange execution remain downstream authorities.

## External dependency policy

No torch dependency.
No transformers dependency.
No Hugging Face dependency.
No Kronos weights.
No network inference.
No external repository connection.

Attribution/design reference: shiyu-coder/Kronos (MIT), copyright 2025 ShiYu.
The NEXUS implementation is independently written for the existing NEXUS
runtime and data contracts.
