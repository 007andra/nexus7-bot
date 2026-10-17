# Capital-independent market observation V1 — prereg #666 (NO LIVE)

## Frozen source of authority

- Primary prereg: https://github.com/007andra/nexus7-bot/issues/666
- Immutable GitHub issue creation time: **2026-10-13T13:27:05Z**.
- Cohort ID: CAPITAL_FREE_SHORT_DOWN_BOS_MARKET_OOS_V1
- Production frozen config reference: 45ebd59d4ca54072e1233ce0667085d92b70ab1d.
- Original canonical #609: **unchanged**, https://github.com/007andra/nexus7-bot/issues/609.
- Original #610 implementation: **unchanged**; it is NOT a branch dependency.
- New code here is independently reviewable on a separate, non-LIVE branch.

## Implemented right now

1. bot/capital_free_market_oos_666_v1.py is a pure standard-library-only
   deterministic envelope/filter/enrollment-shape and exact-time market
   return verifier. It has no trading engine, account, API, DB, env or time
   imports; there is no live call site or schedule.
2. tests/test_capital_free_market_oos_666_v1.py includes synthetic-only
   regressions for t0, source completeness, identity, 25-symbol universe,
   first-60 selection, 12-per-symbol quota, duplicate conflict, 60m/240m
   exact timestamp, censoring, and non-LIVE authority invariants.
3. Every result is OFFLINE_CONTRACT_UNATTESTED. Not even 60 correctly shaped
   fixtures can set source_attested or cohort_membership_proven. The returned
   fractional market direction is **NOT executable NET, fills or profit**.

## Required before *any* real enrollment, all pending

- Approve the exact research implementation SHA via Quality Check,
  Supply Chain Security and independent human review.
- Before collecting future observations, record **effective_t0** as the
  maximum of issue creation, passing implementation CI completion and first
  separately authorized session start; freeze it in an immutable audit record.
  This code does NOT select that cutoff on its own.
- Demonstrate a source-approved rights-compliant durable private market data
  route, with real verified contiguous closed 15m/1h/4h as-of capture-time
  candles and 60m/240m outcome coverage. REST HTTP 451 seen in issue #609
  means that endpoint is NOT validated on the hosted runner. Public
  WebSocket aggTrades alone are insufficient historical kline evidence.
- Validate reproducible *balance-independent signal generation* from a
  code-pinned, non-mutating version of Analyzer.analyze_mtf. Current module
  only validates candidate envelopes provided by a caller; there is **no**
  running collector or installed research scanner. No new production signals
  were produced by this work.
- Add immutable source authentication and append-only cohort membership
  manifest, independent outcome collection and frozen cluster-bootstrap
  statistics (10,000 replicates, seed 609202613, by symbol/UTC 4h bucket)
  and leave-one-symbol-out analysis as specified by #666. Do not allow
  acceptance gates to fire before those are reviewed and tested.
- Never reuse pre-t0 signals, R3 observations, #609 candidates or synthetic
  regression fixtures to increase the first-60 sample size.

## Explicit non-effects

No GitHub Actions market-collection workflow or tag, no new API key,
no service deployment, no Live/trading/risk/stop/TP/sizing/rule changes,
no capital top-up, no Backblaze bucket, no fee assumption, no risk/HWM
rewrite, no market tape published and **no real prospective candidates
enrolled**. Do not merge the draft PR without manual review and exact-SHA
green CI; never interpret an offline PASS as economic or LIVE readiness.

Rollback (research branch only): close the draft PR and delete its branch
after preserving the prereg issue and commit SHAs for audit. Production
branch and Railway remain unchanged.

## Addendum A — implementation added before any capture (2026-10-13)

Added on this draft branch; still **zero real candidates**, no capture run.

1. `bot/capital_free_market_observer_666_v1.py`. Audit finding: production
   never runs the bare `strategy.Analyzer.analyze_mtf`. `runtime_bootstrap`
   (also loaded by `sitecustomize.py`) replaces `strategy.score_tf` and wraps
   `analyze_mtf` with adaptive entry, pullback confirmation, entry-type and
   volume overlays. The observer therefore installs the same bootstrap in a
   **separate interpreter**: credentials/DB/Telegram variables stripped,
   `EXCHANGE=binance`, `PAPER_TRADE=true`, all non-loopback sockets blocked by
   an audit hook, analysis inside `hard_gate_shadow_context.scope()` (no score
   log, no shadow observers, no NEXUS) and `sig or context.signal` exactly as
   the production `HARD_GATE_SHADOW` scan. Production code must hash to the
   pinned digest of `45ebd59` or nothing runs. Candle windows are the
   shadow-scan windows (200/100/120, minimum 60/40/20 closed). The bar still
   forming at `t`, which the strategy discards, is a zero-elapsed placeholder;
   a regression shows its contents do not change any result.
