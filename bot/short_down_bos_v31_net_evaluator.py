"""V3.1 paired 60m/240m Binance USD-M NET evaluator (issue #658 execution contract).

Research ONLY. Pure functions over caller-supplied, already captured evidence:
no Binance session, no database, no clock, no scheduler, no order capability.
Enrollment is decided elsewhere (``short_down_bos_v31_readonly_enrollment`` +
manifest); this module never selects, drops or replaces candidates and never
looks at one horizon to decide the other.

Contract (#658, frozen):
* Decimal arithmetic, UTC milliseconds, deterministic adverse tick/step rounding.
* SHORT entry sells at a fresh contemporaneous bid, minus the declared adverse
  slippage floor; time exits buy at a fresh ask at exactly entry+60m/240m plus
  the floor; stops fill at max(stop, gap open) plus the floor; take profits
  fill no better than their price. Taker fee on every leg on its own notional.
* Path bars must be contiguous, closed and identical for both horizons. A bar
  that touches both stop and a take profit, a target touch inside the bar that
  contains the entry or straddles the horizon, or a funding event at the exact
  exit millisecond is INDETERMINATE: the favorable order is never chosen.
* Funding only from recorded settlement events (Regular/Special) with full
  coverage proof of the exposure interval; SHORT receives positive rates.
* Anything missing, stale, misordered, future-looking or unmodelled (trailing
  stops) is NOT_PROVEN. Neither status is ever counted as a zero or a win.

Statistics: diff = net_60 - net_240 (fractional). PASS needs 40 paired valid
rows, mean(diff) > 0.001, 2.5th percentile of 10,000 UTC-4h-cluster bootstrap
replicates (seed 59031012) > 0 with >= 10 clusters, mean(net_60) > 0,
share(net_60 > 0) >= 0.60, every leave-one-symbol-out mean(diff) > 0, >= 6
symbols and no symbol above 10. Even PASS is only
EVIDENCE_READY_FOR_MANUAL_REVIEW; LIVE and promotion stay forbidden.
"""
from __future__ import annotations

import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

PROTOCOL = "V31_BINANCE_USDM_PAIRED_60_240_NET"
HORIZONS_MIN = (60, 240)
TARGET = 40
MIN_SYMBOLS = 6
MAX_PER_SYMBOL = 10
MIN_CLUSTERS = 10
CLUSTER_MS = 4 * 3600 * 1000
BOOTSTRAP_REPLICATES = 10_000
BOOTSTRAP_SEED = 59031012
# Nearest-rank lower 2.5th percentile: the 250th smallest of 10,000 replicates.
BOOTSTRAP_LOWER_RANK = 250
MIN_MEAN_DIFF = Decimal("0.001")
MIN_POSITIVE_SHARE_60 = Decimal("0.60")
MAX_QUOTE_STALENESS_MS = 2_000
MAX_FRACTION = Decimal("0.05")  # fee/slippage sanity bound; larger is a data error
FEE_SOURCE = "BINANCE_USDM_COMMISSION_RATE"
FUNDING_TYPES = frozenset({"Regular", "Special"})
TP_TYPES = frozenset({"LIMIT", "TAKE_PROFIT_MARKET"})

PROVEN = "PROVEN"
INDETERMINATE = "INDETERMINATE"
NOT_PROVEN = "NOT_PROVEN"

AUTHORITY = {
    "research_only": True,
    "hypothetical_fills_only": True,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
}


class NotProven(ValueError):
    """Constant, non-sensitive reason code; never carries IDs or raw payloads."""


@dataclass(frozen=True)
class Bar:
    open_ms: int
    close_ms: int
    o: Decimal
    h: Decimal
    l: Decimal
    c: Decimal


def _dec(value, code: str) -> Decimal:
    if isinstance(value, (bool, float)) or value is None:
        # floats are refused: their binary value is not the exchange string
        raise NotProven(code)
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise NotProven(code) from None
    if not out.is_finite():
        raise NotProven(code)
    return out


def _geometry(value, code: str) -> Decimal:
    """Candidate prices come from captured JSON numbers; use their shortest repr."""
    if isinstance(value, float):
        value = repr(value)
    return _pos(value, code)


