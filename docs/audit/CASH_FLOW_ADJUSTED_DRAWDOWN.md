# Cash-flow-adjusted drawdown (Binance USD-M)

Status: implemented in `bot/cash_flow_ledger.py` (authority), wired through
`bot/capital_flow_reconciliation.py` → `bot/pilot_live_runtime._refresh_account`.
Operator tool: `python -m bot.cash_flow_admin`.

## 1. Incident (2026-09-26, production, read-only evidence)

| time (UTC) | evidence (Railway logs, read-only) |
|---|---|
| until 21:50 | `[PILOT_LIVE_BALANCE] equity=9.8410 available=9.8410 peak_equity=10.8262 drawdown=9.10%`: flat account, no positions |
| 22:28:59 | `[CAPITAL_FLOW_BINANCE] new_transfers=2 net_amount=0.00000000 ... action=BLOCK_NEW_ENTRY` |
| 22:30:25 | `[CAPITAL_FLOW_BINANCE] new_transfers=3 net_amount=-4.00000000 ... action=BLOCK_NEW_ENTRY` |
| after | equity `5.8410`, durable HWM `10.8262`, raw drawdown `1 - 5.8410/10.8262 = 46.05%` |

The figures come from these sources:
- `10.8262` is the durable HWM (`drawdown_persistence`, key `hwm_namespace.equity_peak_key()`). It was set by a real equity high.
- `5.8410` is `totalMarginBalance` from `/fapi/v3/account` after the operator withdrew 4 USDT.
- `46.05%` is gross drawdown. It mixes a 0.9852 USDT trading loss (9.10%) with the 4 USDT withdrawal.

**Root cause.** The Binance path of the capital-flow reconciliation only
*detected* new TRANSFER rows. It then blocked entries forever. It had no way to
rebase the HWM, because `/fapi/v1/income` has no post-transfer equity anchor.
The durable HWM therefore kept counting the withdrawal as a trading loss.
Entries were correctly blocked (fail-closed), but no mechanism existed to
reconcile the flow.

## 2. Methodology: time-weighted performance HWM

Trading performance is not the gross change in balance. For every external flow
of signed amount `a`, with equity `E-` just before it and `E+ = E- + a` just after:

```
P' = P · E+ / E-                 (performance HWM, rebased)
trading_drawdown = 1 − E / P'
```

Proofs of the required properties:

1. **A flow alone does not change drawdown.** `1 − E+/P' = 1 − E+·E-/(P·E+) = 1 − E-/P`.
   A withdrawal cannot increase drawdown, and a deposit cannot reduce it.
2. **A flow never creates profit.** After the flow, `E+/P' = E-/P ≤ 1`. The HWM
   only rises above `P'` when later equity exceeds it (`restore_update_real_account_peak`,
   reason `new_equity_high`). That can only come from performance.
3. **Real losses keep blocking.** Between flows, `E` moves only by performance
   income (REALIZED_PNL, COMMISSION, FUNDING_FEE, …) and unrealized PnL.
   `1 − E/P'` grows exactly with those losses. Example: 10 → 9 (loss) → withdraw 1 → 8 gives
   `P' = 10·8/9 = 8.889` and `dd = 1 − 8/8.889 = 10%`. The loss is still the full 10%.
   If the withdrawal comes first (10 → 9, then lose 1 → 8), the drawdown is `1/9 = 11.1%`.
   That is the loss measured on the capital that was actually invested.
4. **Production numbers.** `P' = 10.8262 · 5.8410 / 9.8410 = 6.425753`, so
   `dd = 1 − 5.8410/6.425753 = 9.10%`. This is identical to the pre-withdrawal
   drawdown (`1 − 9.8410/10.8262`). It is still below the unchanged 10% limit.
   If the bot lost another 0.0578 USDT (equity below 5.7832), drawdown would reach 10% and entries would block again.

KuCoin (non-production) keeps its existing post-equity-anchored path. Its deposit
rebase stays additive (see Residual risks).

## 3. Income classification (`/fapi/v1/income`)

| class | incomeType |
|---|---|
| EXTERNAL_FLOW | TRANSFER, INTERNAL_TRANSFER, STRATEGY_UMFUTURES_TRANSFER, CROSS_COLLATERAL_TRANSFER, DEPOSIT, WITHDRAW, COIN_SWAP_DEPOSIT, COIN_SWAP_WITHDRAW |
| PERFORMANCE | REALIZED_PNL, COMMISSION, FUNDING_FEE, INSURANCE_CLEAR, rebates/kickbacks, and **every unknown type** (the conservative default) |

## 4. When a flow is applied automatically (evidence-reconstructed)

`E-` must be provable from exchange evidence. All of the following must hold:

