# 2026-09-30 Binance HWM Incident Repair

## Incident evidence

Production persisted an incorrect HWM because a Binance deposit was promoted to
performance HWM before the external cash-flow ledger reconciled the transfer.

The authoritative reconciliation record is:

- reconciliation_id: `auto-176be1f3ca8bbe91`
- withdrawal tranId: `416195536884`, amount `-7.40782133`
- deposit tranId: `416435307318`, amount `+19.18862133`
- pre-flow equity: `7.40782133`
- post-flow equity: `19.18862133`
- last legitimate pre-flow HWM: `8.8015`
- incorrectly supplied previous_hwm: `19.18862133`
- contaminated HWM: approximately `49.7046529801`

The last legitimate HWM and pre-flow equity imply a pre-flow drawdown of about
15.83%. A pure external capital flow must preserve that performance drawdown.

Replaying the same TWR transformation with the legitimate HWM gives:

`8.8015 * 19.18862133 / 7.40782133 = 22.7986938551`

At current equity `19.18862133`, the repaired drawdown is still about 15.83%,
which remains above the configured 10% hard gate. This repair therefore does
not reopen trading.

## Repair contract

The one-shot repair runs only when all of these match:

- current durable HWM equals the known contaminated HWM;
- current equity still matches the incident post-flow equity within 0.02 USDT;
- the durable cash-flow ledger contains exactly the known reconciliation id;
- its two transfer identities/tranIds, pre/post equity, previous HWM, adjusted
  HWM, method and reason match the incident;
- durable HWM provenance points to the same reconciliation and contaminated HWM;
- the incident marker is absent.

The write is one compare-and-swap transaction containing:

1. repaired HWM;
2. `incident_repair` provenance;
3. one-shot incident marker.

The cash-flow ledger itself is left unchanged as historical evidence. Any
evidence mismatch fails closed and writes nothing.

No drawdown threshold, leverage, sizing, strategy, order, SL/TP, or exchange
state is modified.
