# NEXUS Feasibility Frontier v1

RESEARCH / SHADOW ONLY. decision_effect=NONE execution_effect=NONE. No threshold, risk, recovery, leverage, score, EV, R:R or sizing value is changed; equity counterfactuals are analytical and authorize nothing.

- Source: Railway production logs 2026-09-27..2026-10-03 (read-only)
- Equity 8.7583 USDT, effective stop-risk 0.50%, leverage 50x, margin cap 100% of available
- NEXUS net R:R floor 1.60; EV evaluated at win_prob=0.45 (confidence 33.3); observed low-confidence case p=0.356 (confidence 12.4)
- Costs = live runtime static model: taker 5 bps/side; slippage 5 bps/side majors (BTC/ETH/SOL), 10 bps/side alts; MIN_ORDER sizing slippage floor NEXUS_EXPECTED_SLIPPAGE_PCT=0.10%
- ASSUMED rows (suffix (N5)/(N20)) have NO filter evidence: they are hypotheses, never facts, and must not be promoted to any LIVE conclusion until real filters are logged
- Filters provenance: OBSERVED = full filters in [SIZING_DECOMPOSITION]; INFERRED = [MIN_ORDER_FEASIBILITY] min_valid_qty+binding+price; ASSUMED = no evidence (N5/N20 scenarios)

## 1. Per-symbol model at current equity, observed median stop and observed median gross R:R

