# Telegram delivery resilience

- Base: `migration/binance-usdm` @ `29bf052f953e8d177a3ccb4455ffa12650d82130`
- Branch: `fix/telegram-delivery-resilience`
- Scope: observability only. Nothing in trading, NEXUS, score, risk, sizing,
  drawdown, SL/TP, entry filters, order authorization or Binance changed.
  Production was investigated read-only (Railway logs, DNS/network-flow logs
  and metrics). No Railway, variable or secret changes.

## RESULT: MIXED

- **The trigger is the network (Railway egress → Telegram).** Between 13:15 and
  13:37 UTC on 2026-09-27, TCP connections to `149.154.166.110:443` (Telegram)
  intermittently went unanswered: only SYN retransmissions left the container,
  and nothing came back. In the same seconds, Binance REST (`18.65.39.x`) and
  the Binance WebSocket (`18.180.185.231`) kept exchanging data in both
  directions. Egress was not totally lost; the path to Telegram was lossy.
- **The code turned that into wrong or misleading signals.** Five code defects
  made it look like a different problem and hid the real delivery state (see
  ROOT CAUSE).

## PRODUCTION TIMELINE (deployment `67b07e8f`, commit `29bf052`)

| UTC | Event |
|---|---|
| 12:30–13:00 (previous deployment) | `✅ Telegram fallback plain-text aplicado` roughly every 3–10 s: Telegram answered HTTP 400 (Markdown "can't parse entities") and the plain-text resend worked. Most routine messages cost 2 requests. |
| 13:07:32 | Deploy of `29bf052` (the only restart in the window). |
| 13:08:32 | `❌ Telegram: falha ao verificar (TimeoutError)`: getMe timed out at startup. |
| 13:08:54 / 13:13:43 | `STARTUP_READY_NOTIFICATION sent=true`, `MARKET_RADAR sent=true`. |
| 13:15:34–13:16:10 | network-flow: SYN-only flows to 149.154.166.110 on ports 37654, 38638, 36868, 36882 and 37434 (74 B, then 5 packets / 370 B over ~4 s, zero ingress). Successful flows at 13:15:39 (port 35634) and 13:16:18 (port 38484) had ingress. |
| 13:16:01 | `[NEXUS_OBSERVABILITY] Telegram delivery failed: URLError [Errno 101] Network is unreachable`: about 8 s (the urllib timeout) after the SYN of 13:15:53. |
| 13:22:14, 13:30:58, 13:31:30, 13:36:16 | Same Errno 101 from the audit worker. |
| 13:32:16, 13:32:47 | `Telegram fallback falhou HTTP 0`: Telegram answered the first request with 400, and the plain-text resend seconds later got no HTTP response at all. |
| Throughout | `[NEXUS_TELEGRAM_SCORE] … sent=true` logged about every 20–60 s, including during the failures (see ROOT CAUSE 2). |
| 13:32–13:40 | `[PILOT_LIVE_PREFLIGHT] result=PASS private_stream=event_capable` every ~15 s; `[MARKET_DATA_AUTHORITY] source=public_ws … events_total=150857`; `[ADJUSTED_EQUITY] status=RECONCILED equity=5.8827`. Trading was unaffected. |

Pattern: short bursts, not a continuous outage. Several delivery failures fall
inside minutes that also contain successful sends. There is no correlation
with restarts (one deploy at 13:07, failures from 13:16). There is no
correlation with resources: over 2 h, CPU averaged 0.054 of 2 vCPU (peak 0.34),
memory averaged 0.13 of 1 GB (peak 0.15), network RX/TX were flat. DNS for
`api.telegram.org` always returned NOERROR (A `149.154.166.110`, AAAA
`2001:67c:4e8:f004::9`). No 429 and no 5xx appeared in the window.

## ARCHITECTURE

### Before (4 independent transports, 1 credential authority)