def _pos(value, code: str) -> Decimal:
    out = _dec(value, code)
    if out <= 0:
        raise NotProven(code)
    return out


def _fraction(value, code: str) -> Decimal:
    out = _dec(value, code)
    if out < 0 or out > MAX_FRACTION:
        raise NotProven(code)
    return out


def _ms(value, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise NotProven(code)
    return value


def _floor_to(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _ceil_to(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def _on_grid(value: Decimal, step: Decimal) -> bool:
    return (value / step) == (value / step).to_integral_value()


def _quote(raw, at_ms: int, code: str) -> tuple[Decimal, Decimal]:
    if not isinstance(raw, dict):
        raise NotProven(code)
    observed = _ms(raw.get("observed_ms"), code)
    # A quote timestamp after a modelled fill is future information, even
    # if its distance is inside the ordinary freshness tolerance.
    if observed > at_ms:
        raise NotProven(code + "_FUTURE")
    if at_ms - observed > MAX_QUOTE_STALENESS_MS:
        raise NotProven(code + "_STALE")
    bid, ask = _pos(raw.get("bid"), code), _pos(raw.get("ask"), code)
    if bid >= ask:
        raise NotProven(code + "_CROSSED")
    return bid, ask


def _bars(raw, entry_ms: int, end_ms: int, as_of_ms: int) -> list[Bar]:
    if not isinstance(raw, list) or not raw:
        raise NotProven("PATH_MISSING")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise NotProven("PATH_MALFORMED")
        bar = Bar(
            _ms(item.get("open_ms"), "PATH_MALFORMED"),
            _ms(item.get("close_ms"), "PATH_MALFORMED"),
            *(_pos(item.get(k), "PATH_MALFORMED") for k in ("o", "h", "l", "c")),
        )
        if bar.close_ms <= bar.open_ms or not (
            bar.l <= bar.o <= bar.h and bar.l <= bar.c <= bar.h
        ):
            raise NotProven("PATH_MALFORMED")
        if bar.close_ms > as_of_ms:
            raise NotProven("PATH_BAR_NOT_CLOSED")
        out.append(bar)
    width = out[0].close_ms - out[0].open_ms
    for prev, nxt in zip(out, out[1:]):
        if nxt.open_ms != prev.close_ms or nxt.close_ms - nxt.open_ms != width:
            raise NotProven("PATH_NOT_CONTIGUOUS")
    if not (out[0].open_ms <= entry_ms < out[0].close_ms):
        raise NotProven("PATH_DOES_NOT_START_AT_ENTRY")
    if out[-1].close_ms < end_ms or (len(out) > 1 and out[-2].close_ms >= end_ms):
        raise NotProven("PATH_DOES_NOT_END_AT_HORIZON")
    return out


def _plan(raw, entry: Decimal, tick: Decimal, step: Decimal, qty: Decimal):
    if not isinstance(raw, dict):
        raise NotProven("PROTECTION_PLAN_MISSING")
    if raw.get("trailing") is not None:
        # Live trailing is path-dependent intrabar; no reviewed OHLC model yet.
        raise NotProven("TRAILING_NOT_MODELED")
    stop = _pos(raw.get("stop"), "PROTECTION_PLAN_INVALID")
    tp_type = raw.get("tp_order_type")
    if tp_type not in TP_TYPES:
        raise NotProven("TP_TYPE_UNVERIFIED")
    if stop <= entry or not _on_grid(stop, tick):
        raise NotProven("PROTECTION_PLAN_INVALID")
    levels = []
    remaining = qty
    for lvl in raw.get("take_profits") or ():
        if not isinstance(lvl, dict):
            raise NotProven("PROTECTION_PLAN_INVALID")
        price = _pos(lvl.get("price"), "PROTECTION_PLAN_INVALID")
        part = _floor_to(qty * _pos(lvl.get("fraction"), "PROTECTION_PLAN_INVALID"), step)
        new_stop = lvl.get("stop_after")
        new_stop = None if new_stop is None else _pos(new_stop, "PROTECTION_PLAN_INVALID")
        if (price >= entry or not _on_grid(price, tick) or part <= 0
                or (levels and price >= levels[-1][0])
                or (new_stop is not None and not _on_grid(new_stop, tick))):
            raise NotProven("PROTECTION_PLAN_INVALID")
        part = min(part, remaining)
        remaining -= part
        levels.append((price, part, new_stop))
    if not levels:
        raise NotProven("PROTECTION_PLAN_INVALID")
    if remaining > 0:
        # the last take profit closes whatever step rounding left over
        price, part, new_stop = levels[-1]
        levels[-1] = (price, part + remaining, new_stop)
    return stop, tp_type, levels


def evaluate_candidate(candidate: dict, evidence: dict, *, as_of_ms: int) -> dict:
    """Both horizons from ONE shared path. Status PROVEN only if both are proven."""
    try:
        return {"status": PROVEN, **_evaluate(candidate, evidence, as_of_ms)}
    except _Indeterminate as exc:
        return {"status": INDETERMINATE, "reason": str(exc)}
    except NotProven as exc:
        return {"status": NOT_PROVEN, "reason": str(exc)}


class _Indeterminate(Exception):
    pass


def _evaluate(candidate: dict, ev: dict, as_of_ms: int) -> dict:
    if not isinstance(candidate, dict) or not isinstance(ev, dict):
        raise NotProven("EVIDENCE_MISSING")
    if ev.get("candidate_id") != candidate.get("candidate_id") or ev.get("symbol") != candidate.get("symbol"):
        raise NotProven("EVIDENCE_IDENTITY_MISMATCH")
    if ev.get("synthetic") is not False:
        raise NotProven("EVIDENCE_NOT_DECLARED_CAPTURED")
    signal_ms = _ms(candidate.get("captured_ms"), "CANDIDATE_TIME_INVALID")
    entry_ms = _ms(ev.get("entry_ms"), "ENTRY_TIME_INVALID")
    if entry_ms < signal_ms:
        raise NotProven("ENTRY_BEFORE_SIGNAL")
    planned_entry, planned_stop, planned_target = (
        _geometry(candidate.get(k), "CANDIDATE_GEOMETRY_INVALID")
        for k in ("entry", "stop", "target")
    )

    fee = ev.get("fee")
    if not isinstance(fee, dict) or fee.get("source") != FEE_SOURCE:
        raise NotProven("FEE_SOURCE_UNVERIFIED")
    if _ms(fee.get("observed_ms"), "FEE_SOURCE_UNVERIFIED") > entry_ms:
        raise NotProven("FEE_OBSERVED_AFTER_ENTRY")
    taker = _fraction(fee.get("taker_rate"), "FEE_INVALID")

    filters = ev.get("filters")
    if not isinstance(filters, dict) or _ms(filters.get("observed_ms"), "FILTERS_MISSING") > entry_ms:
        raise NotProven("FILTERS_MISSING")
    tick = _pos(filters.get("tick_size"), "FILTERS_INVALID")
    step = _pos(filters.get("step_size"), "FILTERS_INVALID")
    min_notional = _pos(filters.get("min_notional"), "FILTERS_INVALID")

    slip = ev.get("slippage")
    if not isinstance(slip, dict) or slip.get("model") != "DECLARED_STRESS_FLOOR":
        raise NotProven("SLIPPAGE_POLICY_UNDECLARED")
    floor = _fraction(slip.get("fraction"), "SLIPPAGE_POLICY_UNDECLARED")
    if floor <= 0:
        raise NotProven("SLIPPAGE_POLICY_UNDECLARED")

    qty = _floor_to(_pos(ev.get("quantity"), "QUANTITY_INVALID"), step)
    if qty <= 0:
        raise NotProven("QUANTITY_INVALID")
    bid, _ = _quote(ev.get("entry_quote"), entry_ms, "ENTRY_QUOTE")
    entry_fill = _floor_to(bid * (1 - floor), tick)
    if entry_fill <= 0 or qty * entry_fill < min_notional:
        raise NotProven("MIN_NOTIONAL_NOT_MET")
    stop, tp_type, levels = _plan(ev.get("protection"), planned_entry, tick, step, qty)
    if stop != planned_stop or levels[-1][0] != planned_target:
        # the frozen candidate SL/TP are honored, never re-invented
        raise NotProven("PROTECTION_PLAN_NOT_CANDIDATE_GEOMETRY")
    if entry_fill >= stop:
        raise NotProven("ENTRY_BEYOND_STOP")
    if any(price >= entry_fill for price, _, _ in levels):
        raise NotProven("TP_NOT_BELOW_ACTUAL_ENTRY")

    end_ms = entry_ms + max(HORIZONS_MIN) * 60_000
    if as_of_ms < end_ms:
        raise NotProven("HORIZON_NOT_MATURED")
    bars = _bars(ev.get("bars"), entry_ms, end_ms, as_of_ms)
    funding = _funding(ev.get("funding"), entry_ms, end_ms)

    notional_in = qty * entry_fill
    out = {"entry_ms": entry_ms, "symbol": candidate["symbol"], "entry_fill": entry_fill}
    for horizon in HORIZONS_MIN:
        h_end = entry_ms + horizon * 60_000
        exit_quote = (ev.get("exit_quotes") or {}).get(str(horizon))
        fills, path_mfe, path_mae = _walk(
            bars, entry_ms, h_end, qty, stop, levels, tp_type, floor, tick, entry_fill,
        )
        open_qty = qty - sum(q for _, _, q, _ in fills)
        if open_qty > 0:
            _, ask = _quote(exit_quote, h_end, f"EXIT_QUOTE_{horizon}")
            fills.append((h_end, h_end, open_qty, _ceil_to(ask * (1 + floor), tick)))
        exit_cost = sum(q * p for _, _, q, p in fills)
        fees = taker * (notional_in + exit_cost)
        funding_pnl = _funding_pnl(funding, entry_ms, qty, fills)
        net = notional_in - exit_cost - fees + funding_pnl
        out[f"net_{horizon}"] = net / notional_in
        out[f"exit_kind_{horizon}"] = "TIME" if fills[-1][0] == h_end else "PROTECTIVE"
        out[f"mfe_{horizon}"] = path_mfe
        out[f"mae_{horizon}"] = path_mae
    return out


def _walk(bars, entry_ms, h_end, qty, stop, levels, tp_type, floor, tick, entry_fill):
    """Sequential protective fills up to h_end; never picks a favorable order."""
    fills = []  # (window_open_ms, window_close_ms, qty, price); intrabar time unknown
    remaining = qty
    pending = list(levels)
    mfe = mae = Decimal(0)
    for i, bar in enumerate(bars):
        if bar.open_ms >= h_end or remaining <= 0:
            break
        partial = i == 0 or bar.close_ms > h_end  # contains entry or straddles horizon
        mfe = max(mfe, (entry_fill - bar.l) / entry_fill)
        mae = max(mae, (bar.h - entry_fill) / entry_fill)
        stop_hit = bar.h >= stop
        tp_hits = [lvl for lvl in pending if _tp_touched(bar, lvl[0], tp_type, tick)]
        if stop_hit and tp_hits:
            raise _Indeterminate("STOP_AND_TARGET_SAME_BAR")
        if tp_hits and partial:
            raise _Indeterminate("TARGET_IN_PARTIAL_BAR")
        if stop_hit:
            if partial and bar.close_ms > h_end:
                raise _Indeterminate("STOP_IN_HORIZON_STRADDLING_BAR")
            ref = max(stop, bar.o) if i > 0 else stop
            fills.append((bar.open_ms, bar.close_ms, remaining, _ceil_to(ref * (1 + floor), tick)))
            remaining = Decimal(0)
            break
        for price, part, new_stop in tp_hits:
            fill = price if tp_type == "LIMIT" else _ceil_to(price * (1 + floor), tick)
            take = min(part, remaining)
            fills.append((bar.open_ms, bar.close_ms, take, fill))
            remaining -= take
            pending.remove((price, part, new_stop))
            if new_stop is not None:
                # applies from the next bar only: intrabar order is unknown
                stop = new_stop
    return fills, mfe, mae


def _tp_touched(bar: Bar, price: Decimal, tp_type: str, tick: Decimal) -> bool:
    if tp_type == "LIMIT":
        if bar.l == price:
            raise _Indeterminate("LIMIT_TOUCH_WITHOUT_TRADE_THROUGH")
        return bar.l < price
    return bar.l <= price


def _funding(raw, entry_ms: int, end_ms: int) -> list[tuple[int, Decimal, Decimal]]:
    if not isinstance(raw, dict):
        raise NotProven("FUNDING_MISSING")
    cov = raw.get("coverage")
    if (not isinstance(cov, dict) or cov.get("complete") is not True
            or _ms(cov.get("start_ms"), "FUNDING_COVERAGE") > entry_ms
            or _ms(cov.get("end_ms"), "FUNDING_COVERAGE") < end_ms):
        raise NotProven("FUNDING_COVERAGE_INCOMPLETE")
    events = []
    seen = set()
    for item in raw.get("events") or ():
        if not isinstance(item, dict) or item.get("rate_type") not in FUNDING_TYPES:
            raise NotProven("FUNDING_EVENT_AMBIGUOUS")
        t = _ms(item.get("funding_ms"), "FUNDING_EVENT_AMBIGUOUS")
        if t in seen:
            raise NotProven("FUNDING_EVENT_DUPLICATE")
        seen.add(t)
        events.append((t, _dec(item.get("rate"), "FUNDING_EVENT_AMBIGUOUS"),
                       _pos(item.get("mark_price"), "FUNDING_EVENT_AMBIGUOUS")))
    return sorted(events)


def _funding_pnl(events, entry_ms, qty, fills) -> Decimal:
    total = Decimal(0)
    for t, rate, mark in events:
        if t <= entry_ms:
            continue
        if any(lo < t <= hi if lo < hi else t == hi for lo, hi, _, _ in fills):
            # settlement inside the window where a fill happened: order unknown
            raise _Indeterminate("FUNDING_INSIDE_EXIT_WINDOW")
        open_qty = qty - sum(q for _, hi, q, _ in fills if hi < t)
        if open_qty <= 0:
            continue
        total += rate * mark * open_qty  # SHORT receives a positive rate
    return total


# ─── cohort statistics ──────────────────────────────────────────────────────

def _mean(xs):
    return sum(xs, Decimal(0)) / len(xs)


def _percentile(xs, q: Decimal):
    s = sorted(xs)
    rank = max(1, int((q * len(s)).to_integral_value(rounding=ROUND_CEILING)))
    return s[rank - 1]


def cluster_bootstrap_lower(rows: list[dict]) -> tuple[Decimal | None, int]:
    """Deterministic lower 2.5th percentile of mean(diff) over UTC-4h clusters."""
    clusters = defaultdict(list)
    for r in rows:
        clusters[r["entry_ms"] // CLUSTER_MS].append(r["diff"])
    keys = sorted(clusters)
    if len(keys) < MIN_CLUSTERS:
        return None, len(keys)
    rng = random.Random(BOOTSTRAP_SEED)
    stats = []
    for _ in range(BOOTSTRAP_REPLICATES):
        draw = [v for _ in keys for v in clusters[keys[rng.randrange(len(keys))]]]
        stats.append(_mean(draw))
    stats.sort()
    return stats[BOOTSTRAP_LOWER_RANK - 1], len(keys)


def evaluate_cohort(enrolled: list[dict], evidence_by_id: dict, *, as_of_ms: int) -> dict:
    """Classify the frozen roster. Never drops, reorders or replaces enrolled rows."""
    ids = [c.get("candidate_id") for c in enrolled]
    if len(set(ids)) != len(ids):
        raise NotProven("DUPLICATE_ENROLLED_ID")
    results = [evaluate_candidate(c, evidence_by_id.get(c.get("candidate_id")), as_of_ms=as_of_ms)
               for c in enrolled]
    statuses = Counter(r["status"] for r in results)
    reasons = Counter(r["reason"] for r in results if r["status"] != PROVEN)
    valid = [r for r in results if r["status"] == PROVEN]
    for r in valid:
        r["diff"] = r["net_60"] - r["net_240"]
    symbols = Counter(c.get("symbol") for c in enrolled)
    report = {
        **AUTHORITY,
        "protocol": PROTOCOL,
        "enrolled": len(enrolled),
        "target": TARGET,
        "paired_valid": len(valid),
        "indeterminate": statuses[INDETERMINATE],
        "not_proven": statuses[NOT_PROVEN],
        "unproven_reasons": dict(sorted(reasons.items())),
        "distinct_symbols": len(symbols),
        "largest_symbol_count": max(symbols.values(), default=0),
    }
    blockers = []
    if len(enrolled) < TARGET:
        blockers.append("TARGET_40_NOT_REACHED")
    if len(enrolled) > TARGET:
        blockers.append("OVER_TARGET_40")
    if statuses[INDETERMINATE] or statuses[NOT_PROVEN]:
        blockers.append("EVIDENCE_INCOMPLETE")
    if len(symbols) < MIN_SYMBOLS:
        blockers.append("MIN_6_SYMBOLS")
    if report["largest_symbol_count"] > MAX_PER_SYMBOL:
        blockers.append("SYMBOL_CAP")
    if valid:
        report.update(_describe(valid))
    if len(enrolled) < TARGET:
        status = "COLLECTING"
    elif blockers:
        status = "NOT_PROVEN"
    else:
        lower, n_clusters = cluster_bootstrap_lower(valid)
        report["bootstrap_lower_2_5"] = lower
        report["temporal_clusters"] = n_clusters
        loso = {}
        for sym in sorted(symbols):
            rest = [r["diff"] for r in valid if r["symbol"] != sym]
            loso[sym] = _mean(rest)
        report["leave_one_symbol_out_min_diff"] = min(loso.values())
        failed = []
        if lower is None:
            failed.append("MIN_10_TEMPORAL_CLUSTERS")
        elif lower <= 0:
            failed.append("BOOTSTRAP_LOWER_NOT_POSITIVE")
        if report["mean_diff"] <= MIN_MEAN_DIFF:
            failed.append("MEAN_DIFF_NOT_ABOVE_10BPS")
        if report["mean_net_60"] <= 0:
            failed.append("MEAN_NET_60_NOT_POSITIVE")
        if report["positive_share_60"] < MIN_POSITIVE_SHARE_60:
            failed.append("POSITIVE_SHARE_60_BELOW_60PCT")
        if any(v <= 0 for v in loso.values()):
            failed.append("LEAVE_ONE_SYMBOL_OUT_NOT_POSITIVE")
        blockers.extend(failed)
        status = "EVIDENCE_FAIL" if failed else "EVIDENCE_READY_FOR_MANUAL_REVIEW"
    report["status"] = status
    report["blockers"] = tuple(blockers)
    return report


def _describe(valid: list[dict]) -> dict:
    out = {}
    for key in ("net_60", "net_240", "diff"):
        xs = [r[key] for r in valid]
        out[f"mean_{key}"] = _mean(xs)
        out[f"median_{key}"] = _percentile(xs, Decimal("0.5"))
        out[f"p05_{key}"] = _percentile(xs, Decimal("0.05"))
        out[f"p95_{key}"] = _percentile(xs, Decimal("0.95"))
        out[f"min_{key}"] = min(xs)
    for h in HORIZONS_MIN:
        xs = [r[f"net_{h}"] for r in valid]
        gains = sum((x for x in xs if x > 0), Decimal(0))
        losses = -sum((x for x in xs if x < 0), Decimal(0))
        out[f"positive_share_{h}"] = Decimal(sum(1 for x in xs if x > 0)) / len(xs)
        out[f"profit_factor_{h}"] = None if losses == 0 else gains / losses
        out[f"mean_mfe_{h}"] = _mean([r[f"mfe_{h}"] for r in valid])
        out[f"mean_mae_{h}"] = _mean([r[f"mae_{h}"] for r in valid])
    return out


def public_summary(report: dict) -> dict:
    """Aggregates only; rounded strings so logs never carry raw exchange values."""
    out = {}
    for key, value in report.items():
        if isinstance(value, Decimal):
            out[key] = format(value.quantize(Decimal("0.000001")), "f")
        elif key == "unproven_reasons" or not isinstance(value, dict):
            out[key] = value
    return out
