# NEXUS-7 / BGX — Production consistency audit (2026-09-26)

Base: `migration/binance-usdm` @ `5efeb9c` (one commit ahead of the observed
production baseline `8a1be4b`; that commit is preserved).
Branch: `claude/audit-nexus-production-consistency-t14vu5`.
Baseline offline suite before any change: 1671/1675 passing; the only failing
suite (`tests.test_research_process`) passes in isolation (timing-sensitive
subprocess test under parallel load, pre-existing).

This report was written **before** code changes. Each item lists evidence,
affected files and change risk. "Fixed in this PR" / "Not fixed" is recorded in
the PR description and in the final section of this file.

---

## P0-1 — Sizing: execution truth ≠ what the brief (and several logs) describe

### Evidence (install order is the source of truth)

`engine._open` (pilot branch) calls the module-global
`engine.minimum_base_quantity`. That hook is monkey-patched four times; the
**last** installer wins because every later wrapper computes its own quantity
whenever a pilot context exists and never delegates to the earlier one:

| order | installer | pilot quantity it *would* return | reached in LIVE pilot? |
|---|---|---|---|
| 1 | `pilot_live_runtime._install_pilot_notional_sizing` (bootstrap) | `available * PILOT_NOTIONAL_PCT` as **position notional** | no (shadowed) |
| 2 | `pilot_risk_cap_hardening` (bootstrap) | `min(stop_risk_qty, notional_target_qty)` | no (shadowed) |
| 3 | `operator_runtime_policy._install_margin_sizing` (runtime_overlays) | `available * 0.50 * LEVERAGE / price` (initial margin) | no (shadowed) |
| 4 | `final_sizing_invariants.install` (runtime_overlays, **required** by `runtime_contract_guard`) | `available * 0.50 * LEVERAGE / price`; RiskManagerV3 qty only has to be `> 0` | **yes** |

So the executed policy is **not** `min(stop_risk_qty, pilot_target_qty)`.
It is: *operator 50%-of-available **initial margin** × configured leverage;
RiskManagerV3 is a pass/fail gate whose numeric quantity is ignored*
(`final_sizing_invariants._select_final_quantity` returns `target_qty`;
`tests/test_final_sizing_invariants.py::test_risk_numeric_recommendation_does_not_shrink_valid_operator_target`
pins that behaviour).

The only loss ceiling on that quantity is `final_loss_budget.validate`:
`projected_loss <= 0.5 * margin` = `0.25 * available`, independent of
`MAX_RISK_PCT`.

Production numbers (equity 9.8410, available ≈ equity when flat, 50x,
`MAX_RISK_PCT` default 1%):

| quantity | value |
|---|---|
| operator margin cap | ≈ 4.92 USDT |
| position notional | ≈ 246 USDT (25× equity) |
| loss ceiling admitted by `final_loss_budget` | ≈ 2.46 USDT ≈ **25% of equity per stop** |
| RiskManagerV3 budget (`equity * risk_pct`) | ≈ 0.098 USDT (1%) |
| durable drawdown headroom to 10% hard gate | 10.00% − 9.10% ≈ 0.09 USDT |

One stop-out at the admitted size would consume ~25× the configured risk
budget and would breach MAX_DRAWDOWN by itself. Leverage *does* scale the
position (and therefore the loss at a fixed stop distance) in the executed
path; only the ceiling (`0.5*margin`) happens to be leverage-independent.

### Observability contradictions

* `pilot_live_runtime` and `pilot_risk_cap_hardening` install logs claim
  "50pct-available **position-notional**" and "RiskManagerV3 is the **maximum
  quantity authority**" — both false in the executed stack.
* `sizing_semantics_log_hardening` (installed from `sitecustomize` as a global
  `LogRecordFactory`) rewrites those records *after the fact* to
  `OPERATOR_50PCT_EQUITY / VALIDATION_GATE`. The rewritten text matches
  layer 4 but the mechanism itself is a truth-altering filter: any future log
  containing the matched substrings is silently replaced, and a reader of the
  source cannot know what production will print.
* `final_loss_budget` telemetry says `qty_authority=FINAL_OPERATOR_QTY
  risk_v3_qty_authority=NON_AUTHORITATIVE`; `operator_loss_policy` says
  `sizing_authority=OPERATOR_50PCT_EQUITY risk_manager_role=VALIDATION_GATE`.

### Decision (the brief is internally inconsistent here — flagged for review)

The brief says both "do not change sizing just to make old logs true" **and**
requires the invariants `final_qty <= stop_risk_qty`, "projected stop risk
respects the budget", "50x does not alter the risk budget". The executed code
violates those invariants, so they cannot be met by telemetry alone.