| Emitter | Transport | Timeout | Retry | Fallback | Dedup | Error log |
|---|---|---|---|---|---|---|
| `runtime_hardening.robust_notify`, the active `notifier.notify` (engine events, NEXUS score, radar, startup) | aiohttp | total 10 s | none (429 → one background retry) | Markdown → plain on 400 | sha256, 60 s | status 0 + DEBUG only |
| `notifier.notify` (legacy, overridden at boot) | aiohttp | total 10 s | 2 retries | none | hash, 30 s | warning |
| `logger._tg_worker` (NEXUS AI decision audit thread) | urllib | 8 s | none | none | per-key cooldown | `_diag` + exception text |
| `funnel_metrics._tg_worker` (funnel summaries thread) | urllib | 8 s | none | none | pacing | counter only |
| `notifier.test_telegram` (getMe) | aiohttp | 8 s | none | none | n/a | exception text + **full chat id** |

All of them already read credentials through `bot/telegram_credentials.py`
(confirmed; `test_telegram_credentials_authority` still passes).

### After

```
event → formatter (unchanged) → notify / worker
      → dedup (60 s, never replays ambiguous sends) → global lane (serialization, unchanged)
      → bot.telegram_transport.deliver / deliver_sync
            admit()  ← TelegramHealth (HEALTHY / DEGRADED / RECOVERING)
            attempt_once(): IPv4 resolve → connect(4 s) → TLS → write/read(8 s)
            classify → retry policy → [TELEGRAM_DELIVERY] / [TELEGRAM_HEALTH]
      → DeliveryResult (truthy only when Telegram returned ok:true)
```

Every emitter goes through `bot/telegram_transport.py`. The async path runs
each blocking attempt in a worker thread (`asyncio.to_thread`) and every wait
uses `asyncio.sleep`, so the event loop is never blocked. The two daemon
workers use the same policy through `deliver_sync`.

## ROOT CAUSE

1. **Network (primary trigger).** TCP SYN to Telegram's IPv4 address went
   unanswered in bursts while Binance traffic flowed. This is on the
   Railway-egress or Telegram side.
2. **CODE: false `sent=true`.** `robust_notify` swallowed every failure and
   returned `None`. `[NEXUS_TELEGRAM_SCORE]`, `[MARKET_RADAR]`,
   `[STARTUP_READY_NOTIFICATION]` and `[NEXUS_TELEGRAM_TERMINAL]` logged
   `sent=true` whenever no exception was raised. This is proven base-fail: on
   `29bf052`, with DNS failing, `notify_nexus_score` logs `sent=true` and
   returns `True`. On the fix it logs `class=DNS_ERROR … sent=false` and
   returns `False`.
3. **CODE: misleading "Network is unreachable".** The container has no public
   IPv6 route (no egress flow to any public IPv6 address in the network-flow
   logs; fd12::/16 is internal only). urllib used `socket.create_connection`,
   which tried IPv4, timed out, then tried the AAAA address, which failed
   instantly with ENETUNREACH. Python raises the last error, so connect
   timeouts were reported as Errno 101. Reproduced in
   `test_ipv6_last_error_masking_reproduced_and_fixed`.
4. **CODE: "HTTP 0" is a sentinel, not an HTTP status.** `_post` returned
   `(0, {})` for any exception before an HTTP status was received. The
   exception class was logged only at DEBUG, which is invisible at INFO. See
   FALLBACK.
5. **CODE: no health state and no circuit breaker.** During an outage every
   message paid a full timeout inside the serialized lane. Messages queued
   behind each other, and inline `await notify(...)` calls in the engine
   waited up to ~10 s each.
6. **CODE: secret hygiene.** `test_telegram` logged and returned the full chat
   id, which also appears in the `/` status payload.

## CLASSIFICATION

`NETWORK_UNREACHABLE`, `DNS_ERROR`, `CONNECT_TIMEOUT`, `CONNECTION_REFUSED`
(connect phase; the request never left the host); `READ_TIMEOUT`,
`CONNECTION_DROPPED`, `INVALID_RESPONSE` (ambiguous: Telegram may have
processed the message); `RATE_LIMITED` (429); `SERVER_ERROR` (5xx);
`CLIENT_ERROR` (4xx); `UNKNOWN`. Only the class, the HTTP status and the errno
are logged. The exception text, the URL (which contains the token), the chat
id and the message text are never logged.

## RETRY POLICY

- Before: `robust_notify` had no retry for network errors, only one
  background retry for 429. The legacy notifier did 2 retries at fixed
  `2**n` intervals, with no jitter. The workers had no retry.
