# Binance CROSS stress dispatch proof

## Verdict and scope

**RESULT: BYPASS_FOUND on BASE; reproduced before the minimal correction.**

BASE SHA: `29bf052f953e8d177a3ccb4455ffa12650d82130`

Branch: `audit/binance-cross-stress-dispatch-proof`, targeting `migration/binance-usdm`.

The BASE commit and its tree (`cb426d82e42319aed09a638ca90892e101e044c4`)
were retrieved through GitHub because direct GitHub cloning was unavailable.
The complete tree was reconstructed locally and matched that exact tree SHA;
the signed commit object also matched the BASE SHA. `git status --short` was
empty before changes. No merge, deployment, Railway change, secret change,
exchange connection, or real order was performed.

The proof is about **new Binance LIVE exposure through the repository's entry
call graph**. The stronger sentence "no order of any kind can reach place_order
after stress blocks" is false by design: reduce-only exits and emergency
protection must remain available. This patch does not disable those paths.
Nor is an unrestricted direct call to the client API a stress-authorized
capability: callers must enter through the reviewed engine path.

## Reproduction, before correction

The same real-bootstrap harness reaches `BinanceClient.place_order` and its
order `_request` boundary with an in-memory ACK. Upstream strategy approval is
supplied as a valid `NexusDecision`; real final sizing, RiskManagerV3 arithmetic,
both FINAL_LOSS_BUDGET checks, engine control flow, stress evaluation, wrapper
composition, durable-intent serialization, and client quantity formatting run.

| Injected fault | BASE place_order calls | BASE fake order requests | Failure |
|---|---:|---:|---|
| Final quantity context becomes `None` after sizing | 1 | 1 | Final refresh treats it as pre-sizing and skips stress |
| Signal context becomes `None` after sizing | 1 | 1 | Market guard and final stress both skip |
| Context quantity differs from actual order quantity | 1 | 1 | Final stress analyzes a different quantity |
| Candidate CROSS configuration missing at final stress read | 1 | 1 | Evaluator checks existing positions' margin mode, not candidate mode |
| Remove final stress wrapper before contract validation | N/A | N/A | BASE runtime contract still accepts it |

Missing/mismatched context cases are deliberate fault injections at the real
sizing boundary, not a claim that normal production code was observed clearing
ContextVars. They reproduce the requested incoherent-runtime-state failure.
For those cases PRE_ORDER still passes; it is specifically the final check
that is skipped or bound to the wrong quantity. A forced BLOCK or exception in
either existing evaluator call already blocks the engine at BASE.

The first commit contains the initial failing reproduction. The expanded final
harness was also copied onto an isolated BASE worktree: five assertions fail
(the four dispatch cases and the missing runtime-contract protection). All
pass on the corrected candidate.

## Minimal correction

1. Core `_open` binds the actual LIVE candidate to `_PILOT_ENGINE`,
   `_PILOT_SYMBOL`, `_PILOT_SIGNAL` and `_PILOT_FINAL_QTY` immediately before the
   final refresh. Missing, nonpositive, nonfinite, or unequal quantity/context
   blocks before dispatch. This also rejects an unsupported context-free
   Binance LIVE invocation; valid controlled-pilot inputs are unchanged.
2. LIVE stress requires a fresh candidate `get_symbol_config` result explicitly
   confirming `CROSS` or `CROSSED`. Errors/missing configuration fail closed.
3. The runtime contract now requires the final refresh callable to come from
   `binance_cross_portfolio_stress.py` and requires its installation marker.

No changes to leverage, risk, drawdown limits, NEXUS, score, EV, R:R, stops,
targets, sizing formulas, quantity filters, Telegram, or FINAL_LOSS_BUDGET.
The existing 50x stress test fake now supplies explicit CROSS evidence.

## Real call graph

