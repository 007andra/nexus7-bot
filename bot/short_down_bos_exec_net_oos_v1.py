"""#609 pre-registered SHORT / TRENDING_DOWN / BOS_BREAK NET evidence audit.

PURE and research-only. Not imported by trading/runtime. No SQL, HTTP, state
mutation, prices fabricated, order access, or automatic promotion. The source
records and candles must be authenticated independently before human review.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import math
import random

COHORT_ID = "SHORT_DOWN_BOS_EXEC_NET_OOS_V1"
CUTOFF_EPOCH = 1791507490.0  # GitHub #609 created_at 2026-10-09T00:58:10Z
TARGET = 60
MAX_PER_SYMBOL = 12
MIN_SYMBOLS = 8
INTERVAL_S = 900
HORIZONS = (60, 240)
REQUIRED_NET_SOURCE = "AUTHENTIC_CAPTURE_TIME"
REQUIRED_PATH_SOURCE = "VERIFIED_CLOSED_15M_BARS"
AUTHORITY = {
    "research_only": True, "shadow_only": True, "prospective_only": True,
    "thresholds_unchanged": True, "risk_unchanged": True,
    "historical_hwm_preserved": True, "original_oos_unchanged": True,
    "automatic_promotion": False, "promotion_allowed": False,
    "live_allowed": False, "decision_effect": "NONE",
    "execution_effect": "NONE",
}


def _finite(x):
    if x is None or isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _positive(x):
    value = _finite(x)
    return value if value is not None and value > 0 else None


def _valid_candidate(raw):
    if not isinstance(raw, dict):
        return None
    epoch = _finite(raw.get("captured_epoch"))
    if epoch is None or epoch <= CUTOFF_EPOCH:
        return None
    # A numeric 1/0 compares equal to True/False in Python, but is not
    # authenticated canonical boolean decision evidence.
    if any(type(raw.get(k)) is not bool for k in (
        "nexus_called", "nexus_allowed", "shadow_only", "live_eligible"
    )):
        return None
    if any(raw.get(k) != v for k, v in (
        ("population", "HARD_GATE_SHADOW"), ("side", "SHORT"),
        ("regime", "TRENDING_DOWN"), ("setup", "BOS_BREAK"),
        ("nexus_called", True), ("nexus_allowed", True),
        ("shadow_only", True), ("live_eligible", False),
        ("decision_effect", "NONE"), ("execution_effect", "NONE"),
    )):
        return None
    cid, sym = raw.get("candidate_id"), raw.get("symbol")
    if not isinstance(cid, str) or not isinstance(sym, str):
        return None
    parts = cid.split(":")
    if (not sym or len(parts) != 5 or parts[:4] !=
            ["HARD_GATE_SHADOW", sym, "SHORT", "BOS_BREAK"] or not parts[4]):
        return None
    if raw.get("_db_candidate_id", cid) != cid:
        return None
    if (_positive(raw.get("entry")) is None
            or _positive(raw.get("stop")) is None
            or _positive(raw.get("target")) is None):
        return None
    if not (float(raw["target"]) < float(raw["entry"]) < float(raw["stop"])):
        return None
    rr = _finite(raw.get("nexus_net_rr"))
    if rr is None or rr < 1.60:
        return None
    return raw


def freeze_future_candidates(payloads):
    """Select from capture-only fields; NEVER see outcomes while enrolling.

    Without durable enrollment proof, this is a deterministic reconstruction
    only; no caller may claim irreversible frozen membership.
    """
    eligible = [r for r in payloads if _valid_candidate(r) is not None]
    eligible.sort(key=lambda r: (float(r["captured_epoch"]), r["candidate_id"]))
    seen, selected, symbols = set(), [], Counter()
    quota_skipped = duplicates = 0
    for row in eligible:
        cid, symbol = row["candidate_id"], row["symbol"]
        if cid in seen:
            duplicates += 1
            continue
        seen.add(cid)
        if len(selected) >= TARGET:
            break
        if symbols[symbol] >= MAX_PER_SYMBOL:
            quota_skipped += 1
            continue
        selected.append(row)
        symbols[symbol] += 1
    return selected, {
        "eligible_seen_before_target": len(seen),
        "per_symbol_cap_excluded": quota_skipped,
        "duplicate_rows": duplicates,
        "symbol_count": len(symbols),
        "top_symbol_share": max(symbols.values()) / len(selected) if selected else None,
        "source_truncated": False,  # caller must override if source scan was truncated
    }


def _tick_aligned(price, tick):
    ratio = price / tick
    return abs(ratio - round(ratio)) <= 1e-6


def _floor_tick(value, tick):
    return math.floor(value / tick + 1e-9) * tick


def _ceil_tick(value, tick):
    return math.ceil(value / tick - 1e-9) * tick


def _bar_path(row, evidence, horizon, now_epoch):
    """Replay exact signal-to-horizon window with conservative partial candles.

    Unaligned capture timestamps require authenticated *post-entry* partial
    interval paths at both ends. Never omit the adverse first minutes, nor
    borrow a full final candle that extends beyond the evaluated horizon.
    Sources still require a separate independent audit; values alone do not
    authenticate candles or order fills.
    """
    captured = float(row["captured_epoch"])
    finish = captured + horizon * 60
    if now_epoch < finish:
        return None
    start = math.ceil(captured / INTERVAL_S) * INTERVAL_S
    unaligned = start > captured
    bars = evidence.get("bars")
    full_count = horizon // 15 - (1 if unaligned else 0)
    if not isinstance(bars, list) or len(bars) != full_count:
        return None

    def parse_candle(candle):
        if not isinstance(candle, dict):
            return None
        o, h, l, c = (_positive(candle.get(k)) for k in ("o", "h", "l", "c"))
        if None in (o, h, l, c) or not (l <= o <= h and l <= c <= h):
            return None
        return (o, h, l, c)

    output = []
    if unaligned:
        initial = evidence.get("entry_partial_window")
        if (not isinstance(initial, dict) or
                _finite(initial.get("start_epoch")) != captured or
                _finite(initial.get("end_epoch")) != start):
            return None
        first = parse_candle(initial)
        if first is None:
            return None
        output.append(first)

    for i, candle in enumerate(bars):
        if not isinstance(candle, dict):
            return None
        stamp = _finite(candle.get("ts"))
        expected = start + i * INTERVAL_S
        if stamp != expected:
            return None
        parsed = parse_candle(candle)
        if parsed is None:
            return None
        output.append(parsed)

    if unaligned:
        last_start = start + full_count * INTERVAL_S
        final = evidence.get("exit_partial_window")
        if (not isinstance(final, dict) or
                _finite(final.get("start_epoch")) != last_start or
                _finite(final.get("end_epoch")) != finish):
            return None
        last = parse_candle(final)
        if last is None:
            return None
        output.append(last)

    return output

def net_proof(row, evidence, *, horizon, now_epoch, stress=False):
    """Hypothetical stop-first protected outcome; NEVER a confirmed exchange fill.

    Returns None on missing authenticity, market-data, cost or feasibility
    evidence rather than manufacturing net profitability.
    """
    if not isinstance(evidence, dict):
        return None
    if (evidence.get("candidate_id") != row["candidate_id"]
            or evidence.get("symbol") != row["symbol"]
            or type(evidence.get("horizon")) is not int
            or evidence["horizon"] != horizon
            or evidence.get("cost_source") != REQUIRED_NET_SOURCE
            or evidence.get("bar_source") != REQUIRED_PATH_SOURCE
            or evidence.get("capture_reference") != row["candidate_id"]
            or not isinstance(evidence.get("source_evidence_ref"), str)
            or not evidence["source_evidence_ref"]
            or evidence.get("bar_interval_seconds") != INTERVAL_S
            or evidence.get("entry_type") not in ("MARKET", "STOP_MARKET")
            or evidence.get("synthetic_data") is not False):
        return None
    # A STOP_MARKET trigger/fill is conditional. Without a distinct observed
    # activation timestamp and fill-market path, treating it as MARKET would
    # fabricate a trade and optimistic net performance. Reject this unproven
    # route until an independently audited trigger model exists.
    if evidence["entry_type"] == "STOP_MARKET":
        return None
    captured_at = _finite(evidence.get("cost_observed_epoch"))
    if captured_at is None or captured_at > row["captured_epoch"]:
        return None
    values = {}
    for k in ("quantity", "tick_size", "step_size", "min_notional",
              "fee_rate_entry", "fee_rate_exit", "entry_spread_fraction",
              "exit_spread_fraction", "entry_slippage_fraction",
              "exit_slippage_fraction", "funding_cost_usdt"):
        value = _finite(evidence.get(k))
        if value is None or value < 0:
            return None
        values[k] = value
    for k in ("quantity", "tick_size", "step_size", "min_notional"):
        if values[k] <= 0:
            return None
    for k in ("fee_rate_entry", "fee_rate_exit", "entry_spread_fraction",
              "exit_spread_fraction", "entry_slippage_fraction",
              "exit_slippage_fraction"):
        if values[k] >= .1:
            return None
    entry, stop, target = [float(row[k]) for k in ("entry", "stop", "target")]
    tick, qty, step = values["tick_size"], values["quantity"], values["step_size"]
    if (not all(_tick_aligned(price, tick) for price in (entry, stop, target))
            or abs(qty / step - round(qty / step)) > 1e-6
            or qty * entry < values["min_notional"]):
        return None
    path = _bar_path(row, evidence, horizon, now_epoch)
    if path is None:
        return None

    multiplier = 2 if stress else 1
    def price(sell, base, leg):
        spread = values[f"{leg}_spread_fraction"]
        slip = values[f"{leg}_slippage_fraction"]
        factor = multiplier * (spread / 2 + slip)
        return (_floor_tick(base * (1 - factor), tick) if sell else
                _ceil_tick(base * (1 + factor), tick))

    entry_exec = price(True, entry, "entry")
    if entry_exec <= 0:
        return None
    hit = "TIME_EXIT"
    exit_reference = path[-1][3]
    for op, high, low, _ in path:
        if high >= stop:
            # Gap above stop is adverse; do not assume protected stop fill.
            exit_reference = max(stop, op)
            hit = "STOP_FIRST"
            break
        if low <= target:
            exit_reference = target  # no optimistic favorable gap fill
            hit = "TARGET"
            break
    exit_exec = price(False, exit_reference, "exit")
    gross_usdt = qty * (entry_exec - exit_exec)
    fee_usdt = (multiplier * values["fee_rate_entry"] * entry_exec +
                multiplier * values["fee_rate_exit"] * exit_exec) * qty
    net_usdt = gross_usdt - fee_usdt - multiplier * values["funding_cost_usdt"]
    capital = qty * entry_exec
    if capital <= 0:
        return None
    return {
        "net_usdt": net_usdt,
        "net_fraction_notional": net_usdt / capital,
        "exit_type": hit,
        "stress": stress,
        "independent_provenance_review_required": True,
        "hypothetical_fill_only": True,
    }


def _cluster_lower_bound(rows, *, seed):
    """Fixed-seed conservative empirical 5th percentile of cluster resamples."""
    clusters = defaultdict(list)
    for r in rows:
        clusters[(r["symbol"], int(r["captured_epoch"] // 3600))].append(r["net"])
    keys = sorted(clusters)
    if len(keys) < 8:
        return None
    rng = random.Random(seed)
    values = []
    for _ in range(1000):
        draws = [clusters[keys[rng.randrange(len(keys))]] for _ in keys]
        flat = [v for cluster in draws for v in cluster]
        values.append(sum(flat) / len(flat))
    return sorted(values)[49]  # predeclared 5th percentile, extra conservative


def evaluate(payloads, evidence_by_id_and_horizon, *, now_epoch, source_truncated=False):
    """Descriptive proof assembly, never claims runtime activation or LIVE approval.

    evidence_by_id_and_horizon is an externally supplied mapping keyed
    (candidate_id, 60|240); this function never fetches/creates market data.
    """
    if not isinstance(evidence_by_id_and_horizon, dict):
        raise ValueError("MISSING_INDEPENDENT_EXECUTION_EVIDENCE")
    now = _positive(now_epoch)
    if now is None:
        raise ValueError("INVALID_OBSERVATION_CLOCK")
    selected, enrollment = freeze_future_candidates(payloads)
    missing = Counter()
    outcomes = {}
    for horizon in HORIZONS:
        rows = []
        for row in selected:
            evidence = evidence_by_id_and_horizon.get((row["candidate_id"], horizon))
            base = net_proof(row, evidence, horizon=horizon, now_epoch=now)
            stressed = net_proof(row, evidence, horizon=horizon, now_epoch=now,
                                 stress=True)
            if base is None or stressed is None:
                missing[horizon] += 1
                continue
            rows.append({
                "symbol": row["symbol"],
                "captured_epoch": row["captured_epoch"],
                "net": base["net_fraction_notional"],
                "net_usdt": base["net_usdt"],
                "stress_net": stressed["net_fraction_notional"],
                "stress_usdt": stressed["net_usdt"],
                "exit_type": base["exit_type"],
            })
        top_symbol = max(Counter(r["symbol"] for r in selected),
                         key=lambda x: (sum(1 for s in selected if s["symbol"] == x), x),
                         default=None)
        remaining = [r for r in rows if r["symbol"] != top_symbol]
        outcomes[horizon] = {
            "n_complete": len(rows),
            "missing_proof": missing[horizon],
            "gross_outcome_not_substituted": True,
            "mean_net": sum(r["net"] for r in rows) / len(rows) if rows else None,
            "mean_stressed_net": (sum(r["stress_net"] for r in rows) / len(rows)
                                  if rows else None),
            "positive_fraction": (sum(r["net"] > 0 for r in rows) / len(rows)
                                  if rows else None),
            "mean_net_usdt": (sum(r["net_usdt"] for r in rows) / len(rows)
                              if rows else None),
            "cluster_lower_5pct": _cluster_lower_bound(rows, seed=60900 + horizon)
            if len(rows) == TARGET else None,
            "leave_top_symbol_mean_net": (sum(r["net"] for r in remaining) / len(remaining)
                                          if remaining else None),
            "stop_first_count": sum(r["exit_type"] == "STOP_FIRST" for r in rows),
            "target_count": sum(r["exit_type"] == "TARGET" for r in rows),
            "time_exit_count": sum(r["exit_type"] == "TIME_EXIT" for r in rows),
        }
    blockers = []
    if source_truncated:
        blockers.append("SOURCE_SCAN_TRUNCATED")
    if enrollment["duplicate_rows"]:
        blockers.append("DUPLICATE_CANDIDATES_REQUIRE_AUDIT")
    if len(selected) < TARGET:
        blockers.append("TARGET_60_FUTURE_APPROVALS")
    if enrollment["symbol_count"] < MIN_SYMBOLS:
        blockers.append("MIN_8_SYMBOLS")
    if enrollment["top_symbol_share"] is not None and enrollment["top_symbol_share"] > .2:
        blockers.append("SYMBOL_CONCENTRATION")
    for horizon in HORIZONS:
        x = outcomes[horizon]
        if x["n_complete"] < TARGET:
            blockers.append(f"NET_{horizon}_PROOF_MISSING")
            continue
        if (x["mean_net"] <= 0 or x["mean_stressed_net"] <= 0 or
                x["positive_fraction"] < .55 or
                x["cluster_lower_5pct"] is None or x["cluster_lower_5pct"] <= 0 or
                x["leave_top_symbol_mean_net"] is None or
                x["leave_top_symbol_mean_net"] <= 0):
            blockers.append(f"NET_{horizon}_CRITERIA_FAIL")
    if not blockers:
        blockers.append("DURABLE_MEMBER_FREEZE_AND_INDEPENDENT_SOURCE_REVIEW_REQUIRED")
    return {
        **AUTHORITY, "cohort_id": COHORT_ID, "cutoff_epoch": CUTOFF_EPOCH,
        "status": ("COLLECTING_PROSPECTIVE_SAMPLE" if len(selected) < TARGET
                   else "MANUAL_AUDIT_REQUIRED" if blockers == [
                       "DURABLE_MEMBER_FREEZE_AND_INDEPENDENT_SOURCE_REVIEW_REQUIRED"
                   ] else "NET_EVIDENCE_INCOMPLETE_OR_FAILED"),
        "blockers": tuple(blockers),
        "selected": len(selected),
        "membership_durable": False,
        "independently_verified_source": False,
        "enrollment": enrollment, "horizons": outcomes,
        "no_trade_exits_invented": True,
        "precutoff_candidates_excluded": True,
    }
