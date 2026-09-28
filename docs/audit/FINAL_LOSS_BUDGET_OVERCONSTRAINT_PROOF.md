# FINAL_LOSS_BUDGET overconstraint proof

## Scope

Base SHA: `861484966e517b6ee166f245bef36342647b5ac9`

Branch: `audit/final-loss-budget-overconstraint-proof`

This audit is intentionally test/documentation only. It does **not** change:

- `FINAL_LOSS_BUDGET`
- RiskManagerV3
- Binance CROSS stress
- leverage
- `MAX_RISK_PCT`
- drawdown
- stop/target geometry
- order dispatch
- Railway configuration

No real order is required or permitted by this proof.

## Historical context

`final_loss_budget` was introduced by PR #358 when the final LIVE quantity could
still be margin-authoritative. At that time the extra projected-loss ceiling
protected a quantity that was not numerically capped by the RiskManagerV3 stop
risk recommendation.

PR #417 later changed the executed sizing contract to:

`final_qty = min(stop_risk_qty, operator_margin_cap_qty)`

and made RiskManagerV3 the binding monetary-risk authority. The pre-existing
`final_loss_budget` gate was retained.

Current authority map:

- monetary stop risk: `RiskManagerV3.size_for_stop`
- operator collateral cap: `final_sizing_invariants`
- Binance CROSS solvency/liquidation stress: `binance_cross_portfolio_stress`
- additional projected-loss ceiling: `final_loss_budget.validate`

## Mathematical property under test

RiskManagerV3 sizes from:

`qty * entry * (stop_fraction + costs) <= equity * risk_pct`

The legacy projected-loss ceiling requires:

`qty * entry * (stop_fraction + costs) <= 0.50 * (qty * entry / leverage)`

For positive quantity/notional, quantity and entry cancel:

`stop_fraction + costs <= 0.50 / leverage`

At 50x:

`stop_fraction + costs <= 1.00%`

For the conservative altcoin fallback used by the test:

- taker fee: 0.06% per side = 0.12% round trip
- slippage: 0.10% per side = 0.20% round trip
- total stress cost: 0.32%

Therefore the implied maximum technical stop distance is approximately:

`1.00% - 0.32% = 0.68%`

This boundary is independent of quantity.

## Dynamic proof matrix

The test uses:

- equity = 1000 USDT
- available collateral = 1000 USDT
- `MAX_RISK_PCT` semantics = 1%
- entry = 100
- leverage = 50x
- Binance-style base-asset quantity rules
- empty existing portfolio
- CROSS confirmed
- valid Binance leverage bracket
- conservative cost fraction = 0.32%

For each stop below, the test requires:

1. RiskManagerV3 returns a positive quantity.
2. projected stop loss stays within the 1% equity risk budget.
3. the 50%-available operator margin cap does not bind above that risk quantity.
4. Binance CROSS stress passes the exact final risk-sized quantity.
5. `final_loss_budget.validate` blocks.

Expected BLOCK matrix:

| Technical stop | Stop + cost | RiskManagerV3 | CROSS stress | FINAL_LOSS_BUDGET |
|---:|---:|---|---|---|
| 0.70% | 1.02% | PASS | PASS | BLOCK |
| 1.00% | 1.32% | PASS | PASS | BLOCK |
| 2.00% | 2.32% | PASS | PASS | BLOCK |
| 3.00% | 3.32% | PASS | PASS | BLOCK |
| 5.00% | 5.32% | PASS | PASS | BLOCK |

A 0.60% stop is included as a positive control:

`0.60% + 0.32% = 0.92% < 1.00%`

and must pass all three layers.

## Leverage-control proof

A separate test holds equity, risk budget, technical stop and costs constant at a
2% stop, comparing 10x with 50x.

When collateral is not binding, RiskManagerV3 should produce the same monetary
risk budget and same quantity at both leverages.

The legacy ceiling differs:

- 10x: `0.50 / 10 = 5.00%` notional-loss ceiling -> 2.32% passes
- 50x: `0.50 / 50 = 1.00%` notional-loss ceiling -> 2.32% blocks

This demonstrates that the additional gate is leverage-dependent stop geometry,
not an equity-denominated loss budget.

## Quantity-independence proof

The same 2% stop is checked with quantities `0.001`, `1`, and `1000`.
All must receive the same BLOCK outcome from `final_loss_budget`.

This proves that reducing quantity cannot make an incompatible stop pass the
legacy ceiling.

## Interpretation boundary

The test does **not** by itself authorize removal or weakening of
`FINAL_LOSS_BUDGET`.

A policy change should only be considered after this proof passes exact-head CI
and reviewers confirm that no unique risk authority depends on the
`50% of entry initial margin` ceiling.

If the proof passes, the next separate change can evaluate whether
`FINAL_LOSS_BUDGET` should remain blocking or become diagnostic telemetry while
RiskManagerV3 and Binance CROSS stress stay fail-closed.


## Exact-head CI result

Candidate HEAD before this documentation-only result update:
`cac5b6c76f022b45b56d65bdf9b07305c6457bd0`

GitHub Actions:

- Supply Chain Security: PASS
- Quality Check: PASS
- `tests.test_final_loss_budget_overconstraint_proof`: PASS (4)
- full offline suite: `TOTAL=1905 PASSED=1905 FAILED=0 SKIPPED=0`
- release proof: PASS, including `tests.test_release_pilot_postgres`
- compile/static/startup/runtime safety steps: PASS

Observed proof verdict:

`RiskManagerV3 PASS + operator margin cap PASS + BINANCE_CROSS_STRESS PASS + FINAL_LOSS_BUDGET BLOCK`

was reproduced for 0.70%, 1.00%, 2.00%, 3.00%, and 5.00% technical
stops at 50x with 0.32% conservative round-trip stress cost.

The 0.60% positive control passed all layers.

The leverage-control test also passed: the same 2% stop retained the same
RiskManagerV3 monetary risk quantity/budget at 10x and 50x when collateral was
not binding, while the legacy projected-loss ceiling passed at 10x and blocked
at 50x.

The quantity-independence test passed for quantities 0.001, 1, and 1000.

## Audit conclusion

The dynamic evidence supports classification of the current
`FINAL_LOSS_BUDGET` blocking rule as a legacy leverage-dependent geometry
constraint layered on top of the current monetary-risk and CROSS-solvency
authorities.

This PR intentionally makes no policy change. Any conversion of this gate from
blocking to diagnostic behavior must be implemented and reviewed in a separate
PR.
