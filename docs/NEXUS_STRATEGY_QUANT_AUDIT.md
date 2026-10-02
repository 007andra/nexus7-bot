# NEXUS-7 — Quantitative strategy audit (analysis only)

Date: 2026-10-02 · Branch `claude/audit-algorithmic-trading-system-uf3zxo` · BASE `fd96c45`
(after F-002 `6be7a7b`/`4f6ca80` and F-012 `fd96c45`).
**No production code, configuration or threshold was changed for this report.**
Offline research scripts (never imported by production): `research/strategy_audit/`.

| script | what it proves |
|---|---|
| `gate_algebra.py` | closed-form behaviour of R:R, net-R:R, fee and EV gates using the production formulas/constants |
| `exit_payoff_sim.py` | realized R per scripted price path using the REAL `Position` + REAL trailing overlay and the LIVE exit rules |
| `exit_path_probe.py` | minimal reproduction of the trailing-after-partial defect (Q-01) |
| `score_redundancy_synthetic.py` | mechanical coupling of `score_tf` components on synthetic regime-switching paths |

---

## 0. DATA GAP (read first)

No historical dataset exists in the repository (no CSV/Parquet/DB/JSONL) and the
environment cannot reach exchange/market-data hosts (proxy 403 for KuCoin and Binance).
Therefore **no win rate, expectancy, PF, Sharpe, MAE/MFE, score-bucket, regime-bucket,
ATR-bucket, time-of-day, long/short or per-symbol statistic is reported**. Everything
below is either (a) exact algebra of the production code, (b) deterministic simulation of
production logic on scripted/synthetic paths, or (c) structural code analysis. Synthetic
results measure mechanics, never edge.

Data required (and how to obtain it) — §15.

---

## 1. Executive summary

The live NEXUS strategy is a single 15m trend-continuation strategy (4H regime + 4H/1H
EMA alignment + 15M trigger) with fixed ATR-multiple stops/targets. The code analysis
identifies concrete mechanisms that reduce or obscure net expectancy:

1. **The net-R:R gate is the real entry filter, and it filters on stop width, not on edge.**
   With round-trip cost 0.22 % (`nexus_ai.expected_value`), `rr_net ≥ 1.6` requires a stop
   ≥ **1.43 %** of price for MOMENTUM/PULLBACK (gross R=2) and ≥ **0.41 %** for BOS_BREAK
   (R=3). Equivalent ATR_eff thresholds: 0.95 % / 0.72 % / 0.34 %. Hypothesis: the funnel is
   biased toward breakout entries and high-volatility symbols/periods (`gate_algebra.py`).
2. **The strategy R:R gate and the EV gate are non-discriminatory.** Geometry fixes gross R:R
   at 3/2/2 so `MIN_RR_RATIO=2.0` always passes; wherever net-R:R passes, EV>0 needs ensemble
   confidence > 18.8 only (P(win) = 0.30 + 0.45·conf is a fixed heuristic).
3. **The LIVE exit stack caps average winners far below the target.** Real-code simulation:
   for R=2 setups the best possible outcome is **+1.51R gross / +1.28R net** (at a 1 % stop);
   a trade reaching +1R and returning nets **+0.28R**; a stop-out nets −1.22R. Implied
   two-outcome breakeven win rate ≈ 49 % net.
4. **Defect Q-01 (new):** after the 50 % partial, `Position.peak_pnl` stays at full-size
   PnL while `qty` halves; `calc_trailing_sl` (peak/qty) doubles the excursion and proposes a
   LONG stop at +1.5R while price is +1R. The exchange adapter rejects it every cycle, so
   trailing is inert between +1R and +1.5R, and the "2R" exit cannot fire after break-even
   (`|entry − sl| = 0`).
5. **Costs are large relative to admissible stops:** at the minimum BOS stop (0.41 %) one round
   trip costs ≈ **0.54R**; at 1 % ≈ 0.22R.
6. **Score structure:** `trend_s` explains ~55 % of `score_tf` variance (synthetic); RSI
   correlates 0.83 with `trend_s`; order-book input is absent 100 %; trend alignment is
   already a hard pre-condition, then rewarded again.
7. **Stops are partly leverage-driven, not market-driven:** CROSS-geometry hardening may
   tighten the SL to ≥40 % of its ATR distance to fit the liquidation boundary while keeping
   TP (inflating gross R:R).
8. **Sizing is not risk-based in LIVE** (F-003): loss at stop ∝ stop width. At 50× leverage
   the loss budget and the net-R:R gate together forbid every R=2 entry.
9. **Backtest is not a valid judge yet:** 1H/4H windows of 20/15 bars vs 100 live, overlapping
   trades, and the research child drops a closed candle.
