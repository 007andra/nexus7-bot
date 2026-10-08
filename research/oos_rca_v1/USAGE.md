# OOS candidate RCA: offline reproduction

This folder is an **isolated research utility**, not a trading engine feature.

Run from the folder:

```sh
python -m unittest -v test_report.py
python report.py railway-export.jsonl --as-of 2026-10-08T16:30:00Z --prefix output/rca
```

Input is JSONL containing Railway export objects (`{"timestamp":"...Z","message":"[MARKER] candidate_id=... ..."}`) or previously normalized `{"record_type":"approval|terminal|outcome|cost","candidate_id":"..." }` records. The parser accepts only these markers: `PROSPECTIVE_OOS_APPROVED_CANDIDATE_V1`, `CANDIDATE_TERMINAL`, `SHORT_DOWN_BOS_V2_MEMBER_PROOF`, and `COST_SHADOW_BBO_V3_COST_ONLY`; unknown messages and non-whitelisted fields are discarded. **Avoid exporting API credentials and private account data** to this program even though its parser filters unrecognized fields.

Outputs: `output/rca.csv` (one row per candidate and 60/240m horizon) and `output/rca.json` (missingness, counts and descriptive returns).

Critical interpretation rules:

- Approval-marker return60/return240 values are NOT outcome-verification authority; only matching horizon `OUTCOME=OBSERVED` and `verified=true` from a dedicated outcome record counts.
- `NATURAL_COUNTERFACTUAL_NEXUS_APPROVED` is a **research-only approval**; do not confuse it with the separate `CANDIDATE_TERMINAL` snapshot's nexus_called/allowed flags or with LIVE entry authorization.
- `NOT_CALLED_IN_TERMINAL` is not a rejected trade. The script keeps it separate from `NEXUS_REJECTED_TERMINAL`.
- Missing and unverified results are never zero. Records may be delayed, stale, outside the log-export window, or missing their outcome source.
- A cost estimate is only attached when `candidate_id` matches, BBO is fresh (default <=1000ms), and the timestamp on the **log envelope** is within 120s of the candidate capture epoch. This is at best a **log-time proxy**, not proof of the price-time quote or that the cost covers both entry and exit. Gross-minus-cost is labelled `illustrative_net_return` and is **not actual net realized PnL**. Do not use it as a release criterion.
- All comparisons of approved versus rejected remain **disabled** (`comparison_allowed=false`) until a matched-universe verified-outcome dataset is established. Missing cost evidence excludes hypothetical net estimates.
- Fix `--as-of` for reproducibility; never replace prospective V2 #579, independent horizon study #592, or a frozen candidate population with retroactive samples.

No Binance/DB/Railway imports, connections or writes; no orders, risk or strategy modifications. CI/review must complete before merging into production. Even after merge, this tool cannot authorize LIVE.
