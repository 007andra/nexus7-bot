# Durable Recovery Episode Contract

This is the durable continuation of the bounded drawdown Recovery Authorization.

## Core invariant

A Recovery episode is single use.

When a candidate is above the normal drawdown gate but below the separately
configured recovery ceiling, the runtime first requires a durable ARMED
receipt. The receipt is keyed by Railway environment and exchange and records:

- episode id;
- UTC expiry;
- recovery drawdown ceiling;
- recovery risk percentage;
- equity and HWM at arm time;
- drawdown at arm time;
- entry-attempt count.

At the final Binance pre-dispatch boundary, after the normal LIVE gate chain and
the final Binance CROSS stress check pass, the runtime atomically changes the
receipt from ARMED to CONSUMED before exchange network I/O.

A consumed episode can never authorize another Recovery entry.

## Why consume before dispatch

Network ambiguity means a timeout or rejected acknowledgement cannot safely be
interpreted as no order happened. Therefore a Recovery token is not restored
after a dispatch attempt. If the order is rejected, times out, or the process
restarts, a new explicit episode id is required for another Recovery attempt.

This is intentionally stricter than disarm after a losing trade. Every Recovery
attempt disarms itself before dispatch, regardless of outcome.

## Restart semantics

- same episode plus durable ARMED receipt: may resume evaluation if all runtime
  safety gates still pass and the receipt/configuration match exactly;
- same episode plus CONSUMED: new Recovery entry blocked;
- different episode while an old receipt is still ARMED: blocked;
- a new explicit episode id may replace only a terminal receipt;
- malformed, missing, or configuration-drifted durable state fails closed.

An existing position is always managed by the normal position/protection
runtime; this contract governs only new exposure.

## Scope

The durable token does not bypass:

- ownership or fencing;
- durable execution reconciliation;
- private-stream readiness;
- external-performance quarantine;
- position/order coherence;
- Binance CROSS stress;
- market-data or signal-drift checks;
- instrument filters;
- protection readiness.

It changes no HWM, cash-flow/TWR accounting, leverage, normal MAX_DRAWDOWN,
strategy score, SL/TP, or exchange configuration.

Production activation remains a separate operator action. The feature is still
disabled unless all Recovery environment settings are explicitly supplied.