`main.py` constructs `bot.nexus_runtime_engine.TradingEngine`, a subclass of
the core engine. It installs `ProfessionalRiskAdapter` and inherits `_open`.
The harness instantiates this same runtime subclass after the real bootstrap.
The candidate starts at `_open`; upstream scanning and live NEXUS services are
outside this deterministic proof.

Effective calls, in execution order:

1. `_open` wrapper stack below: geometry telemetry, risk ContextVars, read-only
   exposure/private-stream preflight, integrity, equity/collateral refresh.
2. Core viable-symbol and durable-state gates; mandatory valid NEXUS approval.
3. First `_refresh_entry_balance`: fresh account/drawdown; final quantity is
   not computed yet, so final market/stress wrappers intentionally skip.
4. Pilot eligibility; `minimum_base_quantity` final authority calls
   `ProfessionalRiskAdapter.size` -> `RiskManagerV3.size_for_stop` -> quantized
   stop-risk size. Select `min(risk_qty, operator_margin_cap_qty)`.
5. `final_loss_budget.validate` inside final sizing; publish final ContextVar
   quantity. Affordability, pretrade score, quantity filters, SL/TP validity.
6. **CROSS stress PRE_ORDER**, directly in `bot.engine.TradingEngine._open`.
7. Construct idempotent client order ID. Before dispatch, validate exact final
   context (new correction), then perform final refresh: equity/capital-flow
   reconciliation/drawdown -> fresh market guard -> **FINAL_LOSS_BUDGET at fresh
   executable price** -> **CROSS stress FINAL_PREDISPATCH**.
8. Recheck affordability; OrderRegistry SUBMITTING -> durable `persist_orders`
   -> pilot session availability -> `client.place_order`.
9. Client wrappers: distributed advisory-lock fence -> pilot boundary context
   -> native Binance `place_order`: signing prerequisites, account mode,
   decimal quantity formatting, critical storage, execution lease validation,
   publish ownership, readiness -> `_post` -> `_request` -> HTTP transport.
10. ACK, protection and fill/reconciliation processing follow dispatch. These
    downstream risk-reducing paths do not constitute new entry permission.

Observed critical sequence is asserted, not inferred from file names:

```text
FINAL_SIZING_ENTER
  RiskManagerV3 (real computation)
  FINAL_LOSS_BUDGET
FINAL_SIZING_RETURN
STRESS_PRE_ORDER
ACCOUNT_REFRESH
FINAL_LOSS_BUDGET
STRESS_FINAL_PREDISPATCH
PLACE_ORDER
FENCE
OWNERSHIP
READINESS
FAKE_HTTP
```

Account reads also occur before sizing and inside each stress evaluation.
`place_order` is entered **before** the distributed fence executes because the
fence is itself a wrapper around that method. The HTTP boundary is after it.
The positive test compares sizing quantity, both stress quantities, the
place_order argument and the final formatted Binance request quantity, and
asserts there is no later sizing call.

## Effective wrappers and installation order

`sitecustomize.py` installs runtime-truth compatibility/filter shims and calls
`runtime_bootstrap.install()`. `main_hardened.py` provides explicit bootstrap
fallback. `runtime_overlays.install()` is invoked near the end of bootstrap;
runtime-truth downstream hooks are installed afterwards if enabled.

The installation indices below are relative to the specified callable,
starting from its original definition. Runtime execution enters the most
recent wrapper first. The test recovers captured originals from closure cells;
these legacy wrappers generally do **not** expose `__wrapped__`.

