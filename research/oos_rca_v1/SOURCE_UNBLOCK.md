# OOS evidence source unblock — operator-only, NO LIVE

## Findings verified by connector — 2026-10-08

The GitHub repository is **PUBLIC**. Never commit actual Railway logs, API keys, account state, candidate traces, cost snapshots or private output files. `private_oos_evidence/` is excluded in root `.gitignore`.

A read-only scan of Railway log *windows* (not an exhaustive database dump) recovered **90/90 of the visible, exact-ID `PROSPECTIVE_OOS_APPROVED_CANDIDATE_V1` entries** joined to `SHADOW_CANDIDATE_WHILE_LIVE_BLOCKED` entries, with full `entry/stop/target`. Windows were Oct 5, Oct 6, Oct 7 (separate Oct 7 00:00–07:00 UTC recovery for two UNI IDs), and Oct 8. Repeated logs are deduplicated by `candidate_id`. This establishes recovery of the **approved log population**, but is NOT a full row-level PostgreSQL reconciliation, does NOT provide the rejected counterfactual cohort and does NOT prove execution PnL.

### 1. Obtain the raw log export privately

From Railway logs, capture both markers as JSONL records of this exact form:

```json
{"timestamp":"<UTC timestamp>","message":"[PROSPECTIVE_OOS_APPROVED_CANDIDATE_V1] candidate_id=... ..."}
{"timestamp":"<UTC timestamp>","message":"[SHADOW_CANDIDATE_WHILE_LIVE_BLOCKED] candidate_id=... ..."}
```

Use the period **2026-10-05 to 2026-10-08 UTC**, in daily windows, and split Oct 7 as needed: a single 500-line query can omit earlier signals. Keep original timestamps and source identity intact. Do not include unrelated logs or balance/credential fields in published materials. The connected Railway API does not expose a file export into this GitHub runner, so ChatGPT cannot create a private local file on your phone from the connector results directly.

### 2. Produce an exact-ID approved fallback (offline)

```sh
python -m research.oos_rca_v1.railway_approved_log_inputs \
  private_oos_evidence/railway_markers.jsonl \
  --prefix private_oos_evidence/recovered_approved
```

Check `recovered_approved.json` for zero missing approved-to-shadow pairs. The script enforces same ID, symbol, direction, setup, regime, capture epoch, and frozen research-only authority, then emits market-only `entry/stop/target` values. It intentionally omits private fee, PnL, margin, balance and BBO cost fields. These 90/90 counts are a particular log-window snapshot, not a target guaranteed by the program.

For an authorized, **complete approved-and-rejected same-cohort dataset**, use the existing `pg_path_inputs_export.py` and `pg_matched_cohort_export.py` within a PostgreSQL **SELECT-only, REPEATABLE READ READ ONLY** session. Railway OAuth provides no SQL-query executor or database credentials in ChatGPT, so that direct DB step remains external.

### 3. Acquire public CLOSED Binance USD-M 15m candles (no API key)

Binance's public endpoint is `GET https://fapi.binance.com/fapi/v1/klines`, using symbol, interval, startTime and endTime. No authenticated account endpoints are used. Consult [official futures REST documentation](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data) and [Binance public archives](https://github.com/binance/binance-public-data).

```sh
python -m research.oos_rca_v1.fetch_public_binance_klines \
  --candidates private_oos_evidence/recovered_approved.jsonl \
  --as-of-epoch YOUR_FROZEN_UTC_EPOCH \
  --prefix private_oos_evidence/binance_usdm_15m_bars
```

The tool fetches only public `15m` klines; closes are checked against the frozen `as_of`. It validates symbol, candle timestamp and OHLC, notes missing data rather than backfilling, limits query windows and writes source hash. If the API is inaccessible (network restrictions, HTTP 451/429, symbol missing), record the blocker, **do not** synthesize data. Alternative: retrieve Binance Data Vision USD-M **daily** `15m` archives and checksum verify them; note that daily archives are posted the next day and an archive for today's partial data will not yet exist.

### 4. Combine and run research-only path replay

```sh
python -m research.oos_rca_v1.join_candles \
  --candidates private_oos_evidence/recovered_approved.jsonl \
  --candles private_oos_evidence/binance_usdm_15m_bars.jsonl \
  --prefix private_oos_evidence/with_bars

python -m research.oos_rca_v1.binance_path_replay \
  private_oos_evidence/with_bars.jsonl \
  --as-of-epoch YOUR_FROZEN_UTC_EPOCH \
  --prefix private_oos_evidence/path_replay
```

The recovered Railway log fallback has **no cost snapshot**, so net returns MUST stay null. The path model calculates only gross hypothetical full-position stop-first/TP/horizon close with conservative same-candle ambiguity and gap handling. For approved-vs-rejected analysis and net estimates, the missing **database-authorized per-candidate export** and point-in-time cost evidence are still required.

### Integrity and trading safety

- Source log fallback `90/90` is approved-side only and limited to a logged time window. Do NOT say the full selected/rejected population has been reconstructed.
- Existing Binance path simulator is **NOT** complete production parity: no partial TP, trailing, funding, liquidation, mark-price triggers, actual fills/latency or leverage constraints.
- These scripts produce no Binance orders, call no private endpoint, never mutate data and cannot promote LIVE.
- Frozen V2 #579 and new prospective #592 remain separate studies; no retroactive edits.
- No merge/deploy of PR #595 without independent review. `MAX_DRAWDOWN=30%` and the historical hard gate remain unchanged.
