# External-position performance HWM isolation

## Base

`migration/binance-usdm` @ `e3e2e7276e7f971ff3d7dc514043cd6011684ffd`

Branch:

`fix/external-position-performance-hwm`

## Incident evidence

The production account had a non-BGX `ATOMUSDT` position from approximately
2026-09-27 16:26 UTC until 2026-09-28 00:01 UTC.

Before that episode:

- account equity: approximately `5.8827 USDT`;
- durable performance HWM: `6.4680 USDT`;
- NEXUS trading drawdown: approximately `9.05%`.

While `ATOMUSDT` was explicitly classified as
`EXTERNAL_POSITION_IMMUTABLE`, total Binance account equity was still fed into
the durable performance HWM. Production logs prove new highs were persisted at:

`6.5780 → 7.0449 → 7.0500 → 7.0835 → 7.1974 → 7.2714 → 7.3562`.

At the final `7.3562` persistence, the external-position guard was still
blocking `ATOMUSDT`, and accounting evidence still showed no BGX order
evidence.

After the external position disappeared, equity stabilized at `6.0944`, but
the contaminated HWM remained `7.3562`, producing a false NEXUS-performance
drawdown of approximately `17.15%`.

## Exchange-ledger attribution

The Binance income snapshots bracket the episode.

Delta attributable to the external/manual ATOM position:

- opening commission: `-0.06733248`
- realized PnL at close: `+0.45130999`
- closing commission: `-0.14653506`
- funding fee: `-0.02571883`

Net external/manual performance:

`+0.21172362 USDT`

Therefore:

`6.0944 - 0.21172362 = 5.88267638`

which matches the observed pre-position equity.

The incident signature is also checked independently against Binance
`userTrades` and `allOrders` plus the durable BGX order registry. Every fill
must classify as `MANUAL_EXTERNAL`; any BGX/unknown ownership blocks the
repair. The exchange evidence must reconcile:

- income `COMMISSION`: 6 rows, `-0.21386754 USDT`;
- income `REALIZED_PNL`: 1 row, `+0.45130999 USDT`;
- income `FUNDING_FEE`: 1 row, `-0.02571883 USDT`;
- `userTrades` commissions: `+0.21386754 USDT` fee amount;
- `userTrades` realized PnL: `+0.45130999 USDT`.

The commission sign difference is intentional: the income ledger expresses the
fee as a balance debit, while the user-trade record exposes the commission
amount.

The repair preserves the pre-episode performance ratio rather than merely
resetting the HWM:

`P_after = P_before × E_after / E_before`

`P_after = 6.4680 × 6.0944 / 5.88267638 ≈ 6.70079002`

and:

`1 - 6.0944 / 6.70079002 ≈ 9.04953%`.

This removes only the proven external/manual performance contribution. It does
not reset genuine NEXUS drawdown.

## Runtime contract

Account-level equity remains authoritative for:

- collateral;
- RiskManagerV3 monetary sizing;
- exchange affordability;
- Binance CROSS portfolio stress;
- exposure/protection safety.

Performance HWM behavior changes separately:

1. Binance positions are read before a durable new performance high is accepted.
2. If an active exchange position is not BGX-owned, the durable HWM is frozen.
3. New entries remain blocked by the external-position guard and the new
   performance-attribution quarantine.
4. When the external position disappears, the quarantine remains durable until
   the performance episode is reconciled.
5. An accounting/position read failure freezes the HWM and blocks new entries.
6. `LIVE_RISK_OVERRIDE_APPROVED` does not bypass an unresolved performance
   attribution quarantine.
7. The historical ATOM incident is repaired automatically only if all pinned
   predicates match: account flat, exact contaminated HWM/current equity,
   stable double-read Binance income evidence, only ATOM performance rows in the
   incident window, exact per-type counts/totals, manual ownership proven from
   userTrades/allOrders + durable registry, and exact net external result.

Any mismatch fails closed and leaves the existing HWM unchanged.

## Non-goals

This change does not:

- raise or lower `MAX_DRAWDOWN`;
- change leverage;
- change `MAX_RISK_PCT`;
- change NEXUS score/R:R/EV rules;
- weaken external-position ownership;
- weaken Binance CROSS stress;
- mutate or close an external position;
- infer external PnL from balance differences alone.

Future external/manual position episodes that are not the pinned ATOM incident
remain quarantined after closure until a separately reviewed reconciliation
path proves their full attribution.