10. **Regime handling is binary** (trend-only; ranging/compressed blocked), with no volatility
    regime, no BTC-regime filter for alts, and only static pair-based correlation control.

None of these is a promise of improvement; each is a hypothesis with a test design (§13).

---

## 2. Current strategy (reconstructed from executed code)

Pipeline (LIVE pilot composition; overlays in brackets):

```
KuCoin REST seed + WS limitCandle (activity = turnover idx6) [market_data_integrity]
→ closed candles by timestamp + sentinel [market_data_integrity.prepare_strategy_series]
→ engine._scan_all_and_enter: per viable symbol, skip if correlation group occupied
  (_CORR_GROUPS) or cooldown (1800 s after any close; 24 h after 3 consecutive losses)
→ Analyzer.analyze_mtf chain: [market_data_integrity → pullback_confirmation →
  adaptive_mtf_entry → core] + observe_analyze_mtf shadows
→ session penalty (ASIA, selected alts −5..−10) → regime allowed side → expected_pnl>0
→ _open: viable/durable → NEXUS decide (10 s) → balance → pilot.can_open_pilot
→ sizing (final_sizing_invariants: 50 % available margin × leverage)
→ legacy pre-trade score (advisory in LIVE [legacy_pretrade_advisory])
→ liquidation analysis [kucoin_contract_risk_hardening: CROSS MMR, SL compression,
  second NEXUS review; cross_geometry_target_policy keeps TP]
→ pre-dispatch spread ≤12 bps, drift ≤20 bps, depth ≥3× [pilot_risk_cap / pre_dispatch_guard]
→ native TP/SL entry (st-orders)
→ exits: durable partial, trailing, 2R exit, native SL/TP
  (stagnation/CHoCH/regime exits disabled in LIVE [operator_loss_policy])
```

Per trade:
1. **Setup origin** — `strategy.analyze_mtf` (`bot/strategy.py:521-753`).
2. **Indicators** — EMA20/50/200, RSI14, MACD(12,26,9), ATR14, ADX14, Bollinger(20,2), Choppiness14, VWAP (rolling 96), volume profile, SMC swings, delta footprint (price-derived).
3. **Required signals** — 4H regime ∈ {TRENDING_UP, TRENDING_DOWN}; `bull_4h ∧ bull_1h` (EMA20>EMA50 and close>EMA20) or the bear mirror; all three `score_tf` ok; RSI15 ∈ [8,92]; vol_r ≥ 0.40; entry trigger (BOS_BREAK, MOMENTUM, PULLBACK) or −5 score.
4. **Score** — `combined = round(0.25·s4h + 0.30·s1h + 0.45·s15)`; `score_tf = trend(0-30)+volume(0-20)+momentum(0-20)+volatility(0-15)+structure(0-15)`.
5. **Filters** — combined ≥ min_score (60; 72 after daily target); session penalty; regime side; expected_pnl>0; NEXUS (data quality ≥60, regime compat ≥30, ensemble direction = MTF direction, EV>0, rr_net≥1.6, NEXUS score ≥ threshold, news veto); adaptive path: combined≥72, 4H≥60, 1H≥65, 15M≥80, vol≥1.2×, ADX≥18, extension ≤2.5 ATR; pullback needs ≥4/5 reversal votes.
6. **Threshold** — strategy 60, NEXUS 60 (+15 in CHOPPY), multiplied by data-quality.
7. **Stop** — `price ∓ sl_mult·max(ATR15, 0.5·ATR1H)`; sl_mult 1.2/1.5/2.0 (BOS/MOMENTUM/PULLBACK); possibly compressed by CROSS geometry.
8. **TP** — `price ± tp_mult·same ATR`; tp_mult 3.6/3.0/4.0.
9. **R:R** — `|tp−entry|/|entry−sl|` on unrounded levels ⇒ 3.0/2.0/2.0.
10. **EV** — NEXUS: `p·(R·s − c) − (1−p)(s + c)` with `p = min(0.75, 0.30+0.45·conf/100)`, `c = 2·fee+2·slip`.
11. **Sizing** — LIVE: `floor(0.5·available·L / (price·multiplier))` contracts; RiskManager qty must be >0 only; loss budget `qty·(s+c)·price ≤ 0.5·margin`.
12. **Later exits** — §7.

## 3. Score engine

### 3.1 Strategy `score_tf` (per timeframe, core `strategy.py:308-517`; LIVE overlay `scoring_safety_hardening`)

