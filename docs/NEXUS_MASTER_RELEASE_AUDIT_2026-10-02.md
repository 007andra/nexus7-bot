# NEXUS-7 — Master Release Audit (2026-10-02)

Status: **ENGINEERING_RELEASE_READY = NO** — P1-OPEN-1 (stale Binance protective
algo orders after a close) is fixed in code (see below); deploy remains blocked by
P1-DEPLOY-1 (venue inventory of legacy, unmapped `bgx7-` algo orders must be read
before the new fail-closed gate goes live). No merge, no deploy.

## 1. Source of truth (verified from the git DAG, not from summaries)

| Ref | SHA | Relation |
|---|---|---|
| `migration/binance-usdm` (production) | `c14cfd7ac6daad101f5fe306cd05d73ceadf1423` | base of this RC |
| audit branch `claude/audit-algorithmic-trading-system-uf3zxo` | `beb9b7e` | merge-base with production `77ae453` (2026-09-23); 24 ahead / 344 behind |
| PR #466 `feature/nexus-intelligence-core-v1` | `3234a8e` (not `3ef9f64` as briefed) | 214 ahead of c14cfd7, 0 behind |
| PR #465 `feature/nexus-native-market-forecasting` | `520a04f` | 37 ahead |
| PR #467 `audit/model-h-oos-evidence` | `1835f50` | 28 ahead |
| PR #468 `research/model-h-recalibration-v2` | `fb862fb` | 32 ahead, contains #467 |
| PR #469 `research/model-h-v3-multiscale-analogs` | `1ada597` | 36 ahead, contains #468 |
| PR #470 `research/model-h-v4-invariant-cross-sectional` | `c503cda` | 40 ahead, contains #469 |
| Release candidate `claude/release-candidate-binance-hardening` | see git log | 10 commits on c14cfd7 |

DAG: `c14cfd7 ← #467 ← #468 ← #469 ← #470` (linear chain); #465 and #466 branch
independently from `c14cfd7`. Railway deployed SHA: not observable from this
session (no Railway read performed; no deploy performed).

CI that gates production: `quality.yml` and `security.yml` run on PRs into
`migration/binance-usdm` (and on push to `main`); `ci_attest.yml` only follows
Quality on `main`. No workflow deploys on push to a feature branch.

## 2. Audit branch vs production (Phase 1)

The audit branch was built on the KuCoin runtime (merge-base 2026-09-23);
production since migrated to Binance USD-M (344 commits, `bot/binance.py`). The
audit branch must **not** be merged; each finding was re-checked on the Binance
code and ported semantically where it still applied.