- After:
  - At most 3 attempts.
  - Retries happen only for connect-phase classes and for 502/503/504.
  - The backoff is `min(8, 1·2^(n−1)) × uniform(0.75, 1.25)`, i.e. about 1 s
    then about 2 s.
  - Ambiguous classes and 500 are never retried, so there are no duplicate
    messages.
  - 400/401/403 are not retried. The only exception is a Markdown
    "can't parse entities" 400, which is re-sent once as plain text.
  - 429 honours `parameters.retry_after` or the `Retry-After` header. If the
    wait is ≤ 5 s (≤ 30 s for critical messages), the message is retried
    inline. Otherwise sends are suppressed until the window ends.
  - The worst case for one delivery is about 3 × 4 s connect timeouts plus
    about 3 s of backoff. Once the breaker opens, a call returns in about 0 s.

## CIRCUIT BREAKER

`TelegramHealth` (Telegram only; no trading module reads it — enforced by a
test):

- **HEALTHY → DEGRADED** after 3 consecutive failed attempts. This logs
  `[TELEGRAM_HEALTH] state=DEGRADED reason=<CLASS> consecutive_failures=3
  last_success_age=…`. Remaining retries stop immediately.
- **DEGRADED:**
  - Routine messages are skipped and counted (`suppressed`), so there is no
    request storm and no backlog replay.
  - One probe, with a single attempt, is allowed after 30 s, then 60 s,
    120 s and so on, up to 300 s.
  - Critical messages always get one attempt. A message is critical if it
    contains ORDEM ABERTA, ORDEM NÃO ABERTA, TRADE FECHADO, POSIÇÃO FECHADA,
    STOP-LOSS, DRAWDOWN, PERDAS CONSECUTIVAS, BUG, BLOQUEADO NO STARTUP,
    EMERG or LIQUID.
- **RECOVERING:** a probe is in flight. On success the breaker goes back to
  HEALTHY and logs `[TELEGRAM_HEALTH] state=RECOVERED`. On failure it goes
  back to DEGRADED.
- A 4xx response proves the route works, so it closes the transport breaker.

## DEDUPLICATION

- The 60 s identical-text window in `robust_notify` is recorded before the
  send. A resend is therefore never replayed within that window, even after
  an ambiguous failure (test: `test_dedup_no_duplicates_through_notify_layer`).
- The transport never retries an ambiguous class, so a lost response is not
  assumed to be a lost message.

## QUEUES

- The audit and funnel queues are unchanged: bounded (100) and drop-on-full.
- While the breaker is DEGRADED, queued routine items are skipped instead of
  replayed, so old scores are not delivered after recovery.
- There is no new queue and no unbounded buffer. Priority for current events
  comes from critical messages bypassing the breaker (one attempt each).

## FALLBACK

- The existing "fallback" is a **content** fallback: Markdown was rejected
  with 400, so the same request is sent again as plain text. It uses the same
  host, DNS, route and egress, so it is **not** a network fallback and cannot
  help during an egress incident.
- It stays, because production shows it working constantly: most routine
  messages contain Markdown that Telegram rejects. It now lives inside the
  transport as one extra attempt.
- A network failure on that second request now reports its real class, e.g.
  `CONNECTION_DROPPED` or `CONNECT_TIMEOUT`, instead of "HTTP 0".
- **Meaning of HTTP 0:** no HTTP response was received for the plain-text
  resend. The TCP/TLS connect failed, the connection dropped, or the client
  timed out. It is never a status that Telegram returned.

## OBSERVABILITY

```
[TELEGRAM_DELIVERY] source=notify class=CONNECT_TIMEOUT attempt=2/3 latency_ms=4001 status=NA errno=NA result=RETRY decision_effect=NONE
[TELEGRAM_HEALTH] state=DEGRADED reason=CONNECT_TIMEOUT consecutive_failures=3 last_success_age=412.0s decision_effect=NONE
[TELEGRAM_HEALTH] state=RECOVERING probe=true critical=false decision_effect=NONE
[TELEGRAM_HEALTH] state=RECOVERED consecutive_failures=0 last_success_age=0s suppressed=17 decision_effect=NONE
[NEXUS_TELEGRAM_SCORE] symbol=X decision=WAIT sent=false class=CONNECT_TIMEOUT skipped=NA
```

