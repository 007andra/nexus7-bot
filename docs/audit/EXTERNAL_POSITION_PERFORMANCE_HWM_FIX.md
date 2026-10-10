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

## PR #430 one-shot blocker

Reviewed HEAD: `81152bc2d100f94aa9b57e7c956660dacf793419`.
The repair could execute twice when HWM/equity revisited the pinned values.
The restart/revisit regression fails on that HEAD with `True is not false`:
the original implementation performs both rebases using the same ledger.

The four requirements are now enforced:

1. **Durable consumption:** a dedicated `ATOMUSDT_20260927:v1` marker, scoped
   to the existing HWM namespace, records `CONSUMED` and the incident window.
   It survives restarts and subsequent replacement of the latest HWM provenance.
   A consumed incident skips the repair before reading historical ledger data.
2. **Atomic persistence:** HWM, provenance and marker are written in one existing
   database CAS transaction. The guards require the original pre-evidence HWM
   and an absent marker. The existing HWM row is locked before checking the
   marker on PostgreSQL; SQLite uses `BEGIN IMMEDIATE`. Memory is updated only
   after a confirmed commit. No schema migration or separate marker write exists.
3. **Fail closed:** only an absent marker authorizes first-use evaluation.
   Invalid, partial or unknown marker values and strict storage-read failures
   raise `PersistenceError`. A changed HWM or a marker appearing during evidence
   collection causes CAS failure, including the same-HWM/consumed-marker case.
   A lost commit acknowledgement propagates failure; a retry observes consumption.
4. **Regression proof:** nine added tests cover restart plus historical HWM
   revisit, the three committed records, injected final-write rollback and retry,
   ambiguous marker values, marker-read failure, lost commit acknowledgement,
   concurrent repair attempts, same-HWM marker conflict, and HWM drift during
   evidence collection. They use fake Binance evidence and a real SQLite database.

Validation: all 16 external-performance tests and the 62-test targeted pack
(external performance, drawdown persistence, HWM provenance, CROSS dispatch)
pass. Ruff critical checks, pyflakes on the changed Python files, compileall and
selfcheck (zero critical findings) pass. The release proof reports PASS.
The offline suite also passes. Its existing runner overcounts skipped PostgreSQL
tests as passes: two PostgreSQL-only tests in the full suite and one in the
release pack require `TEST_POSTGRES_DSN`, unavailable locally. Exact-head CI
with PostgreSQL remains the deployment gate; local results do not prove that
backend. No trading/risk thresholds, rebase formula or exchange behavior changed.

The marker relies on the same durable namespace/database authority as the HWM.
Restoring a database snapshot from before consumption restores the old state;
this change does not provide replay protection across destructive storage rollback.
