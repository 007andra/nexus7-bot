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

## Redundant-ledger integrity hardening (2026-10-09)

The draft now also fails closed on inconsistent positive/negative flow totals
(`gross_in - gross_out != net_amount`), wrong `direction`, duplicated or
miscounted `tran_ids`, inconsistent recorded TWR
`trading_drawdown_after`, and malformed/duplicated pending flows.
These are **internal structural checks** of fields already produced by
`cash_flow_ledger.build_record`, not a Binance ledger signature.
Five additional tamper/legitimate-drawdown regression tests accompany the code.

This isolated draft was synchronized with production base
`033de6cc0e33df1ba1309956a3bad41644bd4c90` while preserving only its
three dedicated audit files. Its checks must run again against the *new*
commit SHA before merge eligibility; passing checks on an earlier head
is insufficient. The actual read-only production diagnostic is still
**NOT EXECUTED**, and its best outcome cannot lift the drawdown hard gate.

## Release readiness

- G1 remains BLOCKED at 76.68% above 30%. We cannot correct by rewriting historical HWM, reinterpreting external money, changing risk configuration or by depositing money.
- To actually attest HWM provenance, inspect authorized redacted single-session output, certified income entries from the venue, complete chain of atomic CAS ledger events (not just latest provenance), and compare before/after equity at each flow. Independently verify manual-attested vs automatically reconstructed reconciliation and wallet invariant. If evidence is missing, remain NO-GO.
- G3 still blocked independently by old frozen OOS EVIDENCE_FAIL. New #609 is separate future research only. G4 CI and private-account/bootstrap readiness do not waive either gate.


## 2026-10-09 operator evidence — diagnosed legacy-only HWM keys (#620)

An operator connected to the existing production `nexus7-bot` container through
the Railway browser Console, without publishing connection strings, account
fingerprints or raw transactions. The original standalone auditor reported
`EVIDENCE_INCONSISTENT_OR_INCOMPLETE` with
`HWM_MISSING_OR_INVALID` and `PROVENANCE_MISSING_OR_INVALID`, but the
ledger aggregates were independently readable: two reconciliation records,
five transfer events, external net +7.7808 USDT, zero pending.

A separate **read-only, single-SELECT** existence check proved:

| Key source | HWM row non-null | Provenance row non-null |
| --- | --- | --- |
| Stable/current namespace | false | false |
| Runtime legacy namespace | true | true |

A second operator-invoked, read-only audit manually applied the existing
`financial_namespace.legacy_key_for` fallback and returned
`STRUCTURAL_CHECKS_PASSED_INDEPENDENT_EXCHANGE_AUDIT_PENDING` with durable
HWM **22.79869386 USDT**, both source labels `LEGACY`, provenance present,
two reconciliations, five transfers, +7.7808 USDT net, no pending records.
The only status blockers left were the *external* Binance /fapi/v1/income
verification and the absent full durable HWM revision history. There is no
evidence of a deleted historical HWM from this investigation.

### Dedicated reader follow-up (draft, no LIVE)

The standalone reader now optionally computes the same current/legacy HWM and
provenance namespace keys as the runtime, uses **one** separate parameterized
read-only SELECT with bounded timeouts, and selects the legacy value **only
when the current key is absent**, not when it is malformed. It emits only
constant `hwm_source` and `provenance_source` labels
(`CURRENT`/`LEGACY`/`MISSING`) plus existing aggregate fields; keys,
account fingerprints and raw financial records remain private.

A mixed HWM/provenance namespace is considered
`NAMESPACE_SOURCES_MIXED_MANUAL_REVIEW` and never a structural PASS, even
if the numeric peaks happen to match. Regression tests cover legacy-only,
canonical precedence, malformed current HWM, absent/invalid legacy provenance,
mixed namespace, redaction, and SELECT-only execution. Existing safety/NO LIVE
fields and 30% maximum historical drawdown remain unchanged.

This implementation is a code-only draft pending exact-SHA CI and independent
review. It does **not** migrate any keys, modify the database, perform an
exchange API call, reset historical HWM, change risk gates or certify that
Binance transactions agree with the ledger. A pass cannot justify LIVE.
