# FINAL_LOSS_BUDGET observability-only transition

## Base

`migration/binance-usdm` @ `861484966e517b6ee166f245bef36342647b5ac9`

Branch:

`fix/final-loss-budget-observability-only`

## Purpose

Convert the historical `FINAL_LOSS_BUDGET` from an execution veto into
diagnostic telemetry while preserving every current binding risk/safety
authority.

No leverage, score, drawdown, stop/target, sizing formula, exchange filter,
margin cap, release flag, or Railway variable is changed.

## Why

PR #358 introduced the 50%-of-entry-initial-margin ceiling when final LIVE
quantity could still be margin-authoritative.

PR #417 later changed the executed sizing contract to:

`final_qty = min(stop_risk_qty, operator_margin_cap_qty)`

with RiskManagerV3 as the monetary-risk authority.

PR #425 then proved and hardened the Binance CROSS pre-dispatch boundary so a
failed CROSS stress cannot reach entry dispatch.

PR #426 dynamically demonstrated the remaining mismatch:

`RiskManagerV3 PASS + operator margin cap PASS + BINANCE_CROSS_STRESS PASS + FINAL_LOSS_BUDGET BLOCK`

for otherwise valid risk-sized candidates with 0.70%, 1%, 2%, 3%, and 5%
technical stops at 50x.

The legacy rule reduces algebraically to:

`stop_fraction + cost_fraction <= 0.50 / leverage`

so at 50x it imposes a 1.00% notional-loss geometry ceiling. With the
conservative altcoin cost assumption of 0.32%, this implies an approximately
0.68% maximum technical stop regardless of quantity.

## Runtime change

`final_loss_budget.validate()` remains strict and unchanged for deterministic
regression/backward compatibility.

A new `final_loss_budget.diagnose()` reports:

- `PASS` — legacy ceiling is within threshold;
- `WARN` — legacy ceiling would have blocked;
- `UNAVAILABLE` — diagnostic inputs are invalid/unavailable.

Executable call sites in:

- `final_sizing_invariants`
- `pilot_risk_cap_hardening`

consume `diagnose()` and emit telemetry with:

`decision_effect=NONE execution_effect=OBSERVABILITY_ONLY`

They no longer zero final quantity or return a pre-dispatch block solely because
of the legacy 50%-initial-margin ceiling.

## Binding authorities preserved

The following remain fail-closed and unchanged:

- RiskManagerV3 monetary stop-risk sizing;
- `final_quantity_policy=min(stop_risk_qty,operator_margin_cap_qty)`;
- 50% available initial-margin operator cap;
- exchange minimum/step/notional rules;
- drawdown hard gate;
- market quality/freshness gates;
- Binance account-mode/readiness checks;
- Binance CROSS portfolio stress;
- execution ownership/fencing;
- durable execution/idempotency;
- protection fail-closed behavior.

## Regression requirements

The dedicated regression must prove:

1. a 2% technical stop at 50x produces `FINAL_LOSS_BUDGET result=WARN`;
2. final sizing retains the RiskManagerV3-capped quantity instead of setting it
   to zero;
3. the fresh pre-dispatch market guard continues to the normal dispatch boundary
   when all independent gates pass;
4. telemetry explicitly reports `execution_effect=OBSERVABILITY_ONLY`;
5. the strict `validate()` helper still raises for the same legacy-threshold
   violation so historical math remains testable;
6. the existing PR #425 dispatch proof continues to pass, proving CROSS-stress
   failure still blocks before `place_order`.

## Rollout rule

This branch must remain isolated until exact-head CI passes. No deploy is part of
the code-change task. Production rollout, if approved, requires a separate merge
decision followed by startup/runtime verification of:

- `RUNTIME_CONTRACT=PASS`
- `FINAL_SIZING_INVARIANT installed=true`
- `BINANCE_CROSS_STRESS installed=true`
- `PILOT_LIVE_PREFLIGHT=PASS`
- durable reconciliation ready
- ownership/fencing valid
- `FINAL_LOSS_BUDGET` WARN telemetry with no execution veto.
