# Bounded Drawdown Recovery Authorization

## Status

Implemented but disabled by default.

This feature does not change the normal `MAX_DRAWDOWN` hard gate, does not
reset HWM, does not treat deposits as performance, and does not use
`LIVE_RISK_OVERRIDE_APPROVED`.

No recovery episode can exist unless the operator explicitly supplies every
required value.

## Configuration

All values are mandatory when recovery is explicitly approved:

- `LIVE_DRAWDOWN_RECOVERY_APPROVED=true`
- `LIVE_DRAWDOWN_RECOVERY_EPISODE_ID=<unique id>`
- `LIVE_DRAWDOWN_RECOVERY_EXPIRES_AT=<timezone-aware ISO-8601 timestamp>`
- `LIVE_DRAWDOWN_RECOVERY_MAX_DRAWDOWN=<fraction or percent>`
- `LIVE_DRAWDOWN_RECOVERY_MAX_RISK_PCT=<fraction or percent>`

There are no defaults for the recovery ceiling or recovery risk budget.

The recovery ceiling must be above the normal hard gate. The recovery risk
budget must be strictly below the normal `MAX_RISK_PCT`. If
`LIVE_RISK_OVERRIDE_APPROVED=true` at the same time, the recovery path fails
closed.

## Authority layers

### 1. Scan permission

When drawdown is already above the normal hard gate, a valid recovery
configuration may allow the engine to scan/analyze candidates. This has no
execution authority.

The scan permission requires:

- valid, non-expired explicit recovery configuration;
- no broad drawdown override;
- zero local open positions;
- drawdown below the configured recovery ceiling.

### 2. Candidate authorization

A recovery candidate is authorized only after the normal controlled-LIVE
read-only preflight and IntegrityGuard pass.

The recovery authority additionally verifies:

- positive finite authenticated equity/HWM;
- current drawdown remains above the normal gate but below recovery ceiling;
- account locally flat;
- no pending durable order;
- durable execution state allows a new entry;
- no external-performance quarantine;
- pre-live REST exposure is verified and clear;
- execution ownership is locally valid and revalidated against PostgreSQL;
- Binance private stream is EVENT_CAPABLE;
- external cash-flow ledger exists and has zero pending flows;
- durable recovery episode is ARMED, unconsumed, unexpired and config-stable.

Only then is a short-lived candidate context created.

### 3. Risk budget

The candidate context changes only the planned stop-risk percentage:

`effective_risk_pct = recovery_max_risk_pct`

It is required to be strictly below the normal effective risk percentage.

The existing final sizing contract remains:

`final_qty = min(stop_risk_qty, operator_margin_cap_qty)`

Configured leverage is unchanged and does not increase the monetary risk
budget.

### 4. Fresh pre-dispatch gate

The normal fresh authenticated drawdown gate runs again. Above the normal hard
gate it accepts only the current, unexpired recovery context and only while the
account remains locally flat.

All other gates remain authoritative.

## Durable episode lifecycle

A durable episode record stores:

- episode id;
- armed timestamp and expiry;
- equity/HWM/drawdown at arm time;
- recovery ceiling and recovery risk percentage;
- worst observed drawdown;
- entry count;
- recovery symbol;
- durable client order ids;
- disarm reason;
- PnL fields reserved for authoritative attribution.

Each episode permits at most one new INCREASE intent.

The episode becomes `IN_TRADE` only after a new durable INCREASE intent or
local position is observed. A candidate rejected before creating risk does not
consume the episode.

After the recovery trade/intent completes and the account is flat, the episode
is disarmed and a new explicit episode id is required for another recovery
trade.

The episode also disarms when:

- normal drawdown gate is recovered;
- recovery ceiling is reached;
- authorization expires;
- drawdown worsens while still ARMED.

Ownership/private-stream/durable/cash-flow failures block candidate
authorization independently, so they cannot be bypassed by recovery mode.

## Disabled behavior

With `LIVE_DRAWDOWN_RECOVERY_APPROVED` absent or false, the lifecycle
reconciler returns immediately without a database read. Production behavior is
therefore unchanged until explicit authorization is supplied.

## Current production incident

The repaired account state remains approximately:

- equity: 19.18862133 USDT
- performance HWM: 22.7986938551 USDT
- drawdown: 15.83%
- normal hard gate: 10%

This implementation alone does not authorize recovery in production because no
recovery environment configuration is set by this PR.
