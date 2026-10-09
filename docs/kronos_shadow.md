# Kronos shadow integration

This integration brings the useful ideas from [shiyu-coder/Kronos](https://github.com/shiyu-coder/Kronos) into NEXUS-7 without putting an ML model on the order-dispatch path.

## Design

- NEXUS remains the authority that produces `execution_allowed`.
- Existing risk, durable-state, sizing, stress, ownership and exchange fences are unchanged.
- Kronos receives the same closed 15-minute candle context already collected for NEXUS.
- Forecasting runs asynchronously after the NEXUS decision is produced.
- A timeout, provider error, invalid forecast or missing worker cannot authorize, reject or modify a trade.
- The main bot keeps its current lightweight requirements. PyTorch and the Kronos model live in a separate worker.

## Shadow features

Each set of stochastic OHLC paths is summarized into:

- direction probability aligned to LONG/SHORT;
- median/mean and P10/P90 signed return;
- path dispersion;
- mean path volatility;
- median MFE/MAE;
- TP-before-SL and SL-before-TP rates;
- same-bar TP/SL ambiguity rate.

Same-bar TP+SL is deliberately classified as ambiguous instead of assuming a favorable fill.

## Runtime variables

The main NEXUS service accepts:

```
KRONOS_SHADOW_ENABLED=false
KRONOS_SHADOW_URL=
KRONOS_SHADOW_TIMEOUT_S=3
KRONOS_MAX_CONTEXT=400
KRONOS_HORIZON=16
KRONOS_SAMPLE_COUNT=16
KRONOS_TEMPERATURE=1.0
KRONOS_TOP_P=0.9
KRONOS_TIMEFRAME_MINUTES=15
KRONOS_SHADOW_CONCURRENCY=2
```

Keep `KRONOS_SHADOW_ENABLED=false` until the worker is deployed and its resource usage is measured.

## Worker

The optional worker is in `services/kronos_worker/`. It vendors the upstream Kronos model source at commit
`67b630e67f6a18c9e9be918d9b4337c960db1e9a` under the upstream MIT license and downloads the configured pretrained weights from Hugging Face.

Recommended first model: `NeoQuasar/Kronos-small`.

The worker intentionally returns individual stochastic paths rather than only the upstream averaged forecast. NEXUS uses those paths for probability, dispersion and barrier-order diagnostics.

## Promotion rule

Shadow telemetry must be evaluated out-of-sample before it can influence NEXUS scoring. Promotion should require evidence of incremental edge after fees/slippage and should be a separate PR. This PR contains no scoring or execution-policy change.