| symbol | prov | price | minQty | step | minNotional | min valid qty | min notional | fee/side | slip/side | RT cost | stop obs | R obs | R net | EV % | risk@min | budget | margin@min 50x | max stop MIN_ORDER | min stop NEXUS R:R | MIN_ORDER | NEXUS R:R | NEXUS EV | intersection |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| AAVEUSDT | OBSERVED | 181.55 | 0.1 | 0.1 | 5 | 0.1 | 18.16 | 5bp | 10bp | 0.30% | 1.900% | 2.00 | 1.59 | +0.365 | 0.39937 | 0.04379 | 1.8155 | 0.000% | 1.950% | false | false | true | **false** |
| ADAUSDT(N5) | ASSUMED | 0.2535 | 0.001 | 0.001 | 5 | 19.724 | 5.00 | 5bp | 10bp | 0.30% | 1.185% | 2.00 | 1.39 | +0.115 | 0.07423 | 0.04379 | 0.5000 | 0.576% | 1.950% | false | false | true | **false** |
| ADAUSDT(N20) | ASSUMED | 0.2535 | 0.001 | 0.001 | 20 | 78.896 | 20.00 | 5bp | 10bp | 0.30% | 1.185% | 2.00 | 1.39 | +0.115 | 0.29693 | 0.04379 | 2.0000 | 0.000% | 1.950% | false | false | true | **false** |
| APTUSDT(N5) | ASSUMED | 0.8628 | 0.001 | 0.001 | 5 | 5.796 | 5.00 | 5bp | 10bp | 0.30% | 1.148% | 2.00 | 1.38 | +0.102 | 0.07244 | 0.04379 | 0.5001 | 0.576% | 1.950% | false | false | true | **false** |
| APTUSDT(N20) | ASSUMED | 0.8628 | 0.001 | 0.001 | 20 | 23.181 | 20.00 | 5bp | 10bp | 0.30% | 1.148% | 2.00 | 1.38 | +0.102 | 0.28971 | 0.04379 | 2.0001 | 0.000% | 1.950% | false | false | true | **false** |
| ARBUSDT | INFERRED | 0.19869 | 0.1 | 0.1 | 5 | 25.2 | 5.01 | 5bp | 10bp | 0.30% | 2.101% | 2.00 | 1.62 | +0.435 | 0.12020 | 0.04379 | 0.5007 | 0.575% | 1.950% | false | true | true | **false** |
| ATOMUSDT | INFERRED | 1.698 | 0.01 | 0.01 | 5 | 2.95 | 5.01 | 5bp | 10bp | 0.30% | 1.158% | 2.00 | 1.38 | +0.105 | 0.07305 | 0.04379 | 0.5009 | 0.574% | 1.950% | false | false | true | **false** |
| AVAXUSDT | INFERRED | 11.114 | 1 | 1 | 5 | 1 | 11.11 | 5bp | 10bp | 0.30% | 1.813% | 2.00 | 1.57 | +0.334 | 0.23479 | 0.04379 | 1.1114 | 0.094% | 1.950% | false | false | true | **false** |
| BNBUSDT | INFERRED | 789.01 | 0.01 | 0.01 | 5 | 0.01 | 7.89 | 5bp | 10bp | 0.30% | 0.354% | 2.00 | 0.62 | -0.176 | 0.05160 | 0.04379 | 0.7890 | 0.255% | 1.950% | false | false | false | **false** |
| BTCUSDT | INFERRED | 84939.8 | 0.001 | 0.001 | 5 | 0.001 | 84.94 | 5bp | 5bp | 0.20% | 0.298% | 2.00 | 0.80 | -0.096 | 0.42308 | 0.04379 | 8.4940 | 0.000% | 1.300% | false | false | false | **false** |
| DOGEUSDT(N5) | ASSUMED | 0.09594 | 0.001 | 0.001 | 5 | 52.116 | 5.00 | 5bp | 10bp | 0.30% | 0.884% | 2.00 | 1.24 | +0.009 | 0.05918 | 0.04379 | 0.5000 | 0.576% | 1.950% | false | false | true | **false** |
| DOGEUSDT(N20) | ASSUMED | 0.09594 | 0.001 | 0.001 | 20 | 208.464 | 20.00 | 5bp | 10bp | 0.30% | 0.884% | 2.00 | 1.24 | +0.009 | 0.23673 | 0.04379 | 2.0000 | 0.000% | 1.950% | false | false | true | **false** |
| DOTUSDT(N5) | ASSUMED | 1.2517 | 0.001 | 0.001 | 5 | 3.995 | 5.00 | 5bp | 10bp | 0.30% | 1.590% | 2.00 | 1.52 | +0.257 | 0.09454 | 0.04379 | 0.5001 | 0.576% | 1.950% | false | false | true | **false** |
| DOTUSDT(N20) | ASSUMED | 1.2517 | 0.001 | 0.001 | 20 | 15.979 | 20.00 | 5bp | 10bp | 0.30% | 1.590% | 2.00 | 1.52 | +0.257 | 0.37812 | 0.04379 | 2.0001 | 0.000% | 1.950% | false | false | true | **false** |
| ETCUSDT | INFERRED | 8.766 | 0.01 | 0.01 | 20 | 2.29 | 20.07 | 5bp | 10bp | 0.30% | 1.054% | 2.00 | 1.33 | +0.069 | 0.27182 | 0.04379 | 2.0074 | 0.000% | 1.950% | false | false | true | **false** |
| ETHUSDT(N5) | ASSUMED | 2718.91 | 0.001 | 0.001 | 5 | 0.002 | 5.44 | 5bp | 5bp | 0.20% | 0.448% | 2.00 | 1.07 | -0.043 | 0.03524 | 0.04379 | 0.5438 | 0.605% | 1.300% | true | false | false | **false** |
| ETHUSDT(N20) | ASSUMED | 2718.91 | 0.001 | 0.001 | 20 | 0.008 | 21.75 | 5bp | 5bp | 0.20% | 0.448% | 2.00 | 1.07 | -0.043 | 0.14097 | 0.04379 | 2.1751 | 0.001% | 1.300% | false | false | false | **false** |
| FILUSDT | INFERRED | 1.07 | 0.1 | 0.1 | 5 | 4.7 | 5.03 | 5bp | 10bp | 0.30% | 1.982% | 2.00 | 1.61 | +0.394 | 0.11475 | 0.04379 | 0.5029 | 0.571% | 1.950% | false | true | true | **false** |
| INJUSDT(N5) | ASSUMED | 7.31 | 0.001 | 0.001 | 5 | 0.684 | 5.00 | 5bp | 10bp | 0.30% | 2.512% | 2.00 | 1.68 | +0.579 | 0.14063 | 0.04379 | 0.5000 | 0.576% | 1.950% | false | true | true | **false** |
| INJUSDT(N20) | ASSUMED | 7.31 | 0.001 | 0.001 | 20 | 2.736 | 20.00 | 5bp | 10bp | 0.30% | 2.512% | 2.00 | 1.68 | +0.579 | 0.56250 | 0.04379 | 2.0000 | 0.000% | 1.950% | false | true | true | **false** |
| LINKUSDT | OBSERVED | 15.302 | 0.01 | 0.01 | 20 | 1.31 | 20.05 | 5bp | 10bp | 0.30% | 2.005% | 2.00 | 1.61 | +0.402 | 0.46215 | 0.04379 | 2.0046 | 0.000% | 1.950% | false | true | true | **false** |
| LTCUSDT | OBSERVED | 70.04 | 0.001 | 0.001 | 20 | 0.286 | 20.03 | 5bp | 10bp | 0.30% | 0.818% | 2.00 | 1.20 | -0.014 | 0.22396 | 0.04379 | 2.0031 | 0.000% | 1.950% | false | false | false | **false** |
| NEARUSDT | OBSERVED | 5.389 | 1 | 1 | 5 | 1 | 5.39 | 5bp | 10bp | 0.30% | 2.439% | 2.00 | 1.67 | +0.553 | 0.14758 | 0.04379 | 0.5389 | 0.513% | 1.950% | false | true | true | **false** |
| OPUSDT | INFERRED | 0.12588 | 0.1 | 0.1 | 5 | 39.8 | 5.01 | 5bp | 10bp | 0.30% | 1.523% | 2.00 | 1.51 | +0.233 | 0.09132 | 0.04379 | 0.5010 | 0.574% | 1.950% | false | false | true | **false** |
| PEOPLEUSDT | INFERRED | 0.008862 | 1 | 1 | 5 | 565 | 5.01 | 5bp | 10bp | 0.30% | 1.644% | 1.99 | 1.53 | +0.268 | 0.09734 | 0.04379 | 0.5007 | 0.575% | 2.000% | false | false | true | **false** |
| SEIUSDT(N5) | ASSUMED | 0.07491 | 0.001 | 0.001 | 5 | 66.747 | 5.00 | 5bp | 10bp | 0.30% | 1.758% | 2.00 | 1.56 | +0.316 | 0.10292 | 0.04379 | 0.5000 | 0.576% | 1.950% | false | false | true | **false** |
| SEIUSDT(N20) | ASSUMED | 0.07491 | 0.001 | 0.001 | 20 | 266.988 | 20.00 | 5bp | 10bp | 0.30% | 1.758% | 2.00 | 1.56 | +0.316 | 0.41169 | 0.04379 | 2.0000 | 0.000% | 1.950% | false | false | true | **false** |
| SOLUSDT | INFERRED | 119.89 | 0.01 | 0.01 | 5 | 0.05 | 5.99 | 5bp | 5bp | 0.20% | 0.671% | 2.00 | 1.31 | +0.035 | 0.05223 | 0.04379 | 0.5995 | 0.531% | 1.300% | false | false | true | **false** |
| SUIUSDT(N5) | ASSUMED | 1.1868 | 0.001 | 0.001 | 5 | 4.214 | 5.00 | 5bp | 10bp | 0.30% | 1.792% | 2.00 | 1.57 | +0.327 | 0.10461 | 0.04379 | 0.5001 | 0.576% | 1.950% | false | false | true | **false** |
| SUIUSDT(N20) | ASSUMED | 1.1868 | 0.001 | 0.001 | 20 | 16.853 | 20.00 | 5bp | 10bp | 0.30% | 1.792% | 2.00 | 1.57 | +0.327 | 0.41838 | 0.04379 | 2.0001 | 0.000% | 1.950% | false | false | true | **false** |
| TRXUSDT | INFERRED | 0.33422 | 1 | 1 | 5 | 15 | 5.01 | 5bp | 10bp | 0.30% | 0.222% | 2.00 | 0.28 | -0.222 | 0.02618 | 0.04379 | 0.5013 | 0.574% | 1.950% | true | false | false | **false** |
| UNIUSDT(N5) | ASSUMED | 9.982 | 0.001 | 0.001 | 5 | 0.501 | 5.00 | 5bp | 10bp | 0.30% | 1.574% | 2.00 | 1.52 | +0.251 | 0.09370 | 0.04379 | 0.5001 | 0.576% | 1.950% | false | false | true | **false** |
| UNIUSDT(N20) | ASSUMED | 9.982 | 0.001 | 0.001 | 20 | 2.004 | 20.00 | 5bp | 10bp | 0.30% | 1.574% | 2.00 | 1.52 | +0.251 | 0.37478 | 0.04379 | 2.0004 | 0.000% | 1.950% | false | false | true | **false** |
| XRPUSDT(N5) | ASSUMED | 1.5181 | 0.001 | 0.001 | 5 | 3.294 | 5.00 | 5bp | 10bp | 0.30% | 1.015% | 2.00 | 1.32 | +0.055 | 0.06577 | 0.04379 | 0.5001 | 0.576% | 1.950% | false | false | true | **false** |
| XRPUSDT(N20) | ASSUMED | 1.5181 | 0.001 | 0.001 | 20 | 13.175 | 20.00 | 5bp | 10bp | 0.30% | 1.015% | 2.00 | 1.32 | +0.055 | 0.26307 | 0.04379 | 2.0001 | 0.000% | 1.950% | false | false | true | **false** |