| feature | points | range | purpose | coupling (synthetic) | actual influence |
|---|---|---|---|---|---|
| ADX level (requires EMA alignment; overlay also requires ADX direction) | 4/7/10 | 0-10 | trend strength | part of trend_s | in trend_s (55 % of variance) |
| EMA stack (aligned / full 20>50>200) | 6/10 | 0-10 | trend | `aligned` already a hard pre-filter at 4H/1H | double counted |
| SMC BOS / HH-HL | 6 / 4 | 0-10 | structure as trend | part of trend_s | — |
| VWAP side (rolling 96 bars) | 3 | 0-3 | location | 50 % true (synthetic) | low |
| CHoCH penalty / ADX<20 cap | −5 / cap 10 | | | | |
| Volume ratio + last-3 body direction | 2..20 | 2-20 | participation | ρ(vol_s,total)=0.55 | 14 % |
| Order-book imbalance | +2 | | liquidity | **orderbook never passed (100 % N/A)** | **zero** |
| Volume profile POC side | +2 | | location | | low |
| RSI band | 0..10 | | momentum | ρ(RSI, trend_s)=0.83 | in momentum_s |
| MACD histogram sign/slope | 0..10 | | momentum | | in momentum_s (21 %) |
| Delta footprint (from closes/opens) | ±3 | | "order flow" (price-derived) | | low |
| ATR expanding / Choppiness / BB squeeze | 3..15 | 3-15 | volatility | 6 % | low |
| Wick, fake-break, CHoCH, chop penalties | 15 − … | 0-15 | structure | 4 % | low |

Weights across TF: 25 % 4H / 30 % 1H / 45 % 15M, after 4H and 1H alignment is already required.

### 3.2 NEXUS score (`nexus_ai._score_components`, weights `WEIGHTS`)
TREND 15, MOMENTUM 10, VOLUME 10, MARKET_STRUCTURE 15, VOLATILITY 10, DERIVATIVES 10,
MICROSTRUCTURE 10 (always 0 ⇒ excluded and renormalized), MULTI_TIMEFRAME 10, RISK_REWARD 10.
`RISK_REWARD = min(100, rr_net/2·100)` reuses the same geometry that already gated entry;
then `− 0.2·risk`, ± news (≤5), × data_quality/100.

### 3.3 Pre-trade legacy score (`score.calculate`)
Technical + order flow + macro + news. `oi_previous` is always 0 (`score.py:345`) ⇒ OI change
can never be measured (constant +5 "neutral"). In LIVE this score is advisory
(`legacy_pretrade_advisory`).

**Findings:** double counting (trend alignment pre-filter + trend_s + EMA in NEXUS TREND + MTF);
feature without effect (order book; MICROSTRUCTURE; OI delta); feature computed but ignored in
core (`adx_aligned`; overlay fixes); saturation risk (trend_s cap 30 reached easily in trends);
three independent scores (strategy, NEXUS, legacy) with three thresholds and no calibration
linking any of them to outcomes.

## 4. Prior audit items — confirmation

| item | status | location | explanation | impact |
|---|---|---|---|---|
| `ob_ok` always False | **CONFIRMED** (core) | `strategy.py:381-386`, `analyze_mtf` never passes `orderbook` | synthetic run: `ob_bias=N/A` 100 % | 2 points unreachable; dead feature |
| `adx_aligned` ignored | **PARTIAL** | core `strategy.py:331,331-345` unused; `scoring_safety_hardening` requires ADX direction in LIVE | EMA-aligned agrees with ADX direction 86 % (synthetic) | low incremental info |
| R:R tautological | **CONFIRMED** | `strategy.py:665-685` | fixed multipliers ⇒ 3/2/2 | gate never filters |
| NEXUS probability linear | **CONFIRMED** | `nexus_probability.py:17` | `min(0.75, 0.30+0.45·conf)` | not a probability |
| EV gate rarely blocks | **CONFIRMED** | `nexus_ai.py:629-635` vs `:650-659` | where rr_net passes, EV needs conf>18.8 | non-binding; rr_net is the binding gate |

## 5. Indicator audit

