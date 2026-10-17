# NEXUS-7 engineering readiness review — 2026-09-29

This review makes no claim of profitability, "10/10", "production ready" or
"safe". Each statement below is backed by a test, a log line or code, or it
is marked as not proven.

## 0. Baseline (Phase 0)

| Item | Value |
|---|---|
| Branch | `fix/link-rejected-intent-recovery` (PR #435) |
| PR HEAD before this work | `46a38377d482af3f0288520c6e7cfa530f1862ec` |
| Base | `migration/binance-usdm` @ `8e220d3a720ee641650f498f781100c7c66dac78` (= production SHA) |
| Ahead/behind at start | 6 ahead / 0 behind; clean working tree |
| Production | Railway `8f801e05-8023-466d-8b59-2f134cb0a7b3`, Binance USD-M, LEVERAGE 50 |
| Production state (read-only logs, 15:56–16:18 UTC) | `PILOT_LIVE_PREFLIGHT result=BLOCKED private_stream=reconcile_required` (the unresolved LINK intent); `[ENTRY_RISK_STATE] drawdown=9.05% configured_limit=10.00% override=False` |

## 1. Findings by severity

| Sev | Finding | Root cause | Status |
|---|---|---|---|
| P0 (release blocker) | PR #435 CI red: `test_full_budget_cannot_bypass_cross_stop_stress` | The fixture predated the new actual-leverage gate and returned no `leverage`, so evaluation stopped at `state_candidate_configured_leverage_unconfirmed` before the stress math. The gate is correct; the fixture drifted. | **Fixed**: fixture now has `leverage: 50`, the original assertion holds again, plus 3 unit regressions and 5 full-dispatch scenarios. Mutation check: removing the gate fails 5 tests. |
| P1 | Durable order gate opened before persistence in the hardened startup reconcile's empty-pending path | `_clear(engine, "orders")` ran before `await persist_orders(...)`, so `durable_state_ok` read True while nothing had been written (reachable after the LINK terminalization when an earlier persist failed). | **Fixed**: premature clear removed (`persist_orders` clears only on success). The test failed before the change and passes after. |
| P1 | Audited LINK recovery runs only in the startup reconcile | The periodic `reconcile_pending` uses the convenience lookup, which collapses -2013 to `{}` and is correctly never treated as proof. A transient failure during startup recovery therefore needs a restart. | Documented (fail-closed); consolidation planned in the authority map §4. |
| P1 | Existing OOS replay uses **KuCoin** data, funding and cost model | `nexus_oos_real_replay` / `backtest._kucoin_page` / `kucoin_execution_model` predate the Binance migration. | Documented; see §5. Not fixed. |
| P1 | Deploy governance does not gate production | Production deploys `migration/binance-usdm` with `checkSuites=false`, builder RAILPACK and no pre-deploy command. `ci_attest.yml` attests only `main` pushes. `ci_deploy_gate.REPO="AA007A/nexus7-bot"` (legacy slug). `railway.toml` declares DOCKERFILE, which production does not use. | Documented with a concrete proposal (§6). Not changed (requires an operator Railway/GitHub change). |
| P2 | Drawdown hard gate has an explicit env override (`LIVE_RISK_OVERRIDE_APPROVED=true` → ALLOW) | Deliberate operator lever (`docs/LIVE_RISK_OVERRIDE.md`). | Production log shows `override=False`. Listed under operator-approval items; not changed. |
| P2 | Duplicate authorities (drawdown/HWM, daily PnL, regime) | Historical wrapper layering. | Mapped in `NEXUS_RUNTIME_AUTHORITY_MAP.md`; incremental plan. |
| P3 | `engine.py` has 4148 lines and mixes orchestration with policy | History. | Extraction plan with a characterization-first rule. |

## 2. LINK incident (Phase 2)

The recovery in `durable_reconcile_hardening._recover_audited_link_rejection`
was audited against every cumulative condition requested. It checks:

- the exact clientOid, symbol, side and qty (19.34);
- the time window 07:45:00–07:45:22 UTC;
- SUBMITTING state, no orderId, zero fill, zero avgPrice, not reduce-only,
  exposure intent INCREASE;
- no local position, not paper, a real `BinanceClient`, and the exact
  Railway project and environment ids;
- ownership valid (Postgres lease + fencing token + DB available) **before**
  the reads;
- an exact `GET /fapi/v1/order` → HTTP 400 `-2013`;
- every Binance position zero, no open orders and no open algo orders;
- LINKUSDT CROSS with leverage in 1..125 (read only);
- ownership valid again **after** the reads, and the in-memory evidence
  re-checked after the async reads.

Only then does it transition SUBMITTING → REJECTED. It persists strictly
before the gate clears, and it never resubmits or mutates the exchange.

Proof: `tests/test_link_recovery_exactness.py` (12 tests). A single-condition
mutation matrix over all of the above leaves the intent SUBMITTING. The rule
never queries absence for any other clientOid. Removing the post-read
ownership check, the post-read evidence recheck or the exact -2013 match
fails the suite.

Convergence is proven dynamically. Before the recovery the private stream
stays `reconcile_required`. After it, pending durable orders are 0,
`durable_state_ok` is true, `reconcile_pending` converges and the stream
reports `event_capable`.

Production convergence can only be observed after an operator-approved
deploy. The expected log lines are `[AUDITED_LINK_REJECTION] ... state=REJECTED`,
`[DURABLE_RECONCILE] all restored intents resolved`, `[PRIVATE_STREAM]
event=reconciled` and `[PILOT_LIVE_PREFLIGHT] result=PASS`.

## 3. OPEN_NEW_RISK safety (Phase 3)

See the execution graph and the 14-invariant matrix in
`NEXUS_RUNTIME_AUTHORITY_MAP.md` §2. New in this work: 9 invariant tests
through the real `engine._open` chain, each against the dispatching positive
control, requiring zero non-GET exchange requests plus gate-specific
evidence.

Transport idempotency (verified in code): `BinanceClient._post` forces
`single_attempt` for `/fapi/v1/order` and `/fapi/v1/algoOrder`. Ambiguous
errors are reconciled by `newClientOrderId`, never resubmitted. The 3-attempt
retry applies only to reads, DELETE and idempotent configuration POSTs.
Reduce-only and close paths still go through the fence and ownership, with a
verified reduce-only escape when ownership storage is unavailable.

## 4. Risk invariants (Phase 9)

| Invariant | Proof |
|---|---|
| `final_qty <= stop_risk_qty` | `test_final_sizing_invariants`, `test_sizing_truth_invariants`, Binance sizing property test (3000 cases) |
| `final_qty <= operator margin cap` | same, plus DispatchProof positive control |
| `required_margin <= available cap` | `stop_risk_size` assertion, sizing tests |
| exchange constraints | `quantity_rules` fail-closed, `test_binance_live_sizing_minimum_order` |
| CROSS stress pass | 26 DispatchProof scenarios |
| drawdown pass (fresh equity) | `test_inv09`, `test_predispatch_drawdown_race`, cash-flow tests |
| leverage never increases the loss budget | leverage-invariance test across 1x/10x/50x/125x (`test_16_17`), and in `FINAL_LOSS_BUDGET` higher leverage makes the limit stricter |
| actual leverage == configured | new regressions; unknown or mismatched → BLOCK; `cfg.LEVERAGE` never written at runtime (grep) |

No risk parameter, leverage, drawdown limit, filter or threshold was changed.

## 5. Quant / alpha, calibration, drift (Phases 6–8): **NOT COMPLETED**

### Current model (documented from code)

- **Signal:** `strategy.Analyzer` (multi-timeframe 4H/1H/15M; closed candles
  only) → `adaptive_mtf_entry` → pullback confirmation. Entry requires
  `MIN_ENTRY_SCORE` (default 60) and gross R:R ≥ `MIN_RR_RATIO` (2.0).
- **NEXUS:** `nexus_ai`:
  - regime detection;
  - model fusion weighted by `REGIME_MODEL_WEIGHTS` (TREND, MOMENTUM,
    MEAN_REVERSION, BREAKOUT, STRUCTURE, DERIVATIVES; unavailable models
    excluded, not neutral);
  - setup score from `WEIGHTS` (trend 0.15, structure 0.15, momentum,
    volume, volatility, derivatives, microstructure, MTF and R:R at 0.10
    each).
- **HEURISTIC probability:**
  `nexus_probability.heuristic_win_probability(c) = min(0.75, 0.30 + 0.45·c/100)`
  (`PROBABILITY_MODEL="ensemble_linear_v1"`). It is explicitly labelled
  "not an empirically calibrated probability". It must not be read as a win
  rate.
- **EV:** `p·gain_net − (1−p)·loss_net`, with round-trip fee and slippage. In
  LIVE the costs come from the execution-cost snapshot; the net R:R minimum
  is `NEXUS_MIN_RR_NET` (default 0.8 × gross = 1.6).
- **Calibration and statistics tooling already present:** `nexus_calibration`
  (Brier, reliability bins, ECE), `oos_model_validation`, and
  `nexus_oos_edge_gate` (paired bootstrap of baseline vs approved subset).

### Why it is not completed

1. **Data egress blocked here.** From this environment,
   `fapi.binance.com` and `data.binance.vision` are denied by the network
   policy, so no Binance-native history could be loaded.
2. **The existing replay is not Binance-native.** It uses KuCoin candles,
   KuCoin funding and the KuCoin cost model, over a single ~26-day window
   (2500 × 15m) on 5 symbols. It also marks
   `historical_context.parity_complete=false`, so it can only return
   `AI_EDGE_NOT_PROVEN`.

### Required Binance-native framework (specification, not implemented)

- **Data:** `data.binance.vision` USD-M monthly klines (15m/1h/4h), funding
  (`fundingRate`), and `exchangeInfo` snapshots for tick, step, minQty and
  minNotional. Prefer the archive to `fapi` from GitHub runners, which may
  be geo-restricted.
- **Execution model:**
  - fee: the Binance tier taker fee;
  - slippage: bookTicker spread proxy plus impact;
  - funding: 8h settlements;
  - quantity: `quantity_rules` rounding;
  - fills: next-bar open after decision, stop-first on same-bar ambiguity;
  - latency: one candle.
- **Protocol:**
  - walk-forward over at least 4 non-overlapping OOS windows;
  - purged and embargoed splits;
  - segmentation by regime, symbol, long/short and volatility tercile;
  - bootstrap 95% confidence intervals;
  - sensitivity grids reported but never auto-applied.
- **Metrics:** net expectancy per trade, profit factor, win rate,
  average win/loss, payoff, max drawdown, time under water, Sharpe,
  Sortino, Calmar, exposure, turnover, fee/slippage/funding drag, tail loss
  (CVaR 5%), and the number of independent trades.
- **Calibration:** train-window-only isotonic or Platt fit, evaluated OOS
  with reliability diagram, Brier, log loss, ECE and n per bucket. The LIVE
  probability stays heuristic until OOS evidence exists.
- **Drift, champion and challenger:** SHADOW-only telemetry for
  feature/score/regime distributions, hit rate, expectancy, spread/slippage
  and fee/funding. Promotion requires green CI, OOS evidence, shadow
  evidence **and** explicit operator approval. There is no auto-promotion.

To unblock:
- allow `data.binance.vision` (and optionally `fapi.binance.com`) in this
  environment's network settings; or
- run the framework in GitHub Actions once it is implemented.

## 6. CI/CD and Railway governance proposal (Phase 10) — not applied

Goal: **no green CI, no production deploy.** Deployment SUCCESS does not mean
the trading runtime is correct.

1. **Protected branch `migration/binance-usdm`:**
   - required checks: `Quality Check`, `Dependency, source, secrets and SBOM
     audit`;
   - up-to-date branch required, no force-push, reviews required.
2. **Exact-SHA attestation for the production branch:** extend
   `ci_attest.yml` to `branches: [main, migration/binance-usdm]`. Fix
   `ci_deploy_gate.REPO` to `007andra/nexus7-bot` and update its test.
3. **Railway:**
   - enable `checkSuites`;
   - set the pre-deploy command to `python -m bot.ci_deploy_gate`;
   - align the builder with the repository (`railway.toml` says DOCKERFILE,
     production uses RAILPACK; choose one and pin it).
4. **Immutable candidate:** deploy only the attested SHA; record the SHA in
   `[STARTUP_READY_NOTIFICATION]` and `/service_ready`.
5. **Post-deploy verification:**
   - readiness must reach `PILOT_LIVE_PREFLIGHT result=PASS` or an explained
     BLOCK;
   - otherwise use a rollback runbook (Railway rollback to the previous SHA)
     with no exchange mutation.

## 7. Observability, Telegram, chaos, performance (Phases 11–14)

- **Correlation ids:**
  - `clientOid` (`bgx7-…`) links intent → POST → ACK/-2019 → private
    stream → reconcile → protection;
  - `trace=SYMBOL-N` links `[ENTRY_LATENCY_SUMMARY]` stage timings;
  - the deployment SHA is logged at startup.
  - Gap: no single `candidate_id` spans signal → NEXUS → sizing (planned,
    observability-only).
- **Telegram:** observability only. Delivery resilience is handled in PR #436
  (canonical transport, circuit breaker, truthful `sent=`). The trading
  independence tests are in that PR.
- **Chaos coverage in tests:**
  - Binance 418/429 and 5xx (transport code; rate-limit tests);
  - timeouts, malformed JSON, WS disconnect/reconnect, listenKey
    expiration, private stream reconnect;
  - ambiguous submission (`test_inv14`, `test_exec02_ambiguous_order`);
  - crash/restart and partial fill (`test_exec03_*`, durable tests);
  - Postgres unavailable, lease expiry and takeover
    (`test_execution_ownership`);
  - exchange/local divergence.
- **Latency:** `[ENTRY_LATENCY_SUMMARY]` exists per stage. Production shows
  `NA` because no dispatch occurred (entries blocked). No authoritative read
  was replaced by a cache.

## 8. Items requiring explicit operator approval

- Merging PR #435, then deploying it (this activates the audited LINK
  recovery at startup).
