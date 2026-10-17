# Binance USD-M private/user-data stream — runtime authority

- Base: `057882c6297f678f3a669b62ce8e0f4aaa81fa07` (PR #422 merge)
- Branch: `fix/binance-private-ws-runtime`

## CURRENT PROTOCOL (before this fix)

- **Protocol:** REST listenKey user-data stream. It is *not* the WebSocket API (`wss://ws-fapi.binance.com/ws-fapi/v1`).
  - `POST /fapi/v1/listenKey` creates the key (the API key goes in the header; no signature is needed).
  - `PUT /fapi/v1/listenKey` every 30 min keeps it alive.
  - Raw stream at `wss://fstream.binance.com/ws/<listenKey>`, with no subscription message.
- **Handler:** `BinanceClient._handle_private_order_event`.
  - ORDER_TRADE_UPDATE → `OrderRegistry` transition.
  - ALGO_UPDATE → conditional-order cache.
  - Everything else is ignored.
- **Pre-live probe:** `prelive_readonly_probe._binance_private_ws_probe`. It does a POST of the listenKey, connects to `wss://fstream.binance.com/ws/<listenKey>`, and checks ping/pong.
  - The result is cached forever in `_prelive_private_ws_probe_ok`.
  - It was logged as `authenticated=true subscription_ack=true`.
- **What gates new entries (before):**
  - The LIVE preflight checked that cached flag.
  - PilotGuard gate 14 checked only `client._order_registry is not None`.
  - No gate observed the live stream.

## OFFICIAL BINANCE PROTOCOL

Sources: Binance Open Platform, consulted on 2026-09-27.

- [User Data Streams — Connect](https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams)
- [Important WebSocket Change Notice](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Important-WebSocket-Change-Notice)
- [Start](https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams/Start-User-Data-Stream) and [Keepalive](https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams/Keepalive-User-Data-Stream) User Data Stream
- [Event: User Data Stream Expired](https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams/Event-User-Data-Stream-Expired)

**Protocol families** (A–D below are the four families the request asked to compare):

| | family | current status | used by NEXUS-7 |
|---|---|---|---|
| A | market streams | routed `/public` (book ticker, depth) and `/market` (kline, ticker, mark price …) | `/market` since PR #422 |
| B | listenKey user-data stream | **still supported**. The key comes from REST `POST /fapi/v1/listenKey` and is valid 60 min. A POST on an account with an active key returns that key and extends it. A PUT extends it by 60 min. When the key expires a `listenKeyExpired` event is pushed and no more events follow. | **yes** |
| C | WebSocket API `wss://ws-fapi.binance.com/ws-fapi/v1` | an alternative way to start or keep alive a listenKey (`userDataStream.start`) and to place orders over WS | no, and not needed |
| D | base-URL split | `wss://fstream.binance.com/{public,market,private}`. Legacy unrouted URLs (`/ws`, `/stream`) were decommissioned on 2026-04-23. Unrouted connections receive only `/public` data; `/market` and `/private` channels stop pushing. | — |

**Official user-data URL:**
- `wss://fstream.binance.com/private/ws/<listenKey>`, or
- `wss://fstream.binance.com/private/ws?listenKey=<key>&events=ORDER_TRADE_UPDATE/ACCOUNT_UPDATE`.

The protocol is unchanged: same REST listenKey, raw stream, no subscription message. **Only the routed path changed.** That makes a path change protocol-compatible. It is not a blind URL swap: the key lifecycle, the event schema and the absence of a subscription frame are all identical.

## ROOT CAUSE

- Both the private user-data stream and the pre-live probe connected to the legacy unrouted `wss://fstream.binance.com/ws/<listenKey>`.
- On unrouted connections Binance no longer pushes `/private` events (ORDER_TRADE_UPDATE, ACCOUNT_UPDATE, ALGO_UPDATE, TRADE_LITE, listenKeyExpired …).
- The TLS/WebSocket handshake and protocol ping/pong still succeed, so:
  - the stream loop never saw an error, never reconnected and logged nothing;
  - the probe returned PASS and the preflight reported `private_ws=True`, while no private event could ever arrive.
- **Additional latent defects in the same path:**
  1. A keepalive `PUT` failure killed the keepalive task silently. The key would then expire after 60 min, `listenKeyExpired` was ignored, and the stream would stay silent forever.
  2. A malformed frame raised inside the loop and tore the whole stream down.
  3. WS `REJECTED` mapped to `OrderState.FAILED`, which is an invalid transition from `SUBMITTED`, so it was silently skipped.
  4. Nothing required REST reconciliation after a reconnect.

## PRODUCTION EVIDENCE (read-only, deployment `0dd88b27`)

- `[PRIVATE_WS_READONLY_PROBE] result=PASS symbol=ETHUSDT authenticated=true subscription_ack=true` at 03:18:29.
  - In code this means only: a listenKey was issued, the handshake on `/ws/<key>` completed, and ping/pong succeeded.
- `[PILOT_LIVE_PREFLIGHT] result=PASS … private_ws=True` repeats about every 20 s. It is the cached flag, not stream health.
- There is no `Binance private WS reconnect` warning in the deployment, so the legacy socket stays open and silent. This matches the public-stream behaviour proven in PR #422.
- No order and no account mutation occurred, so the absence of private events in production is expected either way. Production alone cannot prove event delivery. The proof is:
  - **Documentation:** unrouted connections do not receive `/private` channels.
  - **Isolation reproduction:** on the exact production commit `057882c`, with the real bootstrap and real loop, an ORDER_TRADE_UPDATE FILLED never reaches the registry (state stays `SUBMITTED`). Routing alone (`BINANCE_FAPI_WS_BASE=…/private`) makes it arrive (`FILLED`). The probe returns PASS in **both** runs.

## RUNTIME OBJECT GRAPH

| question | answer |
|---|---|
| A. who creates `BinanceClient` | `main.lifespan`: `ExchangeClient()` (`bot.exchange` → `bot.binance.BinanceClient`) |
| B. how many instances | one |
| C. who starts the public WS | `TradingEngine._connect` → `client.start_websocket` |
| D. who starts the private WS | `TradingEngine._connect` → `client.start_private_websocket(self.orders, …)`, a task on the same client |
| E. same client for both | yes |
| F. does the private WS create another object | no. The probe opens a separate short-lived connection on the same client. |
| G. overlays replacing handlers | none for Binance. `market_data_integrity` and `runtime_truth_hooks` apply to KuCoin only. The runtime contract now pins `binance._handle_private_order_event` to `binance.py`. |
| H. handler | `BinanceClient._handle_private_order_event` (native) |
| I. where events land | ORDER_TRADE_UPDATE → `OrderRegistry` (`engine.orders`, `ManagedOrder` state machine); ALGO_UPDATE → `_algo_order_cache`; every private event → `private_stream_health` |
| J. consumers | the engine open path, durable persistence/reconciliation, protection readiness |
| K. order/fill/position authority | order: `ManagedOrder` (`bot.order_state`, monotonic, terminal states final); fill: REST (`wait_for_fill` / `get_order_by_client_oid` → `apply_exchange_order_truth`); position: REST (`get_positions`, `refresh_account_exposure`, protection guard) |
| L. REST vs WS divergence | REST wins for terminal truth. WS can only advance monotonically. Stale REST ACKs after a WS fill are a no-op. Divergence that cannot be resolved blocks entries (durable `orders` block). |

## AUTH FLOW

1. `POST /fapi/v1/listenKey` with the `X-MBX-APIKEY` header. No signature, timestamp or recvWindow is involved; the key is not logged.
2. The key is REST-confirmed, so `health.mark_listen_key_confirmed()` sets a 60 min validity window (monotonic).
3. HTTP 401/403 or codes −2014/−2015 → `health.mark_auth_failed` → gate 14 BLOCK with `auth_failed`. The loop retries with backoff.

## SUBSCRIPTION FLOW

There is no subscription message: the listenKey in the path *is* the subscription. The stream connects to `wss://fstream.binance.com/private/ws/<listenKey>`. The key is logged only masked (`abcd…wxyz`).

## EVENT FLOW

```
_private_ws_loop (routed /private)
  ├─ frame not JSON / not an object → counted, WARNING (rate-limited), health unchanged, stream continues
  ├─ listenKeyExpired → reconcile_required → reconnect with a fresh POST
  └─ _handle_private_order_event
        ├─ ALGO_UPDATE        → algo cache (observability) + health.record_event
        ├─ ORDER_TRADE_UPDATE → OrderRegistry monotonic transition (source=WS) + health.record_event
        ├─ ACCOUNT_UPDATE / TRADE_LITE / ACCOUNT_CONFIG_UPDATE / MARGIN_CALL → health.record_event only (REST owns positions/balances)
        └─ unknown event      → ignored (no order state, no health)
     handler exception → reconcile_required (a fill may have been missed)
```

## ORDER STATE FLOW

- The canonical state machine is unchanged:
  `CREATED → SUBMITTING → SUBMITTED → PARTIALLY_FILLED → FILLED`, with `REJECTED` / `CANCELLED` / `FAILED` as the other terminal states.
- An ACK is `SUBMITTED` and a fill is `FILLED`; they are never merged.
- WS status mapping:
  - `NEW` → SUBMITTED
  - `PARTIALLY_FILLED` → PARTIALLY_FILLED
  - `FILLED` → FILLED
  - `CANCELED` / `EXPIRED` / `EXPIRED_IN_MATCH` → CANCELLED
  - `REJECTED` → **REJECTED** (fixed)
- Terminal states admit no exit, so late or duplicated events are no-ops.

## REST/WS AUTHORITY

- The WS event is the fast path. REST is the authoritative confirmation.
  - `wait_for_fill` REST polling happens in the open path.
  - `durable_live_reconciliation.reconcile_pending` looks orders up by clientOrderId.
  - The preflight reads exposure (positions and open orders).
  - The protection guard reads positions and conditional stops.
- **WS absence never means "no order" or "no fill".** After an ambiguous POST, `_recover_ambiguous_order` looks the order up by `origClientOrderId` and never resubmits blindly. A duplicate clientOrderId (−4116) is reconciled by id.
- Unresolved divergence → durable `orders` block → no new entries (fail-closed).

## RECONNECT SEMANTICS

- **Every connection requires reconciliation**, including the first. After a (re)connect, disconnect, keepalive failure, handler error or `listenKeyExpired`, the flag is `reconcile_required`.
- `PrivateStreamHealth.check()` is `event_capable` only when **all** of the following hold:
  - state is `CONNECTED`,
  - the route is `/private`,
  - the listenKey was confirmed by REST within 55 min (60 min validity minus a 5 min margin),
  - there is no auth failure,
  - `mark_reconciled(epoch)` succeeded for the *current* connection epoch.
- `mark_reconciled` is called only by the LIVE preflight (`pilot_live_runtime._private_stream_ready`), and only after both of these succeed in the same preflight:
  1. the authenticated exposure read (`_prelive_account_exposure_verified`), and
  2. `reconcile_pending(engine, min_interval_s=0)` returning True, meaning every pending order converged from REST truth.
- A stale epoch cannot clear the flag.
- Reconnect never invents or removes order state.

## KEEPALIVE

- `PUT` runs every 30 min. On success the validity window becomes 60 min from now.
- On failure: `keepalive_failures += 1`, `reconcile_required`, the socket is closed, and the loop issues a fresh `POST` and reconnects.
- `listenKeyExpired` → reconnect with a new or extended key.
- The key is never logged unmasked.

## IDEMPOTENCY

- Same-state transitions are no-ops. `PARTIALLY_FILLED → PARTIALLY_FILLED` records history only; the WS path never adds quantities.
- Terminal states reject every transition. A late `PARTIALLY_FILLED`, `CANCELED` or `NEW` after `FILLED` is skipped.
- The WS handler never places, cancels or amends orders and never creates SL/TP. A duplicate ALGO_UPDATE updates one cache entry.

## FAIL-CLOSED BEHAVIOR

PilotGuard **gate 14** blocks new entries with `14_WS: stream privado não apto (reason=...)` for each of these reasons:
- `disconnected` / `connecting`
- `unrouted`
- `auth_failed`
- `listen_key_unconfirmed`
- `reconcile_required`

The LIVE preflight line now reads `private_stream=<reason>`. Exits, protection repair and reduce-only safety close are unaffected. An unprotected position keeps `7_8_UNPROTECTED`.

**Public market data (PR #422)** is untouched, and so is `DURABLE_STATE`, which belongs to a separate PR. Note that `durable._block(engine, "orders")`, raised by unresolved reconciliation, is the same durable mechanism; this PR does not change it.

## TESTS

- `tests/test_binance_private_ws_runtime.py`: 2 tests in a child process with the real bootstrap, real probe, real `start_private_websocket(engine.orders)` and real handler. They cover:
  - routing, FILLED reaching the registry, the shared authority, clean task cancellation, a masked key;
  - real reconnect → epoch 2 → `reconcile_required`.
  - **Both fail on 057882c.**
- `tests/test_binance_private_stream_invariants.py`: 23 tests over the real loop, handler, REST normalizer, `apply_exchange_order_truth`, preflight step and protection guard. They cover items 1–30 of the request plus the two simulated end-to-end flows.

## RESIDUAL RISKS

1. **DURABLE_STATE blocker:** `[DURABLE_STATE] nova entrada bloqueada` is still present in production. It is a separate PR, and the bot is **not** released for a first LIVE order.
2. **Events filter:** the `events=` query filter is not used; all user-data events are received.
3. **Idle stream:** Binance pushes no periodic user-data heartbeat, so an idle stream is indistinguishable from a silently dead one except by ping/pong (180 s interval, 600 s timeout). This is why REST remains the authority and the stream is never required to be the only fill signal.
4. **REST lookup after restart:** `get_order_by_client_oid` needs the in-memory `clientOid → symbol` map. After a restart, durable orders rely on the durable reconciliation path, which fails closed (unresolved → block).
5. **ACCOUNT_UPDATE / TRADE_LITE** are not used to mutate position state (by design).

## ROLLBACK

Revert the PR. The previous state is a silent private stream with REST authority, which is not unsafe for fills but gives a false preflight PASS. No persisted state is involved.