| indicator | expected | implemented | warm-up | correct? | used? | note |
|---|---|---|---|---|---|---|
| EMA | SMA-seeded EMA | seeded with first value | ≥3·period advisable | biased on short series | yes | EMA50 on 4H with 99 bars live / 14 in backtest |
| SMA | — | not separate | | | | |
| RSI14 | Wilder | Wilder, SMA seed | 14 | **flat series → 100 (should be 50)** | yes | `al==0 ⇒ rs=1e9` even if `ag==0` |
| MACD | EMA12−EMA26, signal 9 | same with first-value EMAs | ~35 | ok after warm-up | yes | |
| ATR14 | Wilder | Wilder | 14 | **IndexError <14 bars; zeros before index 13** | yes | 20-bar average contaminated when n<34 |
| ADX14 | Wilder | Wilder | 28 | **IndexError for 16–27 bars** | yes | guard `n<period+2` insufficient |
| Bollinger(20,2) | pop. std | pop. std | 20 | ok | yes | squeeze vs 20 historical widths |
| Choppiness14 | log10(ΣTR/range)/log10(n) | same | 15 | ok | yes | |
| VWAP | session-anchored | rolling 96 bars (no timestamps passed) on every TF | | mislabeled | yes | 96 bars = 24 h on 15M, 16 days on 4H |
| Volume | contract volume | **turnover (USDT) idx6** in LIVE | | ok as activity | yes | REST/WS unified by overlay |
| Delta footprint | trade-side volume | price-direction proxy | | not order flow | yes | label misleading |
| Order book | depth20 | fetched only in legacy pre-trade | | — | advisory | |
| SMC/structure | swing pivots | 2-bar fractals / HH-HL on recent bars | | heuristic | yes | |

Per the brief: no parameter optimization is proposed on RSI/ATR/ADX/EMA until the defects are fixed (Phase 1 of §14).

## 6. Feature families and redundancy

TREND: EMA alignment (4H, 1H hard filter), EMA stack, ADX level, SMC BOS/HH-HL, VWAP side, NEXUS TREND/MTF/STRUCTURE models, 4H regime.
MOMENTUM: RSI, MACD, delta proxy, NEXUS MOMENTUM, MOMENTUM entry type.
VOLATILITY: ATR expansion, BB width, Choppiness, NEXUS RISK_REGIME, COMPRESSED regime.
VOLUME: vol_r, POC side, NEXUS BREAKOUT vol_mult.
LIQUIDITY: pre-dispatch spread/depth only (not in score).
ORDER FLOW: none real (order book unused; delta is price-derived; OI delta unmeasurable).
REGIME: strategy `detect_regime` (4H), NEXUS `detect_regime` (15M), engine `_REGIME_PARAMS` (only `allowed_sides` used).
MEAN REVERSION: NEXUS MEAN_REVERSION model only.

Synthetic structural evidence (`score_redundancy_synthetic.py`, 1500 samples): ρ(trend_s,total)=0.87, ρ(RSI,trend_s)=0.83, ρ(momentum_s,RSI)=0.63; trend_s + momentum_s ≈ 76 % of variance; ATR + structure ≈ 10 %. Real-data correlation and mutual information require the dataset in §15.

## 7. Exit logic (LIVE pilot) and its payoff

Active: native SL (st-orders, trigger price type `TP`), native TP at strategy TP, durable
partial (`durable_partial_exit.py`: profit ≥ |entry−sl| + 3 bps ⇒ close 50 % of
`qty_original`, then SL→entry), trailing (`engine._apply_trailing_stops` + overlay: trigger at
50 % of target distance, giveback 25 % of peak favorable excursion; moved stops use `MP`
trigger), 2R exit (`confirmed_rr_exit.py`: profit ≥ 2·|entry − current sl|).
Disabled in LIVE: stagnation, CHoCH invalidation, regime-change exits (`operator_loss_policy`).

Simulation with real classes (`exit_payoff_sim.py`, stop = 1 % of price, fee+slip 0.11 % per fill):

| path | R=2 gross / net | R=3 gross / net |
|---|---|---|
| straight to stop | −1.00 / −1.22 | −1.00 / −1.22 |
| +0.5R then stop | −1.00 / −1.22 | −1.00 / −1.22 |
| +1R then back to entry | +0.51 / +0.28 | +0.52 / +0.29 |
| +1.4R then reverse | +0.51 / +0.28 | +0.52 / +0.29 |
| +1.8R then reverse | +1.26 / +1.04 | +1.29 / +1.07 |
| straight to target | +1.51 / +1.28 | +2.02 / +1.79 |

