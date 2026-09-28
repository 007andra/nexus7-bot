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
