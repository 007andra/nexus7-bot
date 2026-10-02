# MODEL H recalibration V2

This is research-only evidence. It does not activate MODEL H in NEXUS runtime.

Protocol:
- one global configuration across BTC, ETH, SOL, XRP and DOGE;
- 15m, 30m, 1h, 2h and 4h horizons screened on validation only;
- profile grid changes context order, temperature, top-p, dominance threshold,
  confidence threshold and minimum token history;
- causal regime/volatility context buckets are learned on validation only;
- the final 25% chronological suffix remains untouched until the horizon,
  profile and context whitelist are frozen;
- final metrics include coverage, directional accuracy, Brier/Brier skill,
  calibration error, IC, gross/net directional return and bootstrap CI;
- fees and slippage are included; funding is explicitly not included;
- even an EDGE_SCREEN_PASS has no production promotion authority.

Production integration, score weighting and runtime activation require a separate
reviewed change.
