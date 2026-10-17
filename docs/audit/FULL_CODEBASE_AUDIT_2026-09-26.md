# NEXUS-7 — Full codebase audit (2026-09-26)

Audited HEAD: `24b8148` (`migration/binance-usdm`, Merge PR #418).
Branch: `audit/full-codebase-line-by-line`.
Per-file table (all 567 versioned files): `docs/audit/FULL_CODEBASE_INVENTORY_2026-09-26.md`.

## 0. Method and honest coverage statement

| technique | scope |
|---|---|
| AST import graph (top-level + function-local imports) | all 520 `.py` files |
| Runtime bootstrap capture (`import main_hardened` with the repo `sitecustomize`) | 3 configs: Binance LIVE-shaped (dummy tokens, no network), Binance PAPER, KuCoin PAPER |
| Lazy-reachability closure from loaded modules | all `bot/` |
| String/dynamic-import search, workflow references, test imports | all files |
| AST scan of every `except` handler | 773 handlers in `bot/`, `main.py`, `main_hardened.py`, `sitecustomize.py` |
| Security pattern scan (eval/exec/pickle/shell/SQL f-strings/auth) | all production code + HTTP routes |
| Monkey-patch / wrapper stacking census | all `install()` functions (91) |
| Production evidence (read-only Railway logs and service config) | `nexus7-bot` service, production env |
| Manual line-level reading | decision → risk → sizing → dispatch → protection → reconciliation paths, `binance.py` order/read paths, `quantity.py`, account/capital readers, NEXUS (`nexus_ai.py`, `nexus_models.py`, `nexus_probability.py`), integrity, Telegram emitters, HTTP routes |

This was **not** a literal human read of all 56,516 production lines. Every file was
covered by the automated passes above. Manual reading was concentrated where a
defect could lose money, send a wrong order, leave a position unprotected or corrupt
state. Findings below cite code and, where available, production logs.

## 1. Executive summary

* **Two P1 defects make Binance LIVE entries impossible today.** Both fail closed, so no money is lost, but the bot cannot trade.
  1. `bot/account_capital_reader.read_account_capital` always calls the KuCoin endpoint `/api/v1/account-overview`. On Binance this is a 404 → `RuntimeError` inside `_nexus_validate` → the engine logs `decision=REJECT decision_source=exception`. **Production evidence:** 2026-09-26 17:02–17:15 UTC, DOTUSDT was approved by NEXUS (`[OPPORTUNITY_AUDIT] approved=True ... EV +1.521% R:R líquido 2.43`) and then logged `[AI_DECISION] ... decision=REJECT decision_source=exception reason=RuntimeError` four times.
  2. `bot/quantity.quantity_rules` rejects any instrument whose `lotSize`/`minQty` is not an integer ("KuCoin lotSize/minQty must be integer contracts"). `BinanceClient.load_instruments` stores base-asset `minQty` (for example BTC 0.001, DOT 0.1) and `multiplier=1`. The result: stop-risk sizing, the operator cap, the minimum-lot hook and `validate_base_quantity` all raise → qty=0 for every symbol with a fractional lot. Only integer-lot symbols (for example DOGE) could be sized.
* **No P0 defect was found** (wrong order, missing protection, risk/security bypass). Ambiguous submissions, duplicate clientOrderId, restart in OPENING, lost fencing lease and DB outage were already covered by fail-closed code and tests (PR #417 map).
* **The "AI" is a rule-based heuristic ensemble, not a trained model.** No model is trained, loaded or versioned at runtime. The "win probability" is `0.30 + 0.45·confidence/100` (capped at 0.75), and `nexus_probability.py` itself says it is "not an empirically calibrated probability". EV and the EV gate therefore rest on an uncalibrated heuristic.
* **Deployment gating is not what the repo documents.** Production service config (read-only):
  * `checkSuites=false`
  * no pre-deploy command, so `bot/ci_deploy_gate.py` is not used
  * builder `RAILPACK`, while `railway.toml` declares the hardened `Dockerfile`
  * deploys from `migration/binance-usdm` without waiting for CI
  * production logs still show pre-#417 log labels, i.e. an older commit is running
* **Architecture debt.** 91 `install()` overlays and 118 class/module attribute patches. `TradingEngine._open` is wrapped 10×, `_nexus_validate` 8×, `Analyzer.analyze_mtf` 9×, `_refresh_entry_balance` 5×, and `minimum_base_quantity` 4× (only the last one is effective).
* **OOS/backtest is not evidence for the Binance runtime.** Replay uses KuCoin public history (`PublicKuCoinFuturesClient`), KuCoin page semantics and `kucoin_execution_model` costs.

## 2. Architecture (real, from bootstrap capture)

```
sitecustomize → runtime_truth_ws_compat/filter → runtime_bootstrap.install()
  ├─ venue split: exchange.EXCHANGE_NAME (binance|kucoin)
  ├─ release contract (runtime_release_contract) → pilot_live_runtime + pilot_risk_cap_hardening
  │   (or validation_safety_lock when not authorized)
  ├─ ~60 hardening/observability installs (strategy, NEXUS, risk, protection, durable, Telegram)
  └─ runtime_overlays.install() → operator_runtime_policy, final_sizing_invariants,
       operator_loss_policy, binance_cross_portfolio_stress, runtime_contract_guard (asserts 14 callables + 7 markers)
main_hardened (FastAPI app, /ready, destructive-endpoint guard) → main (lifespan starts nexus_runtime_engine.TradingEngine)
```

Loaded `bot.*` modules at boot: 170 in Binance LIVE-shaped (173 incl. entrypoints). Paper and KuCoin boots also load the validation-lock stack (8 more).

## 3. AI / NEXUS map (Phase 3)

```
Binance WS/REST klines (15m,1h,4h, forming candle dropped)
 → strategy.Analyzer.analyze_mtf: EMA/ADX/RSI/CI/BB/VWAP/BOS indicators; strategy.detect_regime (4H);
   4H+1H alignment; score_tf ×3 → combined = .25·4H+.30·1H+.45·15M; RSI/volume/15M/setup vetoes;
   ATR SL/TP; R:R ≥ MIN_RR_RATIO; cost pre-filter (strategy.TOTAL_COST)
 → pullback_confirmation (closed-15m votes)
 → operator_loss_policy (technical geometry; diagnostic net R:R from execution_cost snapshot)
 → NEXUS nexus_ai.decide: validate_data → nexus_ai.detect_regime (second, different regime model)
   → analyze_mtf (NEXUS MTF) → run_ensemble: 7 hand-written rule functions
     (trend, momentum, mean_reversion, breakout, structure, derivatives, risk_regime)
   → _fuse (fixed REGIME_MODEL_WEIGHTS) → direction+confidence
   → heuristic_win_probability(confidence) → expected_value(p, entry, sl, tp, snapshot costs)
   → EV>0, R:R_net ≥ NEXUS_MIN_RR_NET, news veto, fixed WEIGHTS → setup_quality ≥ NEXUS_MIN_SCORE
 → _prepare_professional_risk (RiskManagerV3 plan + capital)   ← P1 defect #1 here on Binance
 → PilotGuard/exposure/market-risk gates → final_sizing_invariants (min(stop_risk, cap)) ← P1 defect #2
 → final_loss_budget, pre-dispatch microstructure, Binance cross stress, drawdown re-check
 → BinanceClient.place_order (newClientOrderId, fenced, non-blind) → protection (algo SL/TP) → reconciliation
```

| # | question | answer (evidence) |
|---|---|---|
| 1 | ML/AI or heuristics? | **Heuristics.** `nexus_models.py` holds 7 deterministic indicator rules; fusion weights and score weights are constants in `nexus_ai.py`. |
| 2 | Models | The 7 rule functions above. The "ensemble" is rule voting. |
| 3–5 | Training / loading / versioning | None at runtime. `score_weights.py` (logistic regression calibrator) was never wired and is removed as dead code. `optimizer.py` (optuna) is an offline strategy-parameter search, not a model. The only version tag is `PROBABILITY_MODEL = "ensemble_linear_v1"`. |
| 6 | Features | OHLCV-derived indicators on 15m/1h/4h; funding, OI delta and long/short ratio when available; news score. |
| 7–8 | Leakage / look-ahead | The forming candle is excluded in the strategy (`[-1]` dropped) and in pretrade (`pretrade_hardening`). `nexus_decision_consistency` enforces closed-candle parity. No look-ahead found in runtime. The replay adapter freezes a historical clock (`nexus_oos_real_replay_corrected`). |
| 9 | Survivorship | The universe is a hard-coded list of currently liquid symbols (`cfg.SYMBOLS`, expanded to 25 in `5cf7ac0`). Backtests over past periods therefore carry survivorship bias. |
| 10 | Train/validation leakage | No trained model, so no train/validation split. Thresholds were set by hand (comments in `nexus_ai.py`: "escolha deliberada"). |
| 11–12 | Calibration / confidence meaning | **No empirical calibration.** Confidence is a heuristic score; probability is a linear map of it. `nexus_calibration.py` can measure calibration offline but is not run on a schedule. |
| 13–15 | Missing data / fallback | `nexus_decision_consistency` and `nexus_optional_evidence` exclude unavailable models (no neutral credit). `terminal_notifications` treats a 0.0 default as unavailable. MICROSTRUCTURE is often unavailable (production log `unavailable=['MICROSTRUCTURE']` with component 0.0 inside the weighted score: this lowers the score and does not inflate it). |
| 16–18 | Drift | **No drift, calibration-drift or concept-drift monitoring.** |
| 19–22 | Champion/challenger, rollback, registry, feature schema | None in runtime. The challenger research workflows exist only on `main`, not on this branch. |
| 23–25 | Reproducibility | Partial. `nexus_persistence` records decision inputs and shadow outcomes. `candidate_id`/`cost_snapshot_id` (PR #417) link cost inputs. Raw candles are not persisted per decision, so bit-exact replay of an old decision is not guaranteed. |
| 26–28 | Post-trade learning | **Telemetry only.** Nothing updates weights or thresholds from outcomes, so there is no feedback-loop risk. Sample adequacy is moot. |
| 29 | Weights from OOS? | **Manual.** |
| 30 | "AI" without a trained model | Yes: `nexus_ai` ("AI DECISION ENGINE"), `[AI_DECISION]` logs, "IA" in Telegram texts. All are heuristic. |

## 4. Canonical authorities vs duplicates (Phase 4)

| concept | writers / implementations found | status |
|---|---|---|
| Equity / available | `pilot_live_runtime._refresh_account` (via `account_balance_semantics`, venue-aware), `account_capital_reader` (**KuCoin-only, P1**), `balance_observability` + `account_balance_observability` (KuCoin endpoint, logs only) | problematic: two readers, one wrong venue |
| Peak / drawdown | `risk.py` legacy, `drawdown_persistence`, `paper_wallet`, `durable_execution`, RiskManagerV3 (its own peak) | problematic: 2 in-memory drawdowns (legacy + V3), durable HWM shared |
| Daily PnL / stop | `daily_tracker`, `durable_daily_pnl`, `daily_stop_runtime_hardening`, `durable_daily_stop`, engine (`daily_stopped` written in 4 modules) | problematic |
| Fees | `binance.TAKER_FEE`, `kucoin.TAKER_FEE`, `kucoin_execution_model.DEFAULT_TAKER_FEE`, `strategy.TAKER_FEE`, `execution_cost` (canonical since #417) | strategy pre-filter still separate (documented) |
| Regime | `strategy.detect_regime` (TRENDING/RANGING/...), `nexus_ai.detect_regime` (BREAKOUT/...), + consistency wrappers | two different regime models gate the same candidate |
| Score | strategy combined score vs NEXUS `setup_quality` | two scores against the same env threshold family (`MIN_ENTRY_SCORE`, `NEXUS_MIN_SCORE`); `main_hardened` enforces equality |
| Sizing | `final_sizing_invariants` effective; 3 shadowed hooks | resolved by #417 (logged), hooks remain |
| Telegram credentials | `cfg.TELEGRAM_TOKEN` (aliases), `logger.py` and `funnel_metrics.py` (env `TELEGRAM_TOKEN` only) | **P2 with production impact**: prod sets `TELEGRAM_BOT_TOKEN`, so 2 of 3 emitters are silently off |
| Release | `runtime_release_contract` | canonical (#417) |
| Exchange | `bot.exchange` | canonical |

## 5. Findings

### P0
None found with evidence.

### P1
| id | finding | evidence | action |
|---|---|---|---|
| P1-1 | Binance LIVE: `read_account_capital` calls the KuCoin `/api/v1/account-overview` → every NEXUS approval turns into `REJECT decision_source=exception` | code; reproduction (`404 /api/v1/account-overview`); prod logs DOTUSDT 17:02–17:15 | fix: use `client.get_account_state()` when the venue provides it (same rule as `account_balance_semantics`) |
| P1-2 | `quantity_rules` rejects Binance base-asset metadata with fractional `minQty` → qty=0 for BTC/ETH/DOT/... | reproduction: BTC/DOT/ETH raise; DOGE passes | fix: BASE_ASSET instruments map to step units (`multiplier=stepSize, lot=1, minimum=minQty/stepSize`); contract conversions still refuse BASE_ASSET (fail-closed preserved) |
| P1-3 | `IntegrityGuard._external_position_map` returns `{}` on any exception → external positions silently not flagged (fail-open) | `integrity.py:268-275` | fix: report `EXTERNAL_POSITIONS_UNREADABLE` BLOCKED |
| P1-4 | Deploy gating: `checkSuites=false`, no pre-deploy gate, builder RAILPACK instead of the declared Dockerfile | Railway service config (read-only) | **operator action**; not changeable from the repo without touching production |

**Important consequence:** P1-1 and P1-2 together are what currently prevent Binance LIVE entries. Fixing them lets real orders be sent again under every other gate, with position size set by the stop-risk budget from PR #417. Each fix is its own commit so the operator can decide.

### P2
| id | finding | evidence |
|---|---|---|
| P2-1 | `logger.py`/`funnel_metrics.py` Telegram emitters ignore `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` aliases; 3 independent emitters with 3 rate limiters | code + prod variable names |
| P2-2 | `status_observability._drawdown_blocked` returns "not blocked" when unreadable (startup notification can claim LIVE OPERACIONAL) | `status_observability.py:15-26` |
| P2-3 | `POST /api/backtest` starts `weekly_backtest_loop` per call (unbounded loop tasks); backtest uses KuCoin endpoints | `main.py:746` |
| P2-4 | `durable_reconcile_hardening._prove_absent_and_flat` queries KuCoin `/api/v1/orders` → on Binance the diagnostic is always `false` | code (diagnostic only) |
| P2-5 | Wrapper stacking (see summary); 3 shadowed `minimum_base_quantity` hooks | census |
| P2-6 | Two regime models, two scores, two in-memory drawdowns, 4 daily-stop writers | §4 |
| P2-7 | `balance_observability` vs `account_balance_observability`: overlapping log-only readers of a KuCoin endpoint | code |
| P2-8 | No invariant that `POST_TARGET_RISK ≤ MAX_RISK_PCT` (defaults are safe; misconfiguration could raise risk after the daily target) | `config.py` |
| P2-9 | `get_order_by_client_oid`/`get_order_status` (Binance) return `{}` for both "not found" and transport errors; callers treat it as unresolved (fail-closed) but cannot distinguish | `binance.py:1036,1494` |

### P3
* Stale comments in `engine._effective_risk_pct` ("15%", "30%"; real defaults are 0.5% and 1%).
* `README.md` describes Bybit.
* The "KuCoin" wording in `quantity.py` messages.
* `runtime_hardening` builds `ALTER TABLE ... {column}` from a constant dict (not user input).
* `pullback_confirmation_hardening.py:227` telemetry `except: pass`.

## 6. Exceptions (Phase 6)

* 773 handlers: 363 logged, 45 re-raise, 21 logged+raise; 203 assign a fallback/reason; 11 `pass`; 33 `continue`; 97 return a sentinel.
* 40 handlers are broad, silent and in a critical context. Triage: 36 are fail-closed (return BLOCK/False/reject or mark unconfirmed), 2 are telemetry, and 2 need action: P1-3, and P2-2 (observability).
* No handler around order/fill/SL/TP/sizing/balance/drawdown/ownership/reconciliation/auth lets an error pass as success.

## 7. Concurrency (Phase 7)

* 55 `create_task` call sites, 27 of them fire-and-forget (mostly Telegram).
* 2 daemon threads (Telegram workers) + `asyncio.to_thread` for NEXUS.
* Position management runs under `_pos_lock`; durable/fenced dispatch is covered by `live_execution_fence` and tests.
* Log handlers that run in threads use locks (`funnel_metrics`, `market_radar`, `logger`).
* Risks: unbounded backtest loops (P2-3); fire-and-forget Telegram tasks whose exceptions surface only as "Task exception was never retrieved" (notifier swallows its own errors).

## 8. Execution forensics (Phase 8)

Scenario → test mapping was established in PR #417 (see `NEXUS_PRODUCTION_CONSISTENCY_AUDIT_2026-09-26.md`). New for this audit:
* the `{}` ambiguity (P2-9) was checked at all 7 call sites; each treats `{}` as unresolved and blocks new entries;
* the P1-2 fix keeps contract-conversion refusal for BASE_ASSET, so restart ownership and protection helpers still fail closed.

## 9. Math (Phase 9)

* Quantity units: P1-2.
* Binance rounding (`_round_qty` ROUND_DOWN to `stepSize`, `_round_price` to `tickSize`) uses Decimal.
* Stop-risk sizing invariants hold across 1x–125x (`test_sizing_truth_invariants`).
* Loss ceiling `projected ≤ 0.5·margin` is scale-invariant in qty, so it acts as a stop-distance limit `stop%+cost% ≤ 0.5/leverage`.
* Cross stress overestimates maintenance (`notional·MMR`, no `cum`).

## 10. Backtest / OOS parity (Phase 10)

| dimension | runtime (Binance) | OOS/backtest |
|---|---|---|
| data | Binance USD-M | KuCoin public futures |
| fees | `/fapi/v1/commissionRate` or ≥6 bps | `kucoin_execution_model` 6 bps + KuCoin fee API |
| slippage | ticker half-spread + impact | static 5/10 bps |
| funding | not in pre-trade cost | KuCoin settlements |
| contract units | base asset, stepSize | KuCoin contracts/multiplier |
| sizing | min(stop-risk, cap) | R-multiple accounting |

**Conclusion:** the OOS reports are not evidence about the current Binance runtime.

## 11. Security (Phase 11)

* No `eval`/`exec`/`pickle`/`shell=True`.
* `subprocess` is used only in the `release_proof` CI tool and the isolated research child with a scrubbed env.
* SQL uses parameters; the one f-string DDL is built from constants.
* All `/api/*` routes require a bearer token (`secrets.compare_digest`); close-all additionally needs a confirmation header.
* Unauthenticated routes: `/health`, `/`, `/dashboard` (static), `/ready`, `/service_ready`.
* Credentials are redacted in logs (`auth_log_redaction`).
* Dependencies are pinned in `requirements.txt`; `pip-audit` runs in `security.yml`.
* Deploy-pipeline gaps: P1-4.

## 12. Performance (Phase 12)

* Scan loop every ~5 s over the viable universe (25 configured); klines served from the WS cache (REST only on cache miss).
* NEXUS runs only for candidates, in a thread.
* Per-cycle duplication: the strategy and NEXUS compute overlapping indicators on the same candles (two regime detectors, two MTF analyses).
* Nine wrappers around `analyze_mtf` add parsing/log overhead per symbol.
* Not benchmarked under live load (no exchange network here).

## 13. Tests (Phase 13)

* 285 test modules, 34,626 LOC; 67 modules assert on source text (implementation-coupled); 68 reference KuCoin.
* 13 runtime-reachable modules have no direct test import. The largest: `runtime_hardening` 373, `news_pipeline` 359, `runtime_bootstrap` 231, `entry_type_shadow_overlay` 215. Several are covered indirectly by bootstrap tests.
* False confidence: tests of Binance sizing used integer-lot fixtures, which is why P1-2 was never caught. `test_binance_usdm_migration` checks the metadata mapping but not that it can be sized.
* Added in this PR: Binance fractional-lot sizing tests and a capital-reader venue test.

## 14. Documentation (Phase 14)

| doc | status |
|---|---|
| README.md | STALE/CONTRADICTORY (Bybit) → rewritten |
| KUCOIN_MIGRATION.md, META_AUDIT.md, CHANGELOG.md | HISTORICAL (labelled) |
| docs/RAILWAY_PRODUCTION_PROMOTION.md | CONTRADICTORY with the production config (pre-deploy gate, Dockerfile) → note added |
| docs/audit/* | CURRENT |
| other docs/*.md | HISTORICAL records of past changes |

## 15. Cleanup plan and decisions (Phase 5)

| path | action | evidence |
|---|---|---|
| bot/score_weights.py | **DELETE_SAFE** (removed) | A: no import/call in runtime or lazily; B: only comments mention it (database.py:120, engine.py:3588), no string/dynamic import; C: not in any workflow; D: no test imports it; E: not migration/compat (never wired); F: suite, runtime contract and release proof green after removal |
| bot/ci_deploy_gate.py | DEPRECATE (kept) | not configured as pre-deploy in production, but it is the documented safety gate; operator decides |
| bot/nexus_oos_evidence_bundle.py | UNKNOWN_REQUIRES_EVIDENCE (kept) | manual research CLI |
| bot/kucoin_order_forensics.py | DEPRECATE (kept) | KuCoin compatibility still supported |
| bot/nexus_oos_*_corrected.py | CONSOLIDATE later (kept) | CI entrypoints |
| balance_observability / account_balance_observability | CONSOLIDATE later (kept) | log-only overlap; merging changes log lines other tools may parse |
| cross_portfolio_stress / binance_cross_portfolio_stress | KEEP both | venue-specific, installed exclusively |
| main.py / main_hardened.py | KEEP both | `main_hardened` wraps `main`'s app; production CMD uses `main_hardened` |

No other file met all of A–F. "Legacy-looking" names were not treated as evidence.

## 16. Resolution in this PR (commit per category)

| # | category | commit | content |
|---|---|---|---|
| 1 | audit report | `4be095f` | this report + generated inventory |
| 2 | dead code | `b4e0aa6` | `bot/score_weights.py` removed (A–F) |
| 3 | duplicate consolidation | `abe5d5e` | one Telegram credential authority (`bot/telegram_credentials.py`); restores the 2 emitters silently disabled in production |
| 4 | P0 | — | none found with evidence; no commit fabricated |
| 5 | P1 | `44dcd55` | P1-1 venue-aware capital read (Binance) |
| 5 | P1 | `8feed8e` | P1-2 Binance base-asset quantity units (byte-pin updated for `bot/quantity.py` only, documented) |
| 5 | P1 | `9c0a1c4` | P1-3 unreadable external positions block instead of vanishing |
| 6 | P2 | `e313f44` | observability fail-closed on unknown drawdown; single backtest loop; venue-aware reconciliation diagnostic; stale risk comments |
| 7 | docs | this commit | README (Binance), HISTORICAL banners, Railway divergence note, canonical-authority rows |

Not fixed (needs operator or larger change): P1-4 deploy gating (Railway settings); P2-5/P2-6 wrapper stacking and duplicate regime/score/drawdown/daily-stop authorities (need a design decision, not a cleanup commit); P2-7 balance-observability consolidation; P2-8 config invariant; P2-9 `{}` ambiguity in Binance order reads; OOS on Binance data; drift/calibration monitoring.
