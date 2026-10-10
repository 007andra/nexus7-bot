# NEXUS-7 — Integrated PostgreSQL / Binance readiness evidence

Status: **READ-ONLY AUDIT PLAN — NOT A LIVE RELEASE**. Base production SHA: `1af6761b1898dcc968d991a5081c891f34993906`.

## A. Durable PostgreSQL / CAS acceptance

- Reuse existing P0 PostgreSQL integration suite against an **ephemeral isolated database**, not production.
- Test concurrent CAS with the same expected version: exactly one writer commits, loser raises `CompareAndSwapConflict`; no partial key updates.
- Test cancellation at transaction begin, guarded read, and write; verify rollback and that subsequent independent-session CAS works.
- Test stale fencing token / expired ownership: no new-risk authority; do not rewrite HWM, cashflow ledger, drawdown or risk settings.
- Verify runtime evidence on exact deployed SHA: engine RUNNING, heartbeat RUNNING, lease renewed, fencing valid, no fresh `SAVEPOINT` / CAS persistence errors for a continuous observation window. Historic logs are not new failures.
- Record integration job URL, actual PostgreSQL version, pass/fail, number of assertions and timestamp. CI green alone is insufficient if mandatory real-Postgres tests were skipped.

## B. Binance reconciliation — signed READ-ONLY

- Use only an authorized runtime integration with **read-only calls**; never log API keys, signatures, raw account identifiers or full order payloads.
- Compare timestamped Binance USDM wallet/available balances and unrealized PnL against internal equity provenance.
- Query all positions, open orders, conditional stop/TP orders and recent fills/income (including fees/funding), by symbol and exact IDs. Confirm protective coverage for any existing position.
- Reconcile external deposits/withdrawals against adjusted equity, `performance_hwm` and historical drawdown without resetting or rebaselining historical losses.
- Publish redacted counts, aggregates, discrepancy types and source timestamps. On mismatch: `RECONCILIATION_FAIL`, do not open new risk.
- A successful `/fapi/v3/balance` authentication probe is **not** reconciliation.

## C. Independent economic proof — issue #609

- Preserve GitHub issue creation time as immutable enrollment cutoff.
- Freeze first 60 natural eligible approvals, <=12 per symbol, >=8 symbols; 60 paired 60m and 240m outcomes; no replacement of losing/censored candidates.
- Authenticate immutable candidate IDs, capture-time BBO, tick/step/min notional, bars and cost inputs, fee/slippage/funding.
- Run stop-first protected execution, base/stressed net cost, clustered 95% lower bound and leave-one-symbol-out per horizon.
- If authenticated source or 60-member sample missing, report `NET_PROOF_MISSING` / `INSUFFICIENT_EVIDENCE`, not PASS.
- PR #630's synthetic evaluator is not independent authenticated exchange evidence.

## D. Release board — strict NO-GO until proved

- Current last observed: adjusted equity ~5.3177 USDT; historical HWM ~22.7987; drawdown ~76.68%, configured maximum 30%; `entries_blocked=true`.
- No LIVE permission from this document. Require independently verified capital, explicit absolute loss budget, segregated execution path, operational stability, successful reconciliation and valid economic evidence.
- No merge, deploy, restart, order placement, gate bypass, risk limit changes, ledger edits or new storage charges under this plan.
- Final evidence must name exact deployment SHA, timestamped sources, unresolved discrepancies and independent reviewer sign-off.

## Evidence register (initial)

| Check | Initial finding | State |
|---|---|---|
| Production engine / ownership | Running with renewed fencing lease in 2026-10-10 13:19–13:33 UTC logs | SHORT-WINDOW PASS |
| New CAS/SAVEPOINT errors | None in current deployment queried | LOG-ONLY PASS |
| Real PostgreSQL concurrency / cancellation | Not independently rerun for this audit | PENDING |
| Binance signed balance probe | HTTP 200 at 2026-10-10 13:19 UTC | AUTH ONLY |
| Binance account / positions / orders reconciliation | No independent signed multi-endpoint proof | PENDING |
| Independent net OOS #609 | Issue open; 60 authenticated paired outcomes not proved | FAIL / INSUFFICIENT |
| LIVE release | Historical drawdown hard gate active | BLOCKED |
