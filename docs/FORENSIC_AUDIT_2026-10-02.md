# NEXUS-7 / BGX — Forensic, quantitative and operational audit (first pass)

Date: 2026-10-02 · Phase A (discovery) + Phase B (audit) + Phase C (reproductions).
**No production code was changed.** No orders were sent, no secrets or LIVE settings were touched.
Executable evidence: `tests/test_forensic_audit_repro.py` (14 tests, all offline).
Module inventory: `docs/FORENSIC_AUDIT_2026-10-02_inventory.md`.

---

## 0. Baseline (Regra Zero)

| item | value |
|---|---|
| Repository | `007andra/nexus7-bot` |
| Branch | `claude/audit-algorithmic-trading-system-uf3zxo` |
| BASE SHA | `77ae453e3d01dc269c3b1ee3a4225900c22d9634` (Merge PR #414) |
| Working tree | clean before the audit |
| Python | 3.11.15 (Dockerfile: `python:3.11-slim`) |
| Main deps | fastapi 0.141.1, uvicorn 0.27.1, aiohttp 3.14.3, numpy 1.26.4, asyncpg 0.29.0, aiosqlite 0.20.0, websockets 12.0, feedparser 6.0.11, optuna 4.5.0 |
| Exchange | KuCoin Futures USDT-M (README still says Bybit — stale) |
| Production entrypoint | `uvicorn main_hardened:app` → imports `sitecustomize` → `bot.runtime_bootstrap.install()` → `bot.runtime_overlays.install()` → `main.app` |
| Deploy | Railway, Dockerfile builder, healthcheck `/service_ready`, 1 worker |
| Test env | `python -m tests.run_offline` (one subprocess per suite, `python -S`, loopback-only network guard, `PAPER_TRADE=true`) |

Local note: `sgmllib3k` (feedparser dep) fails to build as sdist in this container; feedparser was installed `--no-deps`. Not a repository defect.

### Test baseline (before any change)

```
TOTAL=1607 PASSED=1603 FAILED=1 SKIPPED=0 FAILED_SUITES=1   (273 suites)
```
Pre-existing failure: `tests.test_research_process.test_real_child_runs_research_without_blocking_loop`.
Root cause (environmental): the research child runs with `python -S`; `optuna` imports `packaging`, which in this container lives only in `/usr/lib/python3/dist-packages`, so `bot/optimizer.py:22-27` silently sets `optuna = None` and `optimize_snapshot` crashes with `AttributeError`. Code smell recorded (silent `optuna=None` fallback), not attributed to the codebase as a regression.
Note: `SKIPPED=0` is not trustworthy — see F-030.

After adding the reproduction suite: `tests.test_forensic_audit_repro: PASS (14)` (6 of them are `expectedFailure` reproductions that fail for the documented reason — verified individually).

---

## 1. Executive summary

```
SYSTEM AUDIT — EXECUTIVE SUMMARY
Repository: 007andra/nexus7-bot       Branch: claude/audit-algorithmic-trading-system-uf3zxo
BASE SHA: 77ae453e3d01dc269c3b1ee3a4225900c22d9634
Files discovered: 546 (221 bot modules, 278 test files, 3 root .py, configs/docs/workflows)
Files analyzed: 221 bot modules inventoried + import-graph classified; 34 modules (14,112 LOC)
                read end-to-end; 23 modules (7,117 LOC) partially; root entrypoints, Dockerfile,
                railway.toml, quality.yml, run_offline.py read.
Approx. LOC: 83,580 Python total (bot 51,090; tests 31,975; root 855). ~21k LOC line-audited.
Tests discovered: 274 test_*.py (1607 test cases). Executed: 1607 (+14 new). Passed: 1603. Failed: 1 (env).

FINDINGS:   P0: 3   P1: 8   P2: 14   P3: 7   (32 total)

ARCHITECTURE
  Runtime = engine.py + ~100 order-sensitive monkeypatch overlays (runtime_bootstrap/runtime_overlays).
  Duplicate logic: ≥6 families (partial TP ×4, stagnation exit ×4, risk sizing ×3, log redaction ×2,
    daily stop ×3, trailing ×2).
  Dead code: 20 modules unreachable from production entrypoint (most are offline research/CI tools;
    score_weights, log_redaction_hardening, kucoin_order_forensics, nexus_calibration appear dead);
    6 config variables with no consumer; 30 unused imports.
  Unsafe fallbacks: indicator exception defaults that add score (core), synthetic candle defaults in
    _open pre-trade, silent optuna=None.
  Fail-open paths: canonical readiness bypass on LIVE native TP/SL entries (F-002);
    malformed exchange position rows silently dropped (F-014).
  Race conditions: durable "orders" block cleared by unrelated persist (F-010).
  Blind excepts: 446 (ruff BLE001), 469 `except Exception`.

EXECUTION
  LIVE dispatch paths: 1 entry path (TradingEngine._open → place_order chain), 9 reduce-only exit
    paths, 1 stop-repair path (_post /api/v1/orders closeOrder stop), 1 margin-mode mutation.
  Potential dispatch bypasses: native TP/SL entry wrapper skips READY_FOR_NEW_ENTRIES (F-002).
  Duplicate-order risks: mitigated (deterministic clientOid, durable SUBMITTING before dispatch,
    ambiguous → recover by clientOid, pilot single_submission). Residual: exchange-side clientOid
    idempotency is unverified (author admits it); minute-window idem key.
  Position reconciliation risks: F-012 (1000× unit error in naked-guard close), F-014.
  Exchange-state inconsistencies: F-011 (WS identity), F-013 (local stop ≠ exchange stop).

STATE
  Persistence risks: F-010. Restore: validated, fail-closed (good). Restart: LIVE positions come back
  as EXTERNAL/read-only unless lineage proof (conservative); exits disabled without lineage (F-025).
  Corruption vectors: foreign/mismatched WS events overfill & reindex orders (F-011).

TRADING
  Strategy: single trend-following MTF strategy; R:R fixed by ATR multipliers (gate tautological).
  Indicator issues: F-020. Score issues: F-028 (heavy trend double counting).
  Sizing: LIVE pilot = 50% of available collateral × leverage; stop-risk sizing advisory only (F-003).
  Exit issues: F-025. Fee/slippage: modeled in backtest; BE stop ignores fees.
  Overfitting/validation: backtest not representative of live (F-015); OOS gate not wired to runtime.

TESTING
  Missing critical tests: composed runtime (only 3/274 files install overlays), native TP/SL readiness,
    WS identity validation, emergency close endpoint, unit conversion at naked guard.
  Broken invariants: INV-READY (F-002), INV-EMERGENCY (F-001), INV-IDENTITY (F-011), INV-UNITS (F-012).
  Uncovered LIVE paths: /api/close-all, composed place_order chain end-to-end.
```

**Mitigating facts (verified, positive):**
- LIVE selection is fail-closed: `bot/kucoin.py:60-110` requires `PAPER_TRADE=false` **and** `LIVE_TRADING_CONFIRMED`; `bot/pilot_release_control.py` additionally requires four more exact tokens, otherwise `validation_safety_lock` blocks every LIVE mutation. Ambiguous config ⇒ PAPER/SHADOW.
- LIVE ⇒ pilot ⇒ `MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION = 2` (`bot/pilot.py:25`), durably reserved in PostgreSQL (`bot/pilot_submission_counter.py:_reserve_db`). This caps the blast radius of F-002/F-003 to two entries per pilot session id.
- Distributed ownership: PostgreSQL advisory lock (`bot/live_execution_fence.py`) + lease (`bot/execution_ownership.py`) re-validated at the transport boundary for every new-risk POST.
- Durable SUBMITTING intent persisted strictly before dispatch (`bot/engine.py:3083-3088`, `before_dispatch`); ambiguous responses recovered by clientOid, never blindly resubmitted in pilot mode.
- Stop replacement is make-before-break with readback verification (`bot/native_stop_repair.py`).
- Snapshot restore validates the whole snapshot and fails closed (`OrderRegistry.restore`, `durable_execution.restore_engine_state`).

---

## 2. Real architecture (from code, not docs)

```
Railway → uvicorn main_hardened:app
  main_hardened: score-threshold contract → import sitecustomize → from main import app
    sitecustomize → sizing_semantics_log_hardening, runtime_truth_ws_compat/filter,
                    runtime_bootstrap.install()  (≈60 install() calls, order-sensitive)
                      → runtime_overlays.install() (≈40 more) → runtime_contract_guard (verifies
                        source file of 14 final callables + 8 markers; refuses startup on drift)
  main.lifespan: selfcheck/startup_block → KuCoinClient() → nexus_runtime_engine.TradingEngine
                 (subclass of bot.engine.TradingEngine) → _bootstrap task → engine.run()

TradingEngine.run (operator_runtime_policy wrapper → core run):
  db.init → durable.restore_engine_state → wait_for_live_execution_ownership (+heartbeat)
  → background: macro cache, news, correlations, weekly backtest, weekly optimizer, news monitor
  → _connect (balance, instruments, viable symbols, public WS 15/60/240 + ticker, private WS)
  → durable.reconcile_orders → finalize_initial_reconciliation (ONCE) → refresh_protection_readiness
  → loop every 5 s:
       _check_daily_reset, _gc_caches, _update_balance
       under _pos_lock: _guard_naked_positions → _sync_positions → _check_stagnation_and_invalidation
                        → _manage_partial_tp → _apply_trailing_stops → _check_rr_double
       refresh_protection_readiness (incl. durable_live_reconciliation.reconcile_pending)
       daily PnL checkpoint, daily stop, accounting evidence
       if active & !paused & !daily_stop & risk.can_open:
           integrity.assess → viable symbols → integrity.can_open_new → _scan_all_and_enter
```

Market data: KuCoin REST `/api/v1/kline/query` seed (cache deque 500) + WS `/contractMarket/limitCandle` (forming candle updates) + ticker WS. `market_data_integrity` overlay decides candle closure by timestamp boundary.

---

## 3. Trade call graph (entry)

| # | file | function | next | condition |
|---|---|---|---|---|
| 1 | engine.py | `run` loop | `_scan_all_and_enter` | connected, active, !paused, !daily_stopped, `risk.can_open`, integrity OK, viable symbols |
| 2 | engine.py:2193 | `_scan_all_and_enter` | `Analyzer.analyze_mtf` (overlay chain: market_data_integrity → runtime_truth → pullback_confirmation → adaptive_mtf → core) | per viable symbol, cooldown/correlation/session filters |
| 3 | strategy.py:521 | `analyze_mtf` | `score_tf` ×3, `detect_entry` | 4H regime TRENDING, 4H&1H EMA alignment, combined ≥ min_score, RSI/volume blocks, R:R ≥ MIN_RR |
| 4 | engine.py:2666 | `_open` (wrappers: post_trade_forensics → operator_loss_policy → kucoin_contract_risk_hardening → validation_safety_lock → prelive_protection_failclosed → core) | `_nexus_validate` | symbol viable, `durable.can_open` |
| 5 | engine.py | `_nexus_validate` → `nexus_ai` | approve? | `execution_allowed` within 10 s |
| 6 | engine.py | `_refresh_entry_balance`, `pilot.can_open_pilot` | sizing | balance > 0 |
| 7 | engine.py:2741 | pilot: `minimum_base_quantity` (patched → final_sizing_invariants) | qty | 50 % available × leverage, loss budget ≤ 50 % of margin |
| 8 | engine.py | `scoring.calculate` pre-trade | liquidation analysis | `aprovado` |
| 9 | engine.py:3011 | idem key, clientOid, registry SUBMITTING, `durable.persist_orders(strict)`, `pilot.reserve_submission` | `client.place_order` | persisted |
| 10 | kucoin chain | `live_execution_fence` (PG lock) → `pilot_submission_counter` → **`kucoin_native_tpsl`** (LIVE & sl/tp>0) | `_ensure_cross_margin` → `_post('/api/v1/st-orders')` | CROSS verified |
| 11 | kucoin.py:605 | `_post` chain: partial_tp `_post_sized_reduce_only` → cross_margin → core `_post` | `assert_exchange_mutation_allowed` → `_fenced_entry_post` (ownership + pilot durable budget) | capability LIVE |
| 12 | KuCoin REST | `POST /api/v1/st-orders` | — | — |

Reverse flow: KuCoin private WS `symbolOrderChange` → `kucoin._handle_private_order_event` → `OrderRegistry` transition → `persist_callback` (durable persist). REST: `wait_for_fill` → `get_order_status`; `durable_live_reconciliation.reconcile_pending` (byClientOid) each cycle; positions via `get_positions` in `_sync_positions`, `integrity.assess`, `_guard_naked_positions`.

## 4. LIVE order call graph (all mutation paths)

| source | caller | callee | preconditions | risk gates | final dispatch |
|---|---|---|---|---|---|
| entry | `engine._open` | `place_order` (native TPSL) | durable intent, NEXUS approve, pilot budget | ownership+fence, capability, pilot budget; **not** READY_FOR_NEW_ENTRIES (F-002) | `/api/v1/st-orders` |
| entry fallback | `engine._open` w/o sl/tp (unreachable: `_open` rejects sl/tp ≤ 0) | core `place_order` | — | full readiness | `/api/v1/orders` |
| partial TP (LIVE) | `durable_partial_exit.check` | `place_order(reduce_only, single_submission)` | durable intent | fence (reduce allowed) | `/api/v1/orders` reduceOnly |
| 2R exit (LIVE) | `confirmed_rr_exit.check` | same | durable intent | same | same |
| stagnation/invalidation | `stagnation_time_hardening`, `exit_policy_telemetry` | `place_order(reduce_only)` | — | fence | same |
| naked position close | `prelive_protection_failclosed._guard…` | `place_order(reduce_only, qty=CONTRACTS)` | set_stops failed | fence | **wrong unit (F-012)** |
| post-open emergency close | `engine._open` (sl_tp_failed) | `place_order(reduce_only)` | — | fence | same |
| stop repair / trailing / BE | `native_stop_repair.set_stops` | `client._post('/api/v1/orders', closeOrder stop)` | readback | capability; not fence (reduce-only) | `/api/v1/orders` stop |
| margin mode | `kucoin_native_tpsl._ensure_cross_margin` | `_post('/api/v2/position/changeMarginMode')` | entry in progress | capability | config mutation |
| margin requirement | `cross_portfolio_stress._margin_requirement` | `_post('/api/v2/getCrossModeMarginRequirement')` | read-like POST | capability | read |
| PAPER legacy | `risk.check_partial_tps` | `place_order` | **dead** (never called) | — | — |
| HTTP | `/api/close-all` | `engine.close_all_positions` | **method does not exist (F-001)** | bearer + header | none |

No Telegram command path reaches order dispatch (notifier is outbound only). Tests cannot reach the network (`run_offline` audit hook).

## 5. Order state machine (actual)

States: `CREATED → SUBMITTING → SUBMITTED → PARTIALLY_FILLED → FILLED`; terminals `FILLED, REJECTED, CANCELLED, FAILED` (`bot/order_state.py:42-55`). No `ACKNOWLEDGED`, `CANCEL_PENDING`, `EXPIRED` states. Stale REST `SUBMITTED` after WS fill is a monotonic no-op (good). Persisted on every `persist_orders`; private-WS transitions persist via callback.
Gaps: `PARTIALLY_FILLED → PARTIALLY_FILLED` is non-idempotent by design (accepts any filled_qty, including decreases); `filled_qty` is never bounded by `qty` (F-011 shows `filled_qty=7` on a `qty=2` order); `avg_price` is the last `matchPrice`, not VWAP.

---

## 6. Findings

Template fields abbreviated where obvious. "Repro" = test in `tests/test_forensic_audit_repro.py`.

### P0 / CRITICAL

#### F-001 — Emergency `/api/close-all` closes nothing and stops position management
- **Category:** execution / kill-switch · **File:** `main.py:305-310` · **Function:** `close_all`
- **Code:** `engine.stop()` then `result = await engine.close_all_positions()`
- **Expected:** close all positions (reduce-only, confirmed) and keep managing until flat.
- **Actual:** `TradingEngine.stop()` (`bot/engine.py:690-694`) sets `entries_paused`, `active=False`, `_running=False` → the `run()` loop exits and its `finally` cancels all background tasks (ownership heartbeat included). Then `close_all_positions` raises `AttributeError` — the method is defined nowhere in the repository (grep: only the call site). HTTP 500.
- **Root cause → trigger → path → failure → impact:** method removed/never ported in the KuCoin migration → operator presses emergency close (with bearer + `X-Confirm-Action`) → `main_hardened.destructive_admin_guard` → `main.close_all` → `engine.stop()` → `AttributeError` → positions stay open, the engine no longer runs naked-position guard, trailing, partial TP, 2R exit, or reconciliation; only exchange-native SL/TP remain. Restart via `/api/resume` possible but the operator is told nothing closed.
- LIVE impact: YES · Can cause loss: POSSIBLE · Duplicate order: NO · Corrupt state: NO · Bypass risk: NO
- **Repro:** `F001EmergencyCloseAllTests` (actual-behaviour test passes; correct-behaviour test is `expectedFailure` with `AttributeError`).
- **Fix (minimal):** implement `close_all_positions` on the engine using the existing confirmed reduce-only flow (per symbol: exchange size → base qty → `place_order(reduce_only=True)` → `wait_for_fill` → re-read positions), and replace `engine.stop()` with `engine.pause_entries()` so management continues. Fix risk: medium (new mutation path) — needs tests with fake exchange.

#### F-002 — LIVE protected entries bypass the canonical READY_FOR_NEW_ENTRIES authority
- **Category:** risk gate bypass · **Files:** `bot/kucoin_native_tpsl.py:135-285`, `bot/kucoin.py:1080-1102`, `bot/runtime_readiness.py:58`
- **Expected:** every OPEN_NEW_RISK dispatch evaluates `assert_ready_for_new_entries(engine)` (instruments, durable DB, financial sanity, **initial reconciliation**, ownership, exchange, market data, capability, **protection readiness**).
- **Actual:** the only call site of `assert_ready_for_new_entries` is the core `KuCoinClient.place_order`. `place_order_with_native_tpsl` handles every LIVE entry with `sl>0 or tp>0` (all entries — `_open` rejects sl/tp ≤ 0) and **never calls the original**; it posts `/api/v1/st-orders` directly. The outer wrappers (`pilot_submission_counter`, `live_execution_fence`) and the transport (`_fenced_entry_post`) check ownership, capability and the pilot budget only. `_initial_reconciliation_complete` and `_protection_system_ready` are consumed exclusively by `runtime_readiness` → never enforced for LIVE entries. `finalize_initial_reconciliation` runs once at startup; checks such as "active exchange order claiming BGX identity without durable match" (`initial_reconciliation.py:~207`) have no other enforcement point.
- The existing test `tests/test_kucoin_native_tpsl.py` asserts `client.original_calls == []`, i.e. codifies the bypass.
- Partial overlap (mitigations): `durable.can_open` (DB/unresolved intents), `IntegrityGuard` (positions without stop, divergence, external positions) and the pilot budget still apply.
- LIVE impact: YES · Can cause loss: POSSIBLE · Duplicate order: NO (indirectly possible if an unattributed BGX order is live) · Corrupt state: NO · **Bypass risk: YES**
- **Repro:** `F002NativeTpslReadinessBypassTests` — core path raises `READY_FOR_NEW_ENTRIES=false` with no POST; native path POSTs `/api/v1/st-orders` with the same not-ready engine; correct-behaviour test is `expectedFailure`.
- **Fix:** in `place_order_with_native_tpsl`, for non-reduce LIVE entries call `critical_state.assert_available_for_new_risk()` and `assert_ready_for_new_entries(self._engine)` (and `self._engine is None ⇒ refuse`) before `_ensure_cross_margin`. Update `test_kucoin_native_tpsl` fake to provide a ready engine. Regression risk: low; may block entries that currently pass while readiness is false — that is the intended policy.

#### F-003 — LIVE pilot per-trade loss allowance ≈ 25 % of available collateral; configured `MAX_RISK_PCT` is not authoritative
- **Category:** capital risk / sizing policy · **Files:** `bot/operator_runtime_policy.py:23,223-317`, `bot/final_sizing_invariants.py:16-150`, `bot/final_loss_budget.py:10-56`, `bot/config.py:38`
- **Actual math (LIVE pilot):** `qty = floor((0.50 × available × LEVERAGE) / (price × multiplier))`. RiskManager's stop-risk quantity is only required to be `> 0` (`_select_final_quantity`). The only loss check: `projected = qty·(|entry−stop| + entry·cost) ≤ 0.50 × margin`, i.e. ≤ 0.50 × 0.50 × available = **25 % of available collateral per trade** (before gaps/funding). With `MAX_POSITIONS=2`, two simultaneous stop-outs ≈ 50 %. The daily stop (3 %) cannot contain a single stop-out. `MAX_RISK_PCT=0.01` from config is advisory in LIVE.
- This is an explicit operator policy (documented in module docstrings and logs: `sizing_authority=OPERATOR_50PCT_EQUITY risk_manager_role=VALIDATION_GATE`). Per the audit rules it is **not changed**; it is reported because it violates INV-003 ("no order exceeding the risk budget") as configured.
- LIVE impact: YES · Can cause loss: YES · Bypass risk: YES (of the configured risk budget)
- **Evidence:** code above; runtime log captured during composition: `[FINAL_SIZING_INVARIANT] installed=true operator_margin_target=50pct_available …`.
- **Recommendation (needs operator decision):** make the stop-distance risk quantity a hard cap (`final_qty = min(target_qty, risk_qty)`), express the loss budget as a fraction of equity (e.g. ≤ `MAX_RISK_PCT × equity` after rounding), and recompute real risk after lot flooring.

### P1 / HIGH

#### F-010 — Durable block reason "orders" is shared; any successful persist re-authorizes entries
- `bot/durable_execution.py:150-171` (`persist_orders` → `_clear(engine,"orders")` on success), blocked by `engine.py:3115-3118` (ambiguous dispatch), `:3323-3327` (fill timeout), `durable_execution.py:386-392` (startup unresolved).
- A private-WS transition of *any* order (`persist_callback`) or a durable partial-exit persist clears the block while the ambiguous intent is still non-terminal. `reconcile_pending` re-blocks on the next protection-readiness refresh, so the window is the remainder of a loop iteration (the scan phase can take tens of seconds: NEXUS 10 s timeout per candidate).
- Can duplicate order: NO directly (different symbol) · Corrupt state: NO · Bypass: YES (temporary)
- **Repro:** `F010DurableBlockConflationTests`.
- **Fix:** separate reasons (`orders_persist` vs `orders_unresolved`); only `reconcile_pending`/startup reconcile may clear `orders_unresolved`.

#### F-011 — Private-WS events mutate internal orders without identity validation; overfill accepted
- `bot/kucoin.py:1853-1946`. Correlation by orderId, fallback by clientOid, **no check of symbol/side**; `registry.index_order_id(order_id, mo.client_oid)` maps any incoming orderId onto the internal order; `filled_qty` not bounded by `qty`.
- **Repro:** `F011PrivateWsIdentityTests` — an `ETHUSDTM` sell event carrying the BTC order's clientOid indexes `foreign-9` and marks the BTC Buy order `FILLED` with `filled_qty=7.0` (qty 2.0). The result is persisted by the callback.
- External events are untrusted (rule 15). Impact: wrong FILLED state, wrong exposure accounting, `unreconciled_filled_orders` keyed to wrong symbol.
- **Fix:** reject events whose `symbol`/`side` mismatch the ManagedOrder, refuse re-index when an orderId is already bound to another clientOid or the order already has a different `order_id`, clamp/reject `filledSize > qty`.

#### F-012 — Naked-position emergency close sends contracts as base quantity (1000× for BTC)
- `bot/prelive_protection_failclosed.py:183` passes `qty=size` where `size = abs(currentQty)` in **contracts** (`kucoin.get_positions`, `kucoin.py:1603`). `place_order` expects base-asset quantity and converts once more (`quantity.base_to_contracts`).
- **Repro:** `F012NakedGuardUnitTests` — a 5-contract XBTUSDTM position yields a 5000-contract reduce-only order. Because `partial_tp_execution_hardening` strips `closeOrder` for sized orders, correctness now depends on KuCoin clipping oversized reduceOnly orders; if KuCoin rejects instead, the naked position stays open. When ownership storage is unavailable, `_verified_reduction` rejects the oversized qty → the emergency close cannot be sent at all.
- Also: `self.positions.pop(sym)` on a mere `orderId` without fill confirmation (same file :185).
- **Fix:** `qty = engine._contracts_to_base_qty(sym, size)` (or honour `sizeUnit`), and only drop local state after fill + flat readback.
- **Correction (2026-10-02, F-012 fix):** the 1000× amplification is **not reachable in the production composition**. `main.py:118` builds `bot.nexus_runtime_engine.TradingEngine`, whose `__init__` wraps the client in `KuCoinPositionUnitAdapter`; rows reaching the guard are already BASE_ASSET (`sizeUnit=BASE_ASSET`, `sizeContracts` kept). Dynamic proof: `tests/test_emergency_close_units_composed.py` (5 contracts → POST `size="5"`). The original reproduction used the core engine with a raw-contracts fake client. Real defect = implicit unit contract (INV-EXEC-UNITS-002): the guard ignored `sizeUnit` and was correct only by virtue of an adapter in another module; any raw client (core engine, tests, refactor) produced 1000×. Severity re-rated **P2 (latent)**. Fixed by `emergency_close_quantity` (explicit unit, fail closed, request ≤ open contracts). The fill-unconfirmed `positions.pop` remains open.

#### F-014 — Malformed exchange position rows are silently dropped and interpreted as "closed"
- `kucoin.get_positions` drops rows that fail `float()` parsing with a warning (`kucoin.py:1619-1627`). `engine._sync_positions` (`engine.py:1440-1530`) treats a missing symbol as a remote close: records an *estimated* trade, deletes the local position, applies cooldown. The still-open position becomes "external/read-only" → partial/trailing/2R management stops.
- **Fix:** if any row is unparseable, raise `POSITIONS_UNCONFIRMED` (the method already does this for malformed envelopes) so callers fail closed.

#### F-015 — Backtest / optimizer / walk-forward results are not representative of the live strategy
- `bot/backtest.py:247-251`: 1H window 20 bars, 4H window 15 bars, while live caches hold up to 500 and `analyze_mtf` computes EMA50/ADX/ATR(14) — unwarmed (EMA seeded with first value; ADX returns 0 or raises for <28 bars, see F-020).
- Core `analyze_mtf` drops the last element unconditionally (`strategy.py:540-542`), but the backtester already passes **closed** candles (`_closed_window_by_ts`) → each decision uses data one closed bar late on all TFs (**Repro:** `F021BacktestClosedCandleParityTests`). In-process (weekly loop) the overlay stack changes `Analyzer`; in the research child (`python -S`) it does not → two different strategies are being "validated".
- Trades overlap (the `for i` loop never skips while a trade is open) — no MAX_POSITIONS/cooldown; per-trade Sharpe/MC bootstrap treat overlapping trades as independent; max DD from cumulative per-trade sum ignores concurrency; Sortino uses std of losses (not downside deviation).
- OOS evidence modules (`nexus_oos_*`) are unreachable from the runtime: no OOS gate exists for LIVE.
- **Impact:** any reported win rate/PF/Sharpe cannot be used as evidence of edge.

#### F-016 — "EV" and "R:R" gates are tautological; confidence is not a probability
- `bot/nexus_probability.py:17`: `P(win) = min(0.75, 0.30 + 0.45·confidence/100)` (self-described "not empirically calibrated"). With R:R ≥ 2, EV > 0 for confidence > ≈7 % → the EV gate cannot reject a strategy signal.
- `bot/strategy.py:665-669`: `sl_mult/tp_mult` ∈ {1.2/3.6, 1.5/3.0, 2.0/4.0} ⇒ R:R ∈ {3, 2, 2}; `MIN_RR_RATIO=2.0` never filters. `find_support_resistance`/`calc_sl_tp` exist but are not used by `analyze_mtf` (SL/TP are ATR-only, contradicting docstrings/README).
- `Signal.confidence = combined/100` — uncalibrated.

#### F-017 — Test suite does not exercise the production runtime
- `tests/run_offline.py:58-63` runs every suite with `python -S` (sitecustomize/overlays not installed). Only 3/274 test files install `runtime_bootstrap`/`runtime_overlays`. Behaviour that exists only in the composition (F-002 bypass, chain ordering, closeOrder stripping) is untested; the native-TPSL test codifies the bypass. A green suite says little about production.

#### F-018 — Core sizing ignores stop distance; leverage scales "risk"
- `bot/risk.py:370`: `target_notional = balance × LEVERAGE × MAX_RISK_PCT`; `size()` has no stop input. Real stop risk = notional × stop% varies with ATR; doubling leverage doubles exposure ("risk × leverage" error, topic 17). Applies to PAPER/non-pilot; in LIVE it is only a `>0` gate (F-003). `calc_position_size` (same formula) and `check_partial_tps`/`PositionRisk` are dead code.

### P2 / MEDIUM

- **F-013 Local SL/TP shifted after fill, exchange stops stay at original levels** — `engine.py:3430-3434` shifts `sig.entry/sl/tp` by the fill delta after the native TP/SL order was already placed with the original `sl/tp`. Local `Position.sl` ≠ exchange stop; `_guard_naked_positions` later re-applies the *shifted* stop; real risk = |fill − original SL| exceeds the budget validated pre-fill. Magnitude bounded by slippage.
- **F-019 `_post` retry semantics** — `kucoin.py:636`: auth headers (timestamp) computed once outside the retry loop → after 429 backoff the signature is stale → `400005` → API-version flip v2↔v1 (auth flapping). `kucoin.py:732`: unknown non-permanent error codes (e.g. insufficient balance 300003, risk limit) fall through and immediately re-POST (no sleep) up to 3× when not `single_attempt`. `_recover_ambiguous_order` only handles `/api/v1/orders`, not `/api/v1/st-orders` (the native path recovers itself).
- **F-020 Indicators** (Repro: `F020IndicatorTests`) — RSI of a flat series = 99.99 (should be 50; `indicators.py:47-55`, `al==0 ⇒ rs=1e9` even when `ag==0`); `atr()` raises IndexError for <14 bars and its first 13 values are 0, so 20-bar ATR averages include zeros (`atr_expanding` inflated); `adx()` raises IndexError for 16–27 bars despite its `n < period+2` guard; EMA seeded with first value (no SMA seed) and accepted with 9 bars for EMA50 (`analyze_mtf` allows 4H ≥ 10 bars); `vwap()` without timestamps is a rolling 96-bar VWAP on every TF (labelled "session").
- **F-021 Closed-candle parity** — see F-015 (repro).
- **F-022 `wait_for_fill`** — `kucoin.py:1549`: `filled = filled_sz > 0 and not cancelExist` ⇒ partial fill + cancel reported as not filled. Mitigated in `_open` by immediate `_reconcile_exchange_positions(only_symbol)`.
- **F-023 Close accounting is estimated** — `engine.py:1462-1490` uses last local mark × taker fee; daily-stop decisions run on estimates until `daily_pnl_exchange_reconciliation` confirms.
- **F-024 Ticker WS fields** — `kucoin.py:2261-2270`: `volume = data["size"]` (last trade size, not volume); cached ticker has no timestamp, yet `_open` uses `get_cached_ticker` as fill-price fallback.
- **F-025 Exit logic economics** — LIVE: 50 % closed at ≈1R + 3 bps (`durable_partial_exit.py`), stop → exactly entry (no fees ⇒ BE exit is a net loss ≈ 0.12 % + slippage on the remaining half); afterwards `confirmed_rr_exit` can never fire (`distance = |entry − stop| = 0 ⇒ continue`); both exits raise and are skipped when the opening-order lineage is missing (`confirmed_rr_exit.identity`), e.g. after restart. Initial SL/TP trigger on last trade price (`stopPriceType="TP"`), moved stops on mark price (`"MP"`).
- **F-026 Config drift** — `DRAWDOWN_MODE` (default ADVISORY) vs operator hard gate (always on unless `LIVE_RISK_OVERRIDE_APPROVED`) = two configs for one policy; `MAX_RISK_PCT` not normalised (a value of `1` means 100 %) while `MAX_DRAWDOWN` is; dead vars: `MIN_CONFIDENCE`, `SL_ATR_MULT`, `TP_ATR_MULT`, `TRAILING_LOCK_R_MULT`, `COOLDOWN_SECONDS`, `REPORT_INTERVAL_H`; 138 distinct env vars total.
- **F-027 DailyTracker** — `check_reset/recalc_limits` never called in LIVE ⇒ weekly/monthly stops are 0 (inert); daily reset/stop correctness depends on the `daily_stop_runtime_hardening` overlay.
- **F-028 Score construction** — trend family (ADX, EMA stack, SMC BOS/HH-HL, VWAP) up to 30/100 points per TF (`trend_s` cap), plus EMA alignment already required by the direction filter, plus 4H weight 25 % after 4H pre-filter ⇒ heavy double counting; core exception defaults add points (`strategy.py:335,359,393,420`; partly corrected by `scoring_safety_hardening`, which itself defaults `ci_trend=True` on error); `orderbook` never passed ⇒ `ob_ok` always False; `adx_aligned` computed but not used in core; `vol_r = vols[-1]/avg_vol` divides by zero when the 20-bar volume average is 0 (len > 21 branch).
- **F-029 Malformed-candle defaults in pre-trade** — `engine.py:2781-2784` fills missing `c/h/l` with `sig.entry` and missing volume with 1000.0.
- **F-031 Partial-key and passphrase-character logging in core** — `kucoin.py:238-256` logs `API_KEY[:6]…[-4:]`, lengths and the list of special characters in the passphrase; redacted only when the `auth_log_redaction` overlay is installed (not in tests/tools run with `-S`).

- **F-038 Shadow observers run inside every `analyze_mtf` call** — `bot/mtf_strategy_observability.py:14-133` synchronously runs 5 shadow analyses (`volume_gate_shadow`, `score_floor_shadow`, `session_penalty_shadow`, `mtf_shadow`, `htf_transition_shadow`) after the production analysis. Evidence: with the decorator, one `analyze_mtf` call invoked `score_tf` 9 times (vs 3 undecorated), one on the full 60-candle window. Effects: CPU-bound work on the asyncio loop per symbol per scan; the backtester/optimizer call `analyze_mtf` per historical bar, so they pay ~3× compute and feed historical candles into shadow cohort/outcome state (telemetry contamination).

### P3 / LOW

- **F-030** `tests/run_offline.py:123` regex `r'skipped=(\\d+)'` (raw string with escaped backslash) never matches ⇒ `SKIPPED` always 0 (Repro: `F030RunnerSkipCountTests`).
- **F-032** README/CHANGELOG describe Bybit, 50× leverage, 4 positions, 80 % drawdown — stale vs code (KuCoin, 10×, 2, 10 %).
- **F-033** 446 blind `except Exception` (ruff BLE001); 30 unused imports, 6 redefinitions.
- **F-034** Dead/unreachable modules: `score_weights` (222 LOC), `log_redaction_hardening`, `kucoin_order_forensics`, `nexus_calibration`; dead functions `risk.calc_position_size`, `risk.check_partial_tps`, `strategy.find_support_resistance/calc_sl_tp`, `operator_loss_policy.stop_price` (self-declared legacy).
- **F-035** `bot/optimizer.py:22-27` silently degrades to `optuna=None` → later `AttributeError`.
- **F-036** Any Python process with the repo on `PYTHONPATH` auto-imports `sitecustomize` and installs the full overlay stack (side effects for tooling/scripts).
- **F-037** `_check_daily_reset` compares day-of-month only (`engine.py:699`) — harmless in practice.

---

## 7. Cross-cutting sections

**Duplications (semantic):** partial TP — core `_manage_partial_tp`, `partial_tp_execution_hardening` (PAPER), `durable_partial_exit` (LIVE), `risk.check_partial_tps` (dead); stagnation/invalidation exits — core, `runtime_hardening` (PAPER), `stagnation_time_hardening`, `exit_policy_telemetry`/`operator_loss_policy`; sizing — `risk.RiskManager.size`, `risk_manager_v3`, `pilot_live_runtime`, `operator_runtime_policy`, `final_sizing_invariants`; log redaction — `auth_log_redaction` (used) vs `log_redaction_hardening` (dead); daily stop — engine, DailyTracker, `daily_stop_runtime_hardening`, `durable_daily_stop`. Which is used is decided by install order; `runtime_contract_guard` only checks the source file name of 14 callables.

**Fail-closed vs fail-open (critical gates):** environment selection FAIL-CLOSED; ownership/fence FAIL-CLOSED; durable restore FAIL-CLOSED; integrity guard FAIL-CLOSED (stale assessment ⇒ blocked); pilot budget FAIL-CLOSED; canonical readiness on LIVE entry **FAIL-OPEN (F-002)**; exchange positions parsing **FAIL-OPEN per row (F-014)**; durable "orders" block **AMBIGUOUS (F-010)**; scoring exception defaults FAIL-OPEN (core) / mostly closed (overlay).

**Race conditions:** F-010; `_scan_all_and_enter` runs outside `_pos_lock` (entries and management don't overlap in the main loop, but WS callbacks persist concurrently); integrity snapshot can be up to `INTEGRITY_MAX_AGE=300 s` old at dispatch.

**Restart:** orders restored and reconciled by clientOid, never resubmitted (good); LIVE positions are not persisted — they are rebuilt from the exchange as EXTERNAL unless exact lineage proof; exits keyed on lineage are disabled when proof is missing (F-025).

**Observability:** strong log correlation (`clientOid`, `orderId`, `setup_id`, forensic lineage keyed by opening order id); trade reconstruction is possible from logs for LIVE. Gaps: closes are estimates until accounting reconciliation; no single `intent_id` spanning signal→exit in PAPER.

**Telegram:** outbound only; serialized delivery lane overlay; core `notify()` is awaited inline in `_open`'s emergency branch (can sleep on 429 `retry_after`).

**Security:** no hard-coded credentials, private keys or `.env` in tracked files (test fixtures contain synthetic DSNs only). `.gitleaksignore` whitelists a historical finding (commit not in this shallow clone; current line is a constant key name). Core partial-key logging (F-031). Admin API requires `BOT_API_SECRET` (503 if unset) + rate limit; close-all also requires a confirmation header.

---

## 8. Formal invariants and status

| id | invariant | status |
|---|---|---|
| INV-001 | No LIVE order without valid ownership | HOLDS (fence + transport validation) |
| INV-002 | No LIVE entry without confirmed durable state | HOLDS mostly; transient gap F-010 |
| INV-003 | No order exceeding the configured risk budget | **BROKEN by policy (F-003)** |
| INV-004 | One intent ⇒ at most one entry order | HOLDS (durable intent + pilot single submission); exchange clientOid idempotency UNVERIFIED |
| INV-005 | External events cannot alter internal order identity | **BROKEN (F-011)** |
| INV-006 | Child orders cannot overwrite parent identity | **AT RISK (F-011)** |
| INV-007 | Restart converges to exchange truth | HOLDS for orders; positions become read-only externals |
| INV-008 | No position unprotected beyond the operational window | AT RISK (F-012 close unit; F-001 kill switch) |
| INV-009 | NaN/None cannot reach sizing/execution | HOLDS in final sizing/loss budget (finite checks); F-029 defaults upstream |
| INV-010 | Runtime without fencing cannot send orders | HOLDS |
| INV-READY | Every OPEN_NEW_RISK passes READY_FOR_NEW_ENTRIES | **BROKEN (F-002)** |
| INV-UNITS | Exchange contracts are converted exactly once | **BROKEN (F-012)** |

## 9. Scores (current state, 0–10)

| area | score | evidence |
|---|---:|---|
| Software Architecture | 3 | correctness depends on ~100 order-sensitive monkeypatches; contract guard is a name check |
| Code Quality | 4 | 446 blind excepts, stale comments/docs, heavy duplication; good local docstrings |
| Reliability | 5 | many fail-closed guards; F-001/F-014 |
| Execution Safety | 5 | strong idempotency/fence/readback; F-002, F-012 |
| State Integrity | 6 | validated restore, durable intents; F-010, F-011 |
| Concurrency Safety | 6 | single loop + `_pos_lock`; WS callback races |
| Risk Management | 3 | LIVE loss allowance ≈25 %/trade (F-003); stop-agnostic core sizing (F-018) |
| Strategy Design | 3 | single trend strategy, tautological R:R/EV gates, chasing-prone MOMENTUM entry |
| Signal Quality | 3 | double-counted trend features, indicator defects, uncalibrated confidence |
| Backtesting Quality | 3 | costs modeled, but non-representative windows, overlapping trades, parity bug |
| Observability | 7 | rich structured logs and lineage |
| Test Coverage | 4 | 1607 tests but composed runtime barely tested |
| Production Readiness | 3 | P0s open; no OOS evidence gate |

## 10. Top priorities

- **Operational:** F-001 kill switch; F-014 malformed rows; F-012 naked-close units.
- **Capital:** F-003 loss allowance; F-018 stop-agnostic sizing; F-025 BE exit net-negative.
- **Execution:** F-002 readiness bypass; F-019 retry semantics; F-022 partial+cancel.
- **State integrity:** F-011 WS identity/overfill; F-010 block conflation; F-013 local vs exchange stop.
- **Code quality:** overlay composition (F-017/F-036), duplicates, blind excepts.
- **Strategy weaknesses:** tautological gates (F-016); trend double counting (F-028); unwarmed HTF indicators.
- **Expectancy hypotheses (to test, not claims):** (H1) BE stop at entry+fees instead of entry; (H2) remove MOMENTUM entries (body > 0.25 ATR after impulse = chasing) — ablation; (H3) structure-based SL vs fixed ATR multiples; (H4) regime filter by ADX with DI direction instead of ADX>20 any direction.
- **Drawdown hypotheses:** stop-risk-capped sizing at ≤1 % equity; portfolio-level stop stress already exists (`cross_portfolio_stress`) — calibrate to equity, not margin.
- **Cost reduction:** maker entries/exits where latency allows; avoid 15-m micro-targets below 3× round-trip cost.
- **Missing tests:** composed-runtime LIVE dispatch with fake exchange; `/api/close-all`; WS identity; unit conversion at every `place_order(reduce_only)` call site; skip accounting.
- **Invariants to enforce in code:** INV-READY at the final transport boundary; INV-UNITS via typed quantities; INV-005 in the WS handler; filled ≤ qty.

## 11. Hypothesis template (example)

```
IMPROVEMENT: break-even stop includes round-trip cost
HYPOTHESIS: BE exits after TP1 are currently net-negative (≈ −0.12 % − slippage on 50 % size).
CURRENT PROBLEM: durable_partial_exit moves SL to exactly entry.
PROPOSED CHANGE: SL = entry ± (2·taker + est. slippage)·entry.
EXPECTED METRIC: higher net expectancy per trade; unchanged gross win rate.
TRADE-OFF: slightly more BE stop-outs.
OVERFITTING RISK: low (cost-derived, no free parameter).
BACKTEST / OOS / WALK-FORWARD: only after F-015 is fixed (representative windows, no overlap).
ACCEPTANCE: net expectancy ↑ with CI excluding 0 on OOS; MDD not worse.
REJECTION: no significant change or MDD worse.
```

## 12. Assumptions / unknowns

- EXCHANGE: KuCoin Futures (code). Fee tier: UNKNOWN (code default taker 0.06 %, fetched at runtime when possible).
- Exchange idempotency of duplicate `clientOid`: UNVERIFIED (no real exchange access).
- KuCoin behaviour for oversized `reduceOnly` without `closeOrder`: assumed clipped per public docs — UNVERIFIED (affects F-012 severity).
- Production env vars (LEVERAGE, MAX_POSITIONS, pilot tokens): UNKNOWN — Railway variables were not read.
- Trade history / DB: not in the repository ⇒ trade segmentation, expectancy, MAE/MFE, correlation/MI analyses (sections 8, 14, 46) **not performed**; infrastructure exists (`/api/expectancy`, forensic lineage).
- Modules marked INVENTORY in the appendix (≈30k LOC, e.g. `nexus_ai` internals, `market_data`, `database`, many observability overlays) were not line-audited in this pass.

## 13. Remediation roadmap (proposed — one root cause per patch)

| phase | objective | files | change | tests | approval |
|---|---|---|---|---|---|
| 1 Capital safety | kill switch + readiness + units | `main.py`, `engine.py`/`nexus_runtime_engine.py`, `kucoin_native_tpsl.py`, `prelive_protection_failclosed.py` | F-001, F-002, F-012 | flip `expectedFailure` in repro suite; fake-exchange tests | repro tests green, full suite no regression |
| 1b Capital policy | operator decision on F-003 | `final_sizing_invariants.py`, `final_loss_budget.py` | cap qty by stop risk | property tests: risk ≤ budget after rounding | operator sign-off |
| 2 Execution integrity | WS identity, retry semantics | `kucoin.py` | F-011, F-019, F-022 | adversarial WS/REST ordering tests | idempotence ×1/×2/×10 |
| 3 State integrity | block reasons, malformed rows, local stop | `durable_execution.py`, `kucoin.get_positions`, `engine._open` | F-010, F-014, F-013 | restart/crash matrix | convergence tests |
| 4 Concurrency | WS persist vs scan | `durable_execution.py` | serialize clear/block | race test | — |
| 5 Observability | single intent id across PAPER/LIVE | engine/forensics | additive | log contract test | — |
| 6 Test coverage | run a composed-runtime suite | `tests/run_offline.py` + new suite | install overlays in PAPER | ≥1 E2E per mutation path | CI gate |
| 7 Strategy validation | representative backtest | `backtest.py`, `strategy.py`, `indicators.py` | F-015, F-020, F-021 | parity test live vs replay | identical decisions on fixture |
| 8 Quant optimization | H1–H4 with ablation | strategy/exits | one hypothesis per change | OOS + walk-forward | acceptance criteria above |
| 9 Paper/forward | ≥ N weeks paper with new build | — | — | — | metrics within CI of backtest |
| 10 Controlled LIVE | pilot budget 2 | — | — | — | production gate below |

## 14. Production gate (current status)

- [ ] no order can bypass risk gates — **F-002, F-003**
- [x] no order can bypass ownership/fencing
- [x] duplicate-order protection validated (offline; exchange idempotency unverified)
- [~] reconciliation validated — F-014
- [~] restart safety validated — orders yes; exits after restart F-025
- [~] durable-state corruption protected — F-011
- [~] exchange errors handled — F-019
- [x] quantity/price filters correct for entries (Decimal floor, tick overlay) — [ ] naked-close unit F-012
- [~] fees/slippage incorporated — backtest yes; BE exit no
- [ ] critical invariants automated — INV-READY/UNITS/005 missing
- [x] suite without relevant regressions (1 env failure)
- [?] dry-run / paper / testnet validated — not verifiable offline
- [ ] strategy out-of-sample validated — F-015
- [ ] forward test performed — unknown
- [x] observability sufficient to reconstruct each LIVE trade (estimates until accounting reconciliation)

**Verdict:** not ready for unattended LIVE. Proposed patch sequence: F-001 → F-002 → F-012 → F-011 → F-010 → F-014 (each one minimal patch + flip of its reproduction test), then operator decision on F-003, then F-015/F-020 before any strategy optimization.