Observations:
- **Q-01 trailing defect** (`trailing_safety_hardening.calc_trailing_sl` uses `peak_pnl/qty`; `peak_pnl` not rescaled when `durable_partial_exit` sets `pos.qty = remaining`). Repro: `exit_path_probe.py` → stop +1.5R proposed at price +1R; `native_stop_repair` rejects (`invalid_trigger_side`), retried every loop (extra REST reads).
- **Q-02 "2R" exit is not 2R**: it uses the *current* stop distance; after BE it never fires; after trailing to +1.5R it would require +3R.
- **Q-01 / Q-02 FIXED (2026-10-02):** `bot/exit_geometry.py` measures exits in PRICE/R units independent of remaining qty. `Position` carries an immutable `initial_sl` (1R for the whole trade) and `peak_price` (best price seen, maintained by `update_pnl`). Trailing (`trailing_safety_hardening`, core `calc_trailing_sl`) uses `peak_price − entry`; partial (durable/PAPER/core) and 2R (`confirmed_rr_exit`, core) use `|entry − initial_sl|`; thresholds unchanged. A candidate stop on the invalid side of the market is never returned (`[TRAILING_REJECTED_INVALID_SIDE]`). Positions rebuilt from exchange rows (restart/orphan/external) have `initial_sl=None` → partial/2R fail closed, protection and trailing stay. PAPER durable record persists `initial_sl`/`peak_price`; legacy records restore `initial_sl=None` and a conservative peak (`peak_pnl/qty_original`). Simulator (`exit_payoff_sim.py`, `mode=before|after`): invalid triggers 5–79 per path → 0; "+1.4R then reverse" (R=2) gross +0.51R → +1.04R; **R=3 "straight to target" +2.02R → +1.52R because the 2R full exit now actually fires** (it was dead after BE) — a strategy-level consequence of the rule as written, to be decided under H-XX. Tests: `tests/test_exit_geometry*.py`.
- **Q-01B (open):** LIVE restart cannot recover a position's initial stop or MFE (no durable LIVE position record; lineage v2 stores entry only) → after restart R exits fail closed for that position.
- **Q-01C FIXED (2026-10-02):** `bot/stop_monotonic.decide_stop` (pure; tick-quantized) is the single rule INV-STOP-MONOTONIC-001 — LONG stops only up, SHORT only down; BE is a floor; worse/equal-after-rounding/invalid-side candidates are no-ops (no POST, no cancel); first protection allowed when no stop exists; unknown current protection is never assumed improvable. Applied to the LIVE (`durable_partial_exit`), PAPER (`partial_tp_execution_hardening`) and core break-even paths and to `_apply_trailing_stops`; `native_stop_repair.set_stops` additionally refuses to replace a more protective active BGX stop of the same lineage (exchange truth, covers stale local state after restart). Logs `[STOP_IMPROVEMENT_ACCEPTED|SKIPPED]`. `risk.check_partial_tps` (documented as never called, byte-protected by `test_release_source_regression`) is unchanged. Simulator path A (trail ≈+0.75R, partial, fast reversal before the next trailing cycle): gross +0.52R → +0.67R, protection kept 0.77R, one stop replacement avoided; path B (stop below BE): BE still applied. Tests: `tests/test_stop_monotonic*.py`.
- **Q-01C (original description):** when trailing activates before the partial (trigger 0.5×target ≤ 1R for R=2), the BE move after the partial LOWERS a LONG stop from ≈+0.75R to entry (sim rows "+1R"/"+1.4R": `TRAIL->+0.75R` then BE). BE should probably never loosen an existing stop; not changed (stop policy out of scope).
- **Q-01D (open, accounting):** PAPER partial realized PnL is approximated as `stop_distance × partial_qty` (`paper_wallet`, `partial_tp_execution_hardening`), not `(fill − entry) × qty`; journal `rr_achieved` still divides by the current stop distance.
- **Q-03 BE is not break-even**: SL=entry ignores ~2·(fee+slip) ⇒ the "BE" half loses ≈0.11R–0.27R depending on stop width.
- Average winner for R=2 setups is capped at ≈1.5R gross because only half the position can reach TP.
- Rare ordering edge: trailing can activate before the 3 bps partial threshold and the subsequent BE move lowers that stop (sim row "+1R": `TRAIL->+0.75R` then BE).

## 8. MTF / regime

Timeframes: 4H regime (strategy `detect_regime`: ADX>20 ⇒ trend by price vs EMA20, ignoring DI), 4H+1H EMA direction, 15M trigger/score weight 45 %. NEXUS adds its own 15M regime and a 4H/1H/15m EMA MTF check (conflict ⇒ WAIT). Counter-trend entries are structurally prevented (both 4H and 1H must agree), except NEXUS transition cohort (25<ADX≤35).
Regime classes in strategy: TRENDING_UP/DOWN, RANGING, COMPRESSED, CHOPPY (only trending trades). NEXUS: TRENDING_BULL/BEAR, BREAKOUT/BREAKDOWN, RANGE, ACCUMULATION, DISTRIBUTION, HIGH/LOW_VOLATILITY, CHOPPY, EXTREME_EVENT. No STRONG vs WEAK trend, no volatility percentile, no BTC regime. Same entry rules apply in high and low volatility (only the net-R:R stop-width effect differentiates them, implicitly).

## 9. Backtest parity matrix