2. Frozen parameters are the **code defaults** at `45ebd59`:
   `MIN_ENTRY_SCORE=60, FEE_MULTIPLIER=2.0, MIN_VOLUME_MULT=0.5,
   MIN_RR_RATIO=2.0, TAKER_FEE=0.0006`. Whether Railway overrides any of them
   was **not verified**; the operator must confirm or amend before `effective_t0`.
3. `bot/capital_free_market_stats_666_v1.py` implements the gate. Readings
   fixed here before data and flagged for confirmation: both distinct UTC 4h
   buckets and distinct `(symbol, 4h)` clusters must be >= 10; leave-one-
   symbol-out must stay > 0 for every symbol; lower bound is the 0-based
   500th of 10,000 sorted replicate means; one `Random(609202613)` per horizon;
   an empty replicate scores `-inf` (cannot occur with non-empty clusters).
4. `bot/capital_free_market_manifest_666_v1.py`: exclusive header freezing
   `effective_t0` (refused if recorded after it), append-only self-chained
   entries holding only allowlisted capture-time fields, every enrolled /
   cap / duplicate / invalid / post-target decision recorded in order, and a
   replay that must agree with `freeze_offline`. A local chain is not an
   external witness: post `anchor_receipt` head digests on issue #666 within
   the same 15m bar.

### Remaining blockers before the first real candidate
- Network: this session's sandbox denies `fapi.binance.com` and
  `data.binance.vision`; the GitHub-hosted runner previously saw HTTP 451.
  Railway (region `ams`) reaches Binance but running a collector there is a
  new deployment needing separate authorization and a cost check.
- Proposed zero-cost verifiable source: Binance public archive
  `data.binance.vision/data/futures/um/daily/klines/<SYM>/15m/` is documented
  to publish daily ZIPs with `.CHECKSUM` files, which could independently
  re-verify every captured candle and 60m/240m outcome after the fact. NOT
  tested: the host was unreachable from this sandbox (proxy 403). Usage rights for
  redistribution still need review; nothing raw is published.
- Operator confirmation of items 2 and 3; frozen `effective_t0` comment on
  #666 before the first capture; human review of this branch.

## Addendum B — capture route and operating rules (2026-10-13, before t0)

Operator authorized the full sequence on 2026-10-13 (chat instruction "O resto
eu autorizo tudo"). Fixed here before any capture:

- **Route:** `bot/capital_free_market_collector_666_v1.py` runs as a separate
  Railway service (region `ams`; Binance answers HTTP 451 to US hosts), started
  through `research/railway.collector666.toml`. It reads only public
  `GET /fapi/v1/klines` (no API key, no signed endpoint), never shares
  variables with the trading service, and is pinned to one CI-approved commit.
  It is not a GitHub Actions job and has no cron.
- **Parameters:** code defaults at `45ebd59`. `MIN_ENTRY_SCORE=60` matches
  production (shadow-scan log `Score=55/100 < 60`). Production sets
  `MIN_RR_RATIO` and `TAKER_FEE` as Railway variables whose values were not
  readable (redacted); BOS_BREAK geometry is fixed at R:R 3.0 so
  `MIN_RR_RATIO<=3` cannot change it, but a production `TAKER_FEE` different
  from 0.0006 would shift the minimum-move pre-filter slightly. The study
  therefore measures the strategy at code defaults, stated as such.
- **Readings of the gate** (Addendum A item 3) are accepted as fixed.
- **Each 15m boundary t > effective_t0:** 25 symbols in universe order, three
  REST reads each (rows with closeTime >= t dropped), isolated observer, every
  attempt logged in `observations.jsonl` with the raw-body SHA-256; envelopes
  go to the manifest; heads of all chains are submitted to public
  OpenTimestamps calendars and printed to the service log.
- **Outcomes:** for enrolled members only, the 15m candle closing at t+60m
  and t+240m is read inside that same 15m window. A missed window is never
  filled later; the pair stays censored and the cohort reports
  `INSUFFICIENT_EVIDENCE` rather than replacing members.
- **Code changes after t0** produce a new implementation SHA, which the
  manifest header rejects: any fix means a new cohort version, not an edit.
