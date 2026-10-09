# #609 — Independent SHORT / TRENDING_DOWN / BOS_BREAK executable-net OOS V1

## Audit boundary

Registered GitHub issue #609 at **2026-10-09T00:58:10Z / epoch 1791507490**. This branch is **not** wired into the trading engine or Railway scanner: it implements a **pure, offline evaluator**. It does not create candidates, access Binance, access PostgreSQL or retroactively mark approval. CI passing is not evidence of enrollment, durable freeze, authenticated source data or profitable trading.

Frozen 10-member SHORT_DOWN_BOS_PROSPECTIVE_V1_20261006 stays unchanged, including the 41 later ignored observations; general CALIBRATION_GENERALIZATION_V1 EVIDENCE_FAIL is never revised. Avoid reusing past members to inflate independent sample.

## Inputs and cohort semantics

Use function bot.short_down_bos_exec_net_oos_v1.evaluate(payloads, evidence_by_id_and_horizon, now_epoch=...). Inputs must be independently sourced from immutable existing HARD_GATE_SHADOW research tables via a separately authorized read-only export, with captured_epoch strictly > 2026-10-09T00:58:10Z. Do not generate or manipulate captured IDs, times or original canonical NEXUS decision. Require SHORT, TRENDING_DOWN, BOS_BREAK, actual natural nexus_called/nexus_allowed, shadow_only and non-LIVE authority, and original production net RR >= 1.60.

Deterministic chronological enrollment depends **only on capture fields**, not matured outcomes. First 60 approved (max 12 per symbol), at least 8 symbols, <=20% top-symbol concentration. Report duplicate/cap skips before outcomes. The reconstructed set has membership_durable=false; a verified durable capture manifest must be independently audited before an evidentiary READY status can exist. Missing/truncated sources fail closed.

## Net evidence requirements

The pure evaluator accepts a proof keyed by (original candidate_id, horizon) with exact candidate identity, cost-source provenance, source reference, capture-time fees/spread/slippage, explicit quantity/tick/step/min notional, nonnegative funding cost and a full contiguous 15-minute closed-bar OHLC path from the next candle after signal capture. Horizons 60 and 240 min are separately required, with no lookahead to partial entry candle or arbitrary gap substitution.

Stop/TP simulation for SHORT uses STOP first on intrabar collision, adverse-stop gap worse than stop if necessary, conservative tick rounding, adverse entry sell and exit buy slippage/spread, fees and funding. Stress doubles fee/spread/slippage/funding inputs, keeping original position and entry/SL/TP. Every result is a **hypothetical** protected path, never a fill. Missing, mismatched, expired, invalid, synthetic-labelled or unaudited cost/bar evidence cannot be represented as verified net profitability.

With 60 fully evidenced candidates, report base/stress average NET fraction per notional and hypothetical USDT per candidate, positive fraction, stop first count, deterministic cluster-bootstrap 5th percentile grouped by symbol and time bucket, and leave-top-symbol-out. Real capital-normalized equity returns, net fills with funding, basis/mark trigger, partial fills and price-path certification require additional authenticated information/review. Source evidence references alone are not independent certification; the evaluator always reports independently_verified_source=false and does not grant LIVE.

## Test/rollout gates

- Synthetic tests exercise strict post-cutoff inclusion, immutable selection, quota, duplicate/censorship handling, fee/funding/quantization/market-price path, time gaps, collision STOP-first, stress non-optimism and fail-closed authority. Fixtures are not actual trades.
- All required CI: offline suite, ephemeral real PostgreSQL CAS, release proof pack and security must PASS before a human review of any deployment.
- Do not merge or deploy a runtime hook from this PR. Independent future PR may build a bounded read-only export and durable membership attestations, with no production authorization and no new market-data calls.
- All risk, historical HWM, 30% limit, existing OOS, 3s timeout, trading config, region and order execution remain unchanged.

## Financial-gate connection

On the verified runtime, [ADJUSTED_EQUITY] status RECONCILED shows equity 5.3177 USDT, performance_hwm 22.7987 USDT, historical trading drawdown 76.68%, external_flows_applied 2, net +7.7808 USDT, pending 0. Those are summaries, not an independently recovered exchange income history or complete stored provenance chain. Do not reset/change HWM or rebase from the summary. #601 records separate required capital-flow provenance audit.