The safer correction is taken: `final_qty = min(stop_risk_qty,
operator_margin_cap_qty)`. This is strictly risk-reducing: it can only shrink
quantity (never enlarge), it adds no new *blocking* path (a positive
RiskManagerV3 quantity is already ≥ the exchange minimum, and a zero one was
already a block), and every existing gate (50%-margin cap, projected-loss
ceiling, cross stress, drawdown, pilot) is kept. The operator 50%-margin
policy survives as a **cap**. Expected effect in production: order sizes drop
from ~25× equity notional to whatever the configured stop-risk budget allows
(e.g. ≈ 0.1 USDT risk at 1%). Symbols whose exchange minimum cannot fit the
budget are blocked (`qty=0`), as the brief requires.

**Reviewer action required:** if the operator explicitly wants the previous
margin-authoritative sizing, revert commit "sizing: RiskManagerV3 is the
binding quantity authority" and accept that `final_qty <= stop_risk_qty` does
not hold. Telemetry will still be truthful either way.

Canonical terms (used by every sizing log after this PR):
`target_policy=50pct_available_initial_margin_cap risk_authority=RiskManagerV3
final_quantity_policy=min(stop_risk_qty,operator_margin_cap_qty)
binding=RISK_BUDGET|OPERATOR_MARGIN_CAP`.

Files: `final_sizing_invariants.py`, `final_loss_budget.py`,
`pilot_live_runtime.py`, `pilot_risk_cap_hardening.py`,
`operator_runtime_policy.py`, `operator_loss_policy.py`,
`sizing_semantics_log_hardening.py` (removed), `sitecustomize.py`, tests.
Change risk: **medium** (reduces live position size substantially).

---

## P0-2 — Execution cost model: KuCoin data on the Binance path, two cost models

Evidence:

* `nexus_live_cost_calibration._cached_taker_fee → kucoin_execution_model.fetch_actual_taker_fee`
  calls the **KuCoin** endpoint `/api/v1/trade-fees` with a KuCoin symbol
  through the Binance client (`BinanceClient._get` → authenticated GET
  `fapi.binance.com/api/v1/trade-fees`, a non-existent route). It fails every
  hour per symbol and falls back to `TAKER_FEE` env / 6 bps, labelled
  `env_fallback`. The real Binance commission (`/fapi/v1/commissionRate`) is
  never read.
* `operator_loss_policy` (technical R:R), `final_sizing_invariants` and the
  pre-dispatch recheck use `kucoin_execution_model.estimated_round_trip_cost_pct`
  = `2*fee + 2*slip` with **static** slippage 5 bps (BTC/ETH/SOL) or **10 bps
  (all other symbols)**.
* NEXUS EV/R:R uses `nexus_ai.expected_value` with the *calibrated* context:
  same fee fallback but slippage = half-spread + 1–2 bps impact from the
  cached ticker (typically 1.5–3 bps).
* `professional_risk_adapter` imports `TAKER_FEE` from `bot.kucoin` (6 bps)
  for RiskManagerV3 even when `EXCHANGE=binance` (Binance module default 5 bps).

## P0-3 — R:R 1.36 vs 1.54 on the same ATOMUSDT candidate

Both numbers use the identical formula
`rr_net = (tp_dist − cost) / (sl_dist + cost)`; the difference is only the
cost input: technical policy `cost = 2*6bp + 2*10bp = 0.32%`, NEXUS
`cost ≈ 2*6bp + 2*(~2.5bp) ≈ 0.17%`. Solving both equations for the observed
pair gives stop ≈ 1.80% / target ≈ 3.20% (gross R:R ≈ 1.78), i.e. the observed
1.36 vs 1.54 is fully explained by the 0.32% vs ~0.17% cost inputs. Neither log carried a candidate id
or the cost inputs of the other layer, so the divergence was not traceable.

Fix plan: one exchange-aware `ExecutionCostSnapshot` built **once per
candidate** (by the first consumer), attached to the signal, and consumed by
the technical policy, NEXUS EV/R:R, final sizing and the loss-budget
telemetry. The fee source for Binance becomes `/fapi/v1/commissionRate`
(read-only, cached). Conservative fallback preserved: when the live fee is
unavailable the fallback is `max(configured exchange taker fee, 6 bps)`;
when the ticker is unavailable the static per-symbol slippage is used. Every
consumer logs `candidate_id`, `cost_snapshot_id`, fee/slippage/spread/funding
and sources.

## P0-4 — KuCoin text/fees on the Binance production path

* `bot/kucoin.py` is imported unconditionally by `runtime_bootstrap` and
  prints `🔴 OPERAÇÃO REAL ATIVA — ordens serão enviadas à KuCoin` at import
  whenever `PAPER_TRADE=false` + live ack — including `EXCHANGE=binance`.