## 2. Frontier: passing (R, stop) cells per equity (all five gates)

Cell = gross R:R x stop. Count of passing cells out of 54 and the passing set at current equity.

| symbol | E=8.7583 | E=10 | E=15 | E=20 | E=25 | E=50 | passing cells at current equity |
|---|---|---|---|---|---|---|---|
| AAVEUSDT | 0 | 0 | 0 | 0 | 0 | 9 | none |
| ADAUSDT(N5) | 2 | 2 | 9 | 17 | 22 | 32 | R3.5/0.5%, R4/0.5% |
| ADAUSDT(N20) | 0 | 0 | 0 | 0 | 0 | 5 | none |
| APTUSDT(N5) | 2 | 2 | 9 | 17 | 22 | 32 | R3.5/0.5%, R4/0.5% |
| APTUSDT(N20) | 0 | 0 | 0 | 0 | 0 | 5 | none |
| ARBUSDT | 2 | 2 | 9 | 17 | 22 | 32 | R3.5/0.5%, R4/0.5% |
| ATOMUSDT | 2 | 2 | 9 | 17 | 22 | 32 | R3.5/0.5%, R4/0.5% |
| AVAXUSDT | 0 | 0 | 0 | 2 | 5 | 17 | none |
| BNBUSDT | 0 | 0 | 2 | 5 | 13 | 27 | none |
| BTCUSDT | 0 | 0 | 0 | 0 | 0 | 0 | none |
| DOGEUSDT(N5) | 2 | 2 | 9 | 17 | 22 | 32 | R3.5/0.5%, R4/0.5% |
| DOGEUSDT(N20) | 0 | 0 | 0 | 0 | 0 | 5 | none |
| DOTUSDT(N5) | 2 | 2 | 9 | 17 | 22 | 32 | R3.5/0.5%, R4/0.5% |
| DOTUSDT(N20) | 0 | 0 | 0 | 0 | 0 | 5 | none |
| ETCUSDT | 0 | 0 | 0 | 0 | 0 | 5 | none |
| ETHUSDT(N5) | 4 | 4 | 12 | 21 | 26 | 36 | R3/0.5%, R3.5/0.5%, R4/0.25%, R4/0.5% |
| ETHUSDT(N20) | 0 | 0 | 0 | 1 | 1 | 8 | none |
| FILUSDT | 2 | 2 | 9 | 17 | 22 | 32 | R3.5/0.5%, R4/0.5% |
| INJUSDT(N5) | 2 | 2 | 9 | 17 | 22 | 32 | R3.5/0.5%, R4/0.5% |
| INJUSDT(N20) | 0 | 0 | 0 | 0 | 0 | 5 | none |
| LINKUSDT | 0 | 0 | 0 | 0 | 0 | 5 | none |
| LTCUSDT | 0 | 0 | 0 | 0 | 0 | 5 | none |
| NEARUSDT | 2 | 2 | 9 | 17 | 22 | 32 | R3.5/0.5%, R4/0.5% |
| OPUSDT | 2 | 2 | 9 | 17 | 22 | 32 | R3.5/0.5%, R4/0.5% |
| PEOPLEUSDT | 2 | 2 | 9 | 17 | 22 | 32 | R3.5/0.5%, R4/0.5% |
| SEIUSDT(N5) | 2 | 2 | 9 | 17 | 22 | 32 | R3.5/0.5%, R4/0.5% |
| SEIUSDT(N20) | 0 | 0 | 0 | 0 | 0 | 5 | none |
| SOLUSDT | 4 | 4 | 12 | 16 | 21 | 36 | R3/0.5%, R3.5/0.5%, R4/0.25%, R4/0.5% |
| SUIUSDT(N5) | 2 | 2 | 9 | 17 | 22 | 32 | R3.5/0.5%, R4/0.5% |
| SUIUSDT(N20) | 0 | 0 | 0 | 0 | 0 | 5 | none |
| TRXUSDT | 2 | 2 | 9 | 17 | 22 | 32 | R3.5/0.5%, R4/0.5% |
| UNIUSDT(N5) | 2 | 2 | 9 | 17 | 22 | 32 | R3.5/0.5%, R4/0.5% |
| UNIUSDT(N20) | 0 | 0 | 0 | 0 | 0 | 5 | none |
| XRPUSDT(N5) | 2 | 2 | 9 | 17 | 22 | 32 | R3.5/0.5%, R4/0.5% |
| XRPUSDT(N20) | 0 | 0 | 0 | 0 | 0 | 5 | none |

