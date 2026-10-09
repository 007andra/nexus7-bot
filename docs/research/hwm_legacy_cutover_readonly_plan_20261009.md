# NEXUS-7 — LEGACY HWM cutover: READ-ONLY migration preflight

Tracking issue #620. This is a **migration proposal, not authorization** to
alter Railway, HWM, the bot's risk controls, exchange balance or operations.

## Evidence scope (2026-10-09)

* Production branch migration/binance-usdm at 09febee1f14709933d1658f928f3ac0dafa8a68a;
  bot and PostgreSQL 1/1 online, no criticals/warnings. Railway shows one
  existing staged EnvironmentPatch with empty exposed changes: not investigated
  and not applied. No assumption of zero pending work.
* HWM 22.79869386 USDT and latest provenance are in the legacy physical
  namespace according to earlier operator-run read-only PostgreSQL evidence;
  canonical counterpart was absent.
* Five source-recorded exchange transfer events (two reconciliations,
  total +7.7808 USDT) matched a double-read authenticated Binance USD-M
  income API window. This does not prove all prior capital flows/HWM peaks.
* Railway PostgreSQL Backups tab explicitly showed **No Backups** and
  account was labeled Trial; upgrade to Pro was advertised for creating
  backups and PITR. Neither upgrades nor backups were authorized.
* Future-only DRAFT PRs #621, #622, #623 are unmerged/unreleased.
* Historical drawdown about 76.68% exceeds 30% policy and strategy OOS
  EVIDENCE_FAIL persists. LIVE remains NO-GO regardless of migration.

## Read-only diagnostic

The operator-only module bot.hwm_legacy_migration_preflight_ro_v1
performs ONLY one parameterized SELECT on up to six physical state keys
(current HWM/provenance, legacy HWM/provenance, ancient v1 pair). Keys are
derived from hwm_namespace and financial_namespace.legacy_key_for and are
never printed. It uses a new separate asyncpg connection with
default_transaction_read_only=on, statement_timeout=700ms, connection
timeout=5s and 1s SELECT limit; never runs database.init or writes SQL.
All exception messages are redacted. Import is inert and there are no
engine/startup callers.

- CONSISTENT_LEGACY_MANUAL_MIGRATION_REQUIRED: coherent legacy pair alone.
- CANONICAL_PAIR_PRESENT_REVIEW_ANCHOR_REQUIRED: coherent canonical alone.
- MIGRATION_PREFLIGHT_BLOCKED: ambiguous/dual scopes, incomplete/malformed
  values, v1 state, mapping collision or unconfigured account binding.
- UNAVAILABLE_OR_FAILED_CLOSED: connection/timeout failure.

No status grants approval: backup_available_verified=false,
anchor_created=false, historic_provenance_complete=false,
migration_authorized=false and live_allowed=false always.

## Production cutover gates — not yet met

1. Independently verify physical key mapping for actual Binance account and
   PostgreSQL authority, source/target HWM+provenance, current exchange
   account equity and sampled source income event evidence. On any mismatch,
   stop. Do not reset existing peak or shadow the loss history.
2. Produce a restorable **encrypted offsite/volume backup** with separate
   operator cost/security approval. Actually restore and cross-check a copy
   in an isolated environment. No current backups exist on the inspected
   Railway volume, so **NO PRODUCTION MIGRATION** until this step is done.
3. Review the staged Railway EnvironmentPatch (currently pending), exact
   commit/deployment parity, fenced ownership and concurrency. Obtain a
   maintenance window with order dispatch disarmed and no unprotected
   positions. Verify recovery/restore checkpoints.
4. Review **all four known HWM writer paths**:
   drawdown_persistence._write_peak_with_provenance;
   rebase_real_account_peak_for_external_performance;
   cash_flow_ledger.commit_record;
   operational_incident_recovery.maybe_rebase (the direct unpaired legacy
   write, currently only quarantined by draft #623 when future flag true).
   Search raw SQL and every other direct key writer for bypasses.
5. Design and prove a safe **physical namespace bridge**. Draft #623
   intentionally forbids enabling HWM_JOURNAL_INTEGRATION_V1 if LEGACY
   rows still exist, or if canonical physical pair/verified anchor is
   absent. Do not work around this by deleting or editing LEGACY keys.
   Any migration must preserve exact peak and provenance under sorted
   advisory locks, with CAS, exclusive old/new writer fencing, recoverable
   historic source and verified new future-only anchor. Detailed
   reversible schema/bridge migration code is NOT part of this PR.
6. Anchor only the present observation. September HWM changes are not
   reconstructable from a new hash chain; set
   history_before_anchor_verified=false permanently for this segment.
   No artificial financial event, peak rebasing or risk epoch reset.
7. Provide independently retained timestamped digest commitments and
   properly permissioned backup/PITR. DB-only hash chaining and triggers
   can be bypassed by a privileged owner; they are not true WORM.
8. Before any release, test legacy-only/canonical-only/dual/malformed
   sources, cancellations, concurrent writers, exact ledger/cursor/marker
   rollback, direct bypass, failover/restart, source snapshot restore and
   independent account evidence. Require exact SHA green Quality+Security
   AND independent code/DB/security review.
9. Get explicit operator approval separately for any Railway plan change,
   backup procedure, SQL/DDL, production merge/deploy, physical migration,
   journal anchor and feature toggle. No such approval was requested here.

## Current decision

NO-GO for production migration: missing backups, no tested isolated
restore, staged environment patch, and direct legacy writer needing a
reviewed retirement/bridge. NO-GO for LIVE orders independently due to
historical DD and unsuccessful frozen OOS evidence.