| feature | LIVE | BACKTEST (`backtest._run_strategy`) | match |
|---|---|---|---|
| indicators | same functions, 100 bars/TF | same functions, 60 / 20 / 15 bars (15M/1H/4H) | **NO** (warm-up) |
| closed candles | timestamp-closed + sentinel | closed windows; in research child (`-S`) core drops one more | PARTIAL (in-process yes, child no) |
| overlays (adaptive MTF, pullback votes, scoring safety) | installed | in-process yes; research child no | PARTIAL |
| NEXUS gate (EV, rr_net, score, regime) | yes | **no** | **NO** |
| session/regime/correlation/cooldown filters | yes | no | NO |
| entry price | market fill after ~seconds | next 15M open + adverse slippage | ~ |
| stop/TP | ATR geometry, CROSS compression possible | ATR geometry, no compression | PARTIAL |
| exits | partial 1R + BE, trailing (Q-01), 2R-on-current-stop, native TP | partial at TP1 (=TP when tp1=tp ⇒ no partial), BE after TP1, no trailing, 40-bar timeout | **NO** |
| sizing | 50 % available margin × L | per-trade return on notional | NO |
| overlap | MAX_POSITIONS, correlation groups | unlimited overlapping trades | **NO** |
| fees/slippage/funding | live fee, ticker spread | fee, symbol slippage, funding events | ~ |

Conclusion: backtest reliability **NO** for strategy comparison until parity work (Phase 2 of §14).

## 10. Cost model

Round trip used by NEXUS fallback: 2·6 bps + 2·5 bps = **22 bps**; strategy fee gate uses 17 bps (12 fee + 4 slippage + 1 funding). Cost in R units = c/s: 0.54R at s=0.41 %, 0.22R at 1 %, 0.15R at 1.43 %. `cost_to_edge_ratio = c / (R·s)` is not computed anywhere; the strategy fee gate requires only `move_to_tp ≥ 2·cost`. Funding: modeled in backtest; in LIVE only via legacy score (advisory) and the (disabled) stagnation exit.

## 11. Sizing analysis (quantification only; F-003 policy unchanged)

LIVE loss at stop as a fraction of **available** collateral: `0.5·L·(s + c)`.

| leverage | s=0.41 % (min BOS) | s=1.0 % | s=1.43 % (min R=2) | s=3 % |
|---|---|---|---|---|
| 10× | 3.2 % | 6.1 % | 8.3 % | 16.1 % |
| 50× | 15.8 % | 30.5 % ⇒ blocked by loss budget (25 %) | 41 % ⇒ blocked | blocked |

So risk per trade varies ~5× with stop width (wider stop ⇒ larger loss), the opposite of
risk-normalized sizing. At 50× the loss budget (`s + c ≤ 1 %`) and net-R:R (R=2 needs
s ≥ 1.43 %) are jointly unsatisfiable for MOMENTUM/PULLBACK: only BOS with
0.41 % ≤ s ≤ 0.78 % can trade. Risk-based alternative: `qty = equity·r / (price·(s + c))` gives
constant loss r for every stop width.
Score-based sizing: not recommended — no evidence of score monotonicity exists (§15).

## 12. Entry quality, portfolio risk

- Chasing: MOMENTUM trigger = last closed candle in direction with body > 0.25·ATR (enters after the impulse); BOS_BREAK = close above 20-bar high by >0.1·ATR. Only the adaptive path has an extension cap (2.5 ATR). Core path has no distance-to-EMA/VWAP/structure filter.
- Achievable R:R: targets are ATR multiples; `find_support_resistance`/`calc_sl_tp` exist but are unused, so a 2R target can sit beyond nearby resistance.
- Liquidity: LIVE pre-dispatch spread ≤ 12 bps, drift ≤ 20 bps, depth ≥ 3× (good; not in backtest).
- Signal decay: NEXUS ≤10 s + drift check ≤20 bps (good).
- Cooldown: hard-coded 1800 s after any close, 24 h after 3 consecutive losses (in memory, reset on restart); `COOLDOWN_SECONDS` unused.
- Portfolio: static groups (`engine._CORR_GROUPS`, includes symbols not traded, omits NEAR/ATOM); BTC long + SOL long allowed simultaneously; no beta/same-side aggregation; `cross_portfolio_stress` checks margin at stop, not correlation.
- BTC regime for alts: none (BTC dominance only in advisory legacy score).

## 13. Hypotheses H-01 … H-15

Common test protocol (TP): parity-fixed replay engine (§14 Phase 2) on ≥12 months of 15m/1h/4h for the 12 symbols; costs as LIVE (fee from account, spread from ticker history, funding events); one position per symbol and MAX_POSITIONS enforced; walk-forward 6 rolling folds (train 6 m / validate 2 m / OOS 2 m); report net expectancy (R and %), PF, MDD, Sharpe/Sortino/Calmar, trade count, fees, consecutive losses, with bootstrap 95 % CI; acceptance requires OOS CI of the expectancy difference excluding 0 and MDD not worse.

