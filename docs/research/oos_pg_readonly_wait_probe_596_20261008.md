# OOS issue #596 — PostgreSQL server-side, read-only diagnostic

## What is currently proven

The production Railway deployment `728df62c-77bd-4dbd-af24-12d1d880e1bb` on SHA `60ea411b8fb2201f94f93a6d3178a9b6766b0554` is online 1/1; its first read-first OOS snapshot at 2026-10-08 23:41:12 UTC had `status=OK`, elapsed 707.875ms, metadata fetch 292.822ms, lock wait 0.110ms, and no DDL. This is one snapshot on the NEW deployment, not the 6–12 natural observations required for #606.

A previously verified read-first OOS snapshot at 23:38:30 UTC on the former deployment had metadata fetch 1825.878ms with lock wait 0.110ms and no DDL. Historical pre-fast-path metadata read 1412.027ms with 0.124ms lock wait at 23:16:53 UTC. These are CLIENT-SIDE `asyncpg.fetch` wall-clock times, not confirmed PostgreSQL execution times.

Railway resource metrics (1-hour aggregates) cannot attribute single statement latency. Railway distributed tracing is OFF and app exported 0 spans. Read-only Railway `network-flow` data is connection-level and cannot isolate one OOS SELECT. Railway connector in this environment does not expose SQL execution against the Postgres service. No direct database query was executed in this review.

## Diagnostic approach (standalone and opt-in, NOT enabled by merging)

`python -m bot.oos_pg_readonly_wait_probe_v1 --seconds 12 --interval-ms 750`

Run the command **inside an existing authorized, isolated process on the Railway private network**, with `DATABASE_URL` already provided through secure environment injection; do not copy credentials into the CLI or chat. The connected Railway tool **cannot execute** this command directly; no production operator command has been run by this PR.

The command establishes a NEW PostgreSQL session using server settings `default_transaction_read_only=on`, `statement_timeout=700ms`, and a bounded diagnostic `application_name`. It runs ONLY two kinds of SELECT:

1. A bounded `pg_stat_activity` snapshot, filtering on the known static OOS metadata SELECT prefix IN THE DATABASE and returning **aggregate counts only**: matching sessions, active ones, and recognized `IO`, `Lock`, `LWLock`, `Client`, no-wait-event, other-wait categories. Neither SQL text, usernames, row payloads, backend PID nor credentials are fetched into the client process. A snapshot is an instantaneous observation; no matches may mean the statement was not executing at that instant, poor timing, or visibility limitations. It is NOT proof of a client/network cause.
2. If `pg_stat_statements` already exists and is readable, two aggregate snapshots before and after the window: new matching statement calls and total backend `exec_time` delta. The optional extension is NEVER installed/created, and the output contains no query text or statement IDs. Its historical maximum is NOT window-specific. An unavailable extension is reported `UNAVAILABLE_OR_NOT_PERMITTED`, not an error requiring DDL.

Limits: 5–30 seconds duration, sampling interval 500–2000ms, maximum 60 aggregate activity SELECTs; each observation has 1-second async client timeout. The tool never issues `EXPLAIN ANALYZE`, DDL, DML, transactions that write, resets, extension creation, administrative commands, or any exchange API call. Errors fail closed and print only a generic code, not exception text that may contain connection credentials.

The command is not imported by the trading engine, does not schedule itself, and has no effect on `database._io_lock`, OOS membership/thresholds, the 3-second snapshot deadline, HWM, risk or Binance dispatch.

## Interpretation gate

A repeated metadata `asyncpg.fetch` outlier concurrent with active PostgreSQL SELECT and high IO/Lock/LWLock counts would support a server-side wait hypothesis, not establish its full duration. A statistically meaningful `pg_stat_statements` call/total-exec-time delta aligned to the actual OOS query window may help estimate server elapsed time, subject to observability and concurrent calls. **No active observation alone does not prove network delay**. Private-network flow telemetry and an event-loop scheduling signal may be needed to separate transport/asyncpg decode from client scheduling.

Review only with explicit operator authorization to run a diagnostic in production, and do not execute during startup/reconciliation. Never print/copy `DATABASE_URL`, raw `pg_stat_activity.query`, SQL binds, candidate IDs or live account balances. Do not change the database service, expose a new TCP proxy, restart PostgreSQL, install an extension, change the research cohort, increase the 3s OOS timeout or relax the LIVE Risk Gate.

## Acceptance

Dedicated tests must verify exact SELECT-only queries, window bound, optional extension permission fallback, connection cleanup, generic secret-free failures, no output of raw query, PID or credentials, and no trade authorization. PR CI and separate review precede any merge. This prepares future server-side evidence; it does not assert a root cause and must not trigger a deployment solely to gather one sample.