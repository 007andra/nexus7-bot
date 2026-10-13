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


## Operator approval D4 — public Binance-trades-only GitHub Release

**Approved by the operator in ChatGPT on 2026-10-13 BRT:** publication through a **PUBLIC GitHub Release** for the future bounded pilot, **only** when the ZIP contains already-public Binance USD-M `aggTrade` fields and non-sensitive technical integrity metadata. This satisfies the *permission to disclose that restricted class of public data*; it does not prove that a real capture exists or that the storage destination has accepted the bytes.

### Exact authorized scope

- Allowed public trade event fields: `e, E, s, a, p, q, f, l, T, m`. Outer archive-line fields: `prev, hash, seq, received_ms, event`; manifest fields are strictly limited to the recorder's fixed public identity and integrity metadata, with no user account or strategy data.
- **DENY publication** if any unrecognized manifest/event/record/segment key exists, even if local hashes are self-consistent. `bot/short_down_bos_release_bundle_v1.py::verify_bundle` now rejects with `PRIVATE_OR_UNEXPECTED_FIELD_IN_PUBLIC_TAPE`; tests cover unexpected `private_account_key` and `orderId` fields.
- No Binance secret, API token, wallet balance, trade order, execution report, exchange account ID, candidate ID, proprietary signal, source code secrets, or risk/position parameters may be included.
- Original archive cap stays **16 MiB per recording session**; malformed or incomplete sessions must not be published as complete. The archive's source and durability limitations remain explicit.
- The operator **did not** authorize a paid service, bucket/volume, automatic long-running Railway collector, changing `viable_symbols`, bypassing DD, opening positions, or promoting an OOS strategy.

### What is still NOT done

No session has been captured/archived in the current project workflow; GitHub API reports zero Releases at time of authorization. The connected GitHub toolset in this ChatGPT session can review repository metadata and edit code/issues but does **not** expose an action to create a Release or upload binary assets. **No Release, tag or asset has been created, and no cost or successful download-and-reverify has been claimed.**

Next acceptable operational sequence: (a) exact-head CI and security PASS on this privacy-gate patch; (b) source-only pilot capture outside the live bot, with bounded quota and a source-consistency check; (c) verify the public-only ZIP locally; (d) use a GitHub Release publishing workflow with separately checked billing/quota and explicit visibility, then re-download and compare ZIP SHA-256 and contents; (e) only then record archive persistence as demonstrated. Until independent market-data provenance, actual fee and funding records, and protective execution evidence are verified, financial OOS remains `NOT_PROVEN` and LIVE `NO_GO`.


## Reproducible bounded-public pilot runbook — 13/10/2026

Commands below are a prepared procedure, **not a claim of an actual feed capture
or published release**. The operator already authorized one PUBLIC market-trades-only
pilot. Execute on a separately checked-out copy of PR #610 on an isolated local
Linux/macOS host with public internet; NOT on the LIVE Railway bot or its
PostgreSQL. Python 3.11+ is required. This is source-format research, not
canonical enrollment into the first 60 of #609.

### Capture manually, without Binance credentials

~~~sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install websockets==12.0
mkdir -p ./research_public_raw ./research_public_out
PYTHONPATH=. python scripts/short_down_bos_public_aggtrade_capture_v1.py \
  --symbol BTCUSDT \
  --directory ./research_public_raw \
  --duration-seconds 60 \
  --ack-local-not-durable
~~~

Record the actual session_id reported in stdout. Never invent or backfill it.
Abort on gaps, errors, timeouts, missing seal or the 16 MiB raw-data cap.

### Reverify and prepare privacy-gated ZIP