| Callable | Install index, module and installed function | PASS / BLOCK | Next stage |
|---|---|---|---|
| `_open` | 0 `engine._open` | Reviewed core gates / return on rejection, abort on error | registry/dispatch |
| `_open` | 1 `binance_protection_failclosed._open_with_protection` | delegates first; post-open protection requires ownership | core; protection after return |
| `_open` | 2 `pilot_live_runtime._open_live_pilot` | preflight, integrity, fresh positive collateral / failure blocks | protection wrapper |
| `_open` | 3 `pilot_risk_cap_hardening._open_with_pilot_risk_context` | publishes/restores ContextVars; no independent approval | pilot-live wrapper |
| `_open` | 4 `legacy_pretrade_advisory._open_with_context` | scopes candidate for approved-NEXUS advisory handling | risk-context wrapper |
| `_open` | 5 `operator_loss_policy.open_with_technical_loss_geometry` | valid fixed geometry / invalid geometry blocks; never moves stops | legacy-context wrapper |
| `_open` | 6 `post_trade_forensics._open_with_lineage` | transparent delegation, lineage after return | geometry wrapper |
| `_open` | 7 optional `runtime_truth_hooks.open_with_truth` | telemetry only, delegates unchanged | lineage wrapper |
| `_refresh_entry_balance` | 0 `engine._refresh_entry_balance` | positive confirmed balance / failed query or nonpositive balance | caller |
| refresh | 1 `paper_wallet._refresh_entry_balance_paper_safe` | PAPER wallet; LIVE delegates | core refresh |
| refresh | 2 `pilot_live_runtime._refresh_entry_balance_live_pilot` | fresh account and drawdown / errors block; LIVE replaces inner balance implementation | returns to market wrapper |
| refresh | 3 `pilot_risk_cap_hardening._refresh_entry_balance_with_final_market_guard` | valid final market data and loss budget / invalid final data blocks; pre-sizing skip | returns to stress wrapper |
| refresh | 4 `binance_cross_portfolio_stress._refresh_with_binance_cross_stress` | real evaluator allowed / BLOCK returns False, exception propagates to abort | core predispatch continuation |
| sizing hook | 0 quantity function; 1 pilot-live; 2 pilot-risk; 3 operator margin; 4 final-sizing | final hook shadows earlier hooks in LIVE pilot; invalid inputs -> zero | core quantity |
| `place_order` | 0 Binance native; 1 `pilot_submission_counter.place_order_with_boundary_budget`; 2 `live_execution_fence._place_order_with_fence`; 3 optional runtime-truth | owner required for entries; verified reduction exception only; native account/storage/readiness gates | `_post` |
| `_post` | 0 Binance native; 1 `partial_tp_execution_hardening._post_sized_reduce_only` | Binance body unchanged; KuCoin-only payload adjustment is inapplicable | `_request` |

Default LIVE `_open` outer-to-inner order is therefore:
`post_trade_forensics -> operator_loss_policy -> legacy_pretrade_advisory ->
pilot_risk_cap_hardening -> pilot_live_runtime -> binance_protection_failclosed
-> engine`.

Default final refresh ownership and complete captured chain are asserted after
bootstrap. A separate fresh subprocess uses `sitecustomize` with runtime truth
enabled: it proves the optional outer `_open` and `place_order` telemetry
wrappers still capture the expected original functions and do not replace the
CROSS refresh. The contract is a startup check, not a continuous monitor of
arbitrary monkey patches after startup.

Upstream call chains used before the dispatch proof's supplied approval are:

| Boundary | Effective outer-to-inner code origins | Behavior |
|---|---|---|
| Runtime `_nexus_validate` | `nexus_validation_observability` decorator -> `nexus_runtime_engine` override -> inherited core stack | Records the exact returned decision, prepares professional capital/plan, no alternate order sender |
| Core `_nexus_validate` | `post_trade_forensics` -> `nexus_terminal_notifications` -> `nexus_live_cost_calibration` -> `derivatives_news_freshness_hardening` -> `engine` | Lineage/terminal telemetry, frozen candidate cost, refreshed external evidence, canonical decision |
| `nexus_ai.decide` | `nexus_prefinal_veto_observability` -> `nexus_decision_consistency` -> `nexus_ai` | Prefinal diagnostics and closed-candle consistency before canonical decision |
| `scoring.calculate` | `legacy_pretrade_advisory` -> `pretrade_hardening` -> `score` | Exact approved-NEXUS context for advisory behavior, closed-candle handling, score computation |

