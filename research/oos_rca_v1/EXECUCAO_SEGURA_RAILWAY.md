# OOS frozen cohort — authorized PostgreSQL READ ONLY export

This guide fixes the missing-ZIP handoff: all code sources are now in Draft PR #595,
available to any authorized GitHub session without needing a chat attachment.
No database credentials or private CSVs are in the repository.

Project: BGX CAPITAL. Environment: production. Database service: Postgres.
Frozen cohort: CALIBRATION_GENERALIZATION_V1.

## Authorization boundary

The connected Railway OAuth API cannot execute SQL or return connection secrets.
Do not create a public TCP proxy, deploy a service/function, paste tokens into
chat/GitHub, change infrastructure or elevate DB privileges for this audit.

An already-authorized operator with a workstation, Railway CLI, psql and preferably
a SELECT-only database account can perform the extraction locally:

~~~sh
mkdir -p private_oos_evidence
railway login
railway link                     # choose BGX CAPITAL, production
railway connect Postgres --environment production
~~~

From the psql prompt run:

~~~text
\i research/oos_rca_v1/EXPORT_OOS_POSTGRES_PSQL_COPY.sql
\q
~~~

The script opens a REPEATABLE READ READ ONLY transaction, reads the frozen cohort
with SELECT, exports to the client computer with psql \copy ... TO, then ROLLBACK.
It never modifies database rows or schema.

Then run from the repository root:

~~~sh
python -m research.oos_rca_v1.verify_export \
  private_oos_evidence/nexus7_oos_coorte.csv \
  --output private_oos_evidence/nexus7_oos_manifesto.json

python -m unittest -v tests.test_oos_postgres_csv_integrity
~~~

Output CSV remains private in the operator workspace. Do NOT upload source rows,
database credentials, account information or CSV to public GitHub issues/Actions.
Use EXPORT_OOS_POSTGRES_READ_ONLY_SELECT.sql instead for a SQL editor/GUI that
does not implement the psql \copy meta-command.

## Checks and limits

- Validate frozen cohort ID, exact candidate ID, decision, 60m and 240m rows,
  snapshot time and missingness before treating the file as audit evidence.
- The indexed SQL column captured_epoch is REAL (rounded); query filters against
  the more precise candidate JSON captured_epoch.
- Compare the manifest to Railway PROSPECTIVE_OOS_MATURATION_REVIEW_V1 at a
  **matching snapshot time**. Counters change; old totals are not a timeless target.
- Original outcome records may omit return_basis; flag this rather than inventing
  proof that the mark was realized.
- Data are hypothetical gross marks, not Binance fills or cost-adjusted PnL.
- Do not merge/deploy/arm LIVE/change drawdown/HWM from this evidence.
- If the database is inaccessible, STOP: do not claim the SQL ran or a CSV exists.

## GitHub paths on research/oos-rca-report-v1-20261008

- research/oos_rca_v1/EXPORT_OOS_POSTGRES_PSQL_COPY.sql
- research/oos_rca_v1/EXPORT_OOS_POSTGRES_READ_ONLY_SELECT.sql
- research/oos_rca_v1/verify_export.py
- tests/test_oos_postgres_csv_integrity.py

Railway CLI docs: https://docs.railway.com/cli/connect
