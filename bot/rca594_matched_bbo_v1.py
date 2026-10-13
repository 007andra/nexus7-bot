"""R3 same-candidate BBO cost overlap (exploratory, NEVER LIVE authority).

The already stored HARD_GATE_SHADOW candidate can contain its own
bbo_cost_only_observation. This module NEVER joins aggregate BBO-V3 means
to unrelated R3 candidates. Only a candidate-ID-exact, side/setup/regime-
consistent, still-fresh captured observation can be compared with that
same candidate's *original* cost snapshot and original 60m/240m outcome.

No REST, market feed, Binance key, DB query, scheduler or order mutation.
Even valid overlap is a model cost estimate, NOT executable net returns.
"""
from __future__ import annotations

import math

SETUPS = ("MOMENTUM", "BOS_BREAK", "PULLBACK", "OTHER")
MAX_BOOK_AGE_MS = 2_000
MAX_COST_BPS = 1000.0
AUTHORITY = {
    "research_only": True,
    "source_observation_only": True,
    "hypothetical_cost_only": True,
    "matched_exact_candidate_only": True,
    "real_executable_net_proven": False,
    "funding_proven": False,
    "protected_fill_proven": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
}


def _number(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def captured_bbo_cost_fraction(raw, *, candidate_id, symbol, side, setup, regime):
    """Cost fraction of matching capture-time BBO, or None, never imputed."""
    if not isinstance(raw, dict) or raw.get("candidate_id") != candidate_id:
        return None
    bbo = raw.get("bbo_cost_only_observation")
    if not isinstance(bbo, dict):
        return None
    if any(bbo.get(k) != v for k, v in (
        ("cohort", "COST_ONLY"),
        ("candidate_id", candidate_id),
        ("symbol", symbol),
        ("side", side),
        ("setup", setup),
        ("regime", regime),
        ("shadow_only", True),
        ("live_eligible", False),
        ("decision_effect", "NONE"),
        ("execution_effect", "NONE"),
        ("bbo_valid", True),
        ("bbo_reason", "OK"),
    )):
        return None
    if (type(bbo.get("bbo_generation")) is not int
            or bbo["bbo_generation"] < 1
            or type(bbo.get("bbo_update_id")) is not int
            or bbo["bbo_update_id"] < 1):
        return None
    age = _number(bbo.get("bbo_age_ms"))
    bid, ask = _number(bbo.get("bid")), _number(bbo.get("ask"))
    spread = _number(bbo.get("spread_bps"))
    cost = _number(bbo.get("live_total_cost_bps"))
    if (age is None or not 0 <= age <= MAX_BOOK_AGE_MS
            or bid is None or ask is None or bid <= 0 or ask < bid
            or spread is None or spread < 0 or spread > MAX_COST_BPS
            or cost is None or not 0 <= cost <= MAX_COST_BPS):
        return None
    computed_spread = 10000.0 * (ask - bid) / ((bid + ask) / 2)
    if not math.isclose(spread, computed_spread, rel_tol=1e-5, abs_tol=1e-4):
        return None
    # A model cannot claim all-in cost smaller than its observable spread.
    if cost + 1e-6 < spread:
        return None
    return cost / 10000.0


def setup_coverage(rows):
    """Within the frozen matched-outcome arm, never drop unpriced members."""
    groups = {
        name: {arm: {
            "n": 0, "bbo_n": 0, "paired_n": 0,
            "bbo_proxy_sum": 0.0, "static_proxy_sum": 0.0,
        } for arm in ("approved", "rejected")}
        for name in SETUPS
    }
    for row in rows:
        if not isinstance(row, dict) or type(row.get("allowed")) is not bool:
            continue
        name = str(row.get("setup") or "").upper()
        name = name if name in groups else "OTHER"
        arm = "approved" if row["allowed"] else "rejected"
        block = groups[name][arm]
        block["n"] += 1
        ret = _number(row.get("future_return"))
        bbo = _number(row.get("matched_bbo_cost_fraction"))
        static = _number(row.get("cost_fraction"))
        if ret is None or bbo is None or bbo < 0 or bbo > 0.1:
            continue
        block["bbo_n"] += 1
        if static is None or static < 0 or static > 0.1:
            continue
        block["paired_n"] += 1
        block["bbo_proxy_sum"] += ret - bbo
        block["static_proxy_sum"] += ret - static
    return groups


def format_coverage(report, *, horizon):
    """Fixed vocabulary, redacted aggregate-only. Never claims real PnL."""
    if horizon not in (60, 240):
        return "matched_bbo=NOT_PROVEN"
    groups = report.get(f"setup_matched_bbo_{horizon}m")
    dims = report.get(f"dimensions_{horizon}m")
    dims = dims.get("setup") if isinstance(dims, dict) else None
    if not isinstance(groups, dict) or not isinstance(dims, list):
        return "matched_bbo=NOT_PROVEN"
    expected = {n: {"approved": 0, "rejected": 0} for n in SETUPS}
    try:
        for row in dims:
            name = str(row["value"]).upper()
            name = name if name in expected else "OTHER"
            for side, key in (("approved", "allowed_n"), ("rejected", "rejected_n")):
                n = row[key]
                if type(n) is not int or n < 0:
                    raise ValueError("invalid dimensions")
                expected[name][side] += n
        out = []
        for name in SETUPS:
            bits = []
            for arm, pre in (("approved", "a"), ("rejected", "r")):
                item = groups[name][arm]
                n, bn, pn = (item[k] for k in ("n", "bbo_n", "paired_n"))
                if (any(type(x) is not int for x in (n, bn, pn))
                        or n != expected[name][arm] or not 0 <= pn <= bn <= n):
                    raise ValueError("inconsistent identity coverage")
                s_bbo = _number(item["bbo_proxy_sum"])
                s_static = _number(item["static_proxy_sum"])
                if s_bbo is None or s_static is None:
                    raise ValueError("invalid proxy sum")
                bmean = s_bbo / pn if pn else None
                smean = s_static / pn if pn else None
                fmt = lambda v: "NA" if v is None else f"{v:.6f}"
                bits.append(
                    f"{pre}n={n}:{pre}bn={bn}:{pre}pn={pn}"
                    f":{pre}bm={fmt(bmean)}:{pre}sm={fmt(smean)}"
                )
            out.append(name + ":" + ":".join(bits))
    except (KeyError, TypeError, ValueError, OverflowError):
        return "matched_bbo=NOT_PROVEN"
    return (
        "matched_bbo=" + "|".join(out)
        + " same_candidate_only=true costs_are_models=true"
        + " executable_net_proven=false funding_proven=false"
        + " protected_fill_proven=false live_allowed=false execution_effect=NONE"
    )
