# NEXUS-7 HWM — future-only transition journal (V1)

Status: DRAFT / isolated PostgreSQL implementation and tests; **NOT RELEASED**.

## Provenance scope

- Operator's 2026-10-09 read-only evidence: HWM 22.79869386 USDT and latest provenance match in the legacy namespace; 2 reconcilations and 5 transfer events totaling +7.7808 USDT were matched against authenticated Binance USD-M income source records.
- Railway PostgreSQL Backups tab showed **No Backups**. Dated logs from Oct 2–9 repeatedly observed peak approximately 22.7987. The complete pre-October and pre-September history remains **UNVERIFIED**, not eligible for backfill.
- A new append-only anchor cannot establish the actual historical HWM sequence or make prior loss disappear.

## Code design — dormant in production

New module: bot/hwm_transition_journal_v1.py. Import is inert. Schema DDL is a constant only; **no production schema installation, migration, runtime writer hook, deploy or trading authorization** in this PR.

A prospective ANCHOR observes the already-persisted HWM and latest provenance, explicitly stores unknown account equity as null, and marks all history before the anchor UNVERIFIED. Physical peak key is hashed into a scope SHA256; never publish raw scoped keys, account fingerprints, exchange transaction identities or evidence refs.

Each prospective TRANSITION carries UUID v4 identity, monotonic per-scope sequence, old/new peak, account equity, allowed reason, SHA256 commitment of evidence_ref, previous event digest and canonical payload digest. Database time is captured on insertion. History-complete is hard-coded false. An externally timestamped, independently retained chain-head commitment is needed for stronger anti-tamper assurance; NOT implemented here.

A PostgreSQL table trigger blocks UPDATE, DELETE and TRUNCATE in ordinary operation. **Not true WORM storage**: a privileged DB owner/admin can disable triggers or rewrite the entire chain, and a hash chain stored in the same database is not an independently immutable proof.

The future write primitive requires a caller-owned PostgreSQL transaction, the same HWM advisory lock used by CAS, exact current HWM and provenance raw values, and an already recorded anchor. Within the same outer transaction it inserts the journal row and updates HWM+provenance. On stale CAS or any other failure, the transaction rolls back with no partial durable state. Merely calling the primitive in isolation is not approval to transition the account: provenance and current-equity authenticity remain responsibilities of the existing guarded callers.

## IMPORTANT: production integration NOT in this PR

Three active HWM write pathways must be reviewed and integrated **together** in a separate authorized change, otherwise the prospective journal would have gaps:
1. drawdown_persistence._write_peak_with_provenance (new highs / repairs / older flow route, atomic multi-key write).
2. drawdown_persistence.rebase_real_account_peak_for_external_performance (CAS plus incident repair marker).
3. cash_flow_ledger.commit_record (CAS plus flow ledger and transfer cursor).

No after-the-fact journal writes after HWM commit: crashes between two transactions would make the record incomplete. Preserve the original ledger/cursor/repair-marker CAS invariants, existing legacy/current namespace semantics, fencing, lock ordering and fail-closed risk behavior. Never silently activate an anchor against the wrong physical scope. Do not do synthetic migrations of historical values.

## Mandatory tests

Test file: tests/test_hwm_transition_journal_v1.py

- Pure tests: invalid/nonfinite values, fabricated historic completeness, wrong digest / scope / seq, redaction, transaction requirement.
- Disposable real PostgreSQL service only (TEST_POSTGRES_DSN must point to localhost:5432/nexus_release_proof): anchor, atomic transition, caller rollback, stale writer, duplicate event, malformed provenance, immutable trigger behavior and concurrent one-winner CAS. Never connect a test to Railway production.
- CI must *explicitly run* the real-PG suite without allowing an empty/skipped test suite to appear green; do not drop any existing tests.

## Release decision

This draft requires exact-SHA Quality and Security PASS, independent review, approved migration/backup plan, archived source evidence for first anchor, full guarded integration of all HWM writers, production failure-injection/restart proofs without altering actual financial history, external chain-head attestation and **separate explicit approval before merge/deploy**.

LIVE remains blocked: historical DD around 76.68% exceeds the 30% limit and strategy OOS evidence has not demonstrated positive executable net edge. No manipulation of HWM, risk gates, orders or account is permissible.
