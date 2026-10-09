# Exact prospective OOS cohort evidence export (research-only)

Use the same persisted frozen cohort that produced `PROSPECTIVE_OOS_COHORT_V1` and `PROSPECTIVE_OOS_MATURATION_REVIEW_V1` in the Railway runtime. **Do not** join unrelated logs by approximate symbol/time or mix V2 #579 / exit-horizon #592 with the general `CALIBRATION_GENERALIZATION_V1` cohort.

## How to run (authorized read-only PostgreSQL account)

From the repository root in an operator-controlled environment that can privately access the PostgreSQL service:

```sh
python -m unittest -v tests.test_pg_matched_cohort_export
python -m research.oos_rca_v1.pg_matched_cohort_export \
  --as-of 2026-10-08T17:00:00Z \
  --out-prefix ./private_oos_evidence/exact_cohort
```

The script reads `DATABASE_URL` from its environment. Never print, paste, commit or upload database credentials. Use the least-privileged, **SELECT-only** PostgreSQL role where available; additionally, the code always opens a PostgreSQL `REPEATABLE READ, READ ONLY` transaction. It does not use `bot.database.init()`, does not alter tables or call exchanges, and emits only whitelisted candidate, horizon, decision and hypothetical market-outcome fields. A frozen `--as-of` timestamp is required for reproducibility. The SQL has hard result limits and fails closed rather than silently truncating.

Generated:
- `exact_cohort.csv`: one row per candidate/horizon; exact `candidate_id`, fixed cohort cutoff, natural counterfactual approved/rejected state, verified `OBSERVED` 60m/240m, or explicit missing/invalid reason
- `exact_cohort.json`: descriptive group counts and gross-return averages for the same cohort, integrity exclusions and deterministic SHA-256 of all included rows

**Only a verified observation with matching ID/horizon, complete finite MFE/MAE/return, correct 15m start/maturity and `return_basis=hypothetical_entry_gross` counts.** `UNKNOWN_CACHE_GAP`, non-matured and absent data remain missing. `ERROR` candidates are indeterminate, not rejected. General OOS approvals are not canonical V2 LIVE approvals.

## What this cannot prove

The export does not establish realized PnL, execution fill prices, spread/taker/slippage/funding round-trip costs or causal effect of the approval algorithm. The original studies have distinct horizon sample sizes, time dependence, potential censoring and symbol concentration. An export can validate lineage and missingness; it cannot convert the current `EVIDENCE_FAIL` into a pass.

Production credentials are not accessible to the connected Railway OAuth application, so this script has **not yet been executed against the production PostgreSQL database by ChatGPT**. No CI success claim should be made until current PR SHA finishes GitHub workflow checks. Review first and keep PR Draft; no merge/deploy/LIVE arming or risk reset is authorized.

Potential separate fix: `UNKNOWN_CACHE_GAP` may be persisted permanently by the normal `observe_outcomes` path; investigate retry policy in an independent, prospectively specified PR. Never rewrite frozen data or silently replace outcomes.
