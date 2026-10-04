"""MIN_ORDER Capital Adequacy V1.

Research-only observability over the active HARD_GATE_SHADOW risk epoch.

The report answers one narrow engineering question: for candidates that passed
all pre-NEXUS gates except minimum-order sizing, what account equity would have
been required for the exchange-minimum quantity to fit the *same* configured
risk percentage and the *same* strategy stop/cost geometry?

It never recommends funding an account and never changes capital, risk, stop,
leverage, sizing, recovery, drawdown policy or LIVE authority.
"""
from __future__ import annotations

from collections import defaultdict
import json
import math
import os
import statistics

POPULATION = "HARD_GATE_SHADOW"
FLAG = "MIN_ORDER_CAPITAL_ADEQUACY_V1"

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
    return os.environ.get(FLAG, "false").strip().lower() in {"1", "true", "yes", "on"}


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _percentile(values, q):
    xs = sorted(float(v) for v in values if _finite(v) is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * float(q)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return xs[lo]
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def _stats(values):
    xs = [float(v) for v in values if _finite(v) is not None]
    if not xs:
        return {
            "n": 0, "mean": None, "min": None, "max": None,
            "median": None, "p95": None,
        }
    return {
        "n": len(xs),
        "mean": statistics.fmean(xs),
        "min": min(xs),
        "max": max(xs),
        "median": statistics.median(xs),
        "p95": _percentile(xs, 0.95),
    }


def _row(raw):
    from bot import min_order_frontier_audit_v1 as frontier

    rec = frontier.classify_candidate(raw)
    risk_pct = _finite(rec.get("risk_pct"))
    risk_budget = _finite(rec.get("risk_budget"))
    current_equity = (
        risk_budget / risk_pct
        if risk_budget is not None and risk_pct is not None and risk_pct > 0
        else None
    )

    required = _finite(rec.get("required_equity_at_min_qty"))
    if required is None:
        risk_at_min = _finite(rec.get("risk_at_min_qty"))
        if risk_at_min is not None and risk_pct is not None and risk_pct > 0:
            required = risk_at_min / risk_pct

    gap = (
        max(0.0, required - current_equity)
        if required is not None and current_equity is not None
        else None
    )
    multiple = (
        required / current_equity
        if required is not None and current_equity is not None and current_equity > 0
        else None
    )

    pipeline_before_min_order = (
        raw.get("pullback_pass") is True
        and raw.get("production_equivalent_funnel_result") is True
        and raw.get("capital_source") not in (None, "UNCONFIRMED")
    )
    min_order_only_block = (
        pipeline_before_min_order
        and raw.get("shadow_min_order_feasible") is not True
    )

    return {
        **AUTHORITY,
        "candidate_id": rec["candidate_id"],
        "captured_epoch": rec["captured_epoch"],
        "symbol": rec["symbol"],
        "side": rec["side"],
        "setup": rec["setup"],
        "regime": rec["regime"],
        "classification": rec["classification"],
        "classification_reason": rec["classification_reason"],
        "binding": rec["binding"],
        "actual_stop_pct": rec["actual_stop_pct"],
        "max_stop_pct": rec["max_stop_pct"],
        "risk_pct": risk_pct,
        "risk_budget": risk_budget,
        "risk_at_min_qty": _finite(rec.get("risk_at_min_qty")),
        "min_valid_qty": _finite(rec.get("min_valid_qty")),
        "current_equity": current_equity,
        "required_equity": required,
        "equity_gap": gap,
        "required_equity_multiple": multiple,
        "pullback_pass": raw.get("pullback_pass") is True,
        "funnel_pass": raw.get("production_equivalent_funnel_result") is True,
        "pipeline_before_min_order": pipeline_before_min_order,
        "min_order_only_block": min_order_only_block,
        "shadow_min_order_feasible": raw.get("shadow_min_order_feasible"),
    }


def _group(rows):
    reqs = [r["required_equity"] for r in rows]
    gaps = [r["equity_gap"] for r in rows]
    multiples = [r["required_equity_multiple"] for r in rows]
    return {
        "candidates": len(rows),
        "required_equity": _stats(reqs),
        "equity_gap": _stats(gaps),
        "required_equity_multiple": _stats(multiples),
        "median_actual_stop_pct": (
            statistics.median(
                [float(r["actual_stop_pct"]) for r in rows if _finite(r["actual_stop_pct"]) is not None]
            )
            if any(_finite(r["actual_stop_pct"]) is not None for r in rows) else None
        ),
    }


def build_report(payloads, *, epoch_id="UNKNOWN", started_epoch=None):
    records = [_row(raw) for raw in payloads]
    blocked = [
        r for r in records
        if r["min_order_only_block"] and r["required_equity"] is not None
    ]
    blocked.sort(
        key=lambda r: (
            float(r["required_equity"]),
            str(r["symbol"]),
            str(r["setup"]),
            str(r["candidate_id"]),
        )
    )

    by_symbol = defaultdict(list)
    by_setup = defaultdict(list)
    for row in blocked:
        by_symbol[row["symbol"]].append(row)
        by_setup[(row["symbol"], row["setup"])].append(row)

    symbol_rows = [
        {"symbol": symbol, **_group(rows)}
        for symbol, rows in sorted(by_symbol.items())
    ]
    setup_rows = [
        {"symbol": symbol, "setup": setup, **_group(rows)}
        for (symbol, setup), rows in sorted(by_setup.items())
    ]
    symbol_rows.sort(
        key=lambda r: (
            r["required_equity"]["min"]
            if r["required_equity"]["min"] is not None else float("inf"),
            r["symbol"],
        )
    )
    setup_rows.sort(
        key=lambda r: (
            r["required_equity"]["min"]
            if r["required_equity"]["min"] is not None else float("inf"),
            r["symbol"],
            r["setup"],
        )
    )

    current_values = [r["current_equity"] for r in records]
    current_equity = (
        statistics.median([float(v) for v in current_values if _finite(v) is not None])
        if any(_finite(v) is not None for v in current_values) else None
    )

    scenario_multiples = {}
    if current_equity is not None and current_equity > 0:
        for multiple in (1.5, 2.0, 3.0, 5.0, 10.0):
            limit = current_equity * multiple
            scenario_multiples[str(multiple)] = sum(
                1 for row in blocked
                if row["required_equity"] is not None
                and row["required_equity"] <= limit + 1e-12
            )

    closest = blocked[0] if blocked else None
    status = (
        "MIN_ORDER_ONLY_BLOCKERS_QUANTIFIED"
        if blocked else
        "AWAITING_PIPELINE_MIN_ORDER_ONLY_CANDIDATE"
    )
    return {
        **AUTHORITY,
        "epoch_id": epoch_id,
        "started_epoch": started_epoch,
        "status": status,
        "candidates": len(records),
        "pipeline_before_min_order": sum(r["pipeline_before_min_order"] for r in records),
        "min_order_only_blocked": len(blocked),
        "current_equity": current_equity,
        "required_equity": _stats(r["required_equity"] for r in blocked),
        "equity_gap": _stats(r["equity_gap"] for r in blocked),
        "required_equity_multiple": _stats(
            r["required_equity_multiple"] for r in blocked
        ),
        "closest_candidate": closest,
        "scenario_multiples": scenario_multiples,
        "symbol_rows": symbol_rows,
        "setup_rows": setup_rows,
        "records": records,
        "external_capital_does_not_clear_drawdown": True,
        "capital_metric_is_counterfactual_not_recommendation": True,
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


def _fmt(value, digits=4):
    return "NA" if value is None else f"{float(value):.{digits}f}"


def format_summary(report):
    closest = report.get("closest_candidate")
    closest_text = "NONE"
    if closest:
        closest_text = (
            f"{closest['symbol']}/{closest['setup']}"
            f":required_equity={_fmt(closest['required_equity'], 4)}"
            f":gap={_fmt(closest['equity_gap'], 4)}"
            f":multiple={_fmt(closest['required_equity_multiple'], 3)}x"
        )
    return (
        "[MIN_ORDER_CAPITAL_ADEQUACY_V1] "
        f"epoch_id={report['epoch_id']} status={report['status']} "
        f"candidates={report['candidates']} "
        f"pipeline_before_min_order={report['pipeline_before_min_order']} "
        f"min_order_only_blocked={report['min_order_only_blocked']} "
        f"current_equity={_fmt(report['current_equity'], 4)} "
        f"required_equity_min={_fmt(report['required_equity']['min'], 4)} "
        f"required_equity_median={_fmt(report['required_equity']['median'], 4)} "
        f"required_equity_p95={_fmt(report['required_equity']['p95'], 4)} "
        f"closest={closest_text} "
        "external_capital_does_not_clear_drawdown=true "
        "capital_metric_is_counterfactual_not_recommendation=true "
        "promotion_allowed=false live_allowed=false decision_effect=NONE execution_effect=NONE"
    )


def format_top_setups(report, limit=8):
    rows = list(report.get("setup_rows") or [])[: max(1, int(limit))]
    parts = []
    for row in rows:
        req = row["required_equity"]
        mult = row["required_equity_multiple"]
        parts.append(
            f"{row['symbol']}/{row['setup']}:n={row['candidates']}"
            f":min_req={_fmt(req['min'], 4)}"
            f":median_req={_fmt(req['median'], 4)}"
            f":min_multiple={_fmt(mult['min'], 3)}x"
        )
    return (
        "[MIN_ORDER_CAPITAL_ADEQUACY_V1_TOP_SETUPS] "
        + ("|".join(parts) if parts else "NONE")
        + " observability_only=true decision_effect=NONE execution_effect=NONE"
    )


__all__ = [
    "AUTHORITY", "FLAG", "build_report", "enabled", "format_summary",
    "format_top_setups", "snapshot",
]
