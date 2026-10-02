# NEXUS-7 / BGX — Forensic System Audit v2 (post-hardening)

Audit-only pass. **No code was modified.** No merge, no deploy, no LIVE change, no order sent.
All dynamic checks are offline (in-memory fakes, `KUCOIN_REST_BASE=http://127.0.0.1:1`,
offline runner with loopback-only network guard).

## 0. Base

| item | value |
|---|---|
| Branch | `claude/audit-algorithmic-trading-system-uf3zxo` |
| Base SHA | `2e2edc122f3eaf6383110dcd8b7ec4cdb18799af` |
| Working tree | clean |
| Python | 3.11.15 |
| Key deps (`requirements.txt`) | fastapi 0.141.1, uvicorn 0.27.1, aiohttp 3.14.3, numpy 1.26.4, asyncpg 0.29.0, aiosqlite 0.20.0, websockets 12.0, feedparser 6.0.11, optuna 4.5.0 |
| Entrypoint | `uvicorn main_hardened:app --workers 1` (Dockerfile; Railway `railway.toml`, healthcheck `/service_ready`) |
| Previous audit | `docs/FORENSIC_AUDIT_2026-10-02.md` (+ `docs/NEXUS_STRATEGY_QUANT_AUDIT.md`) |

**Exchange scope correction.** The brief targets *Binance Futures*. This system executes on
**KuCoin Futures** only (`bot/kucoin.py:121` `REST_BASE=https://api-futures.kucoin.com`). Binance is
used solely as a public *data* source (`bot/market_data.py:306-329`: BTCUSDT long/short ratio, open
interest, taker ratio; announcements; `market_risk_binance_fallback`). Every Binance execution
concept was audited on its KuCoin equivalent: `clientOrderId→clientOid`, `reduceOnly→reduceOnly`,
`closePosition→closeOrder`, `workingType→stopPriceType (TP|MP)`, `positionSide→one-way (sign of currentQty)`,
`stepSize→multiplier×lotSize (contracts)`, `tickSize→tickSize`.

## 1. Executive summary

```
NEXUS-7 SYSTEM AUDIT (v2)
BASE SHA: 2e2edc1   BRANCH: claude/audit-algorithmic-trading-system-uf3zxo
FILES ANALYZED: 227 bot modules (52,637 LOC) + 3 root (868) + 294 test files (36,421) + 4 research + config/Docker/Railway
TESTS DISCOVERED: 1,792 test functions / 294 files      TESTS EXECUTED: 1,806 (offline runner)
RESULT: 1,802 passed, 1 failed suite (test_research_process — known environment failure), 0 new
CRITICAL: 1   HIGH: 5   MEDIUM: 13   LOW: 9
DUPLICATED LOGIC: 7 families        DEAD CODE: 5 modules + 4 functions
UNSAFE FALLBACKS: 3                  FAIL-OPEN PATHS (execution/risk): 0 found ; 1 fail-closed-too-wide (NEW-01)
RACE CONDITIONS: 2 open (low impact) STATE RISKS: 4   EXECUTION RISKS: 5   STRATEGY RISKS: 9
LIVE ORDER BYPASSES FOUND: 0 (new-risk entry); 1 ownership-boundary bypass (NEW-02, restart adoption)
BROKEN INVARIANTS: 2 (INV-OWNERSHIP-ADOPTION, INV-EMERGENCY-REDUCE-VALID)
TEST COVERAGE GAPS: 9
```

**Headline.** The ten fixes of this branch hold under re-verification, but this pass found that
**two of them introduced or widened defects**, and the green suite did not catch either:

* **NEW-02 (CRITICAL)** — the restart ownership proof can adopt a **manual** position as a BGX trade.
  Root cause predates this branch (the proof never checks that the trade lineage is still open);
  Q-01B (`2e2edc1`) widened it from "exactly equal size" to "any smaller size with partial evidence".
* **NEW-01 (HIGH)** — after F-014, one malformed position row makes the emergency close-all send
  **zero** orders, even for valid, known positions.

Sizing remains the dominant capital risk (F-003: up to ≈25 % of available collateral per trade).

## 2. Architecture map (production composition)

