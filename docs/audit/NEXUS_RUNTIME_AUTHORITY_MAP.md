# NEXUS-7 runtime authority map and OPEN_NEW_RISK execution graph

- Baseline: `fix/link-rejected-intent-recovery` on top of `migration/binance-usdm` @ `8e220d3`
  (production SHA). The map was derived from the live wrapper chains of a
  LIVE-shaped composition (`main_hardened` → `sitecustomize` →
  `runtime_bootstrap`, network guard on), not from the static source layout.
- Companion: `CANONICAL_AUTHORITIES.md` (one-line owners). This document adds
  readers, writers, wrappers, persistent state, exchange truth, tests and
  runtime contracts, and lists duplicated or shadow authorities.
- Size: `bot/` has 234 modules; `bot/engine.py` has 4148 lines.

## 1. Effective wrapper chains (runtime truth)

Outermost first. The last line is the original function.

```
TradingEngine._open
  bot.post_trade_forensics        _open_with_lineage
  bot.operator_loss_policy        open_with_technical_loss_geometry
  bot.legacy_pretrade_advisory    (legacy score.aprovado advisory only after an exact NEXUS approval; hard_block never bypassed)
  bot.pilot_risk_cap_hardening    (final_qty context + LIVE spread/depth/drift guard after final sizing)
  bot.pilot_live_runtime          (fresh equity, drawdown hard gate, private stream, preflight)
  bot.binance_protection_failclosed _open_with_protection
  bot.engine                      _open            (durable intent, registry, dispatch)

TradingEngine._refresh_entry_balance
  bot.binance_cross_portfolio_stress _refresh_with_binance_cross_stress   (final pre-dispatch stress)
  bot.pilot_risk_cap_hardening / bot.pilot_live_runtime
  bot.paper_wallet                _refresh_entry_balance_paper_safe
  bot.engine                      _refresh_entry_balance

engine.minimum_base_quantity (final sizing hook)
  bot.final_sizing_invariants     _final_operator_authoritative_quantity   ← AUTHORITY
  bot.operator_runtime_policy     _margin_target_quantity                  (shadowed)
  bot.quantity                    minimum_base_quantity

BinanceClient.place_order
  bot.live_execution_fence        _place_order_with_fence    (Postgres ownership lease, fencing token)
  bot.pilot_submission_counter    place_order_with_boundary_budget (per-session submission budget)
  bot.binance                     place_order  → validate_execution_ownership → assert_ready_for_new_entries
                                  → _post(single_attempt) → ACK/-2019/ambiguous reconcile
```

The chain lists are pinned by
`tests/test_binance_cross_stress_dispatch_proof.py::test_effective_wrapper_order`
and `runtime_contract_guard` (`RUNTIME_CONTRACT_DRIFT` if a layer is removed;
proven by `test_removing_cross_wrapper_breaks_runtime_contract`).

## 2. OPEN_NEW_RISK execution graph (Binance LIVE)

```
closed candle (WS /market, market_data_health)            -- 11_MARKET_DATA gate
 → strategy.Analyzer → adaptive_mtf_entry → pullback confirmation   (signal score, MIN_ENTRY_SCORE)
 → scan gates: can_open (durable, pause, partial symbols), integrity, PilotGuard (14 gates)
 → NEXUS (nexus_ai: regime, model fusion, heuristic probability, EV/net R:R with cost snapshot)
 → engine._open  [wrapper chain §1]
   → RiskManagerV3.size_for_stop via ProfessionalRiskAdapter (monetary stop-risk)
   → final_sizing_invariants: min(stop_risk_qty, operator margin cap) + final_loss_budget diagnostic
   → pilot_live_runtime: fresh authenticated equity → drawdown hard gate (override env explicit)
                         → private stream event_capable (REST reconcile for current epoch)
   → CROSS stress (PRE_ORDER)  → fresh account refresh → CROSS stress (FINAL_PREDISPATCH)
        requires CROSS margin, actual symbol leverage == cfg.LEVERAGE, brackets, account flags
   → durable intent (OrderRegistry CREATED/SUBMITTING, persist_orders strict)
   → BinanceClient.place_order: fence (lease + fencing token) → ownership revalidation
        → runtime readiness → POST /fapi/v1/order (single attempt, newClientOrderId)
   → ACK → SUBMITTED; -2019/-4xx → REJECTED; timeout/5xx → reconcile by clientOid, never resubmit
 → fill: private stream ORDER_TRADE_UPDATE + REST reconcile_pending (by clientOid)
 → protection: set_position_stops → read-back → repair → emergency close (binance_protection_failclosed)
 → durable persistence of order/trade/position state
```

Other `place_order` call sites (`stagnation_time_hardening`,
`partial_tp_execution_hardening`, `confirmed_rr_exit`, `durable_partial_exit`,
`exit_policy_telemetry`, `risk.py`, `engine.py` close paths,
`binance_protection_failclosed` emergency close) are reduce-only or close
paths. They still pass through the fence and ownership. The documented escape
is `live_execution_fence`: a *verified* reduce-only order may proceed when
ownership storage is unavailable. That escape never applies to entries.