* `notifier.signal_msg` hard-codes "Ordem enviada para a KuCoin".
* `PilotGuard.evaluate` check `1_AUTH` reads **KuCoin** API key / secret /
  passphrase on Binance: it either blocks every Binance pilot entry or passes
  because unrelated KuCoin secrets are still configured. Either way it does not
  prove Binance credentials. Fix: venue-aware credential presence check
  (Binance key + signing key), still fail-closed, values never logged.
* `professional_risk_adapter`, `exit_policy_telemetry`, `shadow_live`,
  `cross_portfolio_stress` import `TAKER_FEE` from `bot.kucoin`.

## P0-5 — CONTROLLED_PILOT_RELEASE authorized=true vs BINANCE_ACCOUNTING execution_effect=BLOCK_LIVE_RELEASE

Evidence: `runtime_overlays` emits a **constant** string
`[BINANCE_ACCOUNTING] ... live_accounting_authority=false
release_state=AWAITING_CONTROLLED_LIVE_EVIDENCE execution_effect=BLOCK_LIVE_RELEASE`
on every Binance start. Nothing reads Binance accounting state in
`pilot_release_control.release_checks()` or anywhere in the entry path; the
real release gate is the env-token set + `BINANCE_LIVE_MIGRATION_READY`.
Truth: Binance accounting evidence is **observability-only**; it does not
block release. The log is false. Fix: a single `runtime_release_contract`
that derives `release_authorized`, `validation_lock`,
`accounting_authority`, and `accounting_execution_effect` from the same
inputs, with a test that forbids `authorized=true` together with any
`execution_effect=BLOCK_LIVE_RELEASE`.

## P0-6 — Liquidation safety at 50x

Existing: `binance_cross_portfolio_stress` (final pre-dispatch) reads user
leverage brackets, rejects `cfg.LEVERAGE > bracket.initialLeverage`, rejects
multi-assets mode / non-CROSS / external positions / missing state, and
requires projected account risk-rate at *every stop simultaneously* < 90%
(maintenance overestimated as `notional*MMR`, no `cum` deduction). Position
mode is asserted in `BinanceClient._assert_live_account_mode`. No liquidation
price is invented when data is missing (fail-closed). No code change
required beyond tests that demonstrate the 1x…125x matrix with the new
sizing.

## P0-7 — Drawdown refresh immediately before dispatch

Evidence: the final `_refresh_entry_balance()` (engine, immediately before
order-registry/durable-intent/dispatch) re-reads authenticated equity and
updates the durable HWM/drawdown, but **no gate re-evaluates drawdown after
that read** — the hard gate is evaluated earlier (scan `can_open`, V3
`can_open` in `_prepare_professional_risk`). Race: candidate approved →
equity deteriorates → final refresh computes drawdown ≥ MAX_DRAWDOWN → order
still sent. Fix: hard-gate check inside the LIVE-pilot entry refresh (same
explicit `LIVE_RISK_OVERRIDE_APPROVED` semantics as every other drawdown gate;
threshold unchanged). Deposits/withdrawals are already reconciled separately
by `capital_flow_reconciliation` before the HWM update.

---

## P1

| id | finding | evidence | action in this PR |
|---|---|---|---|
| P1-1 | `market_to_signal_ms=0.0` when no market-data event was observed | `entry_latency_observability._new_trace` defaults `market_data` to `now` | report `NA`; add `market_data_source=UNKNOWN`; Binance WS logs never match the KuCoin "WS cache hit" regex, so on Binance the value was *always* a fabricated 0 |
| P1-2 | silent excepts | `nexus_decision_consistency.py:240`, `final_loss_budget.py:105` | replace with explicit, tested helpers that log at debug and never raise |
| P1-3 | Telegram | NEXUS WAIT path is fire-and-forget + dedupe/cooldown (5efeb9c) | unchanged; verified observability-only |
| P1-4 | Binance execution chaos | `test_live_adapter_chaos`, `test_durable_*`, `test_order_visibility_race*`, `test_binance_usdm_migration` already cover timeout-after-accept, 5xx ambiguity, duplicate clientOrderId, restart-in-OPENING, fill-before-REST, lease loss | no change; mapped in final section |
| P1-5 | NEXUS OOS / threshold calibration | requires historical market data (network) | **not run** here (offline sandbox); no threshold changed |
| P1-6 | backtest/replay parity | replay uses `kucoin_execution_model` costs | cost parity test added for the shared snapshot; full replay parity requires data |

## P2

* Dead sizing layers 1–3 above: kept (tests/contracts reference them) but
  their install logs now state `superseded_by=final_sizing_invariants`.
* `sizing_semantics_log_hardening` removed (truth-altering normalization).
* Canonical authority map: `docs/audit/CANONICAL_AUTHORITIES.md`.

