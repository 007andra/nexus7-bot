# Binance LIVE Margin Parity Audit

## Scope

Audit the AVAXUSDT LIVE rejection with Binance code `-2019 Margin is insufficient`.
This branch is diagnostic only. It does not change strategy, risk, sizing, leverage,
margin mode, order authorization, retries, or order quantity.

## Production evidence

Deployment `89497482-3ac6-4e4f-80de-2a1ba00029da` on 2026-09-29 produced:

- available balance: 6.0944 USDT
- configured and exchange leverage: 50x, CROSS
- final quantity: 25 AVAX
- signal entry reference: 11.420
- fresh pre-dispatch reference: 11.441
- final sizing margin at signal reference: 5.7100 USDT
- fresh initial-margin equivalent: 5.7205 USDT
- Binance response: HTTP 400, code -2019, Margin is insufficient
- no logical retry, no fill, no protection order

## What the local arithmetic proves

At the fresh 11.441 reference, 25 AVAX and 50x leverage:

- initial margin + 5 bps opening fee is about 5.863513 USDT
- local headroom versus 6.0944 USDT is about +0.230887 USDT
- therefore simple current-price initial margin plus the logged taker fee does not
  explain the exchange rejection.

The effective reference price that would consume all 6.0944 USDT under that same
simplified formula is about 11.891512, roughly +3.94% above 11.441.

## Market-order bound hypothesis

Binance USD-M exchangeInfo exposes `marketTakeBound`, documented as the maximum price
difference rate from mark price that a MARKET order can make. The adapter previously
discarded this field.

If the AVAXUSDT bound is at least about 3.94%, and if Binance uses an equivalent
worst-case price in its pre-acceptance margin reservation, that mechanism is
sufficient to explain the -2019 mechanically. For example, a 5% scenario requires
about 6.156688 USDT for the captured order.

This remains a hypothesis, not a proven Binance formula. The next dynamic proof must
capture AVAXUSDT actual `marketTakeBound` and compare a non-submitting
`POST /fapi/v1/order/test` result or another authoritative exchange signal.

## Additional code gap

The final engine affordability recheck still uses `sig.entry` after the fresh
pre-dispatch market gate has observed a newer executable price. That stale-price
comparison is a separate correctness gap. It is not changed in this audit because
the captured fresh-price arithmetic alone still shows positive local headroom.

## Changes

- retain `marketTakeBound` in normalized Binance instrument metadata;
- add pure `bot.binance_margin_parity` diagnostics;
- add regression tests with the captured AVAX values.

No execution behavior is changed.
