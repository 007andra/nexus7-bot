# External-position drawdown contamination proof

## Scope

Base SHA: `e3e2e7276e7f971ff3d7dc514043cd6011684ffd`

Branch: `audit/external-position-drawdown-contamination`

Audit/test only. No drawdown threshold, risk sizing, Binance CROSS stress, ownership,
Railway variable, release flag, or execution policy is changed.

## Production evidence

The current Binance LIVE account path normalizes account equity from
`totalMarginBalance` and also exposes `totalUnrealizedProfit`.

On 2026-09-27 production logs showed:

- durable HWM initially around `6.4680`;
- an unexpected external/manual `ATOMUSDT` position repeatedly classified as
  `EXTERNAL_POSITION_IMMUTABLE`;
- no BGX order evidence in the accounting evidence snapshots;
- account equity fluctuating materially while that external position existed;
- the durable HWM later at `7.3562`;
- current flat-account equity later at `6.0944`;
- restored drawdown `17.15%`, which blocks new entries at the configured 10% limit.

The external-position guard correctly treats unowned positions as read-only and
blocks new entries while they are unsafe. The separate drawdown HWM path,
however, consumes total account equity and does not consult position ownership
before creating a new performance high.

## Reproduction

The test uses the real LIVE account refresh chain with an in-memory durable
database.

Starting wallet/HWM:

`6.4680`

An external/manual position contributes:

`+0.8882` unrealized PnL

so account equity becomes:

`6.4680 + 0.8882 = 7.3562`

The current durable drawdown code records `7.3562` as a new HWM even though no
BGX-owned order or trade is required and no ownership query participates in the
HWM update.

If the same external exposure then moves to `-0.3736` unrealized PnL, equity is:

`6.4680 - 0.3736 = 6.0944`

and the current drawdown becomes:

`1 - 6.0944 / 7.3562 = 17.15%`

which reproduces the production hard-gate state.

The third proof closes the external/manual position at the same loss. Because
`REALIZED_PNL` is treated as performance by the cash-flow ledger and the
drawdown path has no BGX ownership filter, the `7.3562` HWM remains after the
position disappears and the 17.15% drawdown remains blocking.

## Safety interpretation

This audit does **not** conclude that external exposure should be ignored for
account solvency.

While an external position is open, account-level safety must remain
fail-closed through:

- external-position ownership/protection guard;
- exposure capacity;
- Binance CROSS portfolio stress;
- authenticated account equity/collateral checks.

The separate question is performance attribution: whether unrealized/realized
PnL from a position explicitly classified as external/manual should be allowed to
create or permanently draw down the NEXUS trading-performance HWM after the
external exposure is gone.

## Candidate classification

If exact-head CI reproduces the tests, classify as:

`EXTERNAL_POSITION_PNL_HWM_CONTAMINATION`

The next change should be a separate design/fix PR. It must preserve account-level
solvency gates while separating BGX trading-performance drawdown attribution from
external/manual position PnL. Do not solve this by raising the 10% threshold or
using `LIVE_RISK_OVERRIDE_APPROVED`.
