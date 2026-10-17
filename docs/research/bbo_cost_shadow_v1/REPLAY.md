# BBO cost shadow v1: replay over the audited production population

RESEARCH ONLY. decision_effect=NONE execution_effect=NONE. Evidence class: **UNPROVEN**.

- Source: Railway production logs 2026-10-03 16:34-20:00 UTC: [TECHNICAL_STOP_POLICY] paired with the following [NEXUS_SCORE_DECOMP] (read-only)
- Population: 86 NEXUS evaluations, 5 unique candidates (AVAXUSDT:LONG:PULLBACK:1990054, BNBUSDT:LONG:BOS_BREAK:1990050, SOLUSDT:LONG:MOMENTUM:1990054, SOLUSDT:LONG:MOMENTUM:1990058, SOLUSDT:LONG:PULLBACK:1990055)
- Static net R:R reproduces the logged rr_net in 86/86 evaluations
- NEXUS net R:R floor 1.60; win_prob = runtime heuristic of the logged fusion confidence

## OBSERVED (real bookTicker at evaluation time)

| total | BBO valid | BBO invalid | BBO stale | BBO missing | spread median | p90 | p95 |
|---|---|---|---|---|---|---|---|
| 86 | 0 | 0 | 0 | 86 | NA | NA | NA |

no bookTicker was subscribed in production at evaluation time; observed BBO for this population does not exist. Observed spread statistics will exist only after the shadow runs prospectively in production.

## COUNTERFACTUAL (hypothesis: spread = k x tick, top-of-book qty sufficient)

| scenario | spread median bps | p90 | p95 | static cost median bps | live cost median bps | delta median bps | EV_RR gate changes (evals / unique) | MIN_ORDER ok (static sizing) | MIN_ORDER ok (cf sizing) | would reach final sizing (cf) |
|---|---|---|---|---|---|---|---|---|---|---|
| COUNTERFACTUAL_3_TICKS | 2.502 | 2.502 | 2.502 | 20.0 | 14.50 | -5.50 | 0 / 0 | 85/86 | 85/86 | 0/86 |
| COUNTERFACTUAL_1_TICK | 0.834 | 0.834 | 0.834 | 20.0 | 12.83 | -7.17 | 1 / 1 | 85/86 | 85/86 | 0/86 |

"EV_RR gate changes" is a counterfactual of one NEXUS gate only; later NEXUS stages are not re-run. It is NOT "order would be executable": executability needs MIN_ORDER and final sizing, reported in their own columns (MIN_ORDER sizing keeps the runtime NEXUS_EXPECTED_SLIPPAGE_PCT floor).