---

## Resolution status (end of this PR)

| item | status | commit subject |
|---|---|---|
| P0-1 sizing truth | fixed — executed contract `min(stop_risk_qty, operator_margin_cap_qty)`; log rewriter removed | `sizing: RiskManagerV3 is the binding quantity authority; remove log rewriting` |
| P0-2 cost model | fixed — `bot/execution_cost.py` snapshot; Binance commission endpoint; no KuCoin fee on the Binance risk path | `cost: one exchange-aware execution-cost snapshot per candidate` |
| P0-3 R:R 1.36 vs 1.54 | root cause reproduced in a test; both layers now agree on the shared snapshot | same |
| P0-4 KuCoin text/credentials on Binance | fixed | `binance: venue-truthful banners/credentials; one release/accounting contract` |
| P0-5 release vs accounting | fixed — `runtime_release_contract` | same |
| P0-6 liquidation 50x | verified existing gate; tests added | `risk: re-check drawdown hard gate on the final pre-dispatch equity read` |
| P0-7 drawdown race | fixed + race test | same |
| P1-1 latency 0.0 | fixed (`NA`, `market_data_observed`) | `observability: latency never fabricates 0ms; ...` |
| P1-2 silent excepts | fixed for `nexus_decision_consistency` and `final_loss_budget` | same |
| P1-4 `-4116` duplicate clientOrderId | fixed (reconcile by id) | `binance: treat -4116 duplicate clientOrderId ...` |
| P1-5 OOS / walk-forward | **not run** — exchange hosts denied by this environment's network policy; no thresholds changed, so no before/after is claimed | — |
| P1-6 replay parity | partial — cost parity only; research/replay modules still use `kucoin_execution_model` (KuCoin data source) | — |

### Execution-scenario coverage map (existing + added tests)

| scenario | test |
|---|---|
| HTTP timeout after exchange accepted | `test_binance_usdm_migration::test_ambiguous_entry_response_recovers_by_client_order_id` |
| ambiguous 5xx / -1007 | `test_binance_usdm_migration::test_unresolved_ambiguous_entry_never_requests_blind_resubmission` |
| definitive 4xx is not ambiguous | `test_binance_usdm_migration::test_definitive_binance_order_error_is_not_treated_as_ambiguous` |
| duplicate clientOrderId (-4116) | `test_binance_usdm_migration::test_duplicate_client_order_id_reconciles_instead_of_failing_clean` (new) |
| restart during OPENING | `test_restart_opening_order_lineage`, `test_durable_execution_restart` |
| fill before REST response / private WS race | `test_private_ws_race_hardening`, `test_order_state_rest_ack_race` |
| partial fill | `test_order_visibility_race_hardening::test_partial_then_filled_delays_rest_but_never_synthesizes_fill` |
| SL/TP creation failure | `test_binance_protection_failclosed`, `test_binance_usdm_migration::test_conditional_protection_retry_reuses_existing_sl_and_only_posts_tp`, `test_live_adapter_chaos::test_open_order_with_failed_protection_is_explicitly_flagged` |
| ALGO_UPDATE separate from normal orders | `test_binance_usdm_migration::test_private_algo_update_is_cached_without_managed_order_transition` |
| order invisibility race | `test_order_visibility_race_hardening` |
| Postgres unavailable | `test_live_adapter_chaos::test_db_failure_immediately_before_entry_post_blocks_exchange_mutation`, `test_live_execution_fence::test_db_outage_allows_only_verified_reduction` |
| fencing lease lost before dispatch | `test_live_adapter_chaos::test_stale_fence_at_transport_boundary_blocks_dispatch_race`, `test_live_execution_fence::test_second_instance_cannot_dispatch_live_order` |
| reduce-only while entries blocked | `test_live_adapter_chaos::test_reduce_only_remains_available_when_critical_db_is_down` |

### Residual risks / needs live evidence

* Position size in production will drop to the stop-risk budget (P0-1). Confirm
  `MAX_RISK_PCT` / `_effective_risk_pct` in Railway is the intended budget
  before deploying; at ~10 USDT equity many symbols will size to 0 (exchange
  minimum) and be blocked — that is the requested fail-closed behaviour.
* `/fapi/v1/commissionRate` read must be observed once in production logs
  (`fee_source=binance_commission_rate`).
* Latency SLOs: only the instrumentation was corrected; baseline must be
  measured in production before any SLO is set.
* NEXUS OOS/walk-forward and threshold experiments must run where exchange
  data is reachable (CI `oos_real_replay.yml` or an environment with network
  access). No threshold was changed in this PR.
* Replay/backtest still uses KuCoin public data and `kucoin_execution_model`
  costs; Binance replay parity is open.
