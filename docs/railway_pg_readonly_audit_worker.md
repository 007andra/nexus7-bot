# NEXUS-7 — PostgreSQL diagnostic role audit without terminal

**Preparation only.** This document does NOT authorize merge, deploy, creation
of Railway services, changing any production variable, or enabling OOS/LIVE.

## Isolation contract

Use a **separate, temporary Railway service** (e.g. \`pg-role-audit-once\`)
in the **same project and environment** as the private Postgres. Never modify
the existing \`nexus7-bot\` start/pre-deploy commands, its Dockerfile command,
or risk settings. Do not enable a public domain or TCP proxy for Postgres.

After this PR is merged **with explicit authorization**, configure the
temporary service as follows:

1. Use the exact approved repository commit as its source:
   \`007andra/nexus7-bot\`, branch \`migration/binance-usdm\`, pinned SHA.
   Use its existing Dockerfile, not the trading service as a service template.
2. Set the **start command** to:
   \`python -m scripts.railway_pg_readonly_audit_once\`
   Do not start uvicorn, the trading engine, a scanner, or a database migrator.
3. Configure only \`OOS_DIAGNOSTIC_DATABASE_URL\` as a Railway **private variable
   reference** to the existing diagnostic credential. Do not paste, print,
   rotate, copy to code, or create a new credential. Do not supply exchange
   API keys, \`DATABASE_URL\`, or any trading credentials to the worker.
4. Set restart policy to **NEVER**, one replica, no cron, no public domain,
   no healthcheck; review staged settings before allowing first deployment.
5. Collect only the \`[PG_ROLE_AUDIT_ONCE]\` and
   \`READ_ONLY_ROLE_VERIFIED=\` lines from this isolated service's deployment
   logs. The tool prints only item numbers (1–8), PASS/FAIL and a summary.
   It deliberately omits role names, relation names, DSNs and error messages.
6. After inspection, remove the **temporary worker service**, without
   altering the Postgres or \`nexus7-bot\` services. Maintain a sanitized
   result in the audit record.

The worker performs a single audit attempt per process. It exits **0** for
verified PASS, **1** for detected privilege failures, **2** for invalid DSN,
connection errors, incomplete evidence or other inconclusive conditions.
The Railway deployment may mark a one-shot exit as stopped even when the
script's own status is PASS: the authoritative evidence is the explicit log
summary and exit status, not the service remaining online.

The worker delegates privilege checks to
\`scripts/pg_readonly_role_audit.py\`, inside a PostgreSQL
\`READ ONLY\` transaction. Connection, transaction and query timeouts apply.
It does **not** grant/revoke roles, run DDL/DML, call SET ROLE, alter the
database, or issue Binance orders.

## Mandatory release/enablement gates

- **No automatic action on merge:** adding the script does nothing until the
  separate worker is explicitly created/configured/deployed.
- **Do not enable \`OOS_RESEARCH_READER_V2\`** until the real database
  privileges pass and the responsible review explicitly authorizes the next
  rollout step.
- **Do not change \`DRAWDOWN_HARD_GATE\` or any live execution permission** as
  part of the audit. Real balance/cashflow reconciliation and independent
  OOS evidence remain outstanding.
- On FAIL or INCONCLUSIVE, preserve \`READ_ONLY_ROLE_VERIFIED=false\` and
  investigate without relaxing DB permissions or publishing secrets.
- These checks do not certify that the bot is profitable or ready for LIVE.