- Any change to Railway (checkSuites, pre-deploy, builder) or to GitHub
  branch protection.
- Any use of `LIVE_RISK_OVERRIDE_APPROVED`.
- Allowing Binance data hosts for research.
- Any strategy, threshold or calibration change. This requires OOS and
  shadow evidence first.

## 9. Definition of Done status

| # | Criterion | Status |
|---|---|---|
| 1 | PR #435 100% green | pending CI on the pushed head (local full suite: only the sandbox-only `test_research_process` fails, which also fails on base) |
| 2–10 | suite/release proof/runtime contract/supply chain/undefined names/compileall/ruff/selfcheck | local: PASS (the supply-chain check runs in CI) |
| 11–17 | mutation tests, no bypass of durable state, ownership, fencing or CROSS stress, leverage mismatch/unknown blocks, no blind retry | PASS (tests above) |
| 18–20 | LINK exact/bounded/non-generalizable, no unresolved intent after recovery, stream convergence | PASS offline; production only after an approved deploy |
| 21 | authority map | delivered |
| 22 | quantitative validation report | **NOT MET**: blocked (data egress; KuCoin-based replay) |
| 23 | no strategy/risk change | PASS |
| 24 | no secrets | PASS (diff and logs reviewed) |
| 25 | no automatic deploy | PASS |

**Release decision:** PR #435 can be recommended for merge once its CI is
green, because its safety scope is proven. Items 22 and the Phase 6–8
program remain open, so no strategy or edge claim is made.