Optional runtime-truth wrappers additionally surround the core validator,
decision and score callables, transparently forwarding results. Installation
order for each chain is inner-to-outer. In functions decorated with
`functools.wraps`, `__module__`/`__name__` can report the inner function; code
filenames and `__wrapped__` distinguish actual owners. The proof's closure
inspection uses `__code__.co_filename`, not copied module metadata.

### Ownership/durable boundary limitation

`durable_execution.persist_orders` serializes the exact SUBMITTING registry
before the engine calls place_order. `live_execution_fence.acquire` uses a
PostgreSQL session advisory lock. Native Binance `place_order` separately
validates monotonic ownership and readiness.

The bootstrap also installs `_fenced_entry_post` provenance/budget logic, but
the current Binance `_post -> _request -> session.request` path does **not**
invoke `_fenced_entry_post`. Do not infer a transport-time lease/budget recheck
from the mere existence of that wrapper or its installed marker. This is
recorded as a residual execution-safety issue, not changed in this audit.
In this harness ownership/storage availability are controlled fake
dependencies; it proves their call order, not real PostgreSQL concurrency.

## Actual stress model

For candidate `n`, quantity `q`, entry `e`, protective stop `s`, and direction
sign `d = +1` for LONG, `-1` for SHORT:

```text
PnL_stop_i = d_i * q_i * (s_i - e_i)
N_stop_i = q_i * s_i
N_entry_n = q_n * e_n
opening_fee = N_entry_n * Binance.TAKER_FEE
stressed_margin = crossWalletBalance + sum(PnL_stop_existing)
                  + PnL_stop_n - opening_fee
maintenance = sum(N_stop_i * MMR(bracket(N_stop_i)))
              + max(N_entry_n, N_stop_n) * MMR(bracket(max(N_entry_n, N_stop_n)))
closing_fees = (sum(N_stop_existing) + N_stop_n) * Binance.TAKER_FEE
risk_rate = (maintenance + closing_fees) / stressed_margin
PASS only if stressed_margin is finite and > 0,
             risk_rate is finite and 0 <= risk_rate < 0.90,
             and all geometry/state/bracket checks pass.
```

Brackets are selected from user-specific Binance ranges, not an assumed fixed
tier. Each range uses `floor <= notional < cap`; the last range also includes
its cap. The client validates bracket ID, positive maximum leverage, finite
floor/cap/MMR/cum, `cap > floor`, `0 < MMR < 1`, nonnegative cum and ordered
floors. The evaluator enforces configured leverage <= candidate bracket's
`initialLeverage` (50x in the proof). Existing positions use their own stop
notional brackets. Binance `cum` is deliberately **not deducted**, so
maintenance is a conservative `notional * MMR` estimate. `notionalCoef` is
retained by the client but not separately applied by this evaluator.

| Input/protection | Actual behavior |
|---|---|
| crossWalletBalance | Positive finite wallet base; no available-balance substitution |
| Unrealized PnL | Not added from current `unrealisedPNL`; recomputed at all protective stops from exchange entry/quantity, avoiding double counting |
| Existing positions | Exact count, mapped symbol, direction, CROSS mode and quantity coherence |
| Quantity coherence | Tolerance `max(1.01 * qtyStep, abs(local_qty) * 1e-6, 1e-12)`; not strict bitwise equality |
| Stops | Positive local trailing stop if available, else local original SL; must be adverse to current mark |
| New position | Positive finite quantity, symbol/direction/entry/stop; duplicate local symbol blocks; candidate CROSS readback added by correction |
| Maintenance | Returned tiers, conservative no-cum model above |
| Fees | Opening fee subtracts margin; closing fees add to required headroom; no slippage/funding term here |
| 50x | Candidate bracket support checked; leverage not changed |
| Open orders | Positive `orderMargin` blocks; earlier preflight reads active orders. Stress itself does not enumerate order objects |
| Missing/incoherent state | Exception/invalid geometry/state/bracket -> rejection; uncaught evaluator exception aborts engine entry |
| Single asset | Multi-asset margin mode rejected |

