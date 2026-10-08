"""Offline NEXUS OOS audit: research only, never exchange or database actions."""
import argparse
import csv
import json
import math
from collections import Counter
from pathlib import Path

from parse_logs import parse_record, utc_epoch

HORIZONS = (60, 240)
COLUMNS = ("candidate_id", "symbol", "side", "setup", "horizon", "decision",
    "approved_counterfactual", "terminal_nexus_called", "terminal_nexus_allowed",
    "gross_return", "verified", "missing_reason", "bbo_cost_bps",
    "bbo_age_ms", "cost_status", "illustrative_net_return", "terminal_reason")

def num(x):
    try:
        if x is None or str(x).upper() in ("NA", "NONE", "NULL", ""):
            return None
        y = float(x)
        return y if math.isfinite(y) else None
    except (TypeError, ValueError):
        return None

def boolean(x):
    if isinstance(x, bool):
        return x
    return {"true": True, "false": False, "1": True, "0": False}.get(str(x).lower())

def audit(records, as_of, max_bbo_age=1000, max_alignment_seconds=120):
    items = {}
    for raw in records:
        r = parse_record(raw)
        if not r or not isinstance(r.get("candidate_id"), str) or not r["candidate_id"]:
            continue
        key = r["candidate_id"]
        item = items.setdefault(key, {"outcomes": {}, "cost": []})
        kind = r["record_type"]
        if kind == "cost":
            item["cost"].append(r)
        elif kind == "outcome":
            horizon = num(r.get("horizon"))
            if horizon not in HORIZONS:
                continue
            obs = {"verified": boolean(r.get("verified")) is True,
                   "state": r.get("outcome"), "return": num(r.get("future_return"))}
            old = item["outcomes"].get(int(horizon))
            if old is None or (not (old["verified"] and old["state"] == "OBSERVED")
                               and obs["verified"] and obs["state"] == "OBSERVED"):
                item["outcomes"][int(horizon)] = obs
            if item.get("capture") is None:
                item["capture"] = num(r.get("observation_start"))
        elif kind == "approval":
            item["approved_counterfactual"] = (
                r.get("approval_state") == "NATURAL_COUNTERFACTUAL_NEXUS_APPROVED")
            item["capture"] = num(r.get("captured_epoch")) or item.get("capture")
        elif kind == "terminal":
            item["terminal_nexus_called"] = boolean(r.get("nexus_called"))
            item["terminal_nexus_allowed"] = boolean(r.get("nexus_allowed"))
            item["terminal_reason"] = r.get("terminal_reason")
        for field in ("symbol", "side", "setup", "regime"):
            if r.get(field):
                item[field] = r[field]
    rows = []
    for cid, item in sorted(items.items()):
        called = item.get("terminal_nexus_called")
        allowed = item.get("terminal_nexus_allowed")
        decision = ("NEXUS_APPROVED_TERMINAL" if called and allowed else
                    "NEXUS_REJECTED_TERMINAL" if called and allowed is False else
                    "NOT_CALLED_IN_TERMINAL" if called is False else "UNKNOWN_TERMINAL")
        for horizon in HORIZONS:
            outcome = item["outcomes"].get(horizon)
            verified = bool(outcome and outcome["state"] == "OBSERVED" and
                            outcome["verified"] and outcome["return"] is not None)
            capture = item.get("capture")
            if verified:
                missing = None
            elif outcome and outcome["state"] in ("UNKNOWN_CACHE_GAP", "OUTCOME_NOT_PROVEN"):
                missing = outcome["state"]
            elif capture is None:
                missing = "NO_CAPTURE_TIME"
            elif capture > as_of:
                missing = "CAPTURE_IN_FUTURE"
            elif capture + horizon * 60 > as_of:
                missing = "NOT_MATURED"
            else:
                missing = "MATURED_UNVERIFIED"
            eligible = []
            for cost in item["cost"]:
                age = num(cost.get("bbo_age_ms"))
                bps = num(cost.get("live_total_cost_bps"))
                event = num(cost.get("event_epoch"))
                if (boolean(cost.get("bbo_valid")) and age is not None
                    and 0 <= age <= max_bbo_age and bps is not None and bps >= 0
                    and event is not None and capture is not None
                    and abs(event - capture) <= max_alignment_seconds):
                    eligible.append((abs(event - capture), age, bps))
            eligible.sort()
            cost = eligible[0] if eligible else None
            gross = outcome["return"] if verified else None
            net = gross - cost[2] / 10000 if gross is not None and cost else None
            rows.append({
                "candidate_id": cid, "symbol": item.get("symbol"),
                "side": item.get("side"), "setup": item.get("setup"),
                "horizon": horizon, "decision": decision,
                "approved_counterfactual": item.get("approved_counterfactual"),
                "terminal_nexus_called": called, "terminal_nexus_allowed": allowed,
                "gross_return": gross, "verified": verified, "missing_reason": missing,
                "bbo_cost_bps": cost[2] if cost else None,
                "bbo_age_ms": cost[1] if cost else None,
                "cost_status": "ALIGNED_ESTIMATE_NOT_FILL" if cost else "NO_ALIGNED_COST",
                "illustrative_net_return": net, "terminal_reason": item.get("terminal_reason"),
            })
    summary = {
        "unique_candidates": len(items), "horizons": {}, "comparison_allowed": False,
        "warning": "Not a matched approved/rejected outcome study. Net cost is a shadow proxy, not actual PnL or proven alpha."
    }
    for horizon in HORIZONS:
        sample = [row for row in rows if row["horizon"] == horizon]
        gross = [row["gross_return"] for row in sample if row["verified"]]
        net = [row["illustrative_net_return"] for row in sample
               if row["illustrative_net_return"] is not None]
        summary["horizons"][str(horizon)] = {
            "total": len(sample), "verified_gross": len(gross),
            "net_proxy_covered": len(net),
            "mean_verified_gross": sum(gross) / len(gross) if gross else None,
            "mean_net_proxy_covered": sum(net) / len(net) if net else None,
            "missing": dict(Counter(row["missing_reason"] for row in sample if row["missing_reason"])),
        }
    return rows, summary

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Railway JSONL export or normalized JSONL")
    parser.add_argument("--as-of", required=True, help="Fixed ISO8601 UTC timestamp")
    parser.add_argument("--prefix", required=True, type=Path)
    args = parser.parse_args()
    timestamp = utc_epoch(args.as_of)
    if timestamp is None:
        parser.error("--as-of requires explicit timezone (e.g. 2026-10-08T16:30:00Z)")
    data = []
    for line_no, line in enumerate(args.input.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            data.append(json.loads(line))
        except json.JSONDecodeError as exc:
            parser.error(f"Invalid JSON at line {line_no}: {exc}")
    rows, summary = audit(data, timestamp)
    args.prefix.parent.mkdir(parents=True, exist_ok=True)
    with args.prefix.with_suffix(".csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    args.prefix.with_suffix(".json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8")

if __name__ == "__main__":
    main()
