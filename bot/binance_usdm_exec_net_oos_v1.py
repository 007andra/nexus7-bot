"""Binance USDM prospective OOS evidence primitives. Research only; no trading actions.

This module does not grant LIVE authorization or infer fills from spot returns.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

COHORT = "SHORT_DOWN_BOS_EXEC_NET_OOS_V1"
CUTOFF_UTC = "2026-10-09T00:58:10Z"
CUTOFF_EPOCH = int(datetime.fromisoformat(CUTOFF_UTC.replace("Z", "+00:00")).timestamp())
HORIZONS = (60, 240)
MAX_PER_SYMBOL = 12
TARGET = 60


def _payload(row: dict) -> dict:
    import json
    value = row.get("payload", {})
    return json.loads(value) if isinstance(value, str) else value


def enroll(rows: list[dict]) -> dict:
    """Enroll before reading outcomes; preserve all exclusions and stable ordering."""
    selected, excluded = [], []
    per_symbol: Counter = Counter()
    seen = set()
    ordered = sorted(rows, key=lambda r: (int(r["captured_epoch"]), str(r["candidate_id"])))
    for row in ordered:
        p = _payload(row)
        cid = str(row["candidate_id"])
        symbol = str(row["symbol"])
        ts = int(row["captured_epoch"])
        reasons = []
        if ts <= CUTOFF_EPOCH:
            reasons.append("BEFORE_OR_AT_T0")
        for field, expected in (("side", "SHORT"), ("regime", "TRENDING_DOWN"),
                                ("setup", "BOS_BREAK")):
            if p.get(field) != expected:
                reasons.append("WRONG_" + field.upper())
        for field, expected in (("nexus_called", True), ("nexus_allowed", True),
                                ("shadow_only", True), ("live_eligible", False)):
            if p.get(field) is not expected:
                reasons.append("INVALID_" + field.upper())
        if p.get("decision_effect") not in (None, "NONE"):
            reasons.append("DECISION_EFFECT")
        if p.get("execution_effect") not in (None, "NONE"):
            reasons.append("EXECUTION_EFFECT")
        if not cid.startswith("HARD_GATE_SHADOW:" + symbol + ":SHORT:BOS_BREAK:"):
            reasons.append("INVALID_ID")
        if cid in seen:
            reasons.append("DUPLICATE_ID")
        if per_symbol[symbol] >= MAX_PER_SYMBOL:
            reasons.append("SYMBOL_QUOTA")
        if len(selected) >= TARGET:
            reasons.append("AFTER_TARGET")
        if reasons:
            excluded.append({"candidate_id": cid, "reasons": reasons})
            continue
        seen.add(cid)
        per_symbol[symbol] += 1
        selected.append({"candidate_id": cid, "symbol": symbol, "captured_epoch": ts})
    return {"cohort": COHORT, "cutoff_utc": CUTOFF_UTC, "selected": selected,
            "excluded": excluded, "count": len(selected),
            "distinct_symbols": len(per_symbol),
            "enrollment_complete": len(selected) == TARGET and len(per_symbol) >= 8}


def pair_outcomes(selected: list[dict], outcomes: list[dict]) -> dict:
    """Pair by immutable ID and horizon; duplicates or missing outcomes block proof."""
    by_key = {}
    for row in outcomes:
        key = (str(row["candidate_id"]), int(row["horizon"]))
        by_key.setdefault(key, []).append(row)
    pairs, blockers = [], []
    for item in selected:
        cid = item["candidate_id"]
        for horizon in HORIZONS:
            matches = by_key.get((cid, horizon), [])
            if len(matches) != 1 or _payload(matches[0]).get("outcome") != "OBSERVED":
                blockers.append({"candidate_id": cid, "horizon": horizon,
                                 "reason": "MISSING_DUPLICATE_OR_UNOBSERVED"})
            else:
                pairs.append({"candidate_id": cid, "horizon": horizon,
                              "outcome": matches[0]})
    return {"pairs": pairs, "blockers": blockers, "paired_complete": not blockers}


def conservative_path(*, side: str, entry: Decimal, stop: Decimal,
                      target: Decimal, candles: list[dict]) -> dict:
    """Closed OHLC bars, ordered; simultaneous stop/target => stop first.

    Candles must already be independently authenticated, contiguous, and after
    the candidate's decision time. This helper cannot certify those properties.
    """
    if side not in ("SHORT", "LONG") or entry <= 0 or stop <= 0 or target <= 0:
        return {"status": "NET_PROOF_MISSING", "reason": "INVALID_INPUT"}
    if not candles:
        return {"status": "NET_PROOF_MISSING", "reason": "NO_AUTHENTIC_CANDLES"}
    for candle in candles:
        if not all(k in candle for k in ("high", "low", "close")):
            return {"status": "NET_PROOF_MISSING", "reason": "INCOMPLETE_OHLC"}
        high, low = Decimal(str(candle["high"])), Decimal(str(candle["low"]))
        if low <= 0 or high < low:
            return {"status": "NET_PROOF_MISSING", "reason": "INVALID_OHLC"}
        stop_hit = high >= stop if side == "SHORT" else low <= stop
        target_hit = low <= target if side == "SHORT" else high >= target
        if stop_hit:
            return {"status": "PATH_OBSERVED", "exit": "STOP_FIRST" if target_hit else "STOP",
                    "exit_price": str(stop)}
        if target_hit:
            return {"status": "PATH_OBSERVED", "exit": "TARGET", "exit_price": str(target)}
    return {"status": "NET_PROOF_MISSING", "reason": "NO_EXIT_WITHIN_HORIZON"}


def net_proof(*, path: dict, authenticated_bars: bool, contiguous_bars: bool,
              symbol_filters_verified: bool, fees_verified: bool,
              spread_verified: bool, slippage_verified: bool,
              funding_settlement_verified: bool, size_verified: bool) -> dict:
    missing = [name for name, ok in {
        "PATH": path.get("status") == "PATH_OBSERVED",
        "AUTHENTIC_BARS": authenticated_bars,
        "CONTIGUOUS_BARS": contiguous_bars,
        "SYMBOL_FILTERS": symbol_filters_verified,
        "FEES": fees_verified,
        "SPREAD": spread_verified,
        "SLIPPAGE": slippage_verified,
        "FUNDING_SETTLEMENT": funding_settlement_verified,
        "SIZE": size_verified,
    }.items() if not ok]
    # A proven path alone is insufficient for a numeric net return.
    return {"status": "NET_PROOF_MISSING" if missing else "READY_FOR_NET_CALCULATION",
            "missing": missing, "promotion_allowed": False, "live_allowed": False,
            "decision_effect": "NONE", "execution_effect": "NONE"}
