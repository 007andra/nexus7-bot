"""Research-only sensitivity of static replay returns to hypothetical round-trip costs.

Rates are SCENARIOS, never claims about actual Binance account fees or fills.
Compares approved/rejected only within the same observed symbol/side/UTC-date
strata and reports coverage rather than treating missing observations as zero.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

APP = "COUNTERFACTUAL_APPROVED"
REJ = "COUNTERFACTUAL_REJECTED"
COST_BPS = (0, 5, 10, 15, 20, 30, 50)


def finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def analyze(records):
    report = {
        "source": "OFFLINE_STATIC_STOP_TARGET_GROSS_ONLY",
        "realized_pnl_proven": False, "live_allowed": False,
        "cost_scenarios_are_actual_account_fees": False,
        "horizons": {}, "matched_strata": {},
        "warnings": [
            "Stress costs are round-trip scenarios, not actual fees/slippage/funding.",
            "Matched strata are exploratory and not a causal or prospective test.",
            "Incomplete market coverage, execution-path simplification, and symbol/time dependency remain.",
        ],
    }
    for horizon in (60, 240):
        rows = []
        observed = set()
        for r in records:
            if str(r.get("horizon")) != str(horizon):
                continue
            gain, timestamp = finite(r.get("return_gross_reference")), finite(r.get("captured_epoch"))
            dec = r.get("cohort_decision")
            if gain is None or timestamp is None or dec not in (APP, REJ):
                continue
            cid = r.get("candidate_id")
            if not cid or cid in observed:
                raise ValueError("DUPLICATE_CANDIDATE_HORIZON")
            observed.add(cid)
            day = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
            rows.append({"decision": dec, "symbol": r["symbol"], "side": r["side"],
                         "day": day, "return": gain})
        output = {}
        for decision in (APP, REJ):
            vals = [x["return"] for x in rows if x["decision"] == decision]
            mean = sum(vals) / len(vals) if vals else None
            output[decision] = {
                "n": len(vals), "mean_gross_fraction": mean,
                "gross_break_even_roundtrip_cost_bps": mean * 10000 if mean is not None else None,
                "cost_sensitivity": {
                    str(bps): {"hypothetical_mean_ex_funding": mean - bps / 10000 if mean is not None else None,
                               "hypothetical_positive_rate": sum(v > bps / 10000 for v in vals) / len(vals) if vals else None}
                    for bps in COST_BPS
                },
            }
        report["horizons"][str(horizon)] = output
        by_stratum = defaultdict(lambda: {APP: [], REJ: []})
        for x in rows:
            by_stratum[(x["symbol"], x["side"], x["day"])][x["decision"]].append(x["return"])
        matched = [(key, vals) for key, vals in by_stratum.items() if vals[APP] and vals[REJ]]
        ap_n = sum(len(v[APP]) for _, v in matched)
        lift = (sum(len(v[APP]) * (sum(v[APP]) / len(v[APP]) -
                    sum(v[REJ]) / len(v[REJ])) for _, v in matched) / ap_n
                if ap_n else None)
        report["matched_strata"][str(horizon)] = {
            "matched_symbol_side_day_strata": len(matched),
            "approved_covered": ap_n,
            "approved_unmatched": output[APP]["n"] - ap_n,
            "rejected_covered": sum(len(v[REJ]) for _, v in matched),
            "approved_weighted_mean_lift_fraction": lift,
        }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("replay_csv", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.replay_csv.open(encoding="utf-8", newline="") as file:
        result = analyze(list(csv.DictReader(file)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
