"""Conservative BINANCE USD-M OHLC-path replay; research only.

Consumes operator-provided *historical* 15m closed candles as JSONL, NEVER
connects to exchange/DB, NEVER submits orders or changes LIVE configuration.
Does not simulate partial TP, trailing, forced liquidation or funding.
All execution prices are hypothetical and may diverge from actual fills.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

HORIZONS = (60, 240)
STEP = 900
FIELDS = (
    "candidate_id", "symbol", "side", "regime", "setup", "cohort_decision",
    "captured_epoch", "horizon", "status", "exit_reason",
    "exit_candle_epoch", "both_levels_touched", "entry_reference", "exit_reference",
    "return_gross_reference", "modeled_net_ex_funding", "modeled_fee_fraction",
    "modeled_slippage_cost", "cost_state", "entry_fill_assumption",
    "funding_status", "risk_fraction", "net_R_ex_funding", "is_executed_pnl",
)


def finite(v):
    try:
        if v is None or isinstance(v, bool):
            return None
        n = float(v)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _timestamp(v):
    n = finite(v)
    if n is None:
        return None
    return n / 1000.0 if n >= 1e11 else n


def _candle(raw):
    if not isinstance(raw, dict):
        raise ValueError("CANDLE_NOT_OBJECT")
    ts = _timestamp(raw.get("ts"))
    vals = [finite(raw.get(k)) for k in ("o", "h", "l", "c")]
    if (ts is None or ts < 0 or int(ts) != ts or ts % STEP != 0
            or any(x is None or x <= 0 for x in vals)):
        raise ValueError("CANDLE_INVALID")
    op, hi, lo, close = vals
    if lo > min(op, close) or hi < max(op, close) or hi < lo:
        raise ValueError("CANDLE_OHLC_INCONSISTENT")
    return (int(ts), op, hi, lo, close)


def _cost(candidate):
    s = candidate.get("cost_snapshot")
    if not isinstance(s, dict):
        return None, "MISSING_POINT_IN_TIME_COST"
    epoch = finite(candidate.get("captured_epoch"))
    observed = finite(s.get("observed_at"))
    if (epoch is None or observed is None or observed > epoch
            or epoch - observed > 120
            or s.get("candidate_id") != candidate.get("candidate_id")
            or s.get("symbol") != candidate.get("symbol")
            or str(s.get("exchange", "")).upper() not in ("BINANCE", "BINANCE_USDM", "BINANCE_USD_M")):
        return None, "COST_NOT_POINT_IN_TIME"
    rates = tuple(finite(s.get(k)) for k in ("taker_fee", "entry_slippage", "exit_slippage"))
    if any(x is None or x < 0 or x >= 0.05 for x in rates):
        return None, "COST_RATES_UNVERIFIED"
    return rates, "MODEL_COST_AVAILABLE"


def _base(candidate):
    cid = candidate.get("candidate_id")
    side = candidate.get("side")
    entry = finite(candidate.get("entry"))
    stop = finite(candidate.get("stop"))
    target = finite(candidate.get("target"))
    cap = finite(candidate.get("captured_epoch"))
    if (not isinstance(cid, str) or not cid or side not in ("LONG", "SHORT")
            or not isinstance(candidate.get("symbol"), str)
            or not candidate["symbol"] or cap is None
            or any(x is None or x <= 0 for x in (entry, stop, target))):
        raise ValueError("INVALID_CANDIDATE")
    if side == "LONG" and not (stop < entry < target):
        raise ValueError("INVALID_LONG_LEVELS")
    if side == "SHORT" and not (target < entry < stop):
        raise ValueError("INVALID_SHORT_LEVELS")
    return cid, side, entry, stop, target, cap


def _simulate_path(side, entry, stop, target, candles):
    sign = 1 if side == "LONG" else -1
    for ts, op, hi, lo, close in candles:
        stop_hit = lo <= stop if sign == 1 else hi >= stop
        target_hit = hi >= target if sign == 1 else lo <= target
        if stop_hit:
            # A stop-market can gap adversely past the stop; never credit a better fill.
            gap = min(op, stop) if sign == 1 else max(op, stop)
            reason = "AMBIGUOUS_STOP_FIRST" if target_hit else (
                "STOP_GAP" if gap != stop else "STOP")
            return ts, gap, reason, stop_hit and target_hit
        if target_hit:
            # Do not credit favorable open-price improvement over the target.
            return ts, target, "TARGET", False
    return candles[-1][0], candles[-1][4], "HORIZON_CLOSE", False


def evaluate(candidate, *, as_of_epoch):
    cid, side, entry, stop, target, captured = _base(candidate)
    as_of = finite(as_of_epoch)
    if as_of is None:
        raise ValueError("AS_OF_REQUIRED")
    start = math.ceil(captured / STEP) * STEP
    raw_bars = candidate.get("bars")
    if not isinstance(raw_bars, list):
        raise ValueError("CANDLES_REQUIRED")
    mapping = {}
    for raw in raw_bars:
        candle = _candle(raw)
        if candle[0] in mapping:
            raise ValueError("DUPLICATE_CANDLE_TIMESTAMP")
        mapping[candle[0]] = candle
    costs, cost_state = _cost(candidate)
    sign = 1 if side == "LONG" else -1
    risk_frac = abs(entry - stop) / entry
    result = []
    for horizon in HORIZONS:
        baseline = {
            "candidate_id": cid, "symbol": candidate["symbol"], "side": side,
            "regime": candidate.get("regime"), "setup": candidate.get("setup"),
            "cohort_decision": candidate.get("cohort_decision", "UNKNOWN"),
            "captured_epoch": captured,
            "horizon": horizon, "status": None, "exit_reason": None,
            "exit_candle_epoch": None, "both_levels_touched": False,
            "entry_reference": entry, "exit_reference": None,
            "return_gross_reference": None, "modeled_net_ex_funding": None,
            "modeled_fee_fraction": None, "modeled_slippage_cost": None,
            "cost_state": cost_state, "entry_fill_assumption": "CAPTURED_ENTRY_REFERENCE_NO_FILL",
            "funding_status": "NOT_MODELED", "risk_fraction": risk_frac,
            "net_R_ex_funding": None, "is_executed_pnl": False,
        }
        end = start + horizon * 60
        expected = list(range(int(start), int(end), STEP))
        if as_of < end:
            baseline["status"] = "NOT_MATURED"
            result.append(baseline)
            continue
        if any(ts not in mapping for ts in expected):
            baseline["status"] = "UNKNOWN_CANDLE_GAP"
            result.append(baseline)
            continue
        path = [mapping[ts] for ts in expected]
        exit_ts, exit_price, reason, ambiguous = _simulate_path(
            side, entry, stop, target, path)
        baseline.update({
            "status": "MODELED_GROSS_ONLY" if costs is None else "MODELED_NET_EX_FUNDING",
            "exit_reason": reason, "exit_candle_epoch": exit_ts,
            "both_levels_touched": ambiguous, "exit_reference": exit_price,
            "return_gross_reference": sign * (exit_price - entry) / entry,
        })
        if costs is not None:
            fee_rate, entry_slip, exit_slip = costs
            entry_fill = entry * (1 + sign * entry_slip)
            exit_fill = exit_price * (1 - sign * exit_slip)
            if entry_fill <= 0 or exit_fill <= 0:
                raise ValueError("MODELED_FILL_INVALID")
            gross_on_fill = sign * (exit_fill - entry_fill) / entry_fill
            fee_fraction = fee_rate * (1 + exit_fill / entry_fill)
            net = gross_on_fill - fee_fraction
            baseline.update({
                "modeled_net_ex_funding": net,
                "modeled_fee_fraction": fee_fraction,
                "modeled_slippage_cost": (
                    baseline["return_gross_reference"] - gross_on_fill),
                "net_R_ex_funding": net / risk_frac,
            })
        result.append(baseline)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jsonl", type=Path, help="JSONL with each candidate and historical 15m bars")
    parser.add_argument("--as-of-epoch", type=float, required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    seen = set()
    for idx, line in enumerate(args.jsonl.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        obj = json.loads(line)
        if not isinstance(obj, dict):
            parser.error(f"line {idx}: candidate JSON object required")
        cid = obj.get("candidate_id")
        if cid in seen:
            parser.error(f"line {idx}: duplicate candidate ID")
        seen.add(cid)
        rows.extend(evaluate(obj, as_of_epoch=args.as_of_epoch))
    args.prefix.parent.mkdir(parents=True, exist_ok=True)
    with args.prefix.with_suffix(".csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    stats = {"authority": "OFFLINE_RESEARCH_ONLY", "live_allowed": False,
             "realized_pnl_proven": False, "comparative_inference_allowed": False,
             "rows": len(rows), "statuses": {}, "descriptive_groups": {}}
    for state in sorted({r["status"] for r in rows}):
        stats["statuses"][state] = sum(r["status"] == state for r in rows)
    for h in HORIZONS:
        stats["descriptive_groups"][str(h)] = {}
        for decision in ("COUNTERFACTUAL_APPROVED", "COUNTERFACTUAL_REJECTED"):
            subset = [r for r in rows if r["horizon"] == h and r["cohort_decision"] == decision]
            modeled = [r["modeled_net_ex_funding"] for r in subset
                       if r["modeled_net_ex_funding"] is not None]
            gross = [r["return_gross_reference"] for r in subset
                     if r["return_gross_reference"] is not None]
            stats["descriptive_groups"][str(h)][decision] = {
                "enrolled": len(subset), "path_gross_available": len(gross),
                "path_cost_available": len(modeled),
                "gross_mean": sum(gross) / len(gross) if gross else None,
                "net_ex_funding_mean_covered_only": sum(modeled) / len(modeled) if modeled else None,
            }
    args.prefix.with_suffix(".json").write_text(
        json.dumps(stats, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