## 3. Gate-only frontier (independent of symbol): min stop required by NEXUS net R:R

| R gross | majors (RT 0.20%) | alts (RT 0.30%) | EV min stop p=0.45 majors | EV min stop p=0.45 alts | EV min stop p=0.356 alts |
|---|---|---|---|---|---|
| 1.5 | NA | NA | 1.60% | 2.40% | NA |
| 2.0 | 1.30% | 1.95% | 0.57% | 0.86% | 4.45% |
| 2.5 | 0.58% | 0.87% | 0.35% | 0.52% | 1.22% |
| 3.0 | 0.37% | 0.56% | 0.25% | 0.38% | 0.71% |
| 3.5 | 0.27% | 0.41% | 0.20% | 0.29% | 0.50% |
| 4.0 | 0.22% | 0.33% | 0.16% | 0.24% | 0.39% |

## 4. Minimum gross R:R and minimum equity per symbol (current policies)

| symbol | stop obs (median) | share of signals with R>=3 | min R for NEXUS R:R at stop obs | min equity for MIN_ORDER at stop obs | NEXUS passes at R=2 / R=3 (stop obs) |
|---|---|---|---|---|---|
| AAVEUSDT | 1.900% | 13% | 2.01 | 79.87 USDT | no / yes |
| ADAUSDT(N5) | 1.185% | 20% | 2.26 | 14.85 USDT | no / yes |
| ADAUSDT(N20) | 1.185% | 20% | 2.26 | 59.39 USDT | no / yes |
| APTUSDT(N5) | 1.148% | 33% | 2.28 | 14.49 USDT | no / yes |
| APTUSDT(N20) | 1.148% | 33% | 2.28 | 57.94 USDT | no / yes |
| ARBUSDT | 2.101% | 5% | 1.97 | 24.04 USDT | yes / yes |
| ATOMUSDT | 1.158% | 15% | 2.27 | 14.61 USDT | no / yes |
| AVAXUSDT | 1.813% | 15% | 2.03 | 46.96 USDT | no / yes |
| BNBUSDT | 0.354% | 8% | 3.80 | 10.32 USDT | no / no |
| BTCUSDT | 0.298% | 20% | 3.34 | 84.62 USDT | no / no |
| DOGEUSDT(N5) | 0.884% | 17% | 2.48 | 11.84 USDT | no / yes |
| DOGEUSDT(N20) | 0.884% | 17% | 2.48 | 47.35 USDT | no / yes |
| DOTUSDT(N5) | 1.590% | 0% | 2.09 | 18.91 USDT | no / yes |
| DOTUSDT(N20) | 1.590% | 0% | 2.09 | 75.62 USDT | no / yes |
| ETCUSDT | 1.054% | 12% | 2.34 | 54.36 USDT | no / yes |
| ETHUSDT(N5) | 0.448% | 33% | 2.76 | 7.05 USDT | no / yes |
| ETHUSDT(N20) | 0.448% | 33% | 2.76 | 28.19 USDT | no / yes |
| FILUSDT | 1.982% | 0% | 1.99 | 22.95 USDT | yes / yes |
| INJUSDT(N5) | 2.512% | 0% | 1.91 | 28.13 USDT | yes / yes |
| INJUSDT(N20) | 2.512% | 0% | 1.91 | 112.50 USDT | yes / yes |
| LINKUSDT | 2.005% | 12% | 1.99 | 92.43 USDT | yes / yes |
| LTCUSDT | 0.818% | 25% | 2.55 | 44.79 USDT | no / yes |
| NEARUSDT | 2.439% | 5% | 1.92 | 29.52 USDT | yes / yes |
| OPUSDT | 1.523% | 20% | 2.11 | 18.26 USDT | no / yes |
| PEOPLEUSDT | 1.644% | 12% | 2.07 | 19.47 USDT | no / yes |
| SEIUSDT(N5) | 1.758% | 19% | 2.04 | 20.58 USDT | no / yes |
| SEIUSDT(N20) | 1.758% | 19% | 2.04 | 82.34 USDT | no / yes |
| SOLUSDT | 0.671% | 18% | 2.37 | 10.45 USDT | no / yes |
| SUIUSDT(N5) | 1.792% | 19% | 2.04 | 20.92 USDT | no / yes |
| SUIUSDT(N20) | 1.792% | 19% | 2.04 | 83.68 USDT | no / yes |
| TRXUSDT | 0.222% | 3% | 5.11 | 5.24 USDT | no / no |
| UNIUSDT(N5) | 1.574% | 12% | 2.10 | 18.74 USDT | no / yes |
| UNIUSDT(N20) | 1.574% | 12% | 2.10 | 74.96 USDT | no / yes |
| XRPUSDT(N5) | 1.015% | 0% | 2.37 | 13.15 USDT | no / yes |
| XRPUSDT(N20) | 1.015% | 0% | 2.37 | 52.61 USDT | no / yes |

