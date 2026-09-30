# Binance AVAX Exit / HWM Lineage Audit

## Incident

Deployment `89497482-3ac6-4e4f-80de-2a1ba00029da`, production SHA
`667d4317e11bcafa87a962277ac6533b9c2a4c7e`.

AVAXUSDT LONG was opened by BGX and fully protected. At 21:18:30 UTC the
runtime received an AVAX algo event with `status=EXPIRED` and
`actual_order_linked=false`. At 21:18:37 UTC the position was absent from
Binance. Local post-trade forensics therefore emitted `EXCHANGE_CLOSE_OTHER`
and estimated PnL because it had no confirmed close fill identity.

The later accounting sweep found exchange trades but reported
`authoritative_cycles=0`, with evidence split between BGX order evidence and
manual/external evidence. This is consistent with a real close fill whose
normal orderId cannot be linked to a BGX conditional algo `actualOrderId`.
It does not prove whether the close was protective, manual, liquidation, ADL,
or another exchange-side mechanism.

## Identity gap reproduced

`reconstruct_bgx_lifecycles` correctly refuses to claim BGX ownership when:

- opening fill and durable BGX lineage are present;
- a closing SELL fill exists and returns the position to flat;
- the closing normal order has a non-BGX client id; and
- the BGX algo row has no `actualOrderId` link.

Adding the exact closing order id as the algo `actualOrderId` makes the same
synthetic lifecycle reconstruct successfully as `BGX_ALGO_CLOSE_ORDER`.

Therefore the present blocker is identity correlation, not merely fill
availability.

## HWM finding

The durable drawdown contract stores new highs from authenticated account
equity with no distinction between realized and unrealized PnL. During the
open AVAX position the observed HWM rose to 8.8015 USDT. After the account
became flat at 7.4078 USDT, the same contract computes:

`(8.8015 - 7.4078) / 8.8015 = 15.83%`.

This is current designed account-equity-HWM behavior; this audit does not label
it corruption and does not change the 10% hard gate. A policy decision would
be required before changing whether unrealized intratrade highs can establish
the durable performance HWM.

## Safety

This branch changes tests and documentation only. It does not modify risk,
drawdown thresholds, leverage, sizing, entries, exits, protection, exchange
requests, variables, or secrets.