`main_hardened` → threshold contract (`MIN_ENTRY_SCORE == NEXUS_MIN_SCORE` else refuse) → `import
sitecustomize` → `runtime_bootstrap.install()` (54 `install()` calls) → `runtime_overlays.install()`
(28) → `runtime_contract_guard` (source-file pinning of protected callables) → `main.lifespan` →
`classify_startup_block` (refuses when `sitecustomize != ok` or selfcheck critical, `bot/startup_block.py:72`)
→ `KuCoinClient()` → `nexus_runtime_engine.TradingEngine` (wraps client in `KuCoinPositionUnitAdapter`).

Final callables resolved dynamically from the composed LIVE-pilot runtime (not from docs):

| callable | final implementation |
|---|---|
| `KuCoinClient.place_order` | `bot.live_execution_fence:146` → `pilot_submission_counter` → `kucoin_native_tpsl` → core |
| `KuCoinClient._post` | `bot.partial_tp_execution_hardening:31` → `kucoin_cross_margin_order` → `kucoin._post:614` |
| `KuCoinClient._fenced_entry_post` | `bot.pilot_submission_counter:332` |
| `KuCoinClient._entry_safe_post` | `bot.kucoin:581` (operator pause + F-002 readiness at transport) |
| `KuCoinClient.set_position_stops` | `bot.prelive_protection_failclosed:70` → `native_stop_repair.set_stops` |
| `KuCoinClient.get_positions` | `bot.shadow_position_forensics:16` → core (`position_snapshot` validator) |
| `KuCoinClient.wait_for_fill` | `bot.order_visibility_race_hardening:31` |
| `TradingEngine.run` | `bot.operator_runtime_policy:195` |
| `TradingEngine._open` | `bot.post_trade_forensics:168` (chain: post_trade_forensics → legacy_pretrade_advisory → runtime_truth_hooks → validation_safety_lock → operator_loss_policy → kucoin_contract_risk_hardening → pilot_live_runtime → pilot_risk_cap_hardening → prelive_protection_failclosed → core) |
| `TradingEngine._sync_positions` | `bot.post_trade_forensics:222` → validation_safety_lock → pilot_external_position_guard → paper_lifecycle → core |
| `TradingEngine._manage_partial_tp` | `bot.partial_tp_execution_hardening:49` (LIVE → `durable_partial_exit.check`) |
| `TradingEngine._check_rr_double` | `bot.engine:2008` (LIVE → `confirmed_rr_exit.check`) |
| `TradingEngine._load_existing_positions` | `bot.exit_geometry_durability:275` → restart_opening_order_lineage → pilot_external_position_guard → core |

### Trade flow

```
KuCoin WS klines/ticker (kucoin.py WS loop) + REST seed ──► kline cache (closed-candle boundary, market_data_integrity)
  ► engine._scan_all_and_enter (engine.py:2237) ─► strategy.Analyzer.analyze_mtf (strategy.py:522; score_tf:308)
  ► pre-trade (engine.py ~2640: ticker/funding/OI context) ─► NEXUS validation (nexus_ai; its own analyze_mtf, nexus_ai.py:249)
  ► PilotGuard.evaluate / RiskManager(.can_open) ─► _open chain (operator_loss_policy geometry, pilot sizing 50 % margin,
    final_sizing_invariants, final_loss_budget, LIVE predispatch market guard, integrity/protection readiness)
  ► place_order chain ─► durable SUBMITTING intent persisted (persist_orders "before_dispatch")
  ► _post ─► _fenced_entry_post (ownership lease + PostgreSQL advisory fence + pilot budget)
  ► _entry_safe_post (operator pause + INV-LIVE-READINESS-001) ─► aiohttp POST /api/v1/orders | /api/v1/st-orders (native TP/SL)
  ► ACK (orderId) / ambiguous → recover by clientOid ─► wait_for_fill ─► Position created (engine.py:3487)
  ► protection postcondition (prelive_protection_failclosed) ─► private WS fills (F-011 identity) ─► durable persist
  ► exits: native SL/TP on exchange; durable_partial_exit (1R) → monotonic BE; trailing; confirmed_rr_exit (2R)
  ► _sync_positions (authoritative snapshot only, F-014) ─► accounting (estimated) ─► daily PnL / drawdown
```