This is stop-stress headroom, not an exact liquidation-price calculation or a
guarantee of stop fill. It does not model gaps, unbounded slippage, future
funding, all possible account mode races, or atomic snapshots across REST reads.
Bracket data can be cached by the client for 300 seconds.

## FINAL_LOSS_BUDGET: position and arithmetic, unchanged

It executes inside final sizing **after RiskManagerV3 quantity selection**, and
again inside the final market refresh with the fresh executable entry price.
The first check precedes PRE_ORDER stress; the second precedes final stress.

```text
projected_loss = qty * (abs(entry - stop) + entry * cost_fraction)
limit = 0.50 * qty * entry / leverage
=> abs(entry - stop) / entry + cost_fraction <= 0.50 / leverage
```

For positive quantity/entry and 50x, the right side is `0.01` (1% of notional).
At cost fraction `0.0032` (0.32%), remaining stop distance is `0.0068` (0.68%).
Quantity cancels from the ideal inequality. The implementation also uses
`max(1e-12, limit * 1e-12)` comparison tolerance. The 0.32% figure is conditional
on the actual snapshot/static cost; it is not universal for all symbols and
configurations. No bug conclusion or change to this gate is made here.

## Order-creation inventory and bypass analysis

Global text search covered `place_order`, `create_order`, `_post`, order URLs,
dispatch helpers, emergency/recovery and transport requests. An AST walk of
every Python file also inventoried method calls. There is **one** production
engine call to `place_order` without `reduce_only=True`: `_open`.
No production `create_order` call or second Binance entry caller was found.

| Path / call site | Classification | CROSS requirement / outcome |
|---|---|---|
| `engine._open` -> Binance `place_order` | LIVE ENTRY | PRE_ORDER + final refresh; reproduced BASE context/mode gaps corrected |
| Binance `place_order -> _post('/fapi/v1/order') -> _request` | LIVE ENTRY or EXIT by reduceOnly | Native dispatch boundary; no standalone stress gate in the client |
| `engine._check_stagnation_and_invalidation` three calls; `runtime_hardening` replacement three calls; `stagnation_time_hardening` three calls | EXIT; earlier implementations superseded in LIVE pilot | All reduce-only; final operator loss policy suppresses discretionary LIVE-pilot exits |
| `exit_policy_telemetry._close_with_telemetry` | EXIT | reduce-only |
| `engine._manage_partial_tp`, `partial_tp_execution_hardening._manage_partial_tp_hardened` | EXIT / PAPER | reduce-only; LIVE replacement delegates to durable partial exit |
| `durable_partial_exit.check`, `confirmed_rr_exit.check`, `engine._check_rr_double`, both `risk.check_partial_tps` calls | EXIT | reduce-only, not new exposure |
| `engine._open` emergency close after failed SL/TP | PROTECTION | reduce-only; necessarily after an accepted entry |
| `engine._guard_naked_positions` fallback emergency close | PROTECTION; superseded on Binance | reduce-only |
| `binance_protection_failclosed._safety_close` (post-open/guardian) | PROTECTION | owned position only, reduce-only; can remain available after entry BLOCK |
| `prelive_protection_failclosed` close, `native_stop_repair` stop POST | PROTECTION / DEAD for selected Binance bootstrap | KuCoin-only installation/endpoint, reduce-only |
| Binance `set_position_stops` two `/fapi/v1/algoOrder` calls | PROTECTION | SL/TP with `closePosition=true`; no new entry |
| `kucoin.place_order`, `kucoin_native_tpsl` native entry POST | DEAD/UNREACHABLE in Binance selection | Active only for KuCoin; not a Binance entry bypass |
| `_recover_ambiguous_order`, `get_order_by_client_oid`, durable/live reconciliation | RECONCILIATION | order lookup/identity/fill recovery; no alternate Binance creation POST |
| `/api/close-all` | ADMIN -> EXIT | delegates emergency reduction; no admin new-entry route found |
| Paper dispatch/simulation branches | PAPER | no Binance LIVE POST |
| Calls under `tests/` | TEST | fake/mock/loopback proofs; not production entry callers |
| `set_leverage`, margin-mode POSTs, listenKey, Telegram, runtime-truth export | ADMIN / non-order requests | not order creation; unchanged |
| Fence, submission-counter and truth wrappers | Same classification as wrapped call | delegation, not an independent entry initiator |