| Audit commit(s) | Classification | Binance status |
|---|---|---|
| F-002 readiness gate (6be7a7b, 4f6ca80) | ALREADY_IN_PRODUCTION | Binance `place_order` asserts readiness/ownership; transport fence present |
| naked close contract units (fd96c45) | OBSOLETE | Binance quantities are base asset |
| emergency close-all / F-001A flatten (b98b421, 2a599ed, 3a661cf) | REWRITTEN (Binance-native) | P1-OPEN-1: lineage-scoped algo cancel + readback |
| private order events, causal blocks, snapshot restore (2df44fe, ebbc79b, efa15f7) | ALREADY (Binance-native equivalents PR #458/#459) | — |
| F-014 malformed snapshot (0a8e9a3) | ALREADY | Binance `get_positions` raises on any invalid row |
| Q-01/Q-01B exit geometry (80da7bf, 2e2edc1) | PORTED (restart geometry) | `fd5ed4e` |
| Q-01C monotonic stops (1014595) | NEEDS_REWRITE (P2) | Binance stops never loosened by the new code paths |
| NOVO-02 closed lineage ownership (9263324) | PORTED | `fd5ed4e` (userTrades continuity) |
| NOVO-01 partial snapshot (fb5816b) | NEEDS_REWRITE (P2) | Binance snapshot is all-or-nothing (fail-closed) |
| F-003 risk sizing (d47c53f, f2d5720) | CONFLICTING / MUST_NOT_MERGE | production has its own RiskManagerV3 + final loss budget; not changed |
| F-013 post-fill geometry (b080c6b) | PORTED | `3dd5770` |
| F-013A late fills (891225d) | PARTIAL (P2) | Binance MARKET entries are terminal; timeout path covered by `ff19591` |
| NOVO-F013A-1 timeout adoption (552dd07) | PORTED | `ff19591` |
| NOVO-F013A-1c strong protection lineage (beb9b7e) | PORTED | `2144f3f` |
| docs / research (1bd0c20, 7b6fa07, e609c90) | RESEARCH_ONLY | not merged |

## 3. Findings and fixes on the Binance production base

| ID | Sev | Reproduction (pre-patch, offline composed runtime) | Fix | Regression / mutation |
|---|---|---|---|---|
| F-013 (Binance) | P1 | adverse fill 100.5: exchange SL 99.5, local SL 100.0; real loss/unit 1.0 vs 0.5 believed (≈13.9 vs 10 USDT V3 budget); ticker used as fill | `3dd5770` | `test_binance_postfill_geometry` (7) — reinstating the shift kills 5 |
| NOVO-F013A-1 (Binance) | P1 | timeout adoption sent SL 98.95 / TP 102.1 next to native 99.5/104; local R 1.05, target 2.1R | `ff19591` | 6 tests; disabling the lineage/conditional branches kills 4 |
| NOVO-F013A-1f | P0 (candidate/clientOid collision) | new trade, same symbol/side/qty/minute after close reused the finished trade's clientOid and ManagedOrder | `72fa46f` | 2 tests; mutant reproduces collision |
| NOVO-03 | P1 (durable state) | daily stop, PnL ledger, RR/partial identity, trade lineage keyed by RAILWAY IDs; HWM by Railway env name + KuCoin key/"exchange=kucoin" on Binance | `29b00fa` (+ legacy read fallback) | 9 tests; 2 mutants killed. `test_hwm_namespace_v2` inverted to the NOVO-03 contract (justified) |
| Binance restart unit | P1 (availability) | `_filled_base_qty` sent Binance base qty through `contracts_to_base` → every restart proof failed; every BGX position became EXTERNAL/unmanaged | `fd5ed4e` | `test_binance_restart_ownership_continuity` |
| NOVO-02 (Binance) | P0 (wrong-position ownership, latent until the unit fix) | historical FILLED record grants ownership of a later same-qty position | `fd5ed4e` | 5 tests; removing continuity kills 4 |
| Q-01 (Binance) | P1 | recovered positions carried the startup ATR estimate as SL/TP; 2R/partial/trailing would act on it | `fd5ed4e` | 3 tests; mutant killed |
| NOVO-F013A-1c (Binance) | P0 (weak protection lineage accepted) | any active bgx7- algo order with same side/type/price reused as current protection | `2144f3f` | 3 tests; mutant kills 2. Migration fixture now states the reused SL belongs to the same trade (justified) |
| F-019 | — | does not reproduce on Binance: `_signed_params` runs inside every attempt; order POSTs are single-attempt with clientOid recovery | `da60984` (tests only) | stale-timestamp mutant kills 2 |
| NOVO-F013A-1b | — | does not reproduce on Binance (Decimal step units everywhere); KuCoin-branch artifact | `da60984` (1000+ property cases) | float-validator mutant killed |
| test_research_process | ENV | child of `run_offline` only propagates `purelib`; `optuna` needs `packaging` installed under `/usr/lib/python3/dist-packages` in this container | environment only (`packaging` added to purelib); no code change | 4/4 after env fix |

### P1-OPEN-1 — CLOSED in code (stale Binance protection after flat)
Pre-patch (offline, composed): after trade A's SL filled, TP A (`bgx7-…`,
`closePosition=true`) stayed `NEW`; `cleanup_flat_symbol` classified it as
external (`is_bgx_owned` only knew `bgx-stop-`), cancelled nothing and logged
`cleanup_status=VERIFIED`. Root cause: (1) KuCoin prefix as ownership, (2) KuCoin
cancel endpoint, (3) no durable algo -> lineage map, (4) the flat GC only ran at
global flat, nothing ran before the next same-symbol entry.

Fix:
- `bot/binance_protection_registry.py`: durable map `clientAlgoId -> {symbol,
  kind, opening_order_id, opening_client_oid}` written BEFORE every algo POST
  (stable financial namespace; load-first, never clobbers a durable map it could
  not read).
- `BinanceClient.cancel_algo_order`: `DELETE /fapi/v1/algoOrder` (algoId or
  clientAlgoId, official SDK 17.5.0 semantics), signed per attempt, single
  attempt, ACK validated, result never trusted as final state.
- `bot/binance_stale_protection.py`: `reconcile_symbol` (flat proven -> enumerate
  `openAlgoOrders` -> strong lineage: opening orderId first, clientOid fallback ->
  cancel owned orders of closed lineages -> readback -> VERIFIED). Unmapped
  `bgx7-` -> `UNRESOLVED_UNMAPPED_BGX_PROTECTION`; manual/external on a flat
  symbol -> `UNRESOLVED_EXTERNAL_PROTECTION`; unreadable inventory ->
  `UNRESOLVED_INVENTORY_UNKNOWN`; never cancelled, never VERIFIED.
