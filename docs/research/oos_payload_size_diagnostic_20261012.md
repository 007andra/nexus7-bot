# OOS V1: passive payload-size attribution (NO LIVE)

## 12 October 2026 evidence boundary

NEXUS-7 Railway production app `ams`, PostgreSQL `sfo`, as verified by authenticated service configuration. The frozen prospective OOS cohort snapshot reads existing candidate and outcome JSON rows from PostgreSQL through a separate, read-only connection when enabled. Production logs before this draft instrumentation showed ~6,613 fetched rows per cycle, read wall-clock times near 2.8 seconds, and zero shared application DB lock wait for that dedicated snapshot. These observations do not identify SQL execution latency versus cross-region network latency versus client deserialization.

## What this change does

Inside the existing candidate and outcome JSON parse loops, sum the UTF-8 length of raw, *already fetched* JSON text, separately by cohort stage. Emit:
- candidate_payload_utf8_bytes, outcome_payload_utf8_bytes
- candidate_payload_max_utf8_bytes, outcome_payload_max_utf8_bytes
- candidate_payload_measured_rows, outcome_payload_measured_rows
- payload_size_scope=CLIENT_JSON_UTF8_ONLY_NOT_WIRE_OR_PG_EXECUTION

No new SELECTs, indexes, tables, stores, dependencies, thread/async tasks, wire response contents, DB/HTTP credentials, transaction IDs, candidate IDs, trading symbols, account IDs, or private JSON are emitted or persisted. The previously frozen cohort membership and OOS math remain unchanged. No risk gates, orders, dispatch, threshold, timeout or LIVE authority are affected. Results explicitly maintain promotion_allowed=false and live_allowed=false.

The byte sum is **NOT** measured wire traffic (Postgres protocol overhead, TLS, framing), nor server-side execution time, and cannot alone prove network causality. Report only a candidate for optimization after an independently reviewed query plan and a bounded read-only PG observation. Never reduce OOS samples or rewrite frozen OOS populations to improve latency. If this flag is promoted to runtime through a separately approved change, compare same SHA / row-count bands and revoke instrumentation if material overhead is observed.

## Why not merge with PR #656?

PR #656 is a separate, already reviewed 3-second total deadline coverage patch and remains unmerged due to a security-blocked merge action. This draft PR is passive size observability, with no timeout changes; it must be reviewed and validated independently. Do not use a new branch/PR as a workaround around the prior blocked merge.

## Release requirements

1. GitHub Quality Check and Supply Chain Security completed SUCCESS on exact head commit.
2. Independent review of limited files; no extra SQL/runtime effects.
3. Separate explicit approval before any production merge/deployment.
4. Verify post-deploy tags, redaction, zero trading effects and same frozen OOS outcomes.