### OPEN_NEW_RISK mutation matrix (proof)

All cases go through the real `engine._open` against the dispatching positive
control and require zero non-GET exchange requests plus gate-specific
evidence.

| # | Invariant | Test |
|---|---|---|
| 1, 4 | no/unresolved durable state → zero mutation | `DispatchProof.test_inv01_04_unresolved_durable_state_blocks`; `test_link_recovery_exactness` (gate closed until persisted) |
| 2 | invalid ownership → zero mutation | `test_inv02_invalid_ownership_blocks_at_transport` |
| 3 | stale fencing → zero mutation | `test_inv03_stale_fence_blocks_at_transport`; `test_execution_ownership` (takeover, superseded token) |
| 5 | private stream `reconcile_required` → zero new exposure | `test_link_recovery_exactness.test_private_stream_converges_only_after_proven_recovery`; `test_binance_private_stream_invariants` |
| 6 | CROSS stress fail → zero `place_order` | `DispatchProof.test_fail_closed_*` (26 scenarios) |
| 7 | actual leverage mismatch → zero `place_order` | `test_fail_closed_leverage_mismatch_{lower,higher}`; `test_final_sizing_invariants` |
| 8 | unknown or invalid leverage → zero `place_order` | `test_fail_closed_leverage_{missing,invalid,read_exception}` |
| 9 | drawdown ≥ hard limit → zero new exposure | `test_inv09_drawdown_at_hard_limit_blocks`; `test_predispatch_drawdown_race` |
| 10 | external unowned position conflict | `test_inv10_external_position_conflict_blocks`; `test_binance_external_position_guard` |
| 11 | stale or invalid top-of-book | `test_inv11_invalid_top_of_book_blocks` |
| 12 | missing instrument metadata | `test_inv12_missing_instrument_metadata_blocks`; `test_fail_closed_symbol_unknown` |
| 13 | readiness/protection failure | `test_inv13_readiness_failure_blocks`; `test_binance_protection_failclosed` |
| 14 | ambiguous transport → never blind resubmit | `test_inv14_ambiguous_transport_never_resubmits`; `_post` forces `single_attempt` for `/fapi/v1/order` and `/fapi/v1/algoOrder` |

## 3. Authority table

W = writer, R = main readers. "Exchange truth" is what wins on conflict.

| Concept | Authority (W) | Readers (R) | Wrappers / shadows | Persistent state | Exchange truth | Tests / contract |
|---|---|---|---|---|---|---|
| Signal score | `strategy.Analyzer` + `adaptive_mtf_entry` | engine scan, NEXUS, telemetry | `score_floor_shadow`, `mtf_shadow`, `volume_gate_shadow`, `htf_transition_shadow` (SHADOW only) | none | n/a | strategy tests, shadow tests |
| NEXUS score/decision | `nexus_ai` (+ `nexus_live_cost_calibration`) | engine `_nexus_validate`, notifier, OOS | `nexus_optional_evidence`, `nexus_structure_semantics` (diagnostic) | decisions logged (`[AI_DECISION]`) | n/a | nexus tests |
| Regime | `nexus_ai` regime detection (`Regime`) | fusion weights, strategy `detect_regime` | **duplicate:** `strategy.detect_regime` is a second regime notion used by stagnation logic | none | n/a | — |
| Capital/equity | `account_capital_reader.read_account_capital` → `get_account_state()` | RiskManagerV3, drawdown | `risk.py` legacy `balance` (compat) | durable snapshot | `/fapi/v3/account` | capital reader tests |
| Available collateral | `get_account_state()["available"]` | final sizing cap, CROSS stress | — | none | `/fapi/v3/account` | cross stress tests |
| Drawdown / HWM | `drawdown_persistence` + `cash_flow_ledger` (TWR) | `pilot_live_runtime._entry_drawdown_allows`, `operator_runtime_policy`, readiness | **duplicate writers:** `risk.py` (legacy `drawdown`/`peak_balance`), `paper_wallet`, `durable_execution` restore; gate takes `max(legacy, v3)` | `risk:*` keys, ledger CAS | `/fapi/v1/income` for flows | cash-flow drawdown tests, race tests |
| Daily PnL / daily stop | `durable_daily_pnl` / `daily_stop_runtime_hardening` | engine, notifier | **duplicate:** `engine.daily_tracker`, `daily_tracker.py` | durable keys | realized PnL evidence | daily stop tests |
| Position ownership | `OrderRegistry` lineage + `external_origin_runtime` | protection, stress, guard | `binance_external_position_guard` | durable orders/positions | `/fapi/v3/positionRisk` | external position tests |
| Durable order state | `durable_execution` (+ `durable_reconcile_hardening`, `durable_live_reconciliation`) | `can_open`, readiness, preflight | `pilot_submission_counter` reconciler provenance wrapper | `key_value` orders payload | `GET /fapi/v1/order` by clientOid | durable tests, LINK exactness |
| Execution ownership | `execution_ownership` (Postgres lease) | fence, reconcile, readiness | — | `key_value` ownership row | n/a | `test_execution_ownership` |
| Fencing | `execution_ownership` token + `live_execution_fence` | `place_order` | — | same row | n/a | fence tests |
| Sizing | `final_sizing_invariants` over RiskManagerV3 | `_open`, CROSS stress | `operator_runtime_policy._margin_target_quantity` (shadowed) | none | exchange filters | sizing tests |
| Leverage | `cfg.LEVERAGE` (configured) vs **actual** `get_symbol_config().leverage` (checked, never written) | CROSS stress, margin | no module writes `cfg.LEVERAGE` at runtime (grep-verified) | none | `/fapi/v1/symbolConfig` | leverage regressions (this PR) |
| Margin mode | `get_symbol_config().marginType == CROSS` | CROSS stress | — | none | `/fapi/v1/symbolConfig` | cross_unconfirmed scenario |
| Costs | `execution_cost.ExecutionCostSnapshot` | NEXUS EV, sizing costs | strategy `TOTAL_COST` (pre-filter assumption) | none | `/fapi/v1/commissionRate` | cost tests |
| Stop-loss / TP | strategy geometry → `operator_loss_policy` → protection | sizing, protection | `trailing_safety_hardening`, partial TP modules | durable position | algo orders on Binance | protection tests |
| CROSS stress | `binance_cross_portfolio_stress` (PRE_ORDER + FINAL_PREDISPATCH) | `_refresh_entry_balance` wrapper | `cross_portfolio_stress` (KuCoin legacy) | none | account + brackets | 35 dispatch scenarios |
| Private stream health | `private_stream_health.PrivateStreamHealth` (single writer: private WS loop) | preflight, PilotGuard 14_WS | — | none (in-memory, epoch) | REST reconcile | private stream tests |
| Protection readiness | `protection_readiness.refresh_protection_readiness` | runtime readiness | `prelive_protection_failclosed` | none | algo orders | readiness tests |