~~~sh
export SESSION_ID='PASTE_THE_REAL_REPORTED_SESSION_ID'
PYTHONPATH=. python - <<'PY'
import os
from pathlib import Path
from bot.short_down_bos_event_archive_v1 import verify
from bot.short_down_bos_release_bundle_v1 import prepare_bundle, verify_bundle
sid = os.environ["SESSION_ID"]
raw = Path("research_public_raw")
out = Path("research_public_out") / (sid + ".zip")
assert verify(raw, session_id=sid)["status"] == "SELF_CHAIN_VERIFIED_UNATTESTED"
receipt = prepare_bundle(raw, session_id=sid, destination=out)
assert receipt["status"] == "BUNDLE_SELF_VERIFIED_NOT_UPLOADED"
assert receipt["event_count"] >= 2
assert verify_bundle(out)["bundle_sha256"] == receipt["bundle_sha256"]
print("ZIP:", out)
print("EXPECTED_SHA256:", receipt["bundle_sha256"])
print("EVENT_COUNT:", receipt["event_count"])
PY
~~~

An existing output ZIP is intentionally never overwritten; use a new
destination directory if a previous file already exists. Inspect ZIP entries
and manifest for private data. Only the preapproved public-only schema can
pass the exporter; self-hashes do not independently authenticate Binance.

### Publication — manual GitHub step, not available in connector

GitHub official Releases documentation currently specifies up to 1,000 assets
per release, each **under 2 GiB**, with no published aggregate release-size or
bandwidth quotas:
https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases

These published limits do **not** verify this account's billing or a retention
SLA. Review the GitHub account's billing/quota before posting.
The connected GitHub tools do **not** expose Release creation or upload of binary
release assets. After local ZIP validation, use repository Releases > Draft a new
release in the GitHub web interface. Choose a distinct research tag with a target
commit that does not activate PR #610 in LIVE, and mark it a pre-release.
Before tag creation, inspect whether tag-triggered workflows can deploy.
Attach exactly the verified public-only ZIP. Release notes must not include
account, strategy, candidate, API credential or proprietary information.
If privacy, billing or tag deployment preflight fails, **do not publish**.
The user's PUBLIC-pilot consent is already recorded above; it need not be
requested again.

### Download and verify a published asset independently

Use the actual published asset URL, never a guessed one:

~~~sh
curl --fail --location --output research_public_out/downloaded.zip \
  'PASTE_ACTUAL_PUBLISHED_ASSET_DOWNLOAD_URL'
sha256sum research_public_out/downloaded.zip
PYTHONPATH=. python - <<'PY'
from bot.short_down_bos_release_bundle_v1 import verify_bundle
result = verify_bundle("research_public_out/downloaded.zip")
assert result["status"] == "BUNDLE_SELF_VERIFIED_NOT_UPLOADED"
print("DOWNLOADED_SHA256:", result["bundle_sha256"])
print("EVENT_COUNT:", result["event_count"])
print("FINAL_CHAIN_SHA256:", result["final_event_chain_sha256"])
PY
~~~

Compare SHA-256, event count and final chain digest to the original local
receipt and only then report a real Release URL and receipt in issue #609.
The verifier's github_release_created=false flag is deliberately static: an
offline ZIP cannot attest that GitHub actually accepted its upload.
Public trades alone still do not prove protected order fills, fees/funding,
independent provenance, 60 prospective canonical approvals or LIVE readiness.


## WebSocket-license retention decision — 2026-10-13

**Status: RAW TAPE PUBLIC RELEASE = HOLD; one verified metadata-only Release = DONE.**
This updates/supersedes the raw-ZIP publishing instructions above until scope-specific
redistribution rights have been documented. Operator authorization to publish
public data is recorded, but operator consent does not substitute for source
data provider redistribution rights.

### Checked official sources and exact scope

- Binance's [Vision Dataset Terms (version 1.0, 2026-08-26)](https://github.com/binance/binance-public-data/blob/master/TERMS_AND_CONDITIONS.md)
  define datasets as files available through data.binance.vision and associated
  endpoints. They restrict commercial exploitation and hosting of datasets and
  related market-data feeds. Their terms **do not expressly establish** a
  redistribution license for an independently captured USD-M Futures public
  WebSocket event stream at fstream.binance.com.
