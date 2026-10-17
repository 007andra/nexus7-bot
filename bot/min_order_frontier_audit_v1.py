"""MIN_ORDER Frontier Audit V1 over the active HARD_GATE_SHADOW risk epoch.

RESEARCH ONLY. This module never sizes, authorizes, dispatches, submits, cancels,
protects or mutates a LIVE position/order. It reads append-only research rows
and reconstructs the minimum-order stop-width frontier from evidence already
captured with each candidate.
"""
from __future__ import annotations

import json
import math
import os
import statistics

POPULATION = "HARD_GATE_SHADOW"
NEAR_FEASIBLE_RATIO = 1.25
AUTHORITY = {
    "research_only": True,
    "observability_only": True,
    "shadow_only": True,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
    "live_authority_unchanged": True,
}


def enabled() -> bool:
    return os.environ.get("MIN_ORDER_FRONTIER_AUDIT_V1", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _cost_terms(row):
    snap = row.get("cost_snapshot") if isinstance(row.get("cost_snapshot"), dict) else {}
    fee = _finite(snap.get("taker_fee"))
    entry_slip = _finite(snap.get("entry_slippage"))
    exit_slip = _finite(snap.get("exit_slippage"))
    if fee is None or entry_slip is None or exit_slip is None:
        return None, None
    configured = _finite(os.environ.get("NEXUS_EXPECTED_SLIPPAGE_PCT", "0.001"))
    configured = 0.001 if configured is None or configured < 0 else configured
    return fee, max(configured, entry_slip + exit_slip)


def classify_candidate(row):
    """Return a detached, authority-neutral frontier record."""
    entry = _finite(row.get("entry"))
    stop = _finite(row.get("stop"))
    risk_pct = _finite(row.get("counterfactual_risk_pct"))
    risk_budget = _finite(row.get("risk_budget"))
    min_qty = _finite(row.get("min_valid_qty"))
    risk_at_min = _finite(row.get("risk_at_min_qty"))
    feasible = row.get("shadow_min_order_feasible")
    binding = str(row.get("binding") or row.get("min_order_binding") or "UNKNOWN")
    fee, slippage = _cost_terms(row)

    out = {
        **AUTHORITY,
        "candidate_id": str(row.get("candidate_id") or "UNKNOWN"),
        "captured_epoch": _finite(row.get("captured_epoch")),
        "symbol": str(row.get("symbol") or "UNKNOWN"),
        "side": str(row.get("side") or "UNKNOWN").upper(),
        "setup": str(row.get("setup") or "UNKNOWN"),
        "regime": str(row.get("regime") or "UNKNOWN"),
        "entry": entry,
        "stop": stop,
        "risk_pct": risk_pct,
        "risk_budget": risk_budget,
        "min_valid_qty": min_qty,
        "risk_at_min_qty": risk_at_min,
        "binding": binding,
        "capital_source": row.get("capital_source"),
        "shadow_min_order_feasible": feasible,
        "margin_at_min_qty": _finite(row.get("margin_at_min_qty")),
        "margin_cap": _finite(row.get("margin_cap")),
        "required_equity_at_min_qty": _finite(row.get("required_equity_at_min_qty")),
        "actual_stop_pct": None,
        "max_stop_pct": None,
        "stop_gap_pct": None,
        "stop_gap_bps": None,
        "required_stop_reduction_pct_of_current": None,
        "risk_gap_usdt": None,
        "required_risk_pct": None,
        "would_pass_if_stop_narrowed": False,
        "classification": "UNKNOWN",
        "classification_reason": "INSUFFICIENT_EVIDENCE",
        "near_feasible_ratio": NEAR_FEASIBLE_RATIO,
    }

    if entry is None or stop is None or entry <= 0 or stop <= 0:
        out["classification_reason"] = "INVALID_GEOMETRY"
        return out

    actual_stop_pct = abs(entry - stop) / entry * 100.0
    out["actual_stop_pct"] = actual_stop_pct

    if risk_budget is not None and risk_at_min is not None:
        out["risk_gap_usdt"] = risk_at_min - risk_budget
    if risk_pct is not None and risk_budget and risk_budget > 0 and risk_at_min is not None:
        implied_equity = risk_budget / risk_pct if risk_pct > 0 else None
        if implied_equity and implied_equity > 0:
            out["required_risk_pct"] = risk_at_min / implied_equity

    if feasible is True:
        out["classification"] = "FEASIBLE"
        out["classification_reason"] = "MIN_ORDER_PASS"
    elif row.get("capital_source") in (None, "UNCONFIRMED"):
        out["classification_reason"] = "CAPITAL_UNCONFIRMED"
        return out

    if None in (risk_budget, min_qty, fee, slippage) or min_qty <= 0:
        if feasible is False:
            out["classification"] = "STRUCTURALLY_BLOCKED"
            out["classification_reason"] = binding or "MIN_ORDER_BLOCKED"
        return out

    fixed_cost_per_unit = entry * (2.0 * fee + slippage)
    max_loss_per_unit = risk_budget / min_qty
    max_stop_abs = max_loss_per_unit - fixed_cost_per_unit
    max_stop_pct = max(0.0, max_stop_abs / entry * 100.0)
    gap = actual_stop_pct - max_stop_pct
    out["max_stop_pct"] = max_stop_pct
    out["stop_gap_pct"] = gap
    out["stop_gap_bps"] = gap * 100.0

    if actual_stop_pct > 0 and gap > 0:
        out["required_stop_reduction_pct_of_current"] = gap / actual_stop_pct * 100.0

    if feasible is True:
        out["would_pass_if_stop_narrowed"] = False
        return out

    if max_stop_pct <= 0:
        out["classification"] = "STRUCTURALLY_BLOCKED"
        out["classification_reason"] = "FIXED_COST_FLOOR_EXCEEDS_RISK_BUDGET"
        return out

    out["would_pass_if_stop_narrowed"] = actual_stop_pct > max_stop_pct
    if actual_stop_pct <= max_stop_pct:
        # Defensive mismatch detector: persisted fail with reconstructed PASS frontier.
        out["classification"] = "UNKNOWN"
        out["classification_reason"] = "PERSISTED_RESULT_FRONTIER_MISMATCH"
    elif actual_stop_pct <= max_stop_pct * NEAR_FEASIBLE_RATIO:
        out["classification"] = "NEAR_FEASIBLE"
        out["classification_reason"] = "STOP_WIDTH_WITHIN_25_PERCENT_OF_FRONTIER"
    else:
        out["classification"] = "STRUCTURALLY_BLOCKED"
        out["classification_reason"] = "STOP_WIDTH_ABOVE_FRONTIER"
    return out


def _stats(values):
    xs = [float(v) for v in values if _finite(v) is not None]
    if not xs:
        return {"n": 0, "mean": None, "min": None, "max": None, "median": None}
    return {
        "n": len(xs),
        "mean": statistics.fmean(xs),
        "min": min(xs),
        "max": max(xs),
        "median": statistics.median(xs),
    }


def build_report(rows, *, epoch_id="UNKNOWN", started_epoch=None):
    records = [classify_candidate(row) for row in rows]
    counts = {"FEASIBLE": 0, "NEAR_FEASIBLE": 0, "STRUCTURALLY_BLOCKED": 0, "UNKNOWN": 0}
    bindings = {}
    reasons = {}
    for row in records:
        counts[row["classification"]] = counts.get(row["classification"], 0) + 1
        bindings[row["binding"]] = bindings.get(row["binding"], 0) + 1
        reason = row["classification_reason"]
        reasons[reason] = reasons.get(reason, 0) + 1

    narrowed = sum(1 for row in records if row["would_pass_if_stop_narrowed"])
    exact_margin = sum(
        1 for row in records
        if row["margin_at_min_qty"] is not None and row["margin_cap"] is not None
    )
    return {
        **AUTHORITY,
        "epoch_id": epoch_id,
        "started_epoch": started_epoch,
        "status": "COLLECTING" if records else "NO_CANDIDATES",
        "candidates": len(records),
        "feasible": counts["FEASIBLE"],
        "near_feasible": counts["NEAR_FEASIBLE"],
        "structurally_blocked": counts["STRUCTURALLY_BLOCKED"],
        "unknown": counts["UNKNOWN"],
        "would_pass_if_stop_narrowed": narrowed,
        "exact_margin_context_candidates": exact_margin,
        "binding_counts": bindings,
        "reason_counts": reasons,
        "actual_stop_pct": _stats(r["actual_stop_pct"] for r in records),
        "max_stop_pct": _stats(r["max_stop_pct"] for r in records),
        "stop_gap_pct": _stats(r["stop_gap_pct"] for r in records),
        "required_stop_reduction_pct": _stats(
            r["required_stop_reduction_pct_of_current"] for r in records
        ),
        "risk_gap_usdt": _stats(r["risk_gap_usdt"] for r in records),
        "required_risk_pct": _stats(r["required_risk_pct"] for r in records),
        "records": records,
    }


async def snapshot(db):
    from bot import risk_epoch_shadow

    eid = risk_epoch_shadow.epoch_id()
    meta = await db._fetchall(
        "SELECT payload FROM risk_epoch_shadow_v1 WHERE epoch_id=?",
        (eid,),
    )
    if not meta:
        return build_report([], epoch_id=eid)
    raw = meta[0]["payload"] if hasattr(meta[0], "keys") else meta[0][0]
    baseline = json.loads(raw)
    started = float(baseline["started_epoch"])
    rows = await db._fetchall(
        "SELECT payload FROM hard_gate_shadow_candidates_v1 "
        "WHERE population=? AND captured_epoch>=? ORDER BY captured_epoch",
        (POPULATION, started),
    )
    payloads = []
    for item in rows or []:
        raw = item["payload"] if hasattr(item, "keys") else item[0]
        try:
            payloads.append(json.loads(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return build_report(payloads, epoch_id=eid, started_epoch=started)


def format_summary(report):
    def f(value, digits=4):
        return "NA" if value is None else f"{float(value):.{digits}f}"
    return (
        "[MIN_ORDER_FRONTIER_AUDIT_V1] "
        f"epoch_id={report['epoch_id']} status={report['status']} "
        f"candidates={report['candidates']} feasible={report['feasible']} "
        f"near_feasible={report['near_feasible']} "
        f"structurally_blocked={report['structurally_blocked']} unknown={report['unknown']} "
        f"would_pass_if_stop_narrowed={report['would_pass_if_stop_narrowed']} "
        f"exact_margin_context={report['exact_margin_context_candidates']} "
        f"stop_gap_mean_pct={f(report['stop_gap_pct']['mean'])} "
        f"required_stop_reduction_mean_pct={f(report['required_stop_reduction_pct']['mean'])} "
        f"risk_gap_mean_usdt={f(report['risk_gap_usdt']['mean'], 6)} "
        "promotion_allowed=false live_allowed=false decision_effect=NONE execution_effect=NONE"
    )


__all__ = [
    "AUTHORITY", "NEAR_FEASIBLE_RATIO", "build_report", "classify_candidate",
    "enabled", "format_summary", "snapshot",
]
