# Kronos shadow integration

NEXUS-7 borrows the useful research pattern from the MIT-licensed shiyu-coder/Kronos project: probabilistic OHLC forecast paths are reduced into distributional features instead of treating one predicted close as an order instruction.

## Phase 1 contract

- Shadow/research only.
- No torch, transformers, Hugging Face, or Kronos model weights in the LIVE runtime.
- No call path to order placement.
- No mutation of strategy score, sizing, leverage, stops, targets, portfolio stress, durable state, ownership, fencing, or exchange state.
- Input: externally generated forecast paths.
- Output: direction probabilities, median/mean return, dispersion, path extrema, and bounded descriptive confidence.

The upstream Kronos project describes its fine-tuning/backtest pipeline as a simplified demonstration rather than a production quantitative trading system. NEXUS therefore keeps model inference outside the execution boundary until out-of-sample shadow evidence justifies a later proposal.

## Promotion criteria

Any future move from shadow telemetry into scoring requires a separate reviewed change with walk-forward/out-of-sample evidence, fees and slippage included, and proof that all existing execution/risk gates remain authoritative.