### Duplications and shadow authorities (candidates for consolidation)

1. **Drawdown/HWM**: 4 writers. Only the fresh-equity `max(legacy, v3)` gate
   is authoritative, but the legacy numbers still feed logs. Target: one
   `DrawdownAuthority` object, owned by `drawdown_persistence`.
2. **Daily PnL/stop**: engine attributes, `DailyTracker`, and the durable
   modules. Target: `durable_daily_pnl` as the single writer.
3. **Regime**: `nexus_ai.Regime` vs `strategy.detect_regime`. Target: one
   regime classifier that feeds both.
4. **Sizing shadows**: `operator_runtime_policy._margin_target_quantity`
   runs but is superseded by `final_sizing_invariants`, which logs that it is
   shadowed.
5. **KuCoin compatibility wrappers** (`cross_portfolio_stress`,
   `kucoin_*`): imported on Binance for compatibility. Target: isolate behind
   the venue adapter.

## 4. Incremental consolidation plan (no big-bang)

Rule: one authority per PR. Each PR needs characterization tests first, a
compatibility adapter where readers remain, the runtime contract updated in
the same PR, and no change to any threshold, percentage, leverage, stop,
target, position count, sizing or market selection.

1. `DrawdownAuthority` (read-only facade over `drawdown_persistence`). Legacy
   `risk.drawdown` becomes a property delegating to it. Characterization: the
   existing drawdown/race/cash-flow tests plus byte-identical `[ENTRY_RISK_STATE]`.
2. `DailyPnlAuthority` over `durable_daily_pnl`.
3. `ExchangeStateAuthority`: account, positions, open/algo orders and symbol
   config, with one fresh-read API used by CROSS stress, preflight and
   reconcile (no caching for authoritative reads).
4. `DurableOrderCoordinator`: fold `durable_reconcile_hardening` and
   `durable_live_reconciliation` into one reconcile loop. **Known gap:** the
   audited LINK recovery exists only in the startup reconcile, so a
   transient failure there needs a restart. Folding the loops would let the
   periodic path apply the same exact rule.

## 5. `engine.py` reduction plan

Targets, in extraction order. Each extraction is preceded by
characterization tests on `engine._open`/close paths using the DispatchProof
harness:

1. `ProtectionCoordinator`: stop/TP placement, read-back, repair and
   emergency close (already mostly in `binance_protection_failclosed`).
2. `ExitPipeline`: close paths (lines 1700–2070, 3200–3300, 3980).
3. `EntryPipeline`: `_open` body after sizing: registry, durable intent,
   dispatch and ACK handling.
4. `PositionLifecycle`: `positions` dict mutations and reconcile.
5. `ExecutionOrchestrator`: the scan loop only; `TradingEngine` delegates.

None of these extractions was performed in this change. The characterization
layer (DispatchProof and the invariant matrix) now exists.