**H-01 Risk-based sizing.** Current: 50 % margin × L; loss ∝ s+c (§11). Problem: non-uniform risk, wide-stop trades dominate losses. Change: `qty = equity·r/(price·(s+c))`, capped by current margin policy. Why: equalizes per-trade risk; improves risk-adjusted metrics, not gross edge. Data: OHLCV + fills. Test: same signals, two sizing rules, compare MDD/Calmar/CI. Benefit: lower drawdown variance. Negative: smaller size on wide stops. Overfitting: low. Complexity LOW. Op. risk MEDIUM (touches sizing; F-003 decision). Confidence HIGH (math). **TEST** (after F-003 decision).

**H-02 Market regime engine.** Current: binary trend/no-trend; two inconsistent classifiers. Change: one classifier (ADX+DI, ATR percentile, EMA slope, BB width) shared by strategy/NEXUS/backtest. Data: OHLCV. Test: bucket baseline trades by regime; only then gate. Negative: fewer trades. Overfitting: medium. Complexity MEDIUM. Op. risk LOW (shadow first). Confidence MEDIUM. **NEED MORE DATA**.

**H-03 MTF confirmation.** Current: 4H+1H EMA hard filter + 25 % 4H weight. Change: use HTF only as filter, remove HTF weight from score. Test: ablation (score with/without 4H term). Overfitting low. Complexity LOW. Confidence MEDIUM. **TEST**.

**H-04 ATR extension / chasing filter.** Current: no extension cap on core path; MOMENTUM enters after impulse. Change: block when `|entry − EMA20| / ATR > X`; X chosen from the OOS distribution (expectancy by extension decile), not a priori. Data: OHLCV + signals. Overfitting medium (X). Complexity LOW. Confidence MEDIUM. **TEST**.

**H-05 ADX as regime, not score.** Current: ADX adds 4–10 points and ADX>20 picks trend direction from price vs EMA20 ignoring DI. Change: ADX+DI only in regime classification. Test: ablation. Complexity LOW. Confidence MEDIUM. **TEST**.

**H-06 Score calibration.** Current: no link between any score and outcomes. Change: score buckets → empirical P(win) with isotonic/Platt + Bayesian shrinkage; refit per fold. Data: ≥300 labelled trades. Overfitting medium. Complexity MEDIUM. Confidence LOW until data. **NEED MORE DATA**.

**H-07 Real EV gate.** Current: EV non-binding (§4). Change: EV with calibrated P(win) (H-06), realized-exit payoff (§7, not TP) and live costs; block EV ≤ k·cost. Depends on H-06. Complexity MEDIUM. Confidence MEDIUM. **TEST** after H-06.

**H-08 Structure-based achievable R:R.** Current: ATR targets; S/R code unused. Change: `effective_RR = min(target, nearest opposing structure)/risk`; block if < threshold. Test: replay with/without; count blocked trades where resistance < 1R. Overfitting low–medium. Complexity MEDIUM. Confidence MEDIUM. **TEST**.

**H-09 Net break-even.** Current: SL→entry (Q-03). Change: `be = entry ± (2·fee + 2·slip)·entry`. Why: BE half currently nets −(0.11..0.27)R. Overfitting none (cost-derived). Complexity LOW. Op. risk LOW. Confidence HIGH (arithmetic). **TEST** (A/B on replay; expect fewer net-negative "BE" exits, slightly more BE stop-outs).

**H-10 Trailing by regime / fix Q-01.** Current: trailing inert +1R..+1.5R (Q-01), fixed 25 % giveback. Change: (a) rescale peak on qty change (bug fix, separate patch), then (b) compare 1/1.5/2 ATR, Chandelier, swing trailing per regime. Complexity LOW (a) / MEDIUM (b). Confidence HIGH (a defect) / LOW (b). **TEST** (a first).

**H-11 Partial exit via MFE.** Current: 50 % at 1R, cap ≈1.5R gross winners for R=2. Variants A–E of the brief (100 % at TP; 50 %@1R+trail; 25 %@1R+trail; no partial+trail; regime-adaptive). Data: MFE/MAE per trade. Overfitting medium. Complexity LOW. Confidence MEDIUM. **NEED MORE DATA** (MFE).

**H-12 Liquidity/spread filter in strategy and backtest.** Current: pre-dispatch only (LIVE). Change: model the same thresholds in replay and add cost_to_edge_ratio ≤ k. Complexity LOW. Confidence MEDIUM. **TEST**.

