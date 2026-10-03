# Pilot Guard 11_MARKET_DATA — Binance runtime bridge

## 1. Incident

- **Where:** production, Binance USD-M LIVE. Deployment `537c72d1`, merge commit `72d75a0` (PR #421).
- **Candidates:** approved candidates were blocked from about 02:08 to 02:20 UTC on 2026-09-27, for example SUIUSDT LONG and NEARUSDT.
  - SUIUSDT LONG: strategy score 76.50, NEXUS APPROVE, net R:R 2.007, net EV +0.659%.
  - Account state was clean: account preflight PASS, 0 positions, 0 orders, drawdown 9.10% (limit 10%).
- **Blocker:** every one of them was blocked by
  `11_MARKET_DATA: nenhum dado de mercado recebido`.
- **What PR #421 did:** it tried to fix this by publishing `client._last_ws_update` from the public WS handler. It passed CI and was deployed, but the block continued.

## 2. Forensic audit (writer → reader)

| question | finding (evidence) |
|---|---|
| A. where is the client created | `main.lifespan`: `client = ExchangeClient()` (`main.py:117`); `ExchangeClient` is `bot.binance.BinanceClient` (`bot/exchange.py`). |
| B. instances at LIVE bootstrap | **one**. No other construction site; `main.ExchangeClient()` → `TradingEngine(client)`. |
| C. public WS instance | the same one: `engine.client.start_websocket(...)` (`engine.py:956`). |
| D. private WS instance | the same one: `engine.client.start_private_websocket(...)`. |
| E. engine client | the same object. Binance is not wrapped; `KuCoinPositionUnitAdapter` applies to KuCoin only (`nexus_runtime_engine.py`). |
| F. what PilotGuard receives | `self.pilot.can_open_pilot(self, self.client, ...)` (`engine.py:2777`), i.e. the same object. |
| G. wrappers/proxies/overlays | `PilotGuard.evaluate` is wrapped by `pilot_exposure_capacity`, which calls the original, so gate 11 still runs. The only Binance WS-handler overlay was PR #421's `binance_public_ws_freshness`. `market_data_integrity` and `runtime_truth_hooks` wrap the WS handler only when `is_kucoin()`. |
| H. writers of `_last_ws_update` | before this fix: the PR #421 overlay (Binance) and `kucoin.py` (KuCoin). After: none on Binance; it is now a read-only view of `market_data_health`. |
| I. readers | `pilot.py` gate 11 and `integrity.py` (MARKET_DATA_STALE). |
| J. later override | none. The bootstrap log shows `[BINANCE_MARKET_DATA_FRESHNESS]` installed at 02:07:18, and `_ws_loop` looks up `self._handle_ws_message` dynamically on every frame. |
| K. other task/process | no. The WS task is `asyncio.create_task` on the same client in the same process. |
| L. attribute on A, read on B | no. There is one instance. `tests/test_pilot_market_data_runtime_bridge.py` proves `engine.client is client` in the real bootstrap. |
| M. BinanceClient vs adapter | identical class and object. |
| N. reconnect swaps instance | no. `_ws_loop` reconnects inside the same method on the same `self`. |

**Conclusion of the audit:** there was no instance, authority or overlay-order defect. The writer PR #421 installed was the one that ran, on the object PilotGuard reads. That writer simply **never fired**.

## 3. Root cause (proven)

`_ws_loop` subscribed `@ticker` and `@kline_*` on the **unrouted** legacy URL `wss://fstream.binance.com/stream?streams=...`.

Binance split the USD-M WebSocket base URL into routed entry points: `/public`, `/market` and `/private`.
- Legacy unrouted URLs were decommissioned on 2026-04-23.
- An unrouted connection only receives `/public` streams (book ticker, depth).
- `kline` and `24hrTicker` are **/market** streams. On an unrouted connection they are never pushed: the handshake succeeds and the socket then stays silent.
- No frame means no parse error, no exception and no reconnect, so nothing was ever logged.

**Production proof (read-only Railway data for deployment `537c72d1`):**
- DNS: `fstream.binance.com` was resolved at 02:08:09.7, right after `🔌 Iniciando WebSocket com 25 símbolos`.
- Network flows: three TLS connections to `3.114.180.166:443` received about 5.3 KB each at 02:08:10–02:08:13 (handshake/upgrade, plus the 158 B private-probe ack).
- After that, sampled ingress windows (02:08:13–02:08:16 and 02:24:32–02:24:59) show **zero bytes** from any fstream address, while `fapi` REST, Telegram and Postgres traffic is continuous.
- A healthy 100-stream `/market` combined stream pushes about 4 frames per second.
- There are no `Binance public WS reconnect` warnings; handler parse errors were only logged at DEBUG.

**Isolation proof (same code as production, `72d75a0`, real bootstrap):**
- `BINANCE_FAPI_WS_BASE` unset → URL `/stream` → gate 11 BLOCK.
- `BINANCE_FAPI_WS_BASE=wss://fstream.binance.com/market` → URL `/market/stream` → the same PR #421 writer fires → gate 11 PASS.
- Nothing else changes between the two runs.

**Why market data looked healthy elsewhere.** Strategy, MTF, BOS, NEXUS, R:R and EV all read REST: `get_klines` hits `/fapi/v1/klines` on every scan, and `get_ticker` hits REST. Only gate 11 depended on WS freshness.

## 4. Why PR #421 failed

- It fixed a *writer* for events that never arrived. Its tests called `_handle_ws_message` directly with synthetic frames and never went through the transport, `_ws_loop`, the bootstrap or `PilotGuard.evaluate`.
- `test_pilot_guard_accepts_fresh_timestamp_and_rejects_stale` asserted arithmetic on a stub rather than the gate.
- It also introduced a second, class-patch writer path, which silently added wall-clock state onto the instance.

## 5. Architecture

**Before**

```
_ws_loop(/stream  ← unrouted, silent)
   └─ _handle_ws_message  (class-patched by binance_public_ws_freshness)
          └─ self._last_ws_update = time.time()      (never executed)
PilotGuard gate 11 → getattr(client, "_last_ws_update", 0) → 0 → BLOCK
```

**After**

```
BinanceClient.__init__ → self.market_data_health = MarketDataHealth()   (single authority)
_ws_loop(/market/stream)
   ├─ connect   → health.mark_connected()      (does NOT advance freshness)
   ├─ 90 s with no frame → WARNING silent_stream → reconnect
   └─ native _handle_ws_message
          validated kline/24hrTicker → cache mutated → health.record_public_event()
PilotGuard gate 11 → market_data_blockers(client)
          → isinstance(client.market_data_health, MarketDataHealth) → health.check(120)
BinanceClient._last_ws_update → read-only view (IntegrityGuard, diagnostics)
runtime contract → FAILS startup if _handle_ws_message is wrapped, or if the route is not /market
```

**Why this authority.** `_last_ws_update` stays only as a derived view because IntegrityGuard and the KuCoin/test-double contract read it. It has no setter, so a second writer is impossible on Binance. The authority itself is:
- **Single and injected:** it is owned by the one client and reached through any `__getattr__` proxy.
- **Thread- and async-safe:** protected by a lock.
- **Monotonic:** freshness uses `time.monotonic()`.
- **Isolated:** it has no import cycle (`bot.market_data_health` imports only stdlib).

## 6. Freshness semantics (gate 11)

| state | result |
|---|---|
| never received a valid public event | BLOCK `reason=no_market_data` |
| last valid event age ≤ 120 s | PASS (same `>` boundary as before; 120 s unchanged) |
| age > 120 s | BLOCK `reason=stale_market_data` |
| monotonic regression | BLOCK `reason=clock_regression` |
| malformed/invalid JSON, unknown event, unknown interval, missing symbol | no update |
| handler exception (for example a bad float) | no update (recorded only after the cache mutation) |
| private user-data WS | never updates |
| REST seed / `get_klines` / `get_ticker` | never updates |
| (re)connect | never updates; stale data stays stale until the first valid frame |

Clock: age uses `time.monotonic()`, so wall-clock jumps cannot produce a PASS. The wall-clock value is used only for logs and legacy readers.

## 7. Observability (rate-limited)

- `[MARKET_DATA_AUTHORITY] source=public_ws event=connected route=/market ... freshness_advanced=false` is logged on each connection.
- `[MARKET_DATA_AUTHORITY] source=public_ws client_type=... client_instance=... event=... symbol=... last_ws_update=...` is logged on the first event per connection and then every 300 s.
- `[MARKET_DATA_AUTHORITY] event=silent_stream action=reconnect` and `event=frame_error` are WARNING, rate-limited to 60 s.
- `[PILOT_MARKET_DATA_CHECK] client_type=... client_instance=... last_ws_update=... age_s=... result=PASS|BLOCK reason=...` is logged on each change of result, or at most every 60 s.
- The writer's `client_instance` and the reader's `client_instance` must be equal.

## 8. Tests

- `tests/test_pilot_market_data_runtime_bridge.py`: two tests in a child process, driving the real bootstrap, the real `_ws_loop` and the real `PilotGuard`, over a fake transport that implements Binance's routed-path rule.
  - They check wiring, the incident scenario, and real reconnect → stale → first frame.
  - Both fail on `72d75a0`.
- `tests/test_market_data_freshness_authority.py`: 22 in-process invariant tests covering items 3–20 of the request, the runtime-contract drift check, the routed URL and the silent-stream watchdog.

## 9. Residual risks

1. **Private user-data WS (important, out of scope here).** `_private_ws_loop` and `prelive_readonly_probe` still use the unrouted `wss://fstream.binance.com/ws/<listenKey>`.
   - Per the same Binance change, `/private` events are not pushed on unrouted connections.
   - Consequence: ORDER_TRADE_UPDATE, ALGO_UPDATE and ACCOUNT_UPDATE are probably not arriving. The probe proves only a subscription ack.
   - REST (`wait_for_fill`, `get_order_status`) is the fill authority, so this is not a gate-11 issue.
   - It must be migrated to the `/private` route in a separate reviewed PR **before the dispatch → ACK → fill → SL/TP stage**.
2. IntegrityGuard still computes its age with wall clock from the derived `_last_ws_update`, so it is subject to wall-clock skew. It is a stricter (BLOCK-side) consumer and was left unchanged.
3. Binance could re-classify streams again. The 90 s silent-stream watchdog makes that visible within 2 minutes instead of silently.
4. The route is `WS_BASE + "/market"`. An operator overriding `BINANCE_FAPI_WS_BASE` must give the root URL, not a routed one.

## 10. Rollback

Revert the PR commits.
- The previous behaviour (gate 11 permanently BLOCK on Binance) is fail-closed, so a rollback cannot open entries.
- No persisted state or migration is involved.