### Every path that can mutate the exchange (complete list, grep-verified)

| call site | endpoint | gate |
|---|---|---|
| `kucoin.py:1113` core `place_order` | `/api/v1/orders` | full entry chain; transport gate for non-reduce |
| `kucoin_native_tpsl.py:236` | `/api/v1/st-orders` | readiness predispatch + transport gate |
| `kucoin_native_tpsl.py:67` | change margin mode | only after readiness gate (F-002 test asserts no read/switch when not ready) |
| `native_stop_repair.py:323` | stop order (closeOrder+reduceOnly) | protection path (never opens risk) + Q-01C monotonic floor |
| `conditional_stop_lifecycle.py:439` | DELETE stop order | only superseded/stale BGX-owned orders, after replacement verified |
| `kucoin.py:1241` | DELETE `/api/v1/orders` (`cancel_all_orders`) | **no caller found in bot/** (dead) |
| `kucoin.py:904` | auto-deposit-status | only if `KUCOIN_SET_LEVERAGE_ENDPOINT=true` |
| `kucoin.py:1672`, `prelive_readonly_probe.py:34` | bullet-private (WS token) | read semantics |
| `cross_portfolio_stress.py:157` | `/api/v2/getCrossModeMarginRequirement` | read semantics (POST) |
| reduce-only exits | `durable_partial_exit:59`, `confirmed_rr_exit:63`, `emergency_flatten:168`, `prelive_protection_failclosed:252`, `engine.py:3200/3879` | reduce-only/closeOrder: excluded from entry gate by design |
| discretionary exits | `stagnation_time_hardening:70/103/125`, `runtime_hardening:316+`, `exit_policy_telemetry:96` | **disabled in LIVE pilot** (`operator_loss_policy.py:93-146`) |

**Proof of entry gating (F-002 invariant).** Every non-reduce POST to `/api/v1/orders` or
`/api/v1/st-orders` traverses `_post` → `_fenced_entry_post` → `_entry_safe_post`, which raises
`EntryReadinessRefused` before `session.post` unless `assert_ready_for_new_entries` passes
(`kucoin.py:581-596`). `assert_exchange_mutation_allowed` precedes PAPER/LIVE branching (`kucoin.py:638`).
Composition failure refuses startup. **No new-risk bypass found** (re-verified this pass; tests
`test_live_entry_readiness_gate`, `test_causal_integrity_blocks_composed`, `test_position_snapshot_authority_composed`).

## 3. Findings

### CRITICAL

#### NEW-02 — Restart ownership proof adopts a manual position as a closed BGX trade
* **File:** `bot/restart_ownership_recovery.py:160-279` (`prove_restart_ownership`; reduce branch `:188-232`).
* **Root cause:** the proof matches *some* FILLED BGX opening order (same symbol/side, exchange-confirmed
  terminal fill) but never proves that **this trade lineage is still open**. Terminal opening records
  stay in the registry up to 7 days (`order_state.py:375` `gc`, called with `max_age=7*86400`) and the
  `partial_exit_v1` / `exit_geometry_v1` records are never marked terminal at close.
* **Pre-existing form:** a manual position of *exactly* the old trade's size is adopted.
* **Widened by Q-01B (`2e2edc1`):** any *smaller* manual position is adopted when the old trade had
  partial evidence.
* **Reproduction (offline, both variants confirmed):** registry holds FILLED `bgx7-owned` (0.21 ETH,
  terminal at exchange) + `partial_exit_v1` filled; operator opens 0.08 ETH LONG with a stop; restart →
  `OwnershipProof(recovered=True, reason='exact_durable_exchange_proof_after_reduce', order_id='oid-1', base_qty=0.08)`.
  Exact-size variant (0.21, no evidence) → `recovered=True, reason='exact_durable_exchange_proof'`.
* **Consequence:** BGX starts managing an operator's manual position (trailing/partial/2R/BE stop
  changes, reduce orders) — the P0 incident class the guard exists to prevent.
* LIVE: YES · wrong order: YES (reduce/stop on a foreign position) · loss: YES · state corruption: YES.
* **Fix (P0, first):** durable "lineage closed" marker written when the lineage's exposure is
  confirmed flat; proof rejects closed lineages; the reduced branch additionally requires
  `residual == opening_fill − proven reduce fills` (exchange-confirmed partial order fill) within one lot.
  Tests: closed-lineage adoption (both variants), reduced-with-mismatched-residual.
* **Fix risk:** low; tightening only (worst case: a genuine BGX position stays EXTERNAL = pre-branch behaviour).

### HIGH

#### NEW-01 — One malformed position row blocks emergency close-all for every valid position
* `bot/emergency_flatten.py:112` + `bot/position_snapshot.py:147` (F-014 global non-authority).
* Repro: exchange = BTC +5 (valid) + ETH row with `avgEntryPrice="abc"` → `close_all_positions` →
  `status=FAILED reason=positions_unconfirmed orders_sent=0`; BTC stays open.
* Correct for *inference* (no phantom close), wrong for *risk reduction*: closing a validated,
  identified exposure never needs the other rows. Same over-reach in `native_stop_repair.set_stops`
  (stop repair for valid symbols blocked) and `durable_partial_exit`/`confirmed_rr_exit` (exits blocked).
* Fix: risk-reducing consumers use `PositionSnapshotUnconfirmed.valid_rows` (already carried) and
  report `PARTIAL_FAILURE` with the unknown symbols; inference consumers keep global fail-closed.

#### F-003 — LIVE per-trade loss allowance ≈ 25 % of available collateral (open)
* `final_sizing_invariants.py:107` `target_margin = available × 0.50`; notional = margin × L (L=10 →
  5 × available); `final_loss_budget.py:25` `limit = margin × 0.50` ⇒ projected loss ≤ **0.25 × available**
  per trade; `MAX_POSITIONS=2` ⇒ ≈50 % simultaneously at risk. `MAX_RISK_PCT=0.01` is not authoritative.
* Inconsistency: `pilot_live_runtime.py:291` logs `target_notional = available × 0.50` ("50 % notional")
  while the final authority sizes 50 % *margin* (10× larger notional).
* Leverage determines the *admissible stop width* (stop% + costs ≤ 0.5/L ≈ 4.9 % at 10×), not risk.

#### F-013 (escalated) — Post-fill SL/TP shift now also defines the trade's initial risk
* `engine.py:3476-3479` shifts `sig.entry/sl/tp` by the fill delta **before** `Position(sig, qty)`
  (`engine.py:3487`); the native SL/TP attached at entry stays at the original levels. Since Q-01,
  `initial_sl` (and the durable Q-01B record) is the *shifted* stop ⇒ 1R understated by the slippage;
  `_guard_naked_positions` may re-apply the shifted stop.
* Unsafe fallback: when the order status lacks `dealValue`, the fill price falls back to the cached
  ticker (`engine.py:3466`), which has **no timestamp** (F-024) ⇒ an arbitrary stale delta shifts SL/TP.
* Fix: never shift; take initial_sl/tp from the exchange-confirmed protective orders; no ticker fallback.

#### F-019 — `_post` retries reuse a stale signature (open)
* `kucoin.py:645` headers (incl. `KC-API-TIMESTAMP`) computed once before the retry loop; backoff
  2–3 s then 4–6 s (`kucoin.py:314-318`) ⇒ later attempts fall outside KuCoin's 5 s window ⇒ rejection
  and API-version flapping; unknown non-permanent codes re-POST immediately.
* Not a duplicate-order risk (same clientOid), but it converts recoverable 429/5xx into failures,
  including for reduce-only exits and stop repair.

#### NEW-03 — Ownership extension relies on `RAILWAY_*` env scope for the partial key
* `exit_geometry_durability.reduce_evidence` → `confirmed_rr_exit.identity` hashes
  `RAILWAY_PROJECT_ID/SERVICE_ID/ENVIRONMENT_ID`. Moving the service/environment silently changes every
  partial/RR/geometry key ⇒ restart loses partial state ⇒ possible duplicate partial for an adopted
  pre-partial position (bounded by exchange reduceOnly). Severity HIGH only together with NEW-02; else MEDIUM.

### MEDIUM

| ID | Location | Problem | Consequence |
|---|---|---|---|
| F-020 | `indicators.py` | RSI(flat)=99.99; ATR zeros for first 13 bars; ADX IndexError 16–27 bars; EMA unseeded | biased features, scan exceptions |
| F-022 | `kucoin.py:1556` | partial fill + cancel ⇒ "not filled" | mitigated by immediate reconcile |
| F-023/F-014B | `engine.py` sync close | remote close accounted from local mark × configured fee | daily-stop decisions on estimates |
| F-024 | `kucoin.py:2278` | ticker `volume = data["size"]` (last trade size); no ticker timestamp | wrong volume context; stale fallback (see F-013) |
| F-025 | exits | BE = exact entry (no fees) ⇒ BE half nets ≈ −0.11 to −0.27 R (Q-03) | expectancy drag |
| F-026 | config | 138 env vars; dead vars (`MIN_CONFIDENCE`, `SL_ATR_MULT`, `TP_ATR_MULT`, `TRAILING_LOCK_R_MULT`, `COOLDOWN_SECONDS`, `REPORT_INTERVAL_H`); `MAX_RISK_PCT` not normalised | config drift |
| F-027 | DailyTracker | weekly/monthly limits inert in LIVE | silent no-op controls |
| F-028 | `strategy.py:308-506` | trend family ≤30 pts/TF double-counted with the EMA direction pre-filter; `orderbook` never passed (`ob_ok` always False); `vol_r` divides by a possibly-zero mean (`strategy.py:323-324`) | inflated scores, `inf` volume ratio |
| F-029 | `engine.py` pre-trade | missing candle fields filled with `sig.entry` / volume 1000 | fabricated inputs |
| F-014C | pilot guard | restart ownership preload runs once; unconfirmed first read ⇒ no adoption until next restart | unmanaged (protected) position |
| F-038 | `mtf_strategy_observability.py` | 5 shadow analyses per `analyze_mtf` (≈3× CPU) inside the event loop and backtests | latency, telemetry contamination |
| NEW-04 | `market_data.py:295-345` | crowd data is BTCUSDT-only, labelled "Coinglass" but fetched from Binance, cached without timestamp in core (freshness only via `derivatives_news_freshness_hardening`) | alt decisions use BTC crowd data |
| NEW-05 | `engine.py` (34 sites) | `asyncio.create_task(...)` without retained reference/exception handler (notifications) | tasks can be GC'd; exceptions unobserved |

### LOW

F-030 (runner `skipped=` regex never matches, `tests/run_offline.py:123`), F-032 (README/CHANGELOG
describe Bybit/50×), F-033 (487 `except Exception` + 3 bare `except:`), F-035 (optuna silent
degradation), F-036 (sitecustomize side effects for any process on PYTHONPATH), F-037
(day-of-month reset), Q-01D (PAPER partial PnL approximated), Q-01E (dead `risk.check_partial_tps`
non-monotonic BE), Q-01I/J (no geometry record if crash before entry hook; no downtime MFE backfill).

**Correction to the previous audit:** F-031 (partial API-key logging) is **mitigated in every
process**: `bot/__init__.py:5-7` installs `log_redaction_hardening` on any `import bot`, and its
patterns redact `KuCoin API Key: …` and the passphrase-character list. F-034 listed that module as
dead — it is not.

## 4. Status of earlier findings (re-verified on 2e2edc1)

| ID | status | evidence |
|---|---|---|
| F-001, F-001A | FIXED | `emergency_flatten.py`; tests green — but see NEW-01 |
| F-002 | FIXED | transport gate `kucoin.py:581-596`; composed tests |
| F-010, F-010B | FIXED | `durable_execution.py` causal reasons; restore guard |
| F-011 | FIXED | `order_event_identity.py` |
| F-012 | FIXED | `emergency_close_quantity` |
| F-014 | FIXED (over-wide, NEW-01) | `position_snapshot.py` |
| Q-01/Q-02, Q-01C | FIXED | `exit_geometry.py`, `stop_monotonic.py` |
| Q-01B | FIXED **with regression** (NEW-02 widening) | `exit_geometry_durability.py`, `restart_ownership_recovery.py:188-232` |
| F-003, F-013, F-015..F-030, F-032..F-038 | OPEN | as listed above |
| F-031 | MITIGATED (correction) | `bot/__init__.py:5-7` |

## 5. Duplications (production implementation in bold)

| family | implementations | risk |
|---|---|---|
| partial exit | core `_manage_partial_tp`, **`durable_partial_exit` (LIVE)**, `partial_tp_execution_hardening` (PAPER), `risk.check_partial_tps` (dead) | policy drift PAPER vs LIVE |
| discretionary exits | core, `runtime_hardening`, `stagnation_time_hardening`, `exit_policy_telemetry`; **`operator_loss_policy` no-op in LIVE** | four copies of the same rules |
| sizing | `risk.RiskManager.size`, `risk_manager_v3`, `pilot_live_runtime` (50 % notional, preliminary), **`final_sizing_invariants` (50 % margin)**, `operator_runtime_policy` | contradictory "50 %" semantics (F-003) |
| MTF analysis | **`strategy.Analyzer.analyze_mtf`** (signal) and **`nexus_ai.analyze_mtf`** (validation) | two EMA/MTF definitions can disagree |
| Telegram delivery | async `notifier`, `logger._tg_worker` thread, `funnel_metrics._tg_worker` thread | uncoordinated rate limit |
| symbol normalisation | `kucoin.to_standard`, `order_event_identity.canonical_symbol`, `durable_live_reconciliation._canon_symbol`, `restart_ownership_recovery._normalized_symbol`, `protection_readiness._canon` | divergence on new symbol formats |
| daily stop | engine, `DailyTracker`, `daily_stop_runtime_hardening`, `durable_daily_stop` | which one decides depends on install order |

## 6. Dead code

`score_weights` (222 LOC, referenced only in comments), `kucoin_order_forensics` (83), `nexus_calibration` (74),
`release_proof` (38), `nexus_oos_robustness_replay` (103); functions `risk.calc_position_size`,
`risk.check_partial_tps`, `strategy.find_support_resistance`, `strategy.calc_sl_tp`;
`KuCoinClient.cancel_all_orders` has no caller. `log_redaction_hardening` is **used** (correction).

## 7. Concurrency, persistence, execution

* **Concurrency:** `_scan_all_and_enter` runs outside `_pos_lock` while private-WS callbacks persist
  concurrently (state transitions are now identity-checked and causal blocks are independent, F-010/F-011);
  `durable_partial_exit` and trailing can interleave set_sl calls — final stop is protected by
  Q-01C monotonic decision at both the local and the exchange boundary. No TOCTOU on dispatch found:
  readiness is re-evaluated at the transport boundary immediately before `session.post`.
* **Persistence:** single order-snapshot writer `persist_orders` (F-010B guard); geometry record written
  atomically per lineage; reasons are in-memory and rebuilt on restart. Open: lineage records never
  marked terminal (root of NEW-02).
* **Execution:** F-019 (stale signature), F-022, F-013 (stop shift), NEW-01.

## 8. Strategy, score, sizing, exits, costs (no dataset available)

No historical trade or market dataset exists in the repository and exchange/market hosts are not
reachable from this environment; **no win rate, expectancy, PF, Sharpe or drawdown statistic is
claimed**. Findings are algebraic/mechanical (details: `docs/NEXUS_STRATEGY_QUANT_AUDIT.md`):

* Round-trip cost model = 2×(taker 0.06 % + slippage 0.05 %) = **0.22 %** (`kucoin_execution_model.py:119-123`),
  funding not in the entry gate; ≈0.54 R at the minimum 0.41 % stop, ≈0.22 R at 1 %. Entries and exits
  are always taker (market).
* Score: trend family double counting (F-028); order-book input absent; RSI flat bias (F-020).
* Exits: partial at 1R + 3 bps on initial risk (Q-01); BE at exact entry (Q-03); 2R full exit now
  reachable — for R=3 targets it caps the remainder at 2R (simulator: straight-to-target +2.02R → +1.52R).
* Sizing: F-003 / F-018.

## 9. Invariants (status)

| invariant | status |
|---|---|
| No LIVE new-risk order without readiness/ownership/fence/durable state | HOLDS (tested) |
| One trade intent → at most one entry order | HOLDS (deterministic clientOid, durable SUBMITTING, single_submission) |
| External event cannot alter a ManagedOrder identity (F-011) | HOLDS |
| Unknown position ≠ flat (F-014) | HOLDS |
| Protective stop only tightens (Q-01C) | HOLDS |
| Restart preserves exit geometry (Q-01B) | HOLDS for owned lineages |
| **Only BGX-owned, still-open lineages are adopted after restart** | **BROKEN (NEW-02)** |
| **Emergency close reduces every validated exposure** | **BROKEN (NEW-01)** |
| Per-trade loss ≤ configured risk budget | NOT ENFORCED (F-003: 25 % of available) |

## 10. Test coverage gaps

1. closed-lineage adoption (NEW-02) — both variants; 2. flatten with partial snapshot (NEW-01);
3. `_post` retry with clock-dependent signature (F-019); 4. post-fill shift vs exchange stop (F-013) and
stale-ticker fallback; 5. env-scope change for durable keys (NEW-03); 6. only 12/294 test files run the
composed runtime; 7. runner never reports skips (F-030); 8. indicator edge cases beyond F-020 repro;
9. no property test for sizing → exchange rounding → realised loss.

## 11. Improvement opportunities (hypothesis-driven; none validated — no data)

| area | hypothesis | metric | test / acceptance | overfitting risk |
|---|---|---|---|---|
| C costs | post-only/limit entries on pullback setups cut entry cost ≈0.06 %→maker | net expectancy per trade | paper A/B ≥200 signals; fill rate ≥60 %, no adverse-selection loss > saved fee | low |
| B exits | fee-aware BE (entry ± 2·(fee+slip)) removes the negative BE leg (Q-03/H-09) | avg loss of BE exits | replay on recorded trades; BE exits net ≥ 0 | low |
| D sizing | stop-distance-based sizing at fixed % risk equalises loss per trade (F-003/F-018) | max loss/trade, DD, Calmar | simulation + paper; loss/trade ≤ budget after rounding | low |
| A entries | remove trend double counting (F-028) | score→outcome calibration | OOS walk-forward; score-bucket monotonicity | medium |
| E regimes | gate by volatility percentile instead of fixed ADX | PF by regime | regime-sliced OOS | medium |
| J data | per-symbol derivatives data instead of BTC-only (NEW-04) | signal precision on alts | ablation OOS | medium |
| I execution | fix F-019/F-013 | slippage vs signal, rejection rate | live logs before/after | low |

ML: not recommended until a labelled dataset of real decisions/outcomes exists (none in repo);
first candidate would be meta-labelling/probability calibration of the existing signal.

## 12. Recommended correction plan (one change → test → validate → next)

1. **P0 NEW-02** — lineage-closed marker + exact residual proof (revert the Q-01B ownership widening
   if the fix cannot land immediately).
2. **P1 NEW-01** — risk-reducing consumers act on `valid_rows`.
3. **P0 F-003** — operator decision on per-trade loss budget; align the two "50 %" semantics.
4. **P1 F-013** — no post-fill shift; protective levels from exchange truth; drop stale ticker fallback.
5. **P1 F-019** — re-sign per attempt; classify permanent codes.
6. **P2 NEW-03** — decouple durable keys from deployment env scope (or pin + migrate).
7. **P3** — Q-03/H-09 fee-aware BE, F-028 score dedup, F-020 indicators — each behind OOS evidence.
8. **P5** — remove dead code, unify duplicated families, fix runner skip regex.

## Appendix — reproduction scripts used in this pass (offline)

* NEW-02: `tests/test_restart_ownership_recovery` fixtures + `partial_exit_v1` record + manual 0.08/0.21
  ETH position → `prove_restart_ownership` returns `recovered=True`.
* NEW-01: `KuCoinClient.get_positions` with `_get` returning BTC valid + ETH `avgEntryPrice="abc"` →
  `close_all_positions` returns `FAILED`, zero orders.
* Final callables: composed LIVE-pilot runtime introspection (`inspect` of `KuCoinClient`/`TradingEngine`).
* Full suite: `python -m tests.run_offline` → TOTAL=1806 PASSED=1802 FAILED=1 (`test_research_process`).
