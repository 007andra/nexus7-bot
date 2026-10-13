# Decision #609-D1 — Evidence persistence under the R$0/month ceiling

Status: **DECIDED — OFFLINE ONLY / NO RUNTIME ACTIVATION**  
Date: 2026-10-12 BRT  
PR: #610; original coorte: `SHORT_DOWN_BOS_EXEC_NET_OOS_V1`  
No Binance research API key is required.

## Decision

1. **Reject raw aggTrade retention in the LIVE trading PostgreSQL.** The existing production database supports critical order/risk/ledger functions and OOS views, not unlimited high-frequency event tapes. Introducing multi-hour per-symbol trade rows there could change DB size, lock/IO pressure, OOS latency and production resource cost. No schema migration, new PG write role, DDL, or new DB service is authorized by this decision.
2. **Allow only bounded, offline, manually initiated local-format testing on the isolated research branch.** The existing `short_down_bos_event_archive_v1.py` has exclusive file creation, a cryptographic self-chain and strict event ID/time continuity. Set a hard **16 MiB maximum of raw JSONL data for one recording session**, with fail-closed abort. The manifest records the exact persisted byte count; verification compares it to bytes actually read. Exceeding the limit invalidates the session: do not silently rotate into unlimited files, split/reconnect to reuse IDs, or treat the remainder as complete.
3. **GitHub can anchor a minimal manifest fingerprint, not the tape itself.** `prepare_anchor_receipt` emits only `manifest_sha256`, event-chain digest, event count and size. It explicitly reports `ANCHOR_PREPARED_NOT_PUBLISHED`. Posting it to issue #609 requires review; no GitHub comment is sent automatically. A GitHub issue comment is amendable and does not make the bytes available, immutable or Binance-authenticated. A Git commit referencing the digest would improve audit trail but still not preserve the underlying tape.
4. **No billed infrastructure or hidden fees:** no Backblaze B2 bucket, Railway volume/worker, cloud storage service, paid DB tier or raw research-feed subscription is created. Existing budget ceiling for B2 remains R$0/month. A future independently retained raw archive requires its own approved durability, retention, privacy, operating cost, least-privilege access, quota and rollback plan.
5. **Keep separate authorities.** The offline public market source produces no natural canonical NEXUS approvals, no account balance, no execution quantity/marketability proof, no exact exchange stop-trigger fills, and no legitimate 60-member #609 cohort enrolments. The LIVE hard drawdown gate, capital checks and order paths are untouched; missing raw source or incomplete protected price paths keep `NET_PROOF_MISSING` and `LIVE=NO_GO`.

## Recorded technical contract

| Layer | Current state | What a PASS proves | What it does not prove |
|---|---|---|---|
| Public Binance USD-M `@aggTrade` format | Manual collector code; never started here | Event envelope shape (when captured) | Completeness or Binance-origin authentication |
| Append-exclusive 16 MiB JSONL | Tested offline, fail closed | Locally consistent serialized events | Independent immutability or durable persistence |
| Local self-hash chain & seal | Offline verifier | Detects tampering of a retained chain | Prevents wholesale rewrite of chain and manifest |
| Prepared GitHub fingerprint | No published real-session receipt | Prospective external comparison point if reviewed/published | Raw event availability or an attested exchange tape |
| Protected net evaluator PR #610 | Synthetic tests only | Algorithmic handling of supplied input | Real fees, funding, liquidity, SL/TP exchange fills |
| Natural NEXUS-approved #609 sample | None newly verified | Nothing yet | DO NOT replace with cash-independent market-universe symbols |

## Go/no-go criteria for the next deployment request

Do **not** merge or activate the capture process on production merely because CI is green. Before any real market recording, require a source+storage review confirming *where* raw evidence will live independently of a Railway ephemeral disk, bounded per-symbol daily write sizes, retention/visibility, external hash anchoring, reconnect gap proof, correct trade coverage at exact entry/60m/240m, and an approved monetary ceiling. With a maximum spend of R$0/month and no verified durable raw-data destination, **the next deployment decision remains NO-GO**. A separate strictly local bounded trial is a format test, not an OOS evidence campaign.

Original cutoff and target of #609 cannot be changed; historical R3 and BBO V3 samples may not be re-enrolled. Real trading remains separately blocked by equity=0, historical drawdown and failing prospective economic evidence.

Official public stream envelope reference: https://developers.binance.info/docs/derivatives/usds-margined-futures/websocket-market-streams/All-Market-Mini-Tickers-Stream


## Existing infrastructure and potential zero-price GitHub release retention

**Verified current infrastructure:** Railway `BGX CAPITAL` production has exactly the `nexus7-bot` and `Postgres` services, no buckets and no extra unattached volumes; the ONLY persistent volume is a 500 MB Postgres mount at `/var/lib/postgresql/data`, which must not be used to ingest raw trade tapes. Bot has no persistent mount. GitHub `007andra/nexus7-bot` is **PUBLIC**, so ANY uploaded GitHub release asset is publicly readable.

As documented by GitHub (checked October 2026), GitHub Actions artifacts/logs on public repos default to **90-day expiration**, max 90 days, so they are not a permanent raw event archive:
https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository

GitHub Releases have per-asset limit **under 2 GiB**, up to 1,000 assets per release, and no published total-release size/bandwidth limit. They MAY be a no-added-product-cost manual archive for public market-event-only data, but this is **not** proof of account-specific zero billing, long-term retention SLA, source authenticity, privacy acceptance or immutable third-party anchoring:
https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases

**New offline, no-publish preparation:** `bot/short_down_bos_release_bundle_v1.py` packages one locally verified 16 MiB-limited trade session in a compressed ZIP with exact session manifest and segments only. It validates ZIP member safety (no path traversal, symlinks, duplicates, unbounded expansion), replays the hash chain after extraction into an isolated temporary directory, and returns a SHA-256 for the compressed bundle. Failures are `BUNDLE_NOT_PROVEN`; an existing target path is never deleted. Every successful package explicitly carries `github_release_created=false, github_asset_uploaded=false, source_independently_attested=false, durable_remote_copy_proven=false, storage_cost_verified_zero=false, live_allowed=false`.

**Go/no-go for publishing:** PUBLIC market data only (review all contents for IP/account/candidate/keys and sensitive metadata), separately authorized named release/tag, account quota/cost verified, independent raw-byte retention/second copy or external hash anchor accepted, verified download-and-replay proof, and validated source lineage. No real-session package or GitHub release has been created/uploaded in this implementation. The owner declined new API keys and has not authorized an open-ended external research archive.