- Conversely, a public WebSocket endpoint, no-key access and the word PUBLIC
  on an event do **not establish** permission to mirror its raw events as a
  public GitHub Release. Treat the applicability/licensing as **UNRESOLVED**,
  not as proven prohibited or freely redistributable. Obtain authoritative
  scope-specific permission/terms before public raw ZIP publication.
- GitHub's [public-repository artifact and log retention policy](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository)
  defaults to 90 days and cannot exceed 90 days for public repositories.
  GitHub Actions artifacts are therefore **not a permanent proof archive**;
  do not upload raw events under the assumption that this is confidential,
  rights-cleared, independently attested, or indefinitely retained.

### What the pilot really achieved (verified GitHub)

- Existing PR #610 is still DRAFT, research-only, and has no production merge.
  The original Gitleaks false positive was cleared without ignore rules.
- First isolated runner proof, [run 38396423778](https://github.com/007andra/nexus7-bot/actions/runs/38396423778):
  879 public BTCUSDT aggTrade events, locally validated; no persisted ZIP.
- Second isolated runner proof, [run 38397577283](https://github.com/007andra/nexus7-bot/actions/runs/38397577283):
  **1,714 actual public BTCUSDT aggregate trade events** during a bounded
  60-second capture, local JSONL verification and local release-bundle
  allowlist/integrity checks passed. The **unpublished** ZIP's SHA-256 was
  3ae0e558e3ae1be9e6c6b6618c3ef1d6bab16d6bb25ca363c0d5ca1b7eb56a20.
- Exactly one [public metadata-only prerelease](https://github.com/007andra/nexus7-bot/releases/tag/research-aggtrade-609-receipt-20261013-38397577283)
  with an **824-byte** public-integrity-receipt.json asset has been created.
  Its SHA-256 is
  abd3f23ca3bef949ec60e8e00a34e40ea482dbc3ce547e3eb14970e60c681354.
  GitHub returned the same asset digest and the runner re-downloaded and
  compared that receipt byte-for-byte. **The raw tape was NOT published or
  retained remotely; the hash proves only the receipt's byte identity.**
- The one-shot publishing workflow at
  .github/workflows/research_609_public_receipt_release_once.yml on
  research/aggtrade-public-pilot-609-20261013 has been **HARD DISABLED**
  after that success: no push trigger, and its job has an explicit false gate.
  No new automatically published Release is authorized by its presence.

### Explicit retention and execution decision table

| Path | Raw events remotely retrievable? | Present decision |
|---|---|---|
| Ephemeral GitHub Actions runner | No, temporary filesystem | Allowed one-off format feasibility; never durable proof |
| Public GitHub Release containing raw Binance WebSocket ZIP | Yes, publicly redistributed | **HOLD** until applicable rights confirmed; never infer from public feed |
| GitHub Actions artifact with raw tape | Potentially retrievable; time-limited; access/rights unresolved | **HOLD**; not a replacement for licensing or permanent retention |
| Production Postgres or LIVE bot filesystem | Interferes with execution resources / ephemeral storage | **DENY** per operator's production isolation rule |
| Paid bucket / new worker / additional key | Would change authorized budget and system scope | **DENY** |
| Public Release with minimal non-sensitive receipt only | Receipt retrievable; raw tape not recoverable | **DONE** for integrity of publication metadata only |

**Next unblocked work:** document a provider-authoritative answer on USD-M
WebSocket raw-data archival and redistribution. If rights permit, rerun a
bounded isolated capture and verify a retrievable remote copy against the
fresh receipt; do not claim the prior temporary ZIP can be recovered.
Separately, the original #609 cohort demands **60 natural canonical
NEXUS-approved shadow signals**, not public trades. At zero negotiable equity,
production reported symbols_scanned=0, and a public tape cannot count as
even one eligible cohort member. Do not bypass capital/risk gates, invent
a new cohort membership rule or treat synthetic evaluation as execution proof.

**Safety authority:** live_allowed=false, promotion_allowed=false;
preserve 30% historical drawdown limit/HWM, risk gates, strategy vetoes,
orders and production deployment unchanged.
