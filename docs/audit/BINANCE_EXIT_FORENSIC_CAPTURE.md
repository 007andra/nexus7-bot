# Binance Exit Forensic Capture

## Purpose

Capture exchange-authoritative evidence immediately after a BGX-owned Binance
USD-M position disappears from the live position set, without changing trading
behavior.

## Read-only sources

The collector queries:

- `GET /fapi/v1/userTrades`
- `GET /fapi/v1/allOrders`
- `GET /fapi/v1/allAlgoOrders`
- `GET /fapi/v1/income`
- `GET /fapi/v1/forceOrders`

It never submits, cancels, edits, retries, or replaces an order.

## Reconciliation contract

The opening `orderId` is taken from durable BGX lineage. Closing fills are
derived from opposite-side `userTrades` until the opening quantity is exactly
flattened.

Close cause is considered authoritative only when at least one of these identities
is proven:

1. the closing normal orderId equals a BGX algo `actualOrderId`;
2. the closing orderId appears in `forceOrders` as liquidation/ADL evidence; or
3. the normal closing order has a BGX clientOrderId.

A real closing fill with no such link remains `cause=UNATTRIBUTED`; the code does
not infer SL/TP, liquidation, ADL, or manual closure from price proximity.

Fill-based PnL can still be authoritative independently of cause when all opening
and closing commissions are USDT-denominated. The receipt records:

- opening and closing VWAP;
- closing orderIds and tradeIds;
- exchange `realizedPnl`;
- exact userTrades commissions;
- funding rows during the lifecycle;
- net after funding;
- REALIZED_PNL income cross-check when tradeId correlation is available.

## Trigger and backfill

A wrapper around `_sync_positions` schedules the read-only capture when a position
present before reconciliation is absent afterwards. The task is detached from
execution so query failures cannot block or authorize trading.

On the first sync after startup, the module also backfills up to three recent
(48-hour) flat BGX entry lineages. This is intended to recover incidents such as
the AVAXUSDT close observed before this collector existed.

## Safety

- No risk threshold changes.
- No HWM/drawdown changes.
- No leverage or margin-mode changes.
- No sizing changes.
- No order/SL/TP mutations.
- No Railway variable changes.
- All failures are logged with `decision_effect=NONE execution_effect=NONE`.
