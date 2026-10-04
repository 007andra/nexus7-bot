"""Min-Order Universe Efficiency V1.

Research-only observability that combines:
1) the current cache-only minimum-order feasibility envelope per configured symbol; and
2) active Risk Epoch candidate frontier evidence.

It never changes viable_symbols, scores, strategy stops, risk, leverage, sizing,
dispatch, orders, positions, protection, recovery, override, or LIVE authority.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
import math
import os
import statistics

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
    return os.environ.get(
        "MIN_ORDER_UNIVERSE_EFFICIENCY_V1", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        value = float(value)
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _mean(values):
    xs = [float(v) for v in values if _finite(v) is not None]
    return statistics.fmean(xs) if xs else None


def _median(values):
    xs = [float(v) for v in values if _finite(v) is not None]
    return statistics.median(xs) if xs else None


def _frontier_ratio(record):
    actual = _finite(record.get("actual_stop_pct"))
    maximum = _finite(record.get("max_stop_pct"))
    if actual is None or maximum is None or actual <= 0:
        return None
    return maximum / actual


def _observed_class(records):
    if not records:
        return "NO_ACTIVE_CANDIDATE"
    classes = {str(r.get("classification") or "UNKNOWN") for r in records}
    if "FEASIBLE" in classes:
        return "CAPITAL_COMPATIBLE_OBSERVED"
    if all(
        _finite(r.get("max_stop_pct")) is not None
        and float(r.get("max_stop_pct") or 0) <= 0
        for r in records
    ):
        return "COST_BLOCK"
    if any(c == "NEAR_FEASIBLE" for c in classes):
        return "STOP_WIDTH_BLOCK_NEAR"
    if any(
        _finite(r.get("max_stop_pct")) is not None
        and float(r.get("max_stop_pct") or 0) > 0
        for r in records
    ):
        return "STOP_WIDTH_BLOCK"
    return "UNKNOWN"


def _final_class(universe_status, observed_class):
    universe_status = str(universe_status or "UNAVAILABLE").upper()
    if observed_class == "CAPITAL_COMPATIBLE_OBSERVED":
        return observed_class
    if observed_class in {"STOP_WIDTH_BLOCK", "STOP_WIDTH_BLOCK_NEAR", "COST_BLOCK"}:
        return observed_class
    if universe_status == "COST_BLOCK":
        return "COST_BLOCK"
    if universe_status == "MARGIN_BLOCK":
        return "MARGIN_BLOCK"
    if universe_status == "CONDITIONAL":
        return "CONDITIONAL_NO_ACTIVE_CANDIDATE"
    return "UNAVAILABLE"


def _normalize_universe_row(row):
    keys = (
        "symbol", "status", "binding", "price", "min_qty", "min_notional",
        "min_valid_qty", "min_order_notional", "risk_budget", "margin_at_min",
        "margin_cap", "max_stop_pct", "fee_rate_per_side", "slippage_pct",
    )
    out = {}
    for key in keys:
        value = row.get(key)
        if key in {"symbol", "status", "binding"}:
            out[key] = value
        else:
            out[key] = _finite(value)
    return out


def _setup_group(records):
    ratios = [_frontier_ratio(r) for r in records]
    classes = [str(r.get("classification") or "UNKNOWN") for r in records]
    observed = _observed_class(records)
    return {
        "symbol": str(records[0].get("symbol") or "UNKNOWN"),
        "setup": str(records[0].get("setup") or "UNKNOWN"),
        "candidates": len(records),
        "observed_class": observed,
        "feasible": sum(c == "FEASIBLE" for c in classes),
        "near_feasible": sum(c == "NEAR_FEASIBLE" for c in classes),
        "structurally_blocked": sum(c == "STRUCTURALLY_BLOCKED" for c in classes),
        "unknown": sum(c == "UNKNOWN" for c in classes),
        "median_actual_stop_pct": _median(r.get("actual_stop_pct") for r in records),
        "median_max_stop_pct": _median(r.get("max_stop_pct") for r in records),
        "median_frontier_ratio": _median(ratios),
        "mean_stop_gap_pct": _mean(r.get("stop_gap_pct") for r in records),
        "mean_risk_gap_usdt": _mean(r.get("risk_gap_usdt") for r in records),
        "would_pass_if_stop_narrowed": sum(
            r.get("would_pass_if_stop_narrowed") is True for r in records
        ),
    }


def build_report(universe_rows, frontier_report):
    records = list(frontier_report.get("records") or [])
    by_symbol = defaultdict(list)
    by_setup = defaultdict(list)
    for record in records:
        symbol = str(record.get("symbol") or "UNKNOWN")
        setup = str(record.get("setup") or "UNKNOWN")
        by_symbol[symbol].append(record)
        by_setup[(symbol, setup)].append(record)

    universe = [_normalize_universe_row(row) for row in universe_rows]
    symbol_rows = []
    for row in universe:
        symbol = str(row.get("symbol") or "UNKNOWN")
        observed_records = by_symbol.get(symbol, [])
        observed = _observed_class(observed_records)
        ratios = [_frontier_ratio(r) for r in observed_records]
        symbol_rows.append({
            **row,
            "active_candidates": len(observed_records),
            "observed_class": observed,
            "efficiency_class": _final_class(row.get("status"), observed),
            "feasible_candidates": sum(
                r.get("classification") == "FEASIBLE" for r in observed_records
            ),
            "near_feasible_candidates": sum(
                r.get("classification") == "NEAR_FEASIBLE" for r in observed_records
            ),
            "stop_width_block_candidates": sum(
                _finite(r.get("max_stop_pct")) is not None
                and float(r.get("max_stop_pct") or 0) > 0
                and r.get("classification") != "FEASIBLE"
                for r in observed_records
            ),
            "cost_block_candidates": sum(
                _finite(r.get("max_stop_pct")) is not None
                and float(r.get("max_stop_pct") or 0) <= 0
                for r in observed_records
            ),
            "median_frontier_ratio": _median(ratios),
            "median_actual_stop_pct": _median(
                r.get("actual_stop_pct") for r in observed_records
            ),
            "median_candidate_max_stop_pct": _median(
                r.get("max_stop_pct") for r in observed_records
            ),
        })

    setup_rows = [
        _setup_group(group)
        for _, group in sorted(by_setup.items())
    ]

    rank_order = {
        "CAPITAL_COMPATIBLE_OBSERVED": 0,
        "STOP_WIDTH_BLOCK_NEAR": 1,
        "STOP_WIDTH_BLOCK": 2,
        "CONDITIONAL_NO_ACTIVE_CANDIDATE": 3,
        "COST_BLOCK": 4,
        "MARGIN_BLOCK": 5,
        "UNAVAILABLE": 6,
    }
    symbol_rows.sort(
        key=lambda r: (
            rank_order.get(r["efficiency_class"], 99),
            -(_finite(r.get("median_frontier_ratio")) or -1),
            -(_finite(r.get("max_stop_pct")) or -1),
            str(r.get("symbol")),
        )
    )
    setup_rows.sort(
        key=lambda r: (
            rank_order.get(r["observed_class"], 99),
            -(_finite(r.get("median_frontier_ratio")) or -1),
            str(r.get("symbol")),
            str(r.get("setup")),
        )
    )

    universe_status_counts = defaultdict(int)
    efficiency_counts = defaultdict(int)
    for row in symbol_rows:
        universe_status_counts[str(row.get("status") or "UNAVAILABLE")] += 1
        efficiency_counts[row["efficiency_class"]] += 1

    return {
        **AUTHORITY,
        "epoch_id": frontier_report.get("epoch_id"),
        "started_epoch": frontier_report.get("started_epoch"),
        "status": "COLLECTING" if symbol_rows else "NO_UNIVERSE",
        "universe_symbols": len(symbol_rows),
        "active_epoch_candidates": len(records),
        "active_candidate_symbols": len(by_symbol),
        "active_setup_groups": len(setup_rows),
        "universe_status_counts": dict(sorted(universe_status_counts.items())),
        "efficiency_counts": dict(sorted(efficiency_counts.items())),
        "frontier_feasible_candidates": frontier_report.get("feasible", 0),
        "frontier_near_feasible_candidates": frontier_report.get("near_feasible", 0),
        "frontier_structurally_blocked_candidates": frontier_report.get(
            "structurally_blocked", 0
        ),
        "frontier_unknown_candidates": frontier_report.get("unknown", 0),
        "symbol_rows": symbol_rows,
        "setup_rows": setup_rows,
    }


def _cached_price_map(engine):
    from bot.config import cfg

    prices = {}
    for symbol in cfg.SYMBOLS:
        try:
            ticker = engine.client.get_cached_ticker(symbol) or {}
            price = _finite(ticker.get("lastPrice"))
            if price is not None and price > 0:
                prices[symbol] = price
        except Exception:
            continue
    return prices


def _counterfactual_universe_rows(engine, price_map):
    """Mirror HARD_GATE_SHADOW normal configured risk, never LIVE recovery risk."""
    from bot import execution_cost
    from bot.config import cfg
    from bot import min_order_feasibility_matrix as matrix

    equity = _finite(getattr(getattr(engine, "risk", None), "balance", None))
    if equity is None or equity <= 0:
        raise ValueError("counterfactual equity unavailable")

    # Exact policy used by hard_gate_shadow_scan.counterfactual_min_order:
    # normal configured hypothetical risk, explicitly NOT recovery-adjusted
    # LIVE sizing. The authoritative drawdown hard gate remains untouched.
    risk_pct = float(
        cfg.POST_TARGET_RISK
        if getattr(engine, "daily_target_hit", False)
        else cfg.MAX_RISK_PCT
    )
    if not math.isfinite(risk_pct) or not 0 < risk_pct <= 1:
        raise ValueError("invalid counterfactual risk pct")

    equity_d = Decimal(str(equity))
    available = matrix.available_collateral(engine, equity_d)
    fee = execution_cost.fallback_taker_fee()
    slippage = os.environ.get("NEXUS_EXPECTED_SLIPPAGE_PCT", "0.001")

    rows = []
    for symbol in cfg.SYMBOLS:
        info = (getattr(engine, "instruments", {}) or {}).get(symbol)
        price = price_map.get(symbol)
        if not info or price is None or float(price) <= 0:
            rows.append({"symbol": symbol, "status": "UNAVAILABLE"})
            continue
        row = matrix.audit_symbol(
            info=info,
            price=price,
            equity=equity_d,
            available=available,
            risk_pct=risk_pct,
            leverage=cfg.LEVERAGE,
            max_margin_pct=getattr(cfg, "MAX_MARGIN_PCT", 0.80),
            fee_rate_per_side=fee,
            slippage_pct=slippage,
        )
        row["symbol"] = symbol
        row["price"] = Decimal(str(price))
        rows.append(row)
    return rows


async def snapshot(db, engine):
    from bot import min_order_frontier_audit_v1 as frontier

    frontier_report = await frontier.snapshot(db)
    price_map = _cached_price_map(engine)
    universe_rows = _counterfactual_universe_rows(engine, price_map)
    return build_report(universe_rows, frontier_report)


def _fmt(value, digits=4):
    return "NA" if value is None else f"{float(value):.{digits}f}"


def format_summary(report):
    u = report["universe_status_counts"]
    e = report["efficiency_counts"]
    return (
        "[MIN_ORDER_UNIVERSE_EFFICIENCY_V1] "
        f"epoch_id={report.get('epoch_id') or 'NA'} status={report['status']} "
        f"universe_symbols={report['universe_symbols']} "
        f"active_epoch_candidates={report['active_epoch_candidates']} "
        f"active_candidate_symbols={report['active_candidate_symbols']} "
        f"active_setup_groups={report['active_setup_groups']} "
        f"conditional={u.get('CONDITIONAL', 0)} cost_block={u.get('COST_BLOCK', 0)} "
        f"margin_block={u.get('MARGIN_BLOCK', 0)} unavailable={u.get('UNAVAILABLE', 0)} "
        f"capital_compatible_observed={e.get('CAPITAL_COMPATIBLE_OBSERVED', 0)} "
        f"stop_width_block={e.get('STOP_WIDTH_BLOCK', 0)} "
        f"stop_width_block_near={e.get('STOP_WIDTH_BLOCK_NEAR', 0)} "
        f"conditional_no_active_candidate={e.get('CONDITIONAL_NO_ACTIVE_CANDIDATE', 0)} "
        f"observed_cost_block={e.get('COST_BLOCK', 0)} "
        "promotion_allowed=false live_allowed=false decision_effect=NONE execution_effect=NONE"
    )


def format_top_symbols(report, limit=8):
    rows = report["symbol_rows"][:max(1, int(limit))]
    parts = []
    for row in rows:
        parts.append(
            f"{row['symbol']}:{row['efficiency_class']}:"
            f"max_stop={_fmt(row.get('max_stop_pct'), 4)}%:"
            f"candidates={row['active_candidates']}:"
            f"frontier_ratio={_fmt(row.get('median_frontier_ratio'), 3)}"
        )
    return (
        "[MIN_ORDER_UNIVERSE_EFFICIENCY_V1_TOP_SYMBOLS] "
        + "|".join(parts)
        + " observability_only=true decision_effect=NONE execution_effect=NONE"
    )


def format_top_setups(report, limit=8):
    rows = report["setup_rows"][:max(1, int(limit))]
    parts = []
    for row in rows:
        parts.append(
            f"{row['symbol']}/{row['setup']}:{row['observed_class']}:"
            f"n={row['candidates']}:"
            f"frontier_ratio={_fmt(row.get('median_frontier_ratio'), 3)}:"
            f"stop_gap={_fmt(row.get('mean_stop_gap_pct'), 4)}%"
        )
    return (
        "[MIN_ORDER_UNIVERSE_EFFICIENCY_V1_TOP_SETUPS] "
        + ("|".join(parts) if parts else "NONE")
        + " observability_only=true decision_effect=NONE execution_effect=NONE"
    )


__all__ = [
    "AUTHORITY", "build_report", "enabled", "format_summary",
    "format_top_setups", "format_top_symbols", "snapshot",
]