**H-13 Portfolio correlation gate.** Current: static pairs. Change: rolling BTC-beta / correlation, cap same-side beta-weighted exposure. Complexity MEDIUM. Op. risk LOW. Confidence MEDIUM (MDD). **TEST**.

**H-14 BTC regime filter for alts.** Current: none. Change: block alt LONG when BTC 4H is TRENDING_DOWN or in crash regime (and mirror). Test: bucket alt trades by BTC regime first. Overfitting medium. Complexity LOW. Confidence LOW. **NEED MORE DATA**.

**H-15 Long/short calibration.** Current: symmetric rules (RSI bands mirrored). Change: separate calibration per side if bucket analysis shows asymmetry. Overfitting high (halves sample). Complexity LOW. Confidence LOW. **NEED MORE DATA**.

Also derived from the evidence: **H-16 (new)** rationalize the net-R:R gate: express it as `cost_to_edge_ratio = c/(R·s) ≤ k` explicitly and evaluate per entry type, since today it silently removes most MOMENTUM/PULLBACK entries in normal volatility (§1.1). **TEST**.

## 14. Priority and roadmap

TIER A (high value / low complexity): H-09 (net BE), H-10a (Q-01 fix — defect), H-03, H-05, H-04 (study), H-12, H-16.
TIER B (high value / higher complexity): H-01 (needs F-003 decision), H-08, H-13, H-07 (after H-06), H-02.
TIER C (experimental): H-11 variants, H-14, H-15, H-06 until ≥300 trades.
REJECTED for now: score-based sizing (no monotonicity evidence); adding new indicators (no incremental-information evidence); ML (no labelled dataset).

Roadmap (order changed from the brief where the code justifies it):
1. **Instrument data collection** (§15) — moved first because no dataset exists.
2. Fix indicators (RSI flat, ATR/ADX warm-up, EMA seeding/warm-up guards) and Q-01 — defects bias any later measurement.
3. Backtest parity (§9): shared windows (100 bars), NEXUS gate in replay, LIVE exit stack, no overlap, MAX_POSITIONS, correlation, costs.
4. Baseline (all metrics, CI) and bucket analyses (score, regime, ATR, extension, side, symbol, hour).
5. Cost/edge gates (H-16, H-12) and real EV (H-07 after H-06).
6. Exits (H-09, H-10, H-11) — likely the largest payoff lever per §7.
7. Regime/MTF (H-02, H-03, H-05, H-14).
8. Entry quality (H-04, H-08).
9. Risk-based sizing (H-01) and portfolio (H-13).
10. Walk-forward + Monte Carlo + sensitivity (ADX 18/20/22/25/28, multipliers ±10 %).
11. Paper (≥4 weeks, same build) → 12. controlled LIVE.

## 15. Data gaps and instrumentation

Needed: OHLCV 15m/1h/4h ≥12 months per symbol (+ BTC), funding history, order-book/spread snapshots at signal time, every evaluated signal (features, all three scores, gate outcomes incl. blocked reason), NEXUS decision payloads, fills (price, size, fee, timestamps), exits with reason, realized PnL from exchange, per-trade MAE/MFE (from 1m or tick), latency (signal→ack→fill).
Already partially available in LIVE logs/DB: `[AI_DECISION]`, `[FINAL_LOSS_BUDGET]`, post-trade forensics lineage, shadow cohorts, `db.save_signal`/`log_decision`, accounting evidence. Missing: persisted per-trade MAE/MFE, feature vectors at decision time, blocked-signal forward outcomes for the main path (shadows cover only specific cohorts), exported OHLCV store. Instrument: a decision-time feature snapshot table and an MAE/MFE tracker keyed by opening order id (observability only).

## 16. Strategy V2 concept (not implemented)

```
MARKET DATA → VALIDATION (gaps, staleness) → CLOSED-CANDLE CHECK (timestamp)
→ REGIME (single shared classifier: trend strength+DI, vol percentile, BTC regime)
→ MTF CONTEXT (HTF filter only) → SETUP DETECTION (regime-specific playbook)
→ ENTRY QUALITY (extension in ATR, distance to structure) → LIQUIDITY (spread/depth)
→ SIGNAL SCORE (non-redundant families) → CALIBRATED P_WIN (per fold)
→ TECHNICAL STOP (structure + ATR buffer) → ACHIEVABLE TARGET (structure-capped)
→ TRUE RR and REALIZED-EXIT PAYOFF → COST MODEL (live fee/spread/funding)
→ NET EV (k·cost margin) → PORTFOLIO EXPOSURE (beta-weighted) → RISK-BASED SIZING → EXECUTION
```
