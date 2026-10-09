# #601 — Separate-session, REDACTED, strictly read-only HWM and cash-flow evidence review

The current canonical [ADJUSTED_EQUITY] runtime line at 2026-10-09 ~01:03 UTC shows status RECONCILED, equity 5.3177 USDT, durable performance HWM 22.7987 USDT, drawdown 76.68% against configured limit 30%, applied reconciliation records 2, applied net +7.7808 USDT, no pending flow records. Runtime [DRAWDOWN_HARD_GATE_INSTANCE] remains BLOCK_NEW_ENTRIES. These observations alone do not certify the exchange transaction IDs, individual flow positions or HWM revision history.

## Architecture review findings

1. Canonical bot.cash_flow_ledger classifies Binance /fapi/v1/income TRANSFER-like rows as external cash flows; realized PnL, commissions and funding are performance. It double-reads income and checks wallet against income baseline, records durable flow identity and applies a TWR performance HWM rebase with atomic CAS of ledger, cursor, HWM and latest provenance. Do not interpret raw account deposits as positive trading performance.
2. bot.drawdown_persistence.restore_update_real_account_peak restores the durable HWM and only increases it for a legitimate account high, with evidence-bound known-corruption repair exceptions and atomic provenance. No new local risk epoch authorizes resetting historical drawdown.
3. Important diagnostic side effect in existing operator command: python -m bot.cash_flow_admin show calls bot.database.init(), whose existing initialization attempts CREATE TABLE/ALTER/CREATE INDEX. The command's output additionally contains raw ledger records and exchange transaction identifiers. It should not be used as a supposedly strict no-mutation/redacted audit export.
4. Present connector exposes production logs/metadata but NOT command execution or read-only SQL. No raw source snapshot, complete HWM revision history or exchange income double-read was retrieved through this interaction, and we must NOT assert it was.

## Isolated safe artifact

New standalone bot.hwm_cashflow_readonly_audit_v1 uses a separate asyncpg session, server default_transaction_read_only=on, statement_timeout=700ms, one parameterized SELECT from key_value only, no app database.init(), no writes, no Binance API calls. Output aggregates and tests structural TWR arithmetic, ID duplication, pending/applied overlap, HWM latest provenance equality, cursor and baseline presence — without exposing raw IDs, raw provenance references, individual flow records, SQL values, credentials or ledger raw JSON. It **cannot verify the full audit history or Binance source**; its best status is STRUCTURAL_CHECKS_PASSED_INDEPENDENT_EXCHANGE_AUDIT_PENDING and authority remains NO LIVE.

Command for a separately authorized existing Railway private session ONLY (the connected Railway tools cannot execute this):

python -m bot.hwm_cashflow_readonly_audit_v1

No operator action or run executed here; do not copy DATABASE_URL or raw files into chat. Never create a new PostgreSQL public proxy or migrate volume. Its source needs code review, CI, and a separate deployment decision.

## Release readiness

- G1 remains BLOCKED at 76.68% above 30%. We cannot correct by rewriting historical HWM, reinterpreting external money, changing risk configuration or by depositing money.
- To actually attest HWM provenance, inspect authorized redacted single-session output, certified income entries from the venue, complete chain of atomic CAS ledger events (not just latest provenance), and compare before/after equity at each flow. Independently verify manual-attested vs automatically reconstructed reconciliation and wallet invariant. If evidence is missing, remain NO-GO.
- G3 still blocked independently by old frozen OOS EVIDENCE_FAIL. New #609 is separate future research only. G4 CI and private-account/bootstrap readiness do not waive either gate.