- Pre-entry gate in `BinanceClient.place_order` (opening orders, LIVE): flat
  proven via positionRisk, then reconcile; anything but VERIFIED blocks the entry.
- `cleanup_flat_symbol` delegates Binance clients to the reconciler (readiness
  per-symbol and global flat sweep; emergency flatten and 2R/partial exits).
- `_already_active` reuse also requires the registry record of the current
  opening orderId (a same-minute clientOid can repeat).

Tests: `tests/test_binance_stale_protection.py` (A–Q, composed attack, property
150 cases, bounded durable map, signed single-attempt DELETE). Mutations M1–M5
and gate-removal all killed. Fixture changes (justified): dispatch proof now
expects the read-only `openAlgoOrders` GET before the single POST; two migration
fixtures state flat proof / registry ownership explicitly.

### P1-DEPLOY-1 (blocks deploy; operational, fail-closed)
Algo orders created by the currently deployed release have no registry record.
If any is still active when the new release starts (open BGX position, or a
leftover sibling of a closed trade), it is `UNRESOLVED_UNMAPPED_BGX_PROTECTION`:
entries on that symbol are blocked and, at global flat, the readiness sweep
blocks all entries until an operator resolves it. Not unsafe (no wrong cancel,
no naked position) but it can freeze trading after deploy. Required before
deploy: a read-only `GET /fapi/v1/openAlgoOrders` inventory per configured
symbol plus open positions, and an operator decision for each active order.
Venue behavior on flat (auto-cancel of `closePosition` algo orders) was not
verified read-only in this session (no venue credentials/egress); the fix is
correct either way (`-2011` -> readback converges).

### #466 blocker (not merged)
#466 modifies LIVE files (`engine.py`, `strategy.py`, `order_state.py`,
`professional_risk_adapter.py`, `execution_cost.py`). It replaces the entry
idempotency key with `candidate:<candidate_id>`; the candidate id is the setup id
(symbol:direction:entry_type:15-minute bucket), so a new trade of the same setup
after the previous one closed reuses the previous clientOid → Binance `-4116`
duplicate → treated as ambiguous → recovered to the OLD order (wrong-order
identity, P0). It also conflicts textually with `72fa46f`.

## 4. Release-candidate changes that are LIVE-reachable (Phase 39 inventory)

Unchanged: leverage, risk_pct (MAX_RISK_PCT), aggregate risk, MAX_MARGIN /
operator margin fraction, score and entry thresholds, NEXUS thresholds, TP
policy, partial fraction, 1R/2R thresholds, trailing parameters, MAX_POSITIONS,
drawdown limits, CROSS stress.

Changed (all hardening already approved in this audit program):
- post-fill: no local fill-delta shift; ticker never a fill; the stop may be
  TIGHTENED on the exchange to the most protective of technical level, preserved
  distance and RiskManagerV3 budget stop (the shift's original intent, now
  applied where it protects capital). TP is no longer shifted.
- timeout adoption: original planned SL/TP; no ATR/liquidation estimate when
  the lineage is known or a conditional stop is active.
- order identity: new generation of the idempotency key after a terminal order.
- durable namespace: stable financial scope + legacy read fallback.
- restart: Binance positions can be recovered again (with ledger continuity);
  recovered SL/TP from exchange protection; R exits/trailing disabled without it.
- protection reuse: only algo orders created by the current opening lineage.
- P1-OPEN-1: pre-entry stale-protection gate (one extra read-only GET before
  every opening order; individual DELETE only for owned closed-lineage orders).

## 5. Research (Phases 12–28) — status

Structural isolation verified: #465 and #467–#470 only ADD files (no LIVE module
modified, so no LIVE import path reaches them); no added line calls
`place_order`, `_post`, `set_leverage`, `set_sl`, `set_position_stops`,
`close_position`, nor sets `execution_allowed`/`operator_approved`/
`promotion_allowed` to True. The full methodological audit (causality,
purge/embargo, holdout registry, block/cluster bootstrap, reproduction of the
reported numbers, historical microstructure OOS run) was NOT performed in this
pass and remains open. Verdicts therefore: EDGE_PROVEN = NO (not proven),
MODEL_H_PRODUCTION_READY = NO, MICROSTRUCTURE = NOT_PROVEN.

## 6. Tests

- Full offline suite on the RC: 2181 / 2181 passed (0 failures) after the
  environment fix; production baseline was 2138 / 2134 (only
  `test_research_process`, environmental).
- compileall OK; `ruff --select E9,F63,F7,F82` clean; pyflakes undefined names 0;
  `python -m bot.selfcheck` exit 0; `python -m bot.release_proof` PASS (165).