## 5. Empirical replay: real observed signals through all five gates

Each unique production setup ([STRATEGY_STOP_GEOMETRY]) with its own stop and gross R:R, symbol filters as above (ASSUMED symbols use the optimistic N5 scenario), win_prob as stated.

| win_prob | E=8.7583 | E=10 | E=15 | E=20 | E=25 | E=50 |
|---|---|---|---|---|---|---|
| 0.450 | 5/665 (0.8%) | 9/665 (1.4%) | 33/665 (5.0%) | 39/665 (5.9%) | 64/665 (9.6%) | 132/665 (19.8%) |
| 0.356 | 2/665 (0.3%) | 4/665 (0.6%) | 28/665 (4.2%) | 34/665 (5.1%) | 36/665 (5.4%) | 48/665 (7.2%) |

Gate attribution at current equity (win_prob 0.45): first failing gate per signal.

| first failing gate | signals |
|---|---|
| min_order_ok | 610 |
| nexus_rr_ok | 50 |
| nexus_ev_ok | 0 |
| final_sizing_ok | 0 |
| margin_ok | 0 |
| pass | 5 |

## 6. Cost sensitivity: STATIC_COST vs LIVE_BBO_COST (analytical, shadow only)

Live per-side slippage = half-spread + runtime impact floor (1 bp majors / 2 bp alts); spread = k x inferred tick (k=1 is a hard lower bound, k=3 conservative). Top-of-book depth assumed >= order notional (5-20 USDT orders). MIN_ORDER keeps the runtime sizing floor NEXUS_EXPECTED_SLIPPAGE_PCT. Signals: same replay as section 5, win_prob 0.45.

| cost scenario | E=8.7583 | E=10 | E=15 | E=20 | E=25 | E=50 | NEXUS-only pass |
|---|---|---|---|---|---|---|---|
| STATIC (runtime today) | 5 | 9 | 33 | 39 | 64 | 132 | 264/665 |
| LIVE_BBO 3 ticks | 7 | 11 | 60 | 133 | 200 | 282 | 467/665 |
| LIVE_BBO 1 tick | 8 | 12 | 80 | 164 | 231 | 321 | 511/665 |