The entry dispatch is guarded in the engine, not by a globally latched
"stress ever failed" switch. A later independent healthy candidate may be
evaluated normally. Existing-position exits/protection are intentionally not
blocked by failed candidate stress. Arbitrary future direct client/transport
callers would require their own proof; the current source inventory and
mutation tests are regression evidence, not a sandbox around arbitrary code.

## Deterministic proof and validation

Run in a clean process using the repository runner:

```sh
python -m tests.run_offline tests.test_binance_cross_stress_dispatch_proof
python -m tests.run_offline
python -m compileall -q bot tests main.py main_hardened.py
ruff check bot tests main.py main_hardened.py --select E9,F63,F7,F82
python -m pyflakes bot main.py main_hardened.py
python -m bot.selfcheck
python -m bot.release_proof
```

The harness sets synthetic LIVE release flags **only in its isolated test
process**, enables 50x/1%/10% test configuration, installs a network audit hook
before bootstrap, and has no usable exchange signing dependency. It uses the
real Binance client with fake account/position/config/bracket readers and a
fake `_request`, plus a spy delegating to the real installed `place_order`.
The fake ACK is accepted, with fake protection/fill handling; no real fills or
profit are asserted. NEXUS/score/preflight-integrity inputs and independent
database/ownership availability are controlled; live market acquisition,
PostgreSQL concurrency and Binance behavior are not simulated as proven facts.

| Requested negative scenario | Observed result after correction |
|---|---|
| BLOCK | 0 place_order; 0 order requests; final evaluator reached |
| Evaluator exception | 0; 0; engine aborts |
| Account timeout | 0; 0; injected `asyncio.TimeoutError` on account read |
| Missing account | 0; 0 |
| Malformed account | 0; 0 |
| Local/exchange position divergence | 0; 0 |
| Open-order margin inconsistency | 0; 0 |
| Missing bracket | 0; 0 |
| Invalid bracket | 0; 0 |
| Bracket cannot support 50x | 0; 0 |
| Invalid maintenance ratio | 0; 0 |
| Stressed margin <= 0 | 0; 0 |
| Risk rate >= limit | 0; 0 |
| Missing quantity context | 0; 0; final-context binding blocks |
| Zero quantity context | 0; 0 |
| Negative quantity context | 0; 0 |
| Missing signal context | 0; 0 |
| Unknown symbol | 0; 0; viable-symbol gate blocks earlier |
| Candidate CROSS unconfirmed | 0; 0 |
| Unexpected internal error | 0; 0 |
| Additional: quantity context drift | 0; 0 |
| Positive control | **1 place_order; 1 fake order request; matching quantities at all boundaries** |

The suite also checks PRE_ORDER BLOCK/exception separately, closure wrapper
order, final refresh owner, contract failure when removing the wrapper, and
optional post-bootstrap truth wrappers. Faults injected into evaluator state
are required to reach the second evaluation, avoiding a vacuous earlier block.
Context/unknown-symbol faults assert their intentionally earlier rejection.

