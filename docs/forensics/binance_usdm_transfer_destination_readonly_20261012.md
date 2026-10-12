# Binance USD-M transfer destination - forensic one-shot (#653)

This is an opt-in, isolated, READ ONLY script. It is NOT part of the LIVE trading engine.

Incident evidence: Binance USD-M income recorded a TRANSFER of -5.31768526 USDT on 2026-10-11T19:40Z, but the recipient wallet and initiator are UNKNOWN.

The tool collects a bounded, signed GET /fapi/v1/income window and four signed GET requests at https://api.binance.com/sapi/v1/asset/transfer for UMFUTURE_MAIN, UMFUTURE_FUNDING, UMFUTURE_MARGIN, and UMFUTURE_OPTION. No POST, order, transfer, database mutation, secret output, trading config change, HWM rewrite, or deployment action.

## Operator-only command, inside an ALREADY authorized private process

python -m bot.binance_transfer_destination_readonly_v1 --start-utc 2026-10-11T19:39:00Z --end-utc 2026-10-11T19:43:00Z

Do not create or change Binance API permissions, expose a database, add new credentials to GitHub Actions, or echo API keys. This command has NOT been executed: the currently connected Railway API has no remote shell.

## Status interpretation

- EXACT_ID_AND_AMOUNT_TIME: one confirmed SAPI history record matches the negative USD-M income by transfer ID, amount, and timestamp; reports the documented wallet category. Does NOT establish who initiated the transfer or whether assets remain credited there now.
- AMOUNT_TIME_ONLY: provisional candidate route, destination UNVERIFIED.
- AMBIGUOUS_MULTIPLE_MATCHES / UNMATCHED: inconclusive.
- INCOMPLETE_ROUTE_COVERAGE: any signed GET was unavailable, unauthorized, or incomplete. Do not infer absence.

No raw transaction IDs, account identifiers, incoming wallet addresses, credentials, free-form text or original responses are logged or printed.

Completion of this lookup does not authorize LIVE trading. Maintain hard drawdown gate, existing HWM, OOS FAIL evidence and separate manual release controls.

Source: https://developers.binance.com/en/docs/catalog/core-trading-wallet/api/rest-api/asset#query-user-universal-transfer-history

## Safe completion and authentication failures

The CLI prints a redacted JSON status. Exit code 0 is reserved for a UNIQUE
exact SAPI/FAPI transaction-ID, asset, amount, and time match in a complete
four-route query. All weaker observations (including a single amount/time
candidate), missing permissions, partial pages, unavailable income, or
network/API failure exit nonzero or fail closed, without traceback/raw API body.

The route identifies the Binance internal wallet *category*, not proof
that the amount currently remains credited there. A recent successful
transfer can subsequently have been spent, withdrawn, or moved again.
The operator must independently review receiving-wallet transactions,
current balance and whether the transfer was authorized.

Official schema checked 2026-10-12:
https://developers.binance.com/en/docs/catalog/core-trading-wallet/api/rest-api/asset#query-user-universal-transfer-history