A first-attempt success logs at DEBUG, to avoid a line per message. Retries,
failures, skips and health transitions log at INFO or WARNING.

## RAILWAY EVIDENCE

- Service `nexus7-bot`, region `ams`, 2 vCPU / 1 GB limit, deployment
  `67b07e8f` (SUCCESS).
- DNS: NOERROR for `api.telegram.org` throughout.
- Network-flow: SYN-only flows to Telegram with zero ingress in the failure
  windows, while flows to Binance and Postgres in the same seconds were
  healthy.
- There is no public-IPv6 egress in the flow logs. `fapi.binance.com` has no
  AAAA record, which is why Binance never hits the IPv6 dead end.
- No infrastructure change was made. The finding to escalate to Railway, if it
  recurs: intermittent SYN loss from ams egress to 149.154.166.110.

## TRADING INDEPENDENCE

- Static test: no module outside the observability set imports
  `telegram_transport` or reads Telegram health. The explicit check covers
  `pilot.py`, `pilot_live_runtime.py`, `pilot_risk_cap_hardening.py`,
  `final_sizing_invariants.py`, `risk_manager_v3.py` and `professional_risk.py`.
- Behavioural test: the real sizing path (adapter → RiskManagerV3 → quantity
  rules → final hook) produces identical results in three cases: Telegram
  healthy, Telegram failing with the breaker DEGRADED, and `notify` raising.
  ADA → 23 and UNI → 0 in every case.
- A notify call never raises, and `deliver` has no side effect other than logs
  and health counters.
- In production during the incident, `PILOT_LIVE_PREFLIGHT=PASS`, public and
  private WS and REST reconciliation all kept running.

## TESTS

`tests/test_telegram_delivery_resilience.py` has 34 tests. They use a scripted
HTTP server on the loopback interface and inject socket-level faults. The
real Telegram is never contacted.

- Failure modes: success, DNS, Errno 101, a real connection refused, connect
  timeout, read timeout (no replay), 429 honouring `retry_after` (inline,
  deferred and via the `Retry-After` header), 500 (no replay), 502/503
  (retried), invalid response, drop mid-send, Markdown→plain fallback, a
  fallback network failure without "HTTP 0", recovery.
- Policy: bounded retry, jitter range, breaker storm cut, probe spacing and
  critical single attempt, dedup, 10 concurrent notifications (down,
  recovery, up), shutdown cancellation without orphan tasks, event loop not
  blocked, sync worker parity.
- Configuration and security: disabled or missing credentials, redaction of
  the token, chat id, URL and payload in logs and stderr, the IPv6 masking
  reproduction, getMe chat masking.
- Truthful sent: NEXUS score and market radar report `sent=false` when
  delivery fails.
- Trading independence: the static and behavioural tests above.

Gates: compileall OK; ruff (CI select) clean; pyflakes shows 0 undefined
names; selfcheck has no critical findings and the silent-except count is
unchanged (14); release proof PASS (162/162). The full offline suite ran
1905 tests on the head and 1871 on the base. On both, the only failing suite
is `tests.test_research_process`, which fails on the base too (sandbox-only).
The LIVE-shaped runtime harness passes, and the composed notify chain is
`_serialized_notify → robust_notify → telegram_transport`.

## RESIDUAL RISKS

- The network trigger is outside the code. With the breaker, an outage
  costs about 12–15 s once, then about 0 s per message. Routine messages are
  dropped while DEGRADED, by design.
- Critical-message detection is keyword-based, and new templates must
  contain a marker.
- 5xx 500, read timeouts and dropped connections are never retried. If
  Telegram did not actually process the message, that message is lost; this
  is the price of never sending duplicates.
- `NEXUS_TELEGRAM_IPV4_ONLY=false` restores dual-stack resolution. The
  default (true) matches the observed Railway network.
- The ambiguous-send guarantee holds within the 60 s dedup window of
  `robust_notify`.
- The engine still awaits some notifications inline (unchanged code).
  Blocking is now bounded, and near zero while DEGRADED.