Mutation runs in disposable repository copies all failed the proof as required:
remove overlay; change BLOCK to PASS; convert evaluator exception to PASS;
insert place_order before final stress; increase quantity after stress.

### Validation results and limits

| Validation | Result |
|---|---|
| New dispatch proof | 30 tests PASS; 20 required negative cases plus drift, PRE_ORDER, contract, wrapper and positive controls |
| Related engine/final-sizing/final-loss/CROSS/ownership/fence/contract pack | 90 tests PASS |
| Same final proof on BASE | 30 tests collected; 5 assertion failures, exactly the reproduced cases listed above |
| Same final proof on corrected candidate | 30 PASS; 0 failures |
| Mutation pack, disposable copies | 5/5 regressions detected (each mutant exits nonzero) |
| Full offline suite | Runner reports `TOTAL=1901 PASSED=1901 FAILED=0`; direct skip verification corrects this to **1899 passed, 2 PostgreSQL tests skipped**, zero failing suites |
| compileall | PASS |
| Ruff, repository CI selector `E9,F63,F7,F82` | PASS |
| Pyflakes full command | 52 pre-existing diagnostics, nonzero exit; identical diagnostic set on BASE and candidate, **zero undefined names**; new proof file clean |
| Selfcheck | **CRÍTICOS: 0**; existing noncritical warnings remain |
| Release proof | Prints `RELEASE_PROOF=PASS`, `TOTAL=162`; **161 passed, 1 PostgreSQL proof skipped** after direct skip verification |
| Unchanged protected files | Empty BASE diff for `final_loss_budget.py`, `final_sizing_invariants.py`, `config.py` |

Baseline comparison used the exact BASE worktree with only the new proof file
copied into it. PostgreSQL skip tests were run directly with unittest on both
BASE and candidate; both report `OK (skipped=2)`. Pyflakes was run with the
same interpreter, modules and options on both trees; comparison of diagnostic
sets gives 52 equal entries, no additions or removals (directory iteration order
differs). These pre-existing validation limitations were not hidden or fixed
by changing trading code.

The test code tree uploaded through GitHub matches the locally validated tree
`00a8940397f485a4df13a07ed966a8e999b29288` before this documentation-only commit.

## Residual risks / merge recommendation

- Keep this PR **draft** until review and the exact-head CI proof, including
  isolated PostgreSQL tests and pinned dependency installation, pass.
- Local release proof reports PASS but its skip accounting is inaccurate:
  `tests.run_offline` uses `skipped=(\\d+)`, which fails to count normal unittest
  skip output. Direct execution confirms the PostgreSQL proofs are skipped
  without `TEST_POSTGRES_DSN` at both BASE and candidate. No runner change was
  made because it is outside this audit. Do not call this a complete DB proof.
- Local Python is 3.12.14; CI selects 3.11. Available local aiohttp is 3.13.5
  rather than pinned 3.14.3, cryptography 46.0.0 rather than pinned 50.0.1.
  All tests use offline fakes; this does not replace verification with pins.
- REST account/positions/config/brackets are not one atomic exchange snapshot.
  Brackets have a five-minute cache; local stop assumptions are not an
  exchange stop-fill guarantee. Open-order inconsistencies with zero reported
  order margin after the earlier preflight are not independently enumerated
  by the final stress evaluator.
- Startup contract checks source ownership/marker, not full semantic hashes,
  and does not continuously recheck late arbitrary monkey patches.
- Binance transport currently bypasses the installed `_fenced_entry_post`
  helper; ownership is validated earlier in native place_order. This remains
  a separate residual safety concern, not a reason to weaken CROSS stress.
- Intentional reduce-only/protection calls remain possible independently of
  a rejected candidate. No claim of universal zero order calls across all
  asynchronous account management is made.

**Trading/risk changes: NONE to policy or parameters. Minimal safety corrections
only for reproduced incoherent-context and unconfirmed-CROSS bypasses.**
