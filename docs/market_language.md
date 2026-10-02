# NEXUS Market Language

This subsystem ports useful design ideas from the MIT-licensed
`shiyu-coder/Kronos` project into native NEXUS-7 code.

It does **not** integrate with Kronos at runtime. There is no Kronos package,
Hugging Face request, external model worker, model-weight download, or PyTorch
dependency.

## What was adopted

- hierarchical discrete representation of candle state;
- strictly causal normalization to avoid future leakage;
- autoregressive state progression;
- probabilistic multi-path forecasting;
- nucleus/top-p sampling;
- distribution-level outputs instead of one deterministic predicted close;
- walk-forward evaluation primitives.

The implementation uses NEXUS market history and NumPy. It is deliberately
selective: weak/ambiguous sequence evidence produces WAIT rather than a forced
direction.

## Runtime contract

The Market Language model is one member of the NEXUS ensemble. It can express
LONG, SHORT, WAIT, confidence and risk through the existing `ModelOutput`
contract. Existing final score, EV, MTF, risk, stress, durable-state, ownership,
fencing and execution gates remain downstream and authoritative.

## Attribution

Design inspiration: Kronos, copyright (c) 2025 ShiYu, MIT License.
No upstream model weights or external inference services are included.
