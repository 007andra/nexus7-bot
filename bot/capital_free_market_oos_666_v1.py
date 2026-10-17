"""#666 capital-free public MARKET observation contract; OFFLINE ONLY.

Synthetic tests do not establish an authentic source or real enrolled cohort.
No imports from engine, risk, database, private exchange, network or config.
The frozen canonical #609 cohort, risk gates and LIVE remain unaffected.
"""
from __future__ import annotations

from collections import Counter
import math
import re

COHORT_ID = "CAPITAL_FREE_SHORT_DOWN_BOS_MARKET_OOS_V1"
ISSUE_CREATED_MS = 1791898025000  # #666 immutable GitHub created_at
CONFIG_SOURCE_SHA = "45ebd59d4ca54072e1233ce0667085d92b70ab1d"
UNIVERSE = (
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "TRXUSDT", "LINKUSDT", "AVAXUSDT",
    "DOTUSDT", "LTCUSDT", "NEARUSDT", "ATOMUSDT", "SUIUSDT",
    "PEOPLEUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "UNIUSDT",
    "AAVEUSDT", "FILUSDT", "INJUSDT", "SEIUSDT", "ETCUSDT",
)
TARGET = 60
MAX_PER_SYMBOL = 12
MIN_SYMBOLS = 8
INTERVAL_MS = 900_000
HORIZONS = (60, 240)
_AUTHORITY = {
    "cohort_id": COHORT_ID, "market_only": True, "research_only": True,
    "canonical_nexus_approval": False, "eligible_for_609_enrollment": False,
    "cohort_membership_proven": False, "source_attested": False,
    "live_allowed": False, "promotion_allowed": False,
    "decision_effect": "NONE", "execution_effect": "NONE",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")


def _positive(x):
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def _status(reason, **extras):
    return {**_AUTHORITY, "status": "OFFLINE_CONTRACT_UNATTESTED",
            "reason": reason, **extras}


def _eligible(r, cutoff):
    if not isinstance(r, dict):
        return False
    symbol, ms = r.get("symbol"), r.get("captured_ms")
    if symbol not in UNIVERSE or type(ms) is not int:
        return False
    if (ms <= cutoff or ms % INTERVAL_MS or
            r.get("candidate_id") !=
            f"MARKET_ONLY:{symbol}:SHORT:BOS_BREAK:{ms}"):
        return False
    if (r.get("population") != "MARKET_ONLY"
            or r.get("side") != "SHORT"
            or r.get("regime") != "TRENDING_DOWN"
            or r.get("setup") != "BOS_BREAK"
            or r.get("market_only") is not True
            or r.get("nexus_called") is not False
            or r.get("nexus_allowed") is not False
            or r.get("live_eligible") is not False
            or r.get("decision_effect") != "NONE"
            or r.get("execution_effect") != "NONE"):
        return False
    if (r.get("config_sha") != CONFIG_SOURCE_SHA or
            r.get("snapshot_complete") is not True
            or r.get("no_future_inputs") is not True
            or type(r.get("observed_ms")) is not int
            or not ms <= r["observed_ms"] < ms + INTERVAL_MS
            or not isinstance(r.get("source_sha256"), str)
            or not _SHA256.fullmatch(r["source_sha256"])
            or not isinstance(r.get("implementation_sha"), str)
            or not _SHA40.fullmatch(r["implementation_sha"])):
        return False
    return _positive(r.get("entry_close")) is not None


def freeze_offline(rows, *, effective_cutoff_ms, source_complete=False):
    """Demonstrate chronological selection; NEVER attest membership or sources.

    The effective cutoff must be separately fixed after CI and before real
    capture; it is not inferred from a timestamp on any supplied record.
    """
    if (type(effective_cutoff_ms) is not int
            or effective_cutoff_ms < ISSUE_CREATED_MS
            or type(source_complete) is not bool
            or not source_complete
            or not isinstance(rows, (tuple, list))
            or len(rows) > 200_000):
        return _status("CUTOFF_OR_SOURCE_COMPLETENESS_NOT_PROVEN",
                       selected=(), selected_count=0)
    filtered = [r for r in rows if _eligible(r, effective_cutoff_ms)]
    filtered.sort(key=lambda r: (
        r["captured_ms"], UNIVERSE.index(r["symbol"]), r["candidate_id"]
    ))
    selected, seen, counts = [], {}, Counter()
    duplicate = excluded_cap = 0
    for r in filtered:
        candidate_id = r["candidate_id"]
        if candidate_id in seen:
            if r != seen[candidate_id]:
                return _status("CONFLICTING_DUPLICATE_ID",
                               selected=(), selected_count=0)
            duplicate += 1
            continue
        seen[candidate_id] = r
        if len(selected) >= TARGET:
            continue  # still detect conflicting duplicate identities in the entire source
        if counts[r["symbol"]] >= MAX_PER_SYMBOL:
            excluded_cap += 1
            continue
        counts[r["symbol"]] += 1
        selected.append(r)
    return _status(
        "OFFLINE_FROZEN_SELECTION_NEEDS_SOURCE_AUDIT",
        selected=tuple(selected), selected_count=len(selected),
        screened=len(rows), eligible=len(filtered),
        excluded_invalid=len(rows) - len(filtered),
        excluded_symbol_cap=excluded_cap, duplicate_rows=duplicate,
        distinct_symbols=len(counts),
        frozen_first_60_shape=(len(selected) == TARGET and
                               len(counts) >= MIN_SYMBOLS),
        effective_cutoff_ms=effective_cutoff_ms,
        enrollment_count=None,
    )


def paired_market_observations(selection, outcome_map):
    """Exact-time SHORT mark returns, never executable NET or fill evidence."""
    if (not isinstance(selection, dict)
            or selection.get("status") != "OFFLINE_CONTRACT_UNATTESTED"
            or selection.get("reason") !=
               "OFFLINE_FROZEN_SELECTION_NEEDS_SOURCE_AUDIT"
            or selection.get("cohort_membership_proven") is not False
            or selection.get("source_attested") is not False
            or selection.get("market_only") is not True
            or selection.get("eligible_for_609_enrollment") is not False
            or type(selection.get("effective_cutoff_ms")) is not int
            or not isinstance(selection.get("selected"), tuple)
            or any(not _eligible(r, selection["effective_cutoff_ms"])
                   for r in selection["selected"])
            or not isinstance(outcome_map, dict)):
        return _status("INVALID_SELECTION_OR_OUTCOMES", pairs=(),
                       paired_count=0)
    paired = []
    for row in selection["selected"]:
        entry = _positive(row["entry_close"])
        observations = {}
        for horizon in HORIZONS:
            outcome = outcome_map.get((row["candidate_id"], horizon))
            expected_ms = row["captured_ms"] + horizon * 60_000
            if (not isinstance(outcome, dict)
                    or outcome.get("candidate_id") != row["candidate_id"]
                    or outcome.get("symbol") != row["symbol"]
                    or outcome.get("horizon_minutes") != horizon
                    or type(outcome.get("close_ms")) is not int
                    or outcome["close_ms"] != expected_ms
                    or type(outcome.get("observed_ms")) is not int
                    or not expected_ms <= outcome["observed_ms"] < expected_ms + INTERVAL_MS
                    or outcome.get("complete_candle") is not True
                    or outcome.get("no_interpolation") is not True
                    or not isinstance(outcome.get("source_sha256"), str)
                    or not _SHA256.fullmatch(outcome["source_sha256"])
                    or _positive(outcome.get("exit_close")) is None):
                observations = {}
                break
            exit_price = _positive(outcome["exit_close"])
            observations[horizon] = (entry - exit_price) / entry
        if len(observations) == len(HORIZONS):
            paired.append({
                "candidate_id": row["candidate_id"], "symbol": row["symbol"],
                "captured_ms": row["captured_ms"],
                "raw_short_return_60m": observations[60],
                "raw_short_return_240m": observations[240],
                "fills_proven": False, "executable_net_proven": False,
            })
    return _status(
        "RAW_MARKET_PAIRS_UNATTESTED_NOT_EXECUTABLE",
        pairs=tuple(paired), paired_count=len(paired),
        missing_or_unverified=len(selection["selected"]) - len(paired),
        target_met=(len(selection["selected"]) == TARGET
                    and len(paired) == TARGET
                    and selection["frozen_first_60_shape"]),
        review_ready=False, enrollment_count=None,
    )
