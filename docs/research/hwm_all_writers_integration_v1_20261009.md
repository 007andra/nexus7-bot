# NEXUS-7 — all-writer HWM journal integration / release gate V1

**Status: DRAFT, disabled by default, no production schema, no rollout.**
Tracking #620, foundations #622, legacy reader #621.

## Exact causal scope

Three known code-level HWM write routes now provide journal intent metadata
(reason, previous verified HWM, authenticated equity, provenance evidence ref):

1. drawdown_persistence._write_peak_with_provenance (normal equity high,
   incident repair, older explicit capital flow).
2. drawdown_persistence.rebase_real_account_peak_for_external_performance
   (rebase with consumed incident marker, CAS).
3. cash_flow_ledger.commit_record (atomic cash-flow ledger + cursor +
   HWM/provenance, CAS, exactly five existing independently matched flows).

Both shared writer functions in atomic_key_value accept optional intent,
ignored when feature is off. If `HWM_JOURNAL_INTEGRATION_V1=true` and the
write includes canonical HWM/provenance, both central writers instead call
`hwm_journal_guard_v1.write_with_journal` under the existing global I/O
lock, using a **fresh asyncpg transaction, sorted CAS advisory locks, exact
old HWM/provenance validation, pre-existing scoped journal anchor, one
TRANSITION, and all remaining marker/ledger/cursor writes in one commit**.

The existing provenance payload is conserved exactly (including
recorded_at); the module validates old/new HWM, positive finite equity,
allowed reason and same evidence ref. On any mismatch, missing companion
provenance, schema/anchor missing, stale CAS, invalid evidence or partial
write, it fails CLOSED, regardless of the old caller's strict=False.

## CRITICAL hard stop before activation

Production today has the physical HWM and provenance only in **LEGACY**
namespace. The new guard refuses any observed legacy physical key when
enabled, and refuses an absent current HWM, so it **cannot be turned on
against current production**. This is deliberate and safe.

This branch does **NOT** implement the supervised legacy->canonical key
migration or establish an anchor in production, install the SQL schema,
enable the flag, backfill any missing historical transitions, modify the
historical HWM/drawdown, or enable LIVE. Never manually set the flag to true
as a workaround.

Old peak numerical comparison must remain equivalent to the original
verified value. Future formal migration must test same-process and
cross-process writers, crash/restart, and every direct peak key write path.
Any writer outside the two central atomic APIs still requires a separate
audit/instrumentation before any claim of complete prospective coverage.

## Test proof, isolated and repeatable

New `tests/test_hwm_all_writers_journal_integration_v1.py` runs only if
`TEST_POSTGRES_DSN` points to disposable localhost PostgreSQL on
`/nexus_release_proof`. Synthetic HWM keys and exchange-less evidence;
no Railway account or Binance request.

- Non-CAS HWM path: one validated journal record.
- CAS reconciliation: HWM/provenance, external flow ledger/cursor one tx.
- Missing anchor and wrong provenance: no HWM, marker or ledger writes.
- Stale CAS conflict: no partial durable state.
- Existing legacy physical key: activation rejected even if current key
  and anchor are present.
- Missing paired provenance or missing journal intent: fails closed.
- Parallel CAS writers: one winner, one stale loser, one event.
- Feature explicitly disabled: existing atomic behavior unchanged.

Quality workflow MUST run new tests with local PostgreSQL in addition to
existing CAS and prospective journal real-PG gates. All existing security,
execution and release checks stay in place. No test skip can be counted
as proof.

## Remaining before deployment (none are authorized)

- Exact-SHA Quality and Supply Chain Security PASS and independent review.
- Audit any direct HWM writer beyond the three known canonical call paths.
- Authorized, backed-up, reversible production physical-key migration
  and forensic current-state anchor in a separately approved plan.
- Independently anchored, timestamped journal head and backup/PITR
  policy with permissions and cost approval.
- Shadow / canary proof all HWM writes are covered and crash-safe.
- Operator authorization for any merge, DDL, feature enablement or deploy.

The chain is not independently WORM: a privileged PostgreSQL owner can
rewrite it; missing September backups/historical revisions remain
UNVERIFIABLE. The current HWM is approximately 22.79869386 USDT,
drawdown approximately 76.68% vs 30% policy, strategy OOS EVIDENCE_FAIL.
**LIVE remains blocked and this draft changes no execution authority.**

## GitHub Actions trigger detail (PR #623)

The repository's current Quality and Supply Chain workflows run on pull
requests targeting `main` or `migration/binance-usdm` only. PR #623 has
been retargeted from the stacked draft #622 branch to
`migration/binance-usdm` **solely to execute exact-head CI**. The diff now
includes the unmerged foundation (#622) and this integration proof; no
permission to merge, deploy or enable the journal follows from retargeting.
