# 2026-09-30 HWM Incident Repair

## Proven incident

Production evidence established this sequence:

1. Last stable pre-flow state:
   - equity: 7.40782133 USDT
   - durable HWM: 8.8015 USDT
   - trading drawdown: about 15.83%
2. Binance external flows:
   - TRANSFER tranId 416195536884: -7.40782133 USDT
   - TRANSFER tranId 416435307318: +19.18862133 USDT
3. Before those flows were reconciled, the deposit was incorrectly promoted to
   a performance HWM of 19.18862133.
4. Reconciliation auto-176be1f3ca8bbe91 then used that contaminated HWM as its
   previous HWM and produced 49.70465298008625.

PR #442 prevents this ordering from recurring. This repair addresses only the
already-persisted incident.

## Repair value

External capital must not change performance drawdown. The last legitimate
drawdown ratio is therefore preserved across the capital replacement:

    repaired_hwm =
        8.8015 * 19.18862133 / 7.40782133
        = 22.798693855106116

At equity 19.18862133 this leaves the same legitimate drawdown of about 15.83%.

The repair does NOT set HWM to current equity and does NOT reset drawdown to 0.

## Write contract

The repair is allowed only if all evidence matches:

- reconciliation id: auto-176be1f3ca8bbe91
- method: LEDGER_RECONSTRUCTED
- tranIds: 416195536884 and 416435307318
- pre-flow equity: 7.40782133
- post-flow equity: 19.18862133
- bad previous HWM: 19.18862133
- bad adjusted HWM: 49.70465298008625
- authenticated current equity remains 19.18862133
- durable HWM still equals the bad adjusted HWM

The HWM, HWM provenance, corrected ledger record, and one-shot repair marker are
written in one compare-and-swap transaction. Any mismatch writes nothing and
fails closed.

## Execution safety

This repair changes no:

- MAX_DRAWDOWN threshold
- override
- leverage
- sizing
- strategy score
- SL/TP
- order state
- exchange position
- execution permission

After the repair, the 10% hard gate should still block entries because the
evidence-backed drawdown remains approximately 15.83%.
