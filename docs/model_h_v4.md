# MODEL H V4 — invariant cross-sectional residual model

V4 is a research-only response to V3's failed unseen-symbol transfer.

## What V4 removes

V3 learned a strong development-set up-prevalence and calibrated probabilities
toward that regime. That calibration failed to transfer. V4 therefore has:

- no prevalence calibrator;
- no fitted probability intercept;
- no symbol identity feature;
- no per-symbol coefficient set;
- no regime-specific probability table.

The regression head is globally shared and has an explicit zero intercept.

## New invariant information

At each decision timestamp V4 builds scale-free features from the contemporaneous
peer panel:

- market-factor momentum normalized by realized volatility;
- cross-sectional momentum ranks;
- symbol residual momentum versus the market median;
- relative volatility rank;
- relative volume-surprise rank;
- market breadth;
- symbol/market trend alignment;
- centered native market-language probability edge.

The target is future return divided by current realized-volatility scale. Thus the
same model is trained across assets without using nominal price or symbol identity.

## Fresh holdout

The V3 transfer universe is now considered opened negative evidence and may be
used only as development data. V4's final holdout is a new universe:

- TRXUSDT
- DOTUSDT
- NEARUSDT
- ATOMUSDT
- UNIUSDT

The development fit is also time-fenced: the maximum training decision timestamp
must be strictly earlier than the first final-holdout decision timestamp.

## Screen

Promotion authority remains false. The research screen requires >=50 holdout
signals, >=12% coverage, positive net return after fees/slippage, positive 95%
bootstrap lower bound, >50% directional accuracy, positive Brier skill, positive
IC, and at least 3 positive-net holdout symbols.

Funding is explicitly excluded from this short-horizon experiment.
