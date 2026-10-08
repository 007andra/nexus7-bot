"""Descriptive, research-only OOS horizon diagnostics for exact-cohort CSV.

Never substitute different member populations when comparing horizons.
All results are hypothetical GROSS. No trades, SQL writes, or policy decisions.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

APPROVED = "COUNTERFACTUAL_APPROVED"
REJECTED = "COUNTERFACTUAL_REJECTED"
HORIZONS = (60, 240)
DIMENSIONS = ("side", "regime", "setup", "symbol")


def finite(v):
    if v is None or str(v).strip().upper() in ("", "NA", "NONE", "NULL"):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def observed(row):
    return str(row.get("verified")).lower() == "true" and finite(row.get("future_return_gross")) is not None


def metrics(rows):
    values = [finite(x["future_return_gross"]) for x in rows if observed(x)]
    n = len(values)
    return {
        "enrolled": len(rows),
        "observed": n,
        "missing": len(rows) - n,
        "mean_gross_pct": 100 * sum(values) / n if n else None,
        "positive_rate": sum(v > 0 for v in values) / n if n else None,
        "mean_mae_pct": (
            100 * sum(finite(x["MAE"]) for x in rows if observed(x)) / n
            if n and all(finite(x.get("MAE")) is not None for x in rows if observed(x))
            else None
        ),
        "missing_reasons": dict(sorted(Counter(
            str(x.get("missing_reason") or "UNKNOWN")
            for x in rows if not observed(x)
        ).items())),
    }


def report(rows):
    by_pair = {}
    normalized = []
    for row in rows:
        cid = str(row.get("candidate_id") or "")
        horizon_raw = finite(row.get("horizon"))
        if not cid or horizon_raw not in HORIZONS or int(horizon_raw) != horizon_raw:
            raise ValueError("INVALID_CANDIDATE_OR_HORIZON")
        h = int(horizon_raw)
        key = (cid, h)
        if key in by_pair:
            raise ValueError("DUPLICATE_CANDIDATE_HORIZON")
        by_pair[key] = row
        normalized.append(row)
    for cid in {k[0] for k in by_pair}:
        a, b = by_pair.get((cid, 60)), by_pair.get((cid, 240))
        if not a or not b:
            raise ValueError("MISSING_HORIZON_ROW")
        for field in ("cohort_decision", "side", "regime", "setup", "symbol", "captured_epoch"):
            if a.get(field) != b.get(field):
                raise ValueError("CANDIDATE_METADATA_CONFLICT")
    output = {
        "interpretation": "EXPLORATORY_HYPOTHETICAL_GROSS_NOT_LIVE",
        "promotion_allowed": False,
        "live_allowed": False,
        "cohort_id": "CALIBRATION_GENERALIZATION_V1",
        "cohort_candidate_count": len(by_pair) // 2,
        "by_decision": {}, "paired_horizons": {}, "subgroups_240m": {},
        "warnings": [
            "Compare 60m versus 240m only for identical candidate IDs with both verified.",
            "Gross return is not executed PnL and excludes a proven round-trip fee/spread/slippage model.",
            "Results by symbol, regime, setup and hour are descriptive, not causal or preregistered gates.",
            "Missingness and changing maturity can bias observed outcome comparisons.",
            "The independent 48-member horizon study #592 must not reuse this exploratory dataset.",
        ],
    }
    for decision in (APPROVED, REJECTED):
        subset = [r for r in normalized if r.get("cohort_decision") == decision]
        output["by_decision"][decision] = {
            str(h): metrics([r for r in subset if int(r["horizon"]) == h])
            for h in HORIZONS
        }
        pairs = []
        for (cid, horizon), item in by_pair.items():
            if horizon != 60 or item.get("cohort_decision") != decision:
                continue
            later = by_pair[(cid, 240)]
            if observed(item) and observed(later):
                a, b = finite(item["future_return_gross"]), finite(later["future_return_gross"])
                pairs.append({
                    "candidate_id": cid, "symbol": item.get("symbol"),
                    "side": item.get("side"), "regime": item.get("regime"),
                    "setup": item.get("setup"), "r60": a, "r240": b,
                    "delta_240_minus_60": b - a,
                })
        if pairs:
            ds = [v["delta_240_minus_60"] for v in pairs]
            output["paired_horizons"][decision] = {
                "both_verified": len(ds),
                "delta_240_minus_60_mean_pp": 100 * sum(ds) / len(ds),
                "240m_better_count": sum(x > 0 for x in ds),
                "60m_better_count": sum(x < 0 for x in ds),
                "equal_count": sum(x == 0 for x in ds),
                "mean_60_gross_pct_same_members": 100 * sum(x["r60"] for x in pairs) / len(pairs),
                "mean_240_gross_pct_same_members": 100 * sum(x["r240"] for x in pairs) / len(pairs),
                "symbol_count": len({x["symbol"] for x in pairs}),
            }
        else:
            output["paired_horizons"][decision] = {
                "both_verified": 0, "delta_240_minus_60_mean_pp": None,
                "240m_better_count": 0, "60m_better_count": 0,
                "equal_count": 0, "mean_60_gross_pct_same_members": None,
                "mean_240_gross_pct_same_members": None, "symbol_count": 0,
            }
    for dim in DIMENSIONS:
        groups = defaultdict(lambda: {APPROVED: [], REJECTED: []})
        for row in normalized:
            if int(row["horizon"]) != 240:
                continue
            decision = row.get("cohort_decision")
            if decision in (APPROVED, REJECTED):
                groups[str(row.get(dim) or "UNKNOWN")][decision].append(row)
        output["subgroups_240m"][dim] = {
            k: {d: metrics(vals) for d, vals in v.items()}
            for k, v in sorted(groups.items())
        }
    return output


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path, help="CSV from pg_matched_cohort_export.py")
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()
    with args.input.open(encoding="utf-8", newline="") as fh:
        data = list(csv.DictReader(fh))
    result = report(data)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
