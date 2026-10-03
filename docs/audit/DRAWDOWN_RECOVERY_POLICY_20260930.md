# Drawdown Hard-Gate Recovery Audit — 2026-09-30

## Scope

This audit evaluates how NEXUS-7 can recover from a legitimate account-equity
drawdown above the configured 10% hard gate without:

- resetting or lowering the HWM;
- changing the meaning of external cash flows;
- globally increasing MAX_DRAWDOWN;
- leaving LIVE_RISK_OVERRIDE_APPROVED enabled;
- bypassing ownership, fencing, durable reconciliation, protection, market-data,
  duplicate-order, or position-count gates.

No trading or risk policy is changed by this audit.

## Current production state

Evidence-backed state after the HWM incident repair:

- authenticated equity: 19.18862133 USDT
- performance HWM: 22.798693855106116 USDT
- legitimate performance drawdown: 15.834558541157764%
- configured hard gate: 10%
- account flat
- override disabled

The equity that would mathematically correspond to a 10% drawdown against a
fixed 22.798693855106116 HWM is 20.518824469595503 USDT.

However a pure deposit cannot create that recovery. The cash-flow ledger
correctly applies a TWR rebase to the HWM, preserving performance drawdown.
Therefore adding capital does not reduce the 15.83% performance drawdown.

## Current recovery paths

Current code provides two threshold outcomes:

1. drawdown < MAX_DRAWDOWN: normal entry authorization may continue, subject to
   all other gates;
2. drawdown >= MAX_DRAWDOWN:
   - default: block new entries;
   - LIVE_RISK_OVERRIDE_APPROVED=true: bypass the drawdown threshold.

There is no bounded recovery state between those two outcomes.

## Deadlock proof

When all of the following are simultaneously true:

- drawdown >= hard gate;
- account is flat;
- no new entries are authorized;
- external capital flows preserve performance drawdown;

then NEXUS-7 has no endogenous mechanism that can create positive trading PnL
and bring drawdown below the threshold.

This is an operational deadlock, not a HWM bug.

## Rejected recovery approaches

### Reset HWM to current equity

Rejected. This erases historical loss and turns a 15.83% performance drawdown
into 0% by accounting mutation.

### Treat a deposit as recovery

Rejected. External capital is not trading performance. The TWR cash-flow
contract must continue to preserve drawdown across deposits and withdrawals.

### Raise MAX_DRAWDOWN globally

Rejected as a recovery mechanism. It changes the primary loss boundary for all
normal trading, not only the recovery episode.

### Leave LIVE_RISK_OVERRIDE_APPROVED=true

Rejected as the target design. The existing override is a broad threshold
bypass and has no trade-count, expiry, recovery-risk, or drawdown-worsening
budget.

## Recommended design: bounded Recovery Authorization

Introduce a separate fail-closed recovery authorization. It must not modify the
10% hard gate or HWM. It is a narrowly scoped exception that authorizes limited
new risk while the normal hard gate remains breached.

The feature must be disabled by default and require explicit operator
authorization for each recovery episode.

### Mandatory arming preconditions

Recovery authorization may arm only when all conditions are confirmed:

- authenticated equity is positive and finite;
- account is flat at arming time;
- zero active BGX orders and zero external orders;
- zero pending/unreconciled external cash flows;
- durable HWM and provenance are valid;
- ownership lease and fencing are valid;
- private stream is event-capable and reconciled;
- durable execution state is confirmed;
- no external-performance quarantine;
- no unprotected position;
- no unresolved order identity;
- current drawdown is above MAX_DRAWDOWN;
- a separately configured RECOVERY_MAX_DRAWDOWN exists and current drawdown is
  below it;
- a separately configured RECOVERY_MAX_RISK_PCT exists and is strictly smaller
  than or equal to the normal MAX_RISK_PCT;
- authorization has a unique episode id and expiry.

No implicit defaults should be supplied for the recovery ceiling or recovery
risk. Missing values must fail closed.

### Runtime constraints while armed

- at most one recovery position at a time;
- final quantity remains
  min(stop-risk quantity, operator margin cap quantity);
- recovery stop-risk budget uses RECOVERY_MAX_RISK_PCT, never normal risk;
- leverage configuration remains unchanged; leverage must not expand monetary
  risk budget;
- all existing pre-dispatch checks still run;
- no override of protection, CROSS stress, market-data freshness, fencing,
  durable execution, reconciliation, or instrument filters;
- no HWM reset/rebase except verified external cash flow;
- recovery authorization must be visible in every entry decision log.

### Automatic disarm conditions

Recovery authorization must disarm immediately when any of these occurs:

- drawdown returns below MAX_DRAWDOWN: normal mode resumes;
- a recovery trade closes at a net loss;
- drawdown worsens after a recovery trade;
- RECOVERY_MAX_DRAWDOWN is reached;
- authorization expires;
- ownership/fencing becomes invalid;
- private stream loses trusted state;
- durable reconciliation becomes incomplete;
- external cash-flow reconciliation becomes pending/ambiguous;
- an external/manual position or order appears;
- process restarts unless the authorization receipt is explicitly durable and
  still valid.

### Episode accounting

Each episode should persist:

- episode id;
- armed_at / expires_at;
- equity and HWM at arm time;
- drawdown at arm time;
- configured recovery ceiling;
- configured recovery risk pct;
- entry count;
- realized net PnL attributable to recovery trades;
- worst drawdown during episode;
- disarm reason.

This is necessary so recovery is auditable and cannot silently become a
permanent override.

## Required implementation tests before production

A future implementation PR must prove at minimum:

1. recovery disabled by default;
2. missing recovery ceiling/risk config fails closed;
3. current production 15.83% drawdown cannot enter normal mode;
4. deposit does not lower drawdown or arm recovery;
5. arming fails with any open position/order;
6. arming fails with pending cash-flow evidence;
7. arming fails with invalid ownership/fencing/private-stream/durable state;
8. recovery sizing cannot exceed normal risk or operator margin cap;
9. second concurrent recovery position is rejected;
10. loss or worsening drawdown disarms recovery;
11. expiration disarms recovery;
12. normal 10% hard gate automatically regains sole authority once drawdown is
    below threshold;
13. LIVE_RISK_OVERRIDE_APPROVED is not used by recovery mode;
14. restart behavior is deterministic and fail-closed;
15. final pre-dispatch drawdown/recovery decision is based on fresh authenticated
    equity.

## Conclusion

The current hard gate is functioning correctly but is terminal for a flat
account unless the broad operator override is enabled. The safe next design is
not to reset accounting or weaken the 10% gate; it is a separate, explicitly
armed, bounded recovery authorization with its own upper drawdown ceiling,
smaller risk budget, expiry, one-position limit, and automatic disarm rules.