- The income evidence is stable. It is read, then the account is read, then the income is read again, and both income reads must be identical.
- Single-asset margin mode.
- The account is flat now: `positionMargin == 0`, `unrealisedPNL == 0` and `equity == walletBalance`.
- Every pending flow is inside the 48 h evidence window.
- No PERFORMANCE income row of any asset exists at or after the first pending flow.
- No other unreconciled external flow is interleaved.

Under these conditions the wallet moved only by the pending flows, so
`E- = wallet_now − Σ pending` exactly. No position can have been open at the
flow time: such a position would have produced a closing REALIZED_PNL/COMMISSION row afterwards.

The system never infers a transfer from a balance difference. Amounts,
directions and timestamps always come from ledger rows.

## 5. Fail-closed states (`BLOCK_NEW_ENTRIES`, exits unaffected)

| condition | code |
|---|---|
| income or account API unavailable | `BINANCE_CASH_FLOW_EVIDENCE_UNAVAILABLE` |
| income changed during the double read | `BINANCE_CASH_FLOW_EVIDENCE_UNSTABLE` |
| pending flow whose `E-` is not provable (position open, trading after flow, multi-asset, outside window, implausible ratio) | `BINANCE_CAPITAL_FLOW_REBASE_UNCONFIRMED` |
| wallet moved by an amount the ledger does not explain (`|Δwallet − Σ income after anchor| > max(0.02, 0.5%)`) | `BINANCE_UNRECONCILED_EQUITY_CHANGE` |

Pending flows are durable (`risk:external_cash_flows:binance:ledger:v1`). A flow
that ages out of the exchange window keeps blocking and is never forgotten.
An unexplained drop is treated as a loss until evidence explains it. It is never assumed to be a withdrawal.

## 6. Operator attestation (ambiguous cases)

```
python -m bot.cash_flow_admin show
python -m bot.cash_flow_admin attest --reconciliation-id <ID> --tran-ids <id1,id2> \
    --pre-flow-equity <E-> --evidence-ref "<log line / statement>" --reason "<why>"
# review the dry-run output, then:
python -m bot.cash_flow_admin attest ... --apply --confirm ATTEST:<ID>
```

- Amount, direction and timestamps are taken from the Binance rows named by `--tran-ids`. The operator never types them.
- The operator supplies only `E-`, together with an evidence reference.
- The operation is idempotent by `reconciliation_id`, and a flow can never be applied twice.
- Flows that were checkpointed before the ledger existed are refused.
- Performance income between the attested flows is refused; each such flow must be attested separately.
- The record stores: reconciliation id, method, identities, tranIds, net amount,
  direction, flow timestamps, pre/post-flow equity, previous HWM, adjusted HWM,
  equity at reconciliation, resulting trading drawdown, reason, evidence ref and recorded time.
- One compare-and-swap transaction writes the HWM, provenance, ledger and cursor
  together (`atomic_key_value.save_key_values_atomic_cas`). A concurrent writer
  can never overwrite it silently.
- The running process detects the new record and reloads the durable HWM (`[DURABLE_DRAWDOWN] reload=ledger_changed`).

`rebaseline-wallet` re-anchors only the wallet-completeness invariant. It never changes the HWM.

## 7. Observability

- `[CASH_FLOW]`: a flow was detected (PENDING) or applied.
- `[DRAWDOWN_RECONCILIATION]`: RECONCILED or BLOCK, with the reason.
- `[ADJUSTED_EQUITY]`: equity, performance HWM, trading drawdown (N/A while unreconciled), limit, applied and pending flows.
- `[DURABLE_DRAWDOWN]`: unchanged, plus a reload notice.
- Market Radar footer shows equity, performance HWM, external flows, trading drawdown and the limit. A pending flow shows `N/A` plus a blocked warning. A gross figure caused by a flow is never presented as trading drawdown.

## 8. Unchanged

`MAX_DRAWDOWN`, `MAX_RISK_PCT`, leverage, SL/TP, strategy/NEXUS, the RISK_BLOCK
gates, `LIVE_RISK_OVERRIDE_APPROVED` semantics and daily-stop logic are all
unchanged. The daily stop remains trade-based (`durable_daily_pnl.realized`) and
never uses equity deltas.

## 9. Residual risks

- KuCoin deposit rebase remains additive (non-production venue, pre-existing incident contract).
- In multi-asset margin mode, the wallet invariant is unverifiable (logged) and flows need operator attestation.
- A near-total loss followed by a large deposit (flow ratio > 1000) blocks permanently by design. It needs a new, explicitly approved HWM epoch; no such mechanism exists here.
- Binance income-row lag can block for one or more cycles until the row appears. This is fail-closed and self-heals.
