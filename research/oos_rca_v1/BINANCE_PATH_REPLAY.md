# BINANCE USD-M OHLC path replay — research only

This tool adds a **separate** offline counterfactual simulation of static stop/target vs fixed horizons. It NEVER rewrites the existing frozen prospective OOS outcomes, and NEVER sends an order. It does not claim to replicate the entire NEXUS execution engine, liquidation engine, partial TP/trailing, real fill slippage or funding settlements.

## Workflow

1. Export the exact frozen `CALIBRATION_GENERALIZATION_V1` candidates from an operator-controlled PostgreSQL account with SELECT permissions:

```sh
python -m research.oos_rca_v1.pg_path_inputs_export \
  --as-of-epoch 1791478800 \
  --prefix ./private_oos_evidence/path_inputs
```

2. Obtain an **independently verified point-in-time archive** of Binance USD-M closed 15-minute OHLC candles for the candidate symbols and capture windows. Do not infer OHLC paths from the existing `future_return` marks. Encode **one bar per JSONL line** with `symbol,ts,o,h,l,c`. Timestamps may be UTC seconds or milliseconds (13-digit real-world Unix epoch), aligned to exact 900-second opens.

3. Attach the exact candle path by symbol and 15m timestamp. Missing 60m/240m bars stay missing; the join will never substitute future or neighboring candles:

```sh
python -m research.oos_rca_v1.join_candles \
  --candidates ./private_oos_evidence/path_inputs.jsonl \
  --candles ./private_oos_evidence/binance_usdm_15m_bars.jsonl \
  --prefix ./private_oos_evidence/with_bars
```

4. Run deterministic path simulation and obtain CSV/JSON (the same `as_of_epoch` must be fixed in every run):

```sh
python -m research.oos_rca_v1.binance_path_replay \
  ./private_oos_evidence/with_bars.jsonl \
  --as-of-epoch 1791478800 \
  --prefix ./private_oos_evidence/path_results
```

Run regression tests:

```sh
python -m unittest -v tests.test_binance_path_replay tests.test_oos_binance_path_inputs
```

## Conservative deterministic assumptions and limitations

- The raw `entry` is **captured signal price**, not a verified Binance fill. The first post-capture 15m candle opens at `ceil(captured_epoch / 900)*900`, matching NEXUS's existing `outcome_from_cache` mark-out interval. A position existing since capture is only a hypothetical scenario.
- Order of checks: **stop first, then TP**. If both touch in a single candle, the result is `AMBIGUOUS_STOP_FIRST`, always pessimistic. Stop-market gaps are filled at the opening price when worse than SL; price improvement beyond TP is never credited.
- **Only one static full-position stop or TP**, otherwise horizon-close. No production partial take-profit, trailing stop, dynamic stop replacement, liquidation, exchange lot/tick filters, uncertain order trigger basis, market/mark/index-price distinctions or execution latency. Therefore the result is **NOT a production-parity replay**.
- To estimate expenses, `cost_snapshot` must match the same candidate, symbol, Binance exchange and have a timestamp **not after candidate capture** and at most 120s old. Fee and adverse entry/exit slippage are based on that historical snapshot only. Without complete validated cost snapshot, the tool reports gross hypothetical movement, never silently imputing zero expenses.
- `modeled_net_ex_funding` is **net of assumed fees/slippage only**, not realized PnL and NOT a full net return. Binance funding rates/timestamps and mark-price settlement are not replayed. Funding may occur even within a 240-minute horizon depending on the symbol's funding frequency. `funding_status=NOT_MODELED` is ALWAYS emitted. A true executable net outcome is NOT computed.
- The original frozen OOS `future_return` uses simple directionally signed entry-to-horizon-close mark returns and does not exit early at stop, TP or liquidation. **Never compare those numbers directly with executable PnL without matching basis and missingness.**
- General prospective OOS samples, frozen SHORT/DOWN/BOS V2 #579, and independent preregistration #592 have **different eligibility**. Keep them apart. The script exports the general prospective OOS only; new threshold or hold-time decisions require a new independent prospective study.
- `MAX_DRAWDOWN` 30%, historical HWM, Risk Epoch, score/R:R/EV, leverage and LIVE release controls remain unchanged. Draft PR #595 is for code review only; no merge, deploy or trading authorization.

**Reproducibility and operational risk:** Keep raw market archive timestamp-source provenance and hashes privately, and use a SELECT-only PostgreSQL credential. Never paste keys in GitHub issues or store actual account secrets in the research CSV. The ChatGPT Railway connector has no SQL query execution; this replay has not been run on production database/candles. Until that evidence exists, do not claim a strategy is profitable